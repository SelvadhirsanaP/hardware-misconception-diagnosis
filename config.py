"""config.py - all settings in one place.

Values come from environment variables (optionally loaded from a ".env" file).
Nothing secret is stored in the source code.
"""
import os
from pathlib import Path

try:  # python-dotenv is optional: without it we just use real environment variables
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent


def _int(name, default):
    """Read an integer environment variable, falling back to a default."""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    # --- Limits -------------------------------------------------------
    MAX_CODE_BYTES = _int("MAX_CODE_BYTES", 200_000)      # max size of design / testbench
    MAX_CONTENT_LENGTH = 1024 * 1024                      # max HTTP request size (1 MB)
    TOOL_TIMEOUT_SECONDS = _int("TOOL_TIMEOUT_SECONDS", 20)
    SIM_TIMEOUT_SECONDS = _int("SIM_TIMEOUT_SECONDS", 10)
    ALLOWED_EXTENSIONS = {".v", ".sv"}
    MAX_TOOL_OUTPUT_CHARS = 20_000

    # --- Tool locations (empty = search PATH and common Windows folders) ---
    IVERILOG_PATH = os.getenv("IVERILOG_PATH", "").strip()
    VVP_PATH = os.getenv("VVP_PATH", "").strip()
    YOSYS_PATH = os.getenv("YOSYS_PATH", "").strip()

    # --- Server -------------------------------------------------------
    PORT = _int("PORT", 5000)
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"


def llm_settings():
    """Read AI settings at call time (so changes to .env / tests take effect)."""
    key = os.getenv("LLM_API_KEY", "").strip()
    provider = os.getenv("LLM_PROVIDER", "").strip().lower() or ("anthropic" if key else "")
    return {
        "provider": provider,
        "api_key": key,
        "model": os.getenv("LLM_MODEL", "").strip(),
        "base_url": os.getenv("LLM_BASE_URL", "").strip() or "https://api.openai.com/v1",
        "timeout": _int("LLM_TIMEOUT_SECONDS", 40),
    }
