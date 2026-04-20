"""
Routes for database pool sync operations.
Provides Push / Pull Mine / Pull All / View readonly databases.
Admin users can operate on any user's data.
"""

from flask import Blueprint, request, jsonify, render_template, session
from routes.auth import login_required
from services.db_sync import (
    push_database,
    pull_mine,
    pull_all,
    pull_and_merge,
    list_readonly_databases,
    query_readonly_database,
    get_manifest,
    SFTPSession,
    DEFAULT_REMOTE_BASE,
    _remote_file_exists,
    _read_remote_json,
    _write_remote_json,
)
import os

sync_bp = Blueprint("sync", __name__)

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _is_admin():
    return session.get("role") == "admin"


# ---------- Pages ----------


@sync_bp.route("/api/sync/test-connection", methods=["POST"])
@login_required
def api_test_connection():
    """Test SSH/SFTP connectivity to the remote server using default credentials."""
    try:
        with SFTPSession() as sftp:
            sftp.listdir(".")
        return jsonify({"success": True, "message": "Connected to remote server successfully."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@sync_bp.route("/sync")
@login_required
def sync_page():
    return render_template("sync.html")


@sync_bp.route("/sync/view/<username>")
@login_required
def view_readonly_page(username):
    """View another user's database (readonly)."""
    return render_template("sync_view.html", owner=username)


# ---------- API ----------


@sync_bp.route("/api/sync/push", methods=["POST"])
@login_required
def api_push():
    """Push local database to remote server. Admin can specify target_user."""
    app_username = session.get("username", "unknown")
    data = request.get_json() or {}
    target_user = data.get("target_user", "").strip()

    # Admin can push as any user
    if target_user and _is_admin():
        app_username = target_user

    try:
        result = push_database(
            app_root=BASE_DIR,
            app_username=app_username,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@sync_bp.route("/api/sync/pull-mine", methods=["POST"])
@login_required
def api_pull_mine():
    """Pull own database from remote server. Admin can specify target_user."""
    app_username = session.get("username", "unknown")
    data = request.get_json() or {}
    target_user = data.get("target_user", "").strip()

    # Admin can pull any user's DB
    if target_user and _is_admin():
        app_username = target_user

    try:
        result = pull_mine(
            app_root=BASE_DIR,
            app_username=app_username,
        )
        # After replacing the DB file, dispose SQLAlchemy engine pool
        # so subsequent queries use the new file
        if result.get("success"):
            from models import db as _db

            _db.engine.dispose()
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@sync_bp.route("/api/sync/pull-all", methods=["POST"])
@login_required
def api_pull_all():
    """Pull all users' databases for readonly viewing."""
    try:
        result = pull_all(
            app_root=BASE_DIR,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@sync_bp.route("/api/sync/readonly-list", methods=["GET"])
@login_required
def api_readonly_list():
    """List locally-cached readonly databases."""
    databases = list_readonly_databases(BASE_DIR)
    return jsonify({"success": True, "databases": databases})


@sync_bp.route("/api/sync/readonly/<username>/records", methods=["GET"])
@login_required
def api_readonly_records(username):
    """Query records from a readonly database."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "", type=str)

    result = query_readonly_database(BASE_DIR, username, page, per_page, search)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@sync_bp.route("/api/sync/pull-merge", methods=["POST"])
@login_required
def api_pull_merge():
    """Pull selected users' data and merge into local DB with owner tag."""
    from models.defect_report import DefectReport
    from models import db
    from datetime import datetime

    data = request.get_json() or {}
    usernames = data.get("usernames", [])
    if not usernames or not isinstance(usernames, list):
        return jsonify({"success": False, "error": "No users selected."}), 400

    current_user = session.get("username", "unknown")
    total_merged = 0
    errors = []

    for target in usernames:
        target = target.strip()
        if not target or target == current_user:
            continue  # Skip self — own data is already in local DB

        result = pull_and_merge(app_root=BASE_DIR, target_username=target)
        if not result.get("success"):
            errors.append(f"{target}: {result.get('error', 'unknown error')}")
            continue

        remote_records = result["records"]

        # Delete existing records owned by this target user (full replace)
        DefectReport.query.filter(DefectReport.owner == target).delete()
        db.session.flush()

        # Insert remote records with owner tag
        for row in remote_records:
            rec = DefectReport(
                bu=row.get("bu", ""),
                week_number=row.get("week_number", ""),
                pcap_n=row.get("pcap_n", ""),
                station=row.get("station", ""),
                server=row.get("server", ""),
                sn=row.get("sn", ""),
                record_time=_parse_dt(row.get("record_time")),
                failure=row.get("failure", ""),
                defect_class=row.get("defect_class", ""),
                defect_value=row.get("defect_value", ""),
                root_cause=row.get("root_cause", ""),
                action=row.get("action", ""),
                pn=row.get("pn", ""),
                component_sn=row.get("component_sn", ""),
                log_content=row.get("log_content", ""),
                sequence_log=row.get("sequence_log", ""),
                buffer_log=row.get("buffer_log", ""),
                ai_root_cause=row.get("ai_root_cause", ""),
                status=row.get("status", "complete"),
                created_by=row.get("created_by", target),
                owner=target,
                created_at=_parse_dt(row.get("created_at")) or datetime.now(),
            )
            db.session.add(rec)

        total_merged += len(remote_records)

    db.session.commit()

    msg = f"Merged {total_merged} record(s) from {len(usernames)} user(s)."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"

    return jsonify({"success": True, "merged": total_merged, "errors": errors, "message": msg})


@sync_bp.route("/api/sync/remove-merged/<username>", methods=["DELETE"])
@login_required
def api_remove_merged(username):
    """Remove all merged records from a specific user."""
    from models.defect_report import DefectReport
    from models import db

    count = DefectReport.query.filter(DefectReport.owner == username).delete()
    db.session.commit()
    return jsonify({"success": True, "removed": count, "message": f"Removed {count} record(s) from '{username}'."})


@sync_bp.route("/api/sync/merged-users", methods=["GET"])
@login_required
def api_merged_users():
    """List all users whose data has been merged into local DB."""
    from models.defect_report import DefectReport
    from models import db

    current_user = session.get("username", "unknown")
    rows = (
        db.session.query(DefectReport.owner, db.func.count(DefectReport.id))
        .filter(DefectReport.owner != "", DefectReport.owner != current_user, DefectReport.owner.isnot(None))
        .group_by(DefectReport.owner)
        .all()
    )
    users = [{"username": owner, "count": count} for owner, count in rows]
    return jsonify({"success": True, "users": users})


def _parse_dt(value):
    """Parse datetime from various formats."""
    if not value:
        return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                from datetime import datetime
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


# ---------- Admin-only API ----------


@sync_bp.route("/api/sync/manifest", methods=["GET"])
@login_required
def api_manifest():
    """Fetch the remote manifest."""
    try:
        manifest = get_manifest()
        current_user = session.get("username", "")
        users = []
        for username, info in manifest.items():
            users.append(
                {
                    "username": username,
                    "version": info.get("version"),
                    "updated_at": info.get("updated_at"),
                    "file_hash": info.get("file_hash", "")[:12],
                    "file_name": f"{username}.db",
                    "file_path": f"{DEFAULT_REMOTE_BASE}/{username}.db",
                }
            )
        return jsonify({"success": True, "users": users})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@sync_bp.route("/api/sync/admin/delete/<username>", methods=["DELETE"])
@login_required
def api_admin_delete(username):
    """Delete a user's remote database. Admin only."""
    if not _is_admin():
        return jsonify({"success": False, "error": "Admin access required."}), 403

    remote_db = f"{DEFAULT_REMOTE_BASE}/{username}.db"
    manifest_path = f"{DEFAULT_REMOTE_BASE}/manifest.json"

    try:
        with SFTPSession() as sftp:
            # Remove the DB file
            if _remote_file_exists(sftp, remote_db):
                sftp.remove(remote_db)

            # Remove from manifest
            manifest = _read_remote_json(sftp, manifest_path, default={})
            if username in manifest:
                del manifest[username]
                _write_remote_json(sftp, manifest_path, manifest)

        return jsonify({"success": True, "message": f"Deleted remote database for '{username}'."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
