"""
Database Pool Sync Service
==========================
Architecture:
  Remote server (SFTP)
    └── /root/drt_db_data/
        ├── manifest.json          ← version registry
        ├── sync_log.json          ← audit trail
        ├── <username>.db          ← that user's database
        └── <username2>.db

Each user:
  - Can PUSH only their own database
  - Can PULL everyone's databases (read-only viewing)
  - Uses default SSH connection: root@10.69.230.185:36021
"""

import os
import json
import hashlib
import shutil
import getpass
import sqlite3
from datetime import datetime
from pathlib import Path

import paramiko


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Default SFTP connection settings
# ---------------------------------------------------------------------------

DEFAULT_SSH_HOST = "10.69.230.185"
DEFAULT_SSH_PORT = 36021
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PASSWORD = "nbv12345"
DEFAULT_REMOTE_BASE = "/root/drt_db_data"


class SFTPSession:
    """Context manager that opens an SSH/SFTP connection using default creds."""

    def __init__(
        self,
        host: str = DEFAULT_SSH_HOST,
        port: int = DEFAULT_SSH_PORT,
        username: str = DEFAULT_SSH_USER,
        password: str = DEFAULT_SSH_PASSWORD,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._ssh = None
        self._sftp = None

    def __enter__(self):
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=15,
        )
        self._sftp = self._ssh.open_sftp()
        return self._sftp

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._sftp:
            self._sftp.close()
        if self._ssh:
            self._ssh.close()


# ---------------------------------------------------------------------------
# Remote helpers
# ---------------------------------------------------------------------------


