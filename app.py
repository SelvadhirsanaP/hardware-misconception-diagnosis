"""app.py - Flask web server for the AI-Based Hardware Misconception Diagnosis System.

Run with:  python app.py   then open http://127.0.0.1:5000
"""
import logging

from flask import Flask, jsonify, render_template, request

from config import Config
from modules.ai_diagnosis import ai_status
from modules.pipeline import load_manifest, run_analysis
from modules.verilog_analyzer import ValidationError, get_tool_status, read_upload

app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH
logging.basicConfig(level=logging.INFO)


def _truthy(value, default=True):
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "on", "yes")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    """Which tools are installed and is the AI configured?"""
    return jsonify({"tools": get_tool_status(), "ai": ai_status()})


@app.get("/api/samples")
def samples():
    """Sample Verilog files from test_cases/ for the demo dropdown."""
    return jsonify([
        {k: c.get(k) for k in ("id", "title", "expected_behavior", "code", "testbench_code")}
        for c in load_manifest()
    ])


@app.post("/api/upload")
def upload():
    """Validate an uploaded .v file and return its text so the editor can show it."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file was uploaded."}), 400
    try:
        return jsonify({"code": read_upload(file), "filename": file.filename})
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/analyze")
def analyze():
    """Analyse code sent as JSON ({code, testbench, use_ai}) or as a form with an optional file."""
    try:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            code, testbench, use_ai = data.get("code", ""), data.get("testbench") or None, _truthy(data.get("use_ai"))
        else:
            code, testbench = request.form.get("code", ""), request.form.get("testbench") or None
            use_ai = _truthy(request.form.get("use_ai"))
            file = request.files.get("file")
            if file and file.filename and not code.strip():
                code = read_upload(file)
        return jsonify(run_analysis(code, testbench, use_ai))
    except ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # never show internal details to the browser
        app.logger.exception("Unexpected error during analysis")
        return jsonify({"error": "Internal error while analysing the code. Check the server log."}), 500


@app.post("/report")
def report():
    """Printable report page (re-runs the analysis for the submitted code)."""
    code, testbench = request.form.get("code", ""), request.form.get("testbench") or None
    try:
        result = run_analysis(code, testbench, _truthy(request.form.get("use_ai")))
        return render_template("results.html", result=result, code=code, error=None)
    except ValidationError as exc:
        return render_template("results.html", result=None, code=code, error=str(exc)), 400


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "The request is too large."}), 413


def print_startup_info():
    tools = get_tool_status()
    for name, info in tools.items():
        state = f"found ({info['version']})" if info["available"] else "NOT FOUND"
        print(f"  {name:9s}: {state}")
    ai = ai_status()
    print("  AI        :", f"{ai['provider']} / {ai['model']}" if ai["configured"] else "not configured (rule-based mode)")


if __name__ == "__main__":
    print("Hardware Misconception Diagnosis System - tool check:")
    print_startup_info()
    app.run(host="127.0.0.1", port=Config.PORT, debug=Config.DEBUG)
