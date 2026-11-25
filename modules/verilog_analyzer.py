"""verilog_analyzer.py - validates input and runs the REAL Icarus Verilog and Yosys tools.

Safety rules used here:
  * subprocess is always called with a list of arguments and shell=False
  * every tool run has a timeout
  * code is saved in a temporary directory that is deleted afterwards
  * the student's source text is never modified (it is written byte-for-byte)
  * dangerous Verilog features (`include, $system, file I/O) are rejected
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import Config


class ValidationError(Exception):
    """Raised when the submitted code or file is not acceptable."""


# ---------------------------------------------------------------- validation
# Features that could read/write files or run commands on the server.
FORBIDDEN = [
    (re.compile(r"`\s*include\b"), "`include is not allowed (it can read files on the server)"),
    (re.compile(r"\$system\b"), "$system is not allowed (it can run commands)"),
    (re.compile(r"\$(fopen|fwrite|fdisplay|fstrobe|fmonitor|readmemh|readmemb|writememh|writememb|dumpfile)\b"),
     "File input/output system tasks are not allowed"),
]

_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def strip_comments(code):
    """Remove comments but keep the line count identical (so line numbers stay valid)."""
    def repl(match):
        text = match.group(0)
        return "\n" * text.count("\n") if text.startswith("/*") else ""
    return _COMMENT_RE.sub(repl, code)


def validate_source(code, label="Verilog code"):
    """Check that `code` looks like acceptable Verilog. Returns the code UNCHANGED."""
    if not isinstance(code, str) or not code.strip():
        raise ValidationError(f"The {label} is empty. Paste or upload some Verilog first.")
    if len(code.encode("utf-8")) > Config.MAX_CODE_BYTES:
        raise ValidationError(f"The {label} is too large (limit {Config.MAX_CODE_BYTES // 1000} KB).")
    if "\x00" in code:
        raise ValidationError(f"The {label} contains binary data and is not valid text.")
    clean = strip_comments(code)
    if not re.search(r"\bmodule\b", clean) or not re.search(r"\bendmodule\b", clean):
        raise ValidationError(f"The {label} does not look like Verilog: it needs a 'module' ... 'endmodule'.")
    for pattern, message in FORBIDDEN:
        if pattern.search(clean):
            raise ValidationError(f"The {label} was rejected for safety: {message}.")
    return code


def read_upload(file_storage):
    """Validate an uploaded Flask file object and return its text content."""
    name = (file_storage.filename or "").strip()
    if Path(name).suffix.lower() not in Config.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(Config.ALLOWED_EXTENSIONS))
        raise ValidationError(f"Unsupported file type. Please upload a Verilog file ({allowed}).")
    data = file_storage.read(Config.MAX_CODE_BYTES + 1)
    if len(data) > Config.MAX_CODE_BYTES:
        raise ValidationError(f"The file is too large (limit {Config.MAX_CODE_BYTES // 1000} KB).")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("The file is not valid UTF-8 text.")
    return validate_source(text, "uploaded file")


# ---------------------------------------------------------- tool discovery
_WINDOWS_DIRS = [
    r"C:\iverilog\bin", r"C:\Program Files\iverilog\bin", r"C:\Program Files (x86)\iverilog\bin",
    r"C:\oss-cad-suite\bin", r"C:\yosys", r"C:\Program Files\Yosys",
    r"C:\msys64\mingw64\bin", r"C:\msys64\ucrt64\bin",
]
_tool_cache = {}


def find_tool(name, override=""):
    """Return the full path of an executable, or None if it is not installed."""
    if override and Path(override).is_file():
        return override
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        dirs = list(_WINDOWS_DIRS) + [str(Path.home() / "oss-cad-suite" / "bin")]
        for folder in dirs:
            for exe in (name + ".exe", name):
                candidate = Path(folder) / exe
                if candidate.is_file():
                    return str(candidate)
    return None


def run_tool(cmd, cwd, timeout):
    """Run a command safely (list of args, no shell, with timeout)."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            shell=False, encoding="utf-8", errors="replace",
        )
        return {"ran": True, "returncode": proc.returncode, "timed_out": False,
                "stdout": _clip(proc.stdout), "stderr": _clip(proc.stderr)}
    except subprocess.TimeoutExpired as exc:
        return {"ran": True, "returncode": None, "timed_out": True,
                "stdout": _clip(_to_text(exc.stdout)), "stderr": _clip(_to_text(exc.stderr))}
    except OSError as exc:
        return {"ran": False, "returncode": None, "timed_out": False,
                "stdout": "", "stderr": "", "error": f"Could not start the tool: {exc}"}