def _ensure_remote_dir(sftp, path: str):
    """Recursively create remote directories."""
    parts = path.replace("\\", "/").strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _remote_file_exists(sftp, path: str) -> bool:
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def _read_remote_json(sftp, path: str, default=None):
    """Download a remote JSON file and parse it."""
    if not _remote_file_exists(sftp, path):
        return default if default is not None else {}
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    try:
        sftp.get(path, tmp.name)
        with open(tmp.name, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        os.unlink(tmp.name)


def _write_remote_json(sftp, path: str, data):
    """Write a dict/list as JSON to a remote file."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8")
    json.dump(data, tmp, indent=2, ensure_ascii=False)
    tmp.close()
    try:
        sftp.put(tmp.name, path)
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

LOCAL_DB_NAME = "drt_system.db"
READONLY_DIR = "readonly_dbs"  # local folder for other users' DBs


def _local_db(app_root: str) -> Path:
    return Path(app_root) / LOCAL_DB_NAME


def _readonly_dir(app_root: str) -> Path:
    d = Path(app_root) / READONLY_DIR
    d.mkdir(exist_ok=True)
    return d


def push_database(
    app_root: str,
    app_username: str,
):
    """
    Upload the local database to the remote server.
    Only the logged-in user's own slot is written.

    Returns dict with status info.
    """
    local_db = _local_db(app_root)
    if not local_db.exists():
        return {"success": False, "error": "Local database not found."}

    local_hash = _file_hash(local_db)
    remote_base = DEFAULT_REMOTE_BASE
    remote_db_path = f"{remote_base}/{app_username}.db"
    manifest_path = f"{remote_base}/manifest.json"
    sync_log_path = f"{remote_base}/sync_log.json"

    with SFTPSession() as sftp:
        # Ensure directory structure
        _ensure_remote_dir(sftp, remote_base)

        # Version check — reject if remote is newer than our baseline
        manifest = _read_remote_json(sftp, manifest_path, default={})
        user_entry = manifest.get(app_username, {})
        remote_version = user_entry.get("version", 0)

        # Upload (atomic: write to .tmp then rename)
        tmp_remote = f"{remote_db_path}.tmp"
        sftp.put(str(local_db), tmp_remote)
        # Rename is atomic on most POSIX systems
        try:
            sftp.remove(remote_db_path)
        except FileNotFoundError:
            pass
        sftp.rename(tmp_remote, remote_db_path)

        # Update manifest
        new_version = remote_version + 1
        manifest[app_username] = {
            "version": new_version,
            "updated_at": _now_iso(),
            "file_hash": local_hash,
            "machine": os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
        }
        _write_remote_json(sftp, manifest_path, manifest)

        # Append to sync log
        sync_log = _read_remote_json(sftp, sync_log_path, default=[])
        sync_log.append(
            {
                "action": "push",
                "app_user": app_username,
                "timestamp": _now_iso(),
                "version": new_version,
                "file_hash": local_hash,
                "machine": manifest[app_username]["machine"],
            }
        )
        # Keep last 500 entries
        if len(sync_log) > 500:
            sync_log = sync_log[-500:]
        _write_remote_json(sftp, sync_log_path, sync_log)

    return {
        "success": True,
        "version": new_version,
        "file_hash": local_hash,
        "message": f"Database pushed successfully (v{new_version}).",
    }


def pull_mine(
    app_root: str,
    app_username: str,
):
    """
    Download the logged-in user's own database from the remote server
    and replace the local working database.
    """
    local_db = _local_db(app_root)
    remote_db_path = f"{DEFAULT_REMOTE_BASE}/{app_username}.db"

    with SFTPSession() as sftp:
        if not _remote_file_exists(sftp, remote_db_path):
            return {"success": False, "error": f"No remote database found for user '{app_username}'."}

        # Backup current local DB before overwriting
        if local_db.exists():
            backup = local_db.with_suffix(f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            shutil.copy2(local_db, backup)

        sftp.get(remote_db_path, str(local_db))

    return {
        "success": True,
        "message": f"Your database has been downloaded and is now active.",
    }


def pull_all(
    app_root: str,
):
    """
    Download every user's database into a local readonly directory.
    Returns manifest info so the UI can list available databases.
    """
    ro_dir = _readonly_dir(app_root)
    remote_base = DEFAULT_REMOTE_BASE
    manifest_path = f"{remote_base}/manifest.json"

    with SFTPSession() as sftp:
        manifest = _read_remote_json(sftp, manifest_path, default={})
        downloaded = []
        errors = []

        for username, info in manifest.items():
            remote_db = f"{remote_base}/{username}.db"
            local_path = ro_dir / f"{username}.db"
            try:
                if _remote_file_exists(sftp, remote_db):
                    sftp.get(remote_db, str(local_path))
                    downloaded.append(
                        {
                            "username": username,
                            "version": info.get("version"),
                            "updated_at": info.get("updated_at"),
                            "file": str(local_path.name),
                        }
                    )
                else:
                    errors.append(f"{username}: remote file missing")
            except Exception as e:
                errors.append(f"{username}: {e}")

    return {
        "success": True,
        "downloaded": downloaded,
        "errors": errors,
        "message": f"Downloaded {len(downloaded)} database(s).",
    }


def get_manifest():
    """Fetch the remote manifest (who has what version)."""
    manifest_path = f"{DEFAULT_REMOTE_BASE}/manifest.json"
    with SFTPSession() as sftp:
        return _read_remote_json(sftp, manifest_path, default={})


def pull_and_merge(app_root: str, target_username: str):
    """
    Download a specific user's database from the remote server,
    read all their defect_reports, and merge them into the local DB
    with owner = target_username.

    Existing records owned by that user are deleted first (full replace).
    Returns dict with status info.
    """
    import tempfile

    remote_db_path = f"{DEFAULT_REMOTE_BASE}/{target_username}.db"

    with SFTPSession() as sftp:
        if not _remote_file_exists(sftp, remote_db_path):
            return {"success": False, "error": f"No remote database found for user '{target_username}'."}

        # Download to temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            sftp.get(remote_db_path, tmp.name)
        except Exception as e:
            os.unlink(tmp.name)
            return {"success": False, "error": f"Failed to download: {e}"}

    # Read records from downloaded DB
    try:
        conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM defect_reports").fetchall()
        records = [dict(row) for row in rows]
        conn.close()
    except Exception as e:
        os.unlink(tmp.name)
        return {"success": False, "error": f"Failed to read remote database: {e}"}
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    return {"success": True, "records": records, "count": len(records)}


def get_sync_log(limit: int = 50):
    """Fetch recent sync log entries."""
    sync_log_path = f"{DEFAULT_REMOTE_BASE}/sync_log.json"
    with SFTPSession() as sftp:
        log = _read_remote_json(sftp, sync_log_path, default=[])
        return log[-limit:]


def list_readonly_databases(app_root: str):
    """List locally-cached readonly databases and their info."""
    ro_dir = _readonly_dir(app_root)
    results = []
    for db_file in sorted(ro_dir.glob("*.db")):
        username = db_file.stem
        # Get record count
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            cursor = conn.execute("SELECT COUNT(*) FROM defect_reports")
            count = cursor.fetchone()[0]
            conn.close()
        except Exception:
            count = 0
        results.append(
            {
                "username": username,
                "file": db_file.name,
                "records": count,
                "size_kb": round(db_file.stat().st_size / 1024, 1),
                "downloaded_at": datetime.fromtimestamp(db_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return results


def query_readonly_database(app_root: str, username: str, page: int = 1, per_page: int = 50, search: str = ""):
    """
    Query records from a readonly (other user's) database.
    Returns paginated results.
    """
    ro_dir = _readonly_dir(app_root)
    db_path = ro_dir / f"{username}.db"

    if not db_path.exists():
        return {"success": False, "error": f"Database for user '{username}' not found locally. Pull All first."}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # Count total
        if search:
            count_sql = "SELECT COUNT(*) FROM defect_reports WHERE failure LIKE ? OR sn LIKE ? OR root_cause LIKE ?"
            search_param = f"%{search}%"
            total = conn.execute(count_sql, (search_param, search_param, search_param)).fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) FROM defect_reports").fetchone()[0]

        # Fetch page
        offset = (page - 1) * per_page
        if search:
            data_sql = """SELECT * FROM defect_reports
                          WHERE failure LIKE ? OR sn LIKE ? OR root_cause LIKE ?
                          ORDER BY id DESC LIMIT ? OFFSET ?"""
            rows = conn.execute(data_sql, (search_param, search_param, search_param, per_page, offset)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM defect_reports ORDER BY id DESC LIMIT ? OFFSET ?", (per_page, offset)
            ).fetchall()

        records = [dict(row) for row in rows]

        return {
            "success": True,
            "records": records,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
            "owner": username,
        }
    finally:
        conn.close()
