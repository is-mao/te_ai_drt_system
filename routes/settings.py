from flask import Blueprint, request, jsonify, render_template
from routes.auth import login_required
from models.system_config import SystemConfig
from services.ai_service import test_ai_connection, test_circuit_connection
import os

settings_bp = Blueprint("settings", __name__)

# Path to .env file
_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

# CIRCUIT API defaults – read from .env, with hardcoded fallbacks
CIRCUIT_DEFAULT_ENDPOINT = os.environ.get(
    "CIRCUIT_API_ENDPOINT",
    "https://chat-ai.cisco.com/openai/deployments/gemini-3.1-flash-lite/chat/completions?api-version=2025-04-01-preview",
)
CIRCUIT_DEFAULT_APPKEY = os.environ.get(
    "CIRCUIT_APP_KEY",
    "egai-prd-supplychain-262013805-summarize-1776759998924",
)
CIRCUIT_DEFAULT_ACCESS_TOKEN = os.environ.get("CIRCUIT_ACCESS_TOKEN", "")
CIRCUIT_DEFAULT_MODEL = os.environ.get("CIRCUIT_MODEL", "gemini-3.1-flash-lite")


def _read_env_key():
    """Read GEMINI_API_KEY from .env file."""
    if not os.path.exists(_ENV_FILE):
        return ""
    with open(_ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _write_env_key(value):
    """Write GEMINI_API_KEY to .env file."""
    # Sanitize: reject values with newlines or control characters
    if value and any(c in value for c in "\n\r\x00"):
        raise ValueError("API key contains invalid characters")
    lines = []
    found = False
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("GEMINI_API_KEY="):
                    found = True
                    if value:
                        lines.append(f"GEMINI_API_KEY={value}\n")
                    # If value is empty, skip this line (remove it)
                else:
                    lines.append(line)
    if not found and value:
        lines.append(f"GEMINI_API_KEY={value}\n")

    with open(_ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Also update the runtime environment variable
    if value:
        os.environ["GEMINI_API_KEY"] = value
    elif "GEMINI_API_KEY" in os.environ:
        del os.environ["GEMINI_API_KEY"]


@settings_bp.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")


@settings_bp.route("/api/settings/ai", methods=["GET"])
@login_required
def get_ai_settings():
    api_key = _read_env_key()
    masked = ""
    if api_key:
        masked = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:] if len(api_key) > 8 else "****"
    return jsonify(
        {
            "has_key": bool(api_key),
            "masked_key": masked,
        }
    )


@settings_bp.route("/api/settings/ai", methods=["PUT"])
@login_required
def update_ai_settings():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    api_key = data.get("api_key", "").strip()
    _write_env_key(api_key)

    return jsonify({"success": True, "message": "API key updated successfully"})


@settings_bp.route("/api/settings/ai/test", methods=["POST"])
@login_required
def test_ai():
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "").strip()
    if not api_key:
        api_key = _read_env_key()
    if not api_key:
        return jsonify({"success": False, "message": "No API key configured. Please enter a key first."})

    success, message = test_ai_connection(api_key)
    return jsonify({"success": success, "message": message})


def _mask_token(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


@settings_bp.route("/api/settings/circuit", methods=["GET"])
@login_required
def get_circuit_settings():
    # Return stored values or defaults
    endpoint = SystemConfig.get_value("circuit_api_endpoint", "") or CIRCUIT_DEFAULT_ENDPOINT
    app_key = SystemConfig.get_value("circuit_app_key", "") or CIRCUIT_DEFAULT_APPKEY
    access_token = SystemConfig.get_value("circuit_access_token", "") or CIRCUIT_DEFAULT_ACCESS_TOKEN
    model = SystemConfig.get_value("circuit_model", "") or CIRCUIT_DEFAULT_MODEL

    return jsonify(
        {
            "has_config": bool(access_token),
            "endpoint": endpoint,
            "app_key": app_key,
            "model": model,
            "masked_app_key": _mask_token(app_key),
            "masked_access_token": _mask_token(access_token),
        }
    )


@settings_bp.route("/api/settings/circuit", methods=["PUT"])
@login_required
def update_circuit_settings():
    data = request.get_json(silent=True) or {}

    # Accept all fields; use defaults if not provided
    endpoint = (data.get("endpoint") or "").strip() or CIRCUIT_DEFAULT_ENDPOINT
    app_key = (data.get("app_key") or "").strip() or CIRCUIT_DEFAULT_APPKEY
    access_token = (data.get("access_token") or "").strip()
    model = (data.get("model") or "").strip() or CIRCUIT_DEFAULT_MODEL

    if endpoint:
        SystemConfig.set_value("circuit_api_endpoint", endpoint)
    if app_key:
        SystemConfig.set_value("circuit_app_key", app_key)
    if access_token:
        SystemConfig.set_value("circuit_access_token", access_token)
    if model:
        SystemConfig.set_value("circuit_model", model)

    if data.get("clear"):
        SystemConfig.set_value("circuit_api_endpoint", "")
        SystemConfig.set_value("circuit_app_key", "")
        SystemConfig.set_value("circuit_access_token", "")
        SystemConfig.set_value("circuit_model", "")

    return jsonify({"success": True, "message": "CIRCUIT API settings updated."})


@settings_bp.route("/api/settings/circuit/test", methods=["POST"])
@login_required
def test_circuit():
    data = request.get_json(silent=True) or {}

    # Use provided values, fallback to stored values, fallback to defaults
    endpoint = (
        (data.get("endpoint") or "").strip()
        or SystemConfig.get_value("circuit_api_endpoint", "")
        or CIRCUIT_DEFAULT_ENDPOINT
    )
    app_key = (
        (data.get("app_key") or "").strip() or SystemConfig.get_value("circuit_app_key", "") or CIRCUIT_DEFAULT_APPKEY
    )
    access_token = (data.get("access_token") or "").strip() or SystemConfig.get_value("circuit_access_token", "") or ""
    model = (data.get("model") or "").strip() or SystemConfig.get_value("circuit_model", "") or CIRCUIT_DEFAULT_MODEL

    success, message = test_circuit_connection(endpoint, app_key, access_token, model)
    return jsonify({"success": success, "message": message})
