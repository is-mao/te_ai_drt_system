"""
Excel DF/DR Transform Route - Upload source Excel, AI-classify failures, download temp.xlsx
Persists source + output files in transform_files/ for history browsing.
"""

import os
import re
import tempfile
from datetime import datetime

import pandas as pd
from flask import Blueprint, request, jsonify, render_template, send_file
from routes.auth import login_required
from werkzeug.utils import secure_filename

transform_bp = Blueprint("transform", __name__, url_prefix="/transform")

# Persistent directory for transform files
_FILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transform_files")
os.makedirs(_FILES_DIR, exist_ok=True)


@transform_bp.route("/", methods=["GET"])
@login_required
def transform_page():
    """Render the Excel transform page."""
    return render_template("transform.html")


@transform_bp.route("/upload", methods=["POST"])
@login_required
def transform_upload():
    """Accept uploaded Excel, run AI transform, return result as download."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename.endswith((".xlsx", ".xls")):
        return jsonify({"error": "Only .xlsx/.xls files are supported"}), 400

    use_ai = request.form.get("use_ai", "true").lower() != "false"

    # Build timestamped filenames
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    orig_name = secure_filename(file.filename)
    base_stem = os.path.splitext(orig_name)[0]
    src_name = f"{base_stem}_{ts}.xlsx"
    out_name = f"{base_stem}_{ts}_output.xlsx"
    src_path = os.path.join(_FILES_DIR, src_name)
    out_path = os.path.join(_FILES_DIR, out_name)

    file.save(src_path)

    try:
        import services.excel_transform as _etx

        # Inject CIRCUIT credentials from DB (SystemConfig) if not in env
        if use_ai and not _etx.CIRCUIT_ACCESS_TOKEN:
            from models.system_config import SystemConfig

            db_token = (SystemConfig.get_value("circuit_access_token") or "").strip()
            if db_token:
                _etx.CIRCUIT_ACCESS_TOKEN = db_token
                _etx.CIRCUIT_API_ENDPOINT = (
                    SystemConfig.get_value("circuit_api_endpoint") or ""
                ).strip() or _etx.CIRCUIT_API_ENDPOINT
                _etx.CIRCUIT_APP_KEY = (SystemConfig.get_value("circuit_app_key") or "").strip() or _etx.CIRCUIT_APP_KEY
                _etx.CIRCUIT_MODEL = (SystemConfig.get_value("circuit_model") or "").strip() or _etx.CIRCUIT_MODEL

        _etx.transform_excel(src_path, out_path, use_ai=use_ai)

        if not os.path.exists(out_path):
            # Cleanup source if transform failed to produce output
            return jsonify({"error": "Transform produced no output file"}), 500

        # Determine if AI was actually used (token available + use_ai flag)
        ai_actually_used = use_ai and bool(_etx.CIRCUIT_ACCESS_TOKEN)

        resp = send_file(
            out_path,
            as_attachment=True,
            download_name="temp.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp.headers["X-AI-Used"] = "true" if ai_actually_used else "false"
        resp.headers["Access-Control-Expose-Headers"] = "X-AI-Used"
        return resp
    except Exception as e:
        import traceback

        traceback.print_exc()
        # Cleanup on error
        for p in (src_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500


@transform_bp.route("/history", methods=["GET"])
@login_required
def transform_history():
    """Return list of transform file pairs (source + output) as JSON."""
    if not os.path.isdir(_FILES_DIR):
        return jsonify([])

    files = sorted(os.listdir(_FILES_DIR), reverse=True)
    # Group by timestamp: <stem>_<ts>.xlsx and <stem>_<ts>_output.xlsx
    pairs = {}
    for f in files:
        if not f.endswith(".xlsx"):
            continue
        stat = os.stat(os.path.join(_FILES_DIR, f))
        size_kb = round(stat.st_size / 1024, 1)
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        if f.endswith("_output.xlsx"):
            key = f.replace("_output.xlsx", "")
            pairs.setdefault(key, {})["output"] = {"name": f, "size_kb": size_kb, "time": mtime}
        else:
            key = f.replace(".xlsx", "")
            pairs.setdefault(key, {})["source"] = {"name": f, "size_kb": size_kb, "time": mtime}

    result = []
    for key in pairs:
        entry = pairs[key]
        if "source" in entry:
            result.append(
                {
                    "source": entry.get("source"),
                    "output": entry.get("output"),
                }
            )
    return jsonify(result)


@transform_bp.route("/files/<filename>", methods=["GET"])
@login_required
def transform_download_file(filename):
    """Download a specific transform file."""
    filename = secure_filename(filename)
    filepath = os.path.join(_FILES_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)


@transform_bp.route("/preview/<filename>", methods=["GET"])
@login_required
def transform_preview(filename):
    """Return first 50 rows of an Excel file as an HTML table."""
    filename = secure_filename(filename)
    filepath = os.path.join(_FILES_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        df = pd.read_excel(filepath, nrows=50)
        html = df.to_html(classes="table table-sm table-striped table-bordered", index=False, border=0)
        return jsonify({"filename": filename, "rows": len(df), "html": html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@transform_bp.route("/files/<filename>", methods=["DELETE"])
@login_required
def transform_delete_file(filename):
    """Delete a transform file pair (source + output)."""
    filename = secure_filename(filename)
    filepath = os.path.join(_FILES_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    # Delete both source and output
    if filename.endswith("_output.xlsx"):
        pair = filepath.replace("_output.xlsx", ".xlsx")
    else:
        pair = filepath.replace(".xlsx", "_output.xlsx")
    deleted = []
    for p in (filepath, pair):
        try:
            if os.path.isfile(p):
                os.unlink(p)
                deleted.append(os.path.basename(p))
        except Exception:
            pass
    return jsonify({"deleted": deleted})
