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
            # Non-admin users only see their own entry
            if not _is_admin() and username != current_user:
                continue
            users.append(
                {
                    "username": username,
                    "version": info.get("version"),
                    "updated_at": info.get("updated_at"),
                    "file_hash": info.get("file_hash", "")[:12],
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
