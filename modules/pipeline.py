"""pipeline.py - connects validation -> tools -> detection -> corrections -> (optional) AI."""
import json
from pathlib import Path

from config import BASE_DIR
from modules.ai_diagnosis import ai_status, diagnose_with_ai
from modules.correction_engine import enrich_issues
from modules.error_detector import CAT_FUNC, CAT_LATCH, CAT_MULTI, CAT_SYNTAX, detect_issues
from modules.verilog_analyzer import analyze_verilog, validate_source

DISCLAIMER = ("This tool cannot detect every hardware misconception. 'Confirmed' findings come from real tool "
              "output; 'Potential' findings are simple heuristics and may be false alarms. Functional (logic) "
              "errors need a testbench or specification that says what the circuit should do.")


def _step_status(report, issues, sources):
    """pass / fail / not_run for one tool, based on its return code and error issues."""
    if not report or not report.get("ran"):
        return "not_run"
    if report.get("timed_out"):
        return "timeout"
    errors = [i for i in issues if i["severity"] == "error" and i["source"] in sources]
    return "fail" if (report.get("returncode") != 0 or errors) else "pass"


def run_analysis(code, testbench=None, use_ai=True):
    """Full analysis of one submission. Returns a JSON-friendly dictionary."""
    validate_source(code)
    if testbench:
        validate_source(testbench, "testbench")
    else:
        testbench = None

    reports = analyze_verilog(code, testbench)
    issues = enrich_issues(detect_issues(code, reports), code)
    ai = diagnose_with_ai(code, reports, issues) if use_ai else {
        "used": False, "provider": None, "summary": None, "note": "AI explanations were turned off."}

    tools = reports["tools"]
    compile_status = _step_status(reports["iverilog"], issues, ("iverilog",))
    synth_status = _step_status(reports["yosys"], issues, ("yosys",))
    sim = reports.get("simulation")
    if not sim:
        sim_status = "not_requested"
    elif not sim.get("run"):
        sim_status = "fail" if sim.get("compile") else "not_run"
    else:
        sim_status = ("timeout" if sim["run"].get("timed_out")
                      else "fail" if any(i["category"] == CAT_FUNC for i in issues) else "pass")

    warnings = []
    if not tools["iverilog"]["available"]:
        warnings.append("Icarus Verilog was not found. Compilation checks were skipped (see README to install it).")
    if not tools["yosys"]["available"]:
        warnings.append("Yosys was not found. Synthesis checks (latches, multiple drivers) fall back to "
                        "heuristic 'potential' findings only (see README to install it).")
    if testbench and not tools["vvp"]["available"]:
        warnings.append("vvp was not found, so the testbench could not be simulated.")

    counts = {
        "total": len(issues),
        "errors": sum(i["severity"] == "error" for i in issues),
        "warnings": sum(i["severity"] == "warning" for i in issues),
        "info": sum(i["severity"] == "info" for i in issues),
        "confirmed": sum(i["confidence"] == "confirmed" for i in issues),
        "potential": sum(i["confidence"] == "potential" for i in issues),
        "by_category": {},
    }
    for i in issues:
        counts["by_category"][i["category"]] = counts["by_category"].get(i["category"], 0) + 1

    checks = [
        {"name": "Icarus Verilog compile", "status": compile_status},
        {"name": "Yosys synthesis check", "status": synth_status},
        {"name": "Testbench simulation", "status": sim_status},
        {"name": "Latch check", "status": "fail" if any(i["category"] == CAT_LATCH for i in issues)
         else ("pass" if synth_status == "pass" else "heuristic")},
        {"name": "Multiple-driver check", "status": "fail" if any(i["category"] == CAT_MULTI for i in issues)
         else ("pass" if synth_status == "pass" else "heuristic")},
    ]

    return {
        "status": {"compilation": compile_status, "synthesis": synth_status, "simulation": sim_status},
        "checks": checks, "counts": counts, "issues": issues, "summary": _summary(counts, tools, ai),
        "warnings": warnings, "tools": tools, "ai": ai, "disclaimer": DISCLAIMER,
        "raw_reports": {
            "iverilog": reports["iverilog"], "yosys": reports["yosys"],
            "simulation": reports["simulation"],
        },
        "line_count": len(code.splitlines()),
    }


def _summary(counts, tools, ai):
    if ai.get("summary"):
        return ai["summary"]
    ran = [n for n in ("iverilog", "yosys") if tools[n]["available"]]
    if counts["total"] == 0:
        if ran:
            return ("No issues were found by the tools that ran (" + ", ".join(ran) + "). "
                    "This does not prove the design is correct - functional errors need a testbench.")
        return "No issues found by the built-in heuristic checks. Install Icarus Verilog and Yosys for real tool checks."
    return (f"Found {counts['total']} issue(s): {counts['confirmed']} confirmed by tools or testbench and "
            f"{counts['potential']} potential (heuristic). Start with the first error and re-run the analysis.")


def load_manifest():
    """Read test_cases/manifest.json and attach the code of each sample."""
    base = BASE_DIR / "test_cases"
    data = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    for case in data["cases"]:
        case["code"] = (base / case["file"]).read_text(encoding="utf-8")
        case["testbench_code"] = (base / case["testbench"]).read_text(encoding="utf-8") if case.get("testbench") else ""
    return data["cases"]


def ai_info():
    return ai_status()
