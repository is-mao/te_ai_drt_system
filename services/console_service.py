"""Console service: jump-host chain sessions for the Device Console section.

Provides a small session manager used by routes/console.py.  Each session
SSHes into the jump host and opens an interactive PTY, then automates the
nested logins (jump -> ISR ssh + clear line -> UUT telnet) while buffering
output for HTTP polling.

Connection defaults are read from environment variables (``.env``,
``CONSOLE_JUMP_*`` / ``CONSOLE_ISR_*`` / ``CONSOLE_UUT_*``); per-connection
values entered on the page override them.
"""

import os
import re
import socket
import threading
import time
import uuid

import paramiko

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Keep at most this many characters of scrollback per session in memory.
MAX_BUFFER = 200_000
# Auto-close sessions idle longer than this (seconds); env-configurable.
IDLE_TIMEOUT = int(os.environ.get("CONSOLE_IDLE_TIMEOUT", str(8 * 60 * 60)))
# Send an SSH keepalive this often (seconds) so idle links aren't dropped by
# NAT/firewall/sshd.  Set CONSOLE_KEEPALIVE=0 to disable.
KEEPALIVE_INTERVAL = int(os.environ.get("CONSOLE_KEEPALIVE", "20"))


def _env_bool(name, default=True):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_console_config():
    """Build the jump-chain console config from environment variables (.env)."""
    return {
        # Jump-host chain defaults (jump -> ISR ssh -> UUT telnet).
        "jump": {
            "host": os.environ.get("CONSOLE_JUMP_HOST", "scfam-alln-jump"),
            "port": int(os.environ.get("CONSOLE_JUMP_PORT", "22")),
            "username": os.environ.get("CONSOLE_JUMP_USER", "scjump"),
            "password": os.environ.get("CONSOLE_JUMP_PASSWORD", ""),
        },
        "isr": {
            "username": os.environ.get("CONSOLE_ISR_USER", "apollo-debug"),
            "password": os.environ.get("CONSOLE_ISR_PASSWORD", ""),
            "clear_line": _env_bool("CONSOLE_ISR_CLEAR_LINE", True),
        },
        "uut": {
            "username": os.environ.get("CONSOLE_UUT_USER", "apollo"),
            "password": os.environ.get("CONSOLE_UUT_PASSWORD", ""),
        },
    }


# ---------------------------------------------------------------------------
# Minimal telnet IAC handling (reverse-console ports speak raw telnet)
# ---------------------------------------------------------------------------
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240

# Options we agree to so interactive character-mode + server echo work.
OPT_ECHO = 1
OPT_SGA = 3  # suppress go-ahead


def _reply_option(sock, state, cmd, opt):
    """Answer a telnet option negotiation, only when the answer changes.

    Accept server ECHO and SGA (needed so typed commands are echoed back),
    and agree to suppress go-ahead ourselves; refuse everything else.
    Tracking prior answers in ``state`` prevents negotiation loops.
    """
    if cmd in (WILL, WONT):
        if cmd == WILL and opt in (OPT_ECHO, OPT_SGA):
            desired = DO
        else:
            desired = DONT
        key = ("recv_will", opt)
    else:  # DO or DONT (peer asks us to enable/disable an option)
        if cmd == DO and opt == OPT_SGA:
            desired = WILL
        else:
            desired = WONT
        key = ("recv_do", opt)

    if state.get(key) == desired:
        return
    state[key] = desired
    try:
        sock.sendall(bytes([IAC, desired, opt]))
    except OSError:
        pass


