from models import db
from models.user import User
from flask import Blueprint, request, session, jsonify, render_template, redirect, url_for
from functools import wraps
from datetime import datetime

auth_bp = Blueprint("auth", __name__, url_prefix="")


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated_function


@auth_bp.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


@auth_bp.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json()

    if not data:
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required"}), 400

    user = User.query.filter_by(username=username).first()

    if not user or not user.check_password(password):
        return jsonify({"success": False, "error": "Invalid username or password"}), 401

    # Update last login timestamp
    user.last_login = datetime.now()
    db.session.commit()

    # Set session data
    session.permanent = True
    session["user_id"] = user.id
    session["username"] = user.username
    session["role"] = user.role

    return jsonify({"success": True, "message": "Login successful", "user": user.to_dict()})


@auth_bp.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully"})


@auth_bp.route("/api/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")
    confirm_password = data.get("confirm_password", "")

    if not old_password or not new_password or not confirm_password:
        return jsonify({"success": False, "error": "All fields are required"}), 400

    if new_password != confirm_password:
        return jsonify({"success": False, "error": "New passwords do not match"}), 400

    if len(new_password) < 8:
        return jsonify({"success": False, "error": "New password must be at least 8 characters"}), 400

    user = User.query.get(session["user_id"])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if not user.check_password(old_password):
        return jsonify({"success": False, "error": "Current password is incorrect"}), 401

    user.set_password(new_password)
    db.session.commit()

    return jsonify({"success": True, "message": "Password changed successfully"})


@auth_bp.route("/api/auth/status", methods=["GET"])
def auth_status():
    if session.get("user_id"):
        user = User.query.get(session["user_id"])
        if user:
            return jsonify({"authenticated": True, "user": user.to_dict()})

    return jsonify({"authenticated": False, "user": None})