def _to_text(value):
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _clip(text):
    text = text or ""
    limit = Config.MAX_TOOL_OUTPUT_CHARS
    return text if len(text) <= limit else text[:limit] + "\n... [output truncated]"


def clear_tool_cache():
    _tool_cache.clear()


def get_tool_status():
    """Report which tools are installed (cached after the first call)."""
    if _tool_cache:
        return _tool_cache
    iverilog = find_tool("iverilog", Config.IVERILOG_PATH)
    vvp = find_tool("vvp", Config.VVP_PATH)
    if not vvp and iverilog:  # vvp normally sits next to iverilog
        for exe in ("vvp.exe", "vvp"):
            sibling = Path(iverilog).parent / exe
            if sibling.is_file():
                vvp = str(sibling)
                break
    yosys = find_tool("yosys", Config.YOSYS_PATH)
    for key, path in (("iverilog", iverilog), ("vvp", vvp), ("yosys", yosys)):
        _tool_cache[key] = {"available": bool(path), "path": path,
                            "version": _version(path) if path else None}
    return _tool_cache


def _version(path):
    result = run_tool([path, "-V"], cwd=None, timeout=8)
    text = (result.get("stdout") or result.get("stderr") or "").strip()
    return text.splitlines()[0] if text else "unknown version"


# ----------------------------------------------------------------- analysis
def analyze_verilog(code, testbench=None):
    """Run Icarus Verilog + Yosys on the code and return the raw tool reports.

    Nothing here is simulated: if a tool is missing its report is None.
    """
    tools = get_tool_status()
    reports = {"tools": tools, "iverilog": None, "yosys": None, "simulation": None}

    with tempfile.TemporaryDirectory(prefix="vdiag_") as tmp:
        work = Path(tmp)
        (work / "design.v").write_bytes(code.encode("utf-8"))  # byte-for-byte copy
        timeout = Config.TOOL_TIMEOUT_SECONDS

        if tools["iverilog"]["available"]:
            iv = tools["iverilog"]["path"]
            reports["iverilog"] = run_tool(
                [iv, "-g2012", "-Wall", "-o", "design.vvp", "design.v"], str(work), timeout)
            reports["iverilog"]["command"] = "iverilog -g2012 -Wall -o design.vvp design.v"

        if tools["yosys"]["available"]:
            script = "read_verilog -sv design.v; hierarchy -auto-top; proc; check"
            reports["yosys"] = run_tool([tools["yosys"]["path"], "-p", script], str(work), timeout)
            reports["yosys"]["command"] = f'yosys -p "{script}"'

        if testbench:
            reports["simulation"] = _run_simulation(work, testbench, tools)

    return reports


def _run_simulation(work, testbench, tools):
    """Compile design + student's testbench and run it with vvp."""
    sim = {"requested": True, "compile": None, "run": None}
    if not (tools["iverilog"]["available"] and tools["vvp"]["available"]):
        sim["error"] = "Icarus Verilog (iverilog + vvp) is needed to run a testbench."
        return sim
    (work / "tb.v").write_bytes(testbench.encode("utf-8"))
    sim["compile"] = run_tool(
        [tools["iverilog"]["path"], "-g2012", "-Wall", "-o", "sim.vvp", "design.v", "tb.v"],
        str(work), Config.TOOL_TIMEOUT_SECONDS)
    if sim["compile"].get("ran") and sim["compile"]["returncode"] == 0:
        sim["run"] = run_tool([tools["vvp"]["path"], "sim.vvp"], str(work), Config.SIM_TIMEOUT_SECONDS)
    return sim