def _negotiate_telnet(data, sock, state):
    """Strip telnet IAC command sequences and answer option negotiation.

    Returns ``(clean_bytes, leftover)`` where ``leftover`` is an incomplete
    trailing IAC sequence that must be prepended to the next chunk.
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:  # bare IAC at end -> carry over
            return bytes(out), bytes(data[i:])
        cmd = data[i + 1]
        if cmd == IAC:  # escaped 0xFF -> literal byte
            out.append(IAC)
            i += 2
            continue
        if cmd in (DO, DONT, WILL, WONT):
            if i + 2 >= n:  # incomplete option -> carry over
                return bytes(out), bytes(data[i:])
            opt = data[i + 2]
            _reply_option(sock, state, cmd, opt)
            i += 3
            continue
        if cmd == SB:  # subnegotiation: skip until IAC SE
            j = i + 2
            while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            if j + 1 >= n:  # SE not yet received -> carry over
                return bytes(out), bytes(data[i:])
            i = j + 2
            continue
        i += 2  # other 2-byte commands (NOP, etc.)
    return bytes(out), b""


class ConsoleSession:
    """A single telnet/SSH connection with a buffered output stream."""

    def __init__(self, host, port, protocol, auto_login, config,
                 username=None, password=None, jump=None):
        self.id = uuid.uuid4().hex
        self.owner_pid = os.getpid()
        self.host = host
        self.port = int(port)
        self.protocol = (protocol or "telnet").lower()
        self.auto_login = bool(auto_login)
        self.config = config or {}
        # Per-connection credentials entered on the page override .env defaults.
        self.override_username = username
        self.override_password = password
        # Jump-chain parameters (protocol == "jump"); see _jump_automation.
        self.jump = jump or {}

        self.running = False
        self.error = None
        self.close_reason = None

        self.sock = None            # telnet
        self.ssh = None             # paramiko SSHClient
        self.chan = None            # paramiko channel

        self._telnet_state = {}     # negotiated telnet options (loop guard)
        self._telnet_partial = b""  # incomplete IAC sequence carried over

        self._lock = threading.Lock()
        self._window = ""           # sliding scrollback window
        self._window_start = 0      # absolute offset of _window[0]
        self._produced = 0          # total chars ever produced
        self._recv_tail = ""        # small tail used for auto-login matching

        self.last_activity = time.time()
        self._reader = None
        self._line_no = None        # console line cleared on the ISR (for revert)

    # -- lifecycle ----------------------------------------------------------
    def _creds(self):
        """Resolve credentials: page-entered values override .env defaults."""
        cfg = self.config.get("credentials", {}) or {}
        username = self.override_username if self.override_username is not None else cfg.get("username", "")
        password = self.override_password if self.override_password is not None else cfg.get("password", "")
        return username or "", password or ""

    def start(self):
        if self.protocol == "jump":
            self._start_jump()
            self.running = True
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            threading.Thread(target=self._jump_automation, daemon=True).start()
            return
        if self.protocol == "ssh":
            self._start_ssh()
        else:
            self._start_telnet()
        self.running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if self.auto_login:
            threading.Thread(target=self._auto_login, daemon=True).start()

    def _start_jump(self):
        """SSH into the jump host and open an interactive PTY shell."""
        j = self.jump
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(
            hostname=j.get("host"),
            port=int(j.get("port", 22)),
            username=j.get("username") or None,
            password=j.get("password") or None,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
        )
        self.chan = self.ssh.invoke_shell(term="xterm", width=120, height=32)
        self.chan.settimeout(0.0)
        if KEEPALIVE_INTERVAL > 0:
            _t = self.ssh.get_transport()
            if _t is not None:
                _t.set_keepalive(KEEPALIVE_INTERVAL)

    def _start_telnet(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(None)

    def _start_ssh(self):
        username, password = self._creds()
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(
            hostname=self.host,
            port=self.port,
            username=username or None,
            password=password or None,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        self.chan = self.ssh.invoke_shell(term="xterm", width=120, height=32)
        self.chan.settimeout(0.0)
        if KEEPALIVE_INTERVAL > 0:
            _t = self.ssh.get_transport()
            if _t is not None:
                _t.set_keepalive(KEEPALIVE_INTERVAL)

    # -- io -----------------------------------------------------------------
    def _read_loop(self):
        try:
            while self.running:
                if self.chan is not None:
                    chunk = self._read_ssh()
                else:
                    chunk = self._read_telnet()
                if chunk is None:
                    self.close_reason = self.close_reason or "远端关闭了连接（可能被跳板机/防火墙断开）"
                    break
                if chunk:
                    self._append(chunk.decode("utf-8", errors="replace"))
                else:
                    time.sleep(0.03)
        except Exception as exc:  # noqa: BLE001
            self.close_reason = f"连接错误: {exc}"
            self._append(f"\r\n[connection error] {exc}\r\n")
        finally:
            self.close()

    def _read_telnet(self):
        try:
            data = self.sock.recv(4096)
        except OSError:
            return None
        if data == b"":
            return None
        data = self._telnet_partial + data
        clean, self._telnet_partial = _negotiate_telnet(
            data, self.sock, self._telnet_state
        )
        return clean

    def _read_ssh(self):
        if self.chan is None or self.chan.closed:
            return None
        try:
            if self.chan.recv_ready():
                return self.chan.recv(4096)
            return b""
        except OSError:
            return None

    def _append(self, text):
        if not text:
            return
        with self._lock:
            self._window += text
            self._produced += len(text)
            if len(self._window) > MAX_BUFFER:
                drop = len(self._window) - MAX_BUFFER
                self._window = self._window[drop:]
                self._window_start += drop
            self._recv_tail = (self._recv_tail + text)[-4096:]

    def read_since(self, cursor):
        """Return (text, new_cursor, closed) for output after ``cursor``."""
        with self._lock:
            if cursor < self._window_start:
                cursor = self._window_start
            data = self._window[cursor - self._window_start:] if cursor <= self._produced else ""
            self.last_activity = time.time()
            return data, self._produced, (not self.running)

    def send(self, data):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.last_activity = time.time()
        try:
            if self.chan is not None:
                self.chan.send(raw)
            elif self.sock:
                self.sock.sendall(raw)
        except OSError:
            pass

    def resize(self, cols, rows):
        if self.chan is not None:
            try:
                self.chan.resize_pty(width=int(cols), height=int(rows))
            except OSError:
                pass

    # -- auto login ---------------------------------------------------------
    def _auto_login(self):
        cfg = self.config.get("auto_login", {}) or {}
        username, password = self._creds()

        user_kw = (cfg.get("username_prompt") or "Username:").lower()
        pass_kw = (cfg.get("password_prompt") or "Password:").lower()

        if not username and not password:
            return

        if cfg.get("send_initial_return", True) and self.protocol == "telnet":
            time.sleep(0.6)
            self.send("\r\n")

        sent_user = False
        sent_pass = False
        deadline = time.time() + 30
        while self.running and time.time() < deadline and not sent_pass:
            with self._lock:
                buf = self._recv_tail.lower()
            if not sent_user and username and user_kw in buf:
                self.send(username + "\r\n")
                sent_user = True
                with self._lock:
                    self._recv_tail = ""
                time.sleep(0.4)
                continue
            if sent_user and password and pass_kw in buf:
                self.send(password + "\r\n")
                sent_pass = True
                break
            if not username and password and pass_kw in buf:
                self.send(password + "\r\n")
                sent_pass = True
                break
            time.sleep(0.25)

    # -- jump-host chain (jump ssh -> ISR ssh clear-line -> UUT telnet) ------
    def _log(self, text):
        """Write an informational line to the terminal stream."""
        self._append(text)

    def _tail(self):
        with self._lock:
            return self._recv_tail

    def _clear_tail(self):
        with self._lock:
            self._recv_tail = ""

    def _expect(self, needles, timeout):
        """Wait until any of ``needles`` (case-insensitive) appears in output."""
        low = [n.lower() for n in needles]
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            buf = self._tail().lower()
            for n in low:
                if n in buf:
                    self._clear_tail()
                    return n
            time.sleep(0.15)
        return None

    def _expect_prompt(self, chars, timeout):
        """Wait for a shell/EXEC prompt whose last non-space char is in chars."""
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            buf = self._tail().rstrip()
            if buf and buf[-1] in chars:
                self._clear_tail()
                return buf[-1]
            time.sleep(0.2)
        return None

    def _isr_show_line(self, line_no):
        """Display the current line status: 'terminal length 0; show line <n>'.

        Output streams to the terminal so the operator can eyeball the state
        before the line is modified/cleared (no automatic verification).
        """
        self.send("terminal length 0\n")
        self._expect_prompt(">#", 8)
        self.send(f"show line {line_no}\n")
        self._expect_prompt(">#", 12)

    def _isr_line_dtr(self, line_no, keep_power):
        """Configure the console line's modem DTR (power) behaviour on the ISR.

        keep_power=True  -> 'no modem dtr-active' (clearing the line won't drop
                            power on ports where console and power share a line)
        keep_power=False -> 'modem dtr-active'    (restore normal DTR/power ctrl)
        """
        self.send("config t\n")
        self._expect_prompt("#", 8)
        self.send(f"line {line_no}\n")
        self._expect_prompt("#", 8)
        self.send(("no modem dtr-active" if keep_power else "modem dtr-active") + "\n")
        self._expect_prompt("#", 8)
        self.send("end\n")
        self._expect_prompt("#>", 8)

    def _jump_automation(self):
        """Drive nested logins from the jump shell to the UUT console."""
        j = self.jump
        isr_ip = (j.get("isr_host") or "").strip()
        try:
            port = int(j.get("uut_port"))
        except (TypeError, ValueError):
            port = 0

        line_no = j.get("line_number")
        if line_no in (None, "", 0):
            line_no = port - 2000 if port > 2000 else port
        self._line_no = line_no
        power_same = bool(j.get("power_same"))

        # Let the jump shell settle and show its prompt.
        time.sleep(1.0)

        # Step 1 (optional): SSH to the ISR and clear the console line/port.
        if j.get("clear_line") and isr_ip:
            isr_user = j.get("isr_user") or "apollo-debug"
            isr_pw = j.get("isr_password") or ""
            self._log(f"\r\n[自动] 连接 ISR 并清除线路 {line_no} ...\r\n")
            self.send(f"ssh {isr_user}@{isr_ip}\n")
            m = self._expect(["(yes/no", "continue connecting", "password:"], 25)
            if m in ("(yes/no", "continue connecting"):
                self.send("yes\n")
                m = self._expect(["password:"], 25)
            if isr_pw:
                self.send(isr_pw + "\n")
            # Wait for the ISR EXEC prompt (ends with > or #).
            if self._expect_prompt(">#", 30):
                # Always show the current line status first (for a quick look).
                self._log(f"\r\n[自动] 查看线路状态 (terminal length 0; show line {line_no}) ...\r\n")
                self._isr_show_line(line_no)
                if power_same:
                    # CONSOLE and Power share this port: keep the UUT powered on
                    # by disabling DTR before clearing the line.
                    self._log(f"\r\n[自动] CONSOLE 与 Power 同端口：先保持上电 (no modem dtr-active) ...\r\n")
                    self._isr_line_dtr(line_no, keep_power=True)
                self.send(f"clear line {line_no}\n")
                if self._expect(["[confirm]", "confirm"], 8):
                    self.send("\n")
                time.sleep(0.6)
            self.send("exit\n")
            # Return to the jump prompt before the next hop.
            self._expect(["]$", "$ "], 15)

        # Step 2: telnet to the UUT reverse-console port through the jump host.
        if isr_ip and port:
            self._log(f"\r\n[自动] telnet {isr_ip} {port} 连接 UUT 控制台 ...\r\n")
            self.send(f"telnet {isr_ip} {port}\n")
            uut_user = j.get("uut_username") or ""
            uut_pw = j.get("uut_password") or ""
            if uut_user:
                if self._expect(["username:", "login:"], 30):
                    self.send(uut_user + "\n")
            if uut_pw:
                if self._expect(["password:"], 30):
                    self.send(uut_pw + "\n")
            self._log("\r\n[自动] 已进入 UUT 控制台，可开始交互。\r\n")
        else:
            self._log("\r\n[自动] 已连接跳板机，可手动操作。\r\n")

    def _revert_power(self):
        """On disconnect, restore 'modem dtr-active' on the ISR console line.

        Only meaningful when CONSOLE and Power share the port (power_same).
        Escapes any active telnet, re-enters the ISR, restores DTR (which lets
        power be controlled again), and clears the line.
        """
        j = self.jump
        isr_ip = (j.get("isr_host") or "").strip()
        line_no = self._line_no
        if not isr_ip or line_no is None or not self.running or self.chan is None:
            return
        self._log(f"\r\n[自动] 断开前恢复电源控制 (modem dtr-active) 并清线 {line_no} ...\r\n")
        # Escape any active telnet session back to the jump prompt.
        self.send("\x1d")            # Ctrl+]  -> telnet> prompt
        time.sleep(0.5)
        self.send("quit\n")
        self._expect(["]$", "$ "], 8)
        isr_user = j.get("isr_user") or "apollo-debug"
        isr_pw = j.get("isr_password") or ""
        self.send(f"ssh {isr_user}@{isr_ip}\n")
        m = self._expect(["(yes/no", "continue connecting", "password:"], 20)
        if m in ("(yes/no", "continue connecting"):
            self.send("yes\n")
            m = self._expect(["password:"], 20)
        if isr_pw:
            self.send(isr_pw + "\n")
        if self._expect_prompt(">#", 25):
            self._log(f"\r\n[自动] 查看线路状态 (terminal length 0; show line {line_no}) ...\r\n")
            self._isr_show_line(line_no)
            self._isr_line_dtr(line_no, keep_power=False)
            self.send(f"clear line {line_no}\n")
            if self._expect(["[confirm]", "confirm"], 8):
                self.send("\n")
            time.sleep(0.6)
        self.send("exit\n")
        self._expect(["]$", "$ "], 10)

    def request_disconnect(self):
        """Handle an explicit user disconnect, reverting power first if needed."""
        revert = (bool(self.jump.get("power_same"))
                  and bool(self.jump.get("clear_line"))
                  and self.running and self.chan is not None)
        if revert:
            threading.Thread(target=self._teardown_with_revert, daemon=True).start()
        else:
            self.close()

    def _teardown_with_revert(self):
        try:
            self._revert_power()
        except Exception as exc:  # noqa: BLE001
            self._log(f"\r\n[自动] 恢复电源设置时出错: {exc}\r\n")
        finally:
            self.close()

    # -- teardown -----------------------------------------------------------
    def close(self):
        self.running = False
        for closer in (
            lambda: self.chan and self.chan.close(),
            lambda: self.ssh and self.ssh.close(),
            lambda: self.sock and self.sock.close(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


class ConsoleManager:
    """Owns all active console sessions in this process."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        self._janitor = threading.Thread(target=self._reap, daemon=True)
        self._janitor.start()

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def connect_jump(self, jump):
        """Open a jump-host chain session (jump ssh -> ISR -> UUT telnet)."""
        jhost = (jump.get("host") or "").strip()
        if not jhost or not HOST_RE.match(jhost):
            raise ValueError("invalid jump host")
        isr_ip = (jump.get("isr_host") or "").strip()
        if isr_ip and not HOST_RE.match(isr_ip):
            raise ValueError("invalid ISR IP")
        config = load_console_config()
        session = ConsoleSession(jhost, int(jump.get("port", 22)), "jump",
                                 False, config, jump=jump)
        session.start()
        with self._lock:
            self._sessions[session.id] = session
        return session

    def disconnect(self, session_id):
        # Do not pop immediately: power-revert teardown may need to keep
        # streaming to the browser.  The reaper removes it once it stops
        # running.  For non-revert sessions request_disconnect closes at once.
        session = self.get(session_id)
        if session:
            session.request_disconnect()

    def _reap(self):
        while True:
            time.sleep(60)
            now = time.time()
            with self._lock:
                stale = []
                for sid, s in self._sessions.items():
                    if now - s.last_activity > IDLE_TIMEOUT:
                        s.close_reason = s.close_reason or f"空闲超过 {IDLE_TIMEOUT // 60} 分钟，自动断开"
                        stale.append(sid)
                    elif not s.running:
                        stale.append(sid)
                for sid in stale:
                    s = self._sessions.pop(sid, None)
                    if s:
                        s.close()


# Module-level singleton shared by the blueprint.
manager = ConsoleManager()
