"""Device Console blueprint.

A web terminal for reaching Cisco console ports (reverse-telnet on a
terminal server, e.g. ``telnet 10.79.150.135 2014``) or SSH.  The browser
talks to these endpoints via HTTP short-polling so the feature works under
the existing Werkzeug/gunicorn stack without an async server.
"""

from flask import Blueprint, jsonify, render_template, request

from routes.auth import login_required
from services.console_service import load_console_config, manager

console_bp = Blueprint("console", __name__, url_prefix="")


@console_bp.route("/device-console", methods=["GET"])
@login_required
def console_page():
    return render_template("console.html")


@console_bp.route("/api/console/config", methods=["GET"])
@login_required
def console_config():
    cfg = load_console_config()
    jump = cfg.get("jump", {}) or {}
    isr = cfg.get("isr", {}) or {}
    uut = cfg.get("uut", {}) or {}
    return jsonify({
        # Prefill defaults for the jump-host chain (no passwords returned).
        "jump": {"host": jump.get("host", ""), "username": jump.get("username", "")},
        "isr": {"username": isr.get("username", ""),
                "clear_line": bool(isr.get("clear_line", True))},
        "uut": {"username": uut.get("username", "")},
    })


@console_bp.route("/api/console/connect-jump", methods=["POST"])
@login_required
def console_connect_jump():
    data = request.get_json(silent=True) or {}
    cfg = load_console_config()
    jd = cfg.get("jump", {}) or {}
    isrd = cfg.get("isr", {}) or {}
    uutd = cfg.get("uut", {}) or {}

    def pick(val, default):
        return val if val not in (None, "") else default

    jump = {
        "host": pick(data.get("jump_host"), jd.get("host")),
        "port": int(pick(data.get("jump_port"), jd.get("port", 22))),
        "username": pick(data.get("jump_user"), jd.get("username")),
        "password": pick(data.get("jump_password"), jd.get("password")),
        "isr_host": (data.get("isr_host") or "").strip(),
        "isr_user": pick(data.get("isr_user"), isrd.get("username")),
        "isr_password": pick(data.get("isr_password"), isrd.get("password")),
        "clear_line": bool(data.get("clear_line", isrd.get("clear_line", True))),
        "power_same": bool(data.get("power_same", False)),
        "line_number": data.get("line_number") or None,
        "uut_port": data.get("uut_port"),
        "uut_username": pick(data.get("uut_user"), uutd.get("username")),
        "uut_password": pick(data.get("uut_password"), uutd.get("password")),
    }

    if not jump["host"]:
        return jsonify({"success": False, "error": "jump host is required"}), 400
    if not jump["isr_host"] or not jump["uut_port"]:
        return jsonify({"success": False, "error": "ISR IP and UUT port are required"}), 400

    try:
        session = manager.connect_jump(jump)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": f"failed to connect: {exc}"}), 502

    return jsonify({
        "success": True,
        "session_id": session.id,
        "host": jump["isr_host"],
        "port": jump["uut_port"],
        "protocol": "jump",
    })


@console_bp.route("/api/console/output", methods=["GET"])
@login_required
def console_output():
    session_id = request.args.get("session_id", "")
    try:
        cursor = int(request.args.get("cursor", "0"))
    except ValueError:
        cursor = 0

    session = manager.get(session_id)
    if not session:
        return jsonify({"success": False, "closed": True, "error": "session not found"}), 404

    data, new_cursor, closed = session.read_since(cursor)
    return jsonify({
        "success": True,
        "data": data,
        "cursor": new_cursor,
        "closed": closed,
        "error": session.error,
        "reason": session.close_reason if closed else None,
    })


@console_bp.route("/api/console/input", methods=["POST"])
@login_required
def console_input():
    data = request.get_json(silent=True) or {}
    session = manager.get(data.get("session_id", ""))
    if not session:
        return jsonify({"success": False, "closed": True, "error": "session not found"}), 404
    session.send(data.get("data", ""))
    return jsonify({"success": True})


@console_bp.route("/api/console/resize", methods=["POST"])
@login_required
def console_resize():
    data = request.get_json(silent=True) or {}
    session = manager.get(data.get("session_id", ""))
    if session:
        session.resize(data.get("cols", 80), data.get("rows", 24))
    return jsonify({"success": True})


@console_bp.route("/api/console/disconnect", methods=["POST"])
@login_required
def console_disconnect():
    data = request.get_json(silent=True) or {}
    manager.disconnect(data.get("session_id", ""))
    return jsonify({"success": True})
