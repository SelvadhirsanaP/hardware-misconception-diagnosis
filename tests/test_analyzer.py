"""Automated tests.  Run from the project folder with either:

    python -m unittest discover -s tests -v
    pytest -v                      (if you installed pytest)

Three kinds of tests:
  1. Unit tests that need NO external tools (validation, parsers, static checks, corrections, AI fallback, web routes).
     The parser tests use short HAND-WRITTEN sample lines in the usual tool format. They check our parsing code,
     they are NOT claimed to be real tool output.
  2. Integration tests that run the REAL Icarus Verilog / Yosys. They are skipped automatically
     (and reported as skipped) when the tools are not installed.
"""
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402
from modules import ai_diagnosis, verilog_analyzer  # noqa: E402
from modules.correction_engine import build_correction, enrich_issues  # noqa: E402
from modules.error_detector import (  # noqa: E402
    CAT_FUNC, CAT_LATCH, CAT_MULTI, CAT_SYNTAX, detect_issues, parse_iverilog_output,
    parse_simulation, parse_yosys_output, static_checks)
from modules.pipeline import load_manifest, run_analysis  # noqa: E402
from modules.verilog_analyzer import ValidationError, validate_source  # noqa: E402

TOOLS = verilog_analyzer.get_tool_status()
HAVE_IVERILOG = TOOLS["iverilog"]["available"] and TOOLS["vvp"]["available"]
HAVE_YOSYS = TOOLS["yosys"]["available"]
CASES = {c["id"]: c for c in load_manifest()}


def categories(issues):
    return {i["category"] for i in issues}


class TestValidation(unittest.TestCase):
    def test_empty_rejected(self):
        for bad in ("", "   \n  ", None):
            with self.assertRaises(ValidationError):
                validate_source(bad)

    def test_not_verilog_rejected(self):
        with self.assertRaises(ValidationError):
            validate_source("print('hello')")

    def test_forbidden_features_rejected(self):
        for text in ('`include "/etc/passwd"', '$system("dir");', '$fopen("x.txt");'):
            with self.assertRaises(ValidationError):
                validate_source(f"module m; initial begin {text} end endmodule")

    def test_forbidden_inside_comment_is_ok(self):
        validate_source("module m; // $system is not used\nendmodule")

    def test_too_large_rejected(self):
        with self.assertRaises(ValidationError):
            validate_source("module m; endmodule\n" + "// x\n" * 100000)

    def test_valid_code_returned_unchanged(self):
        code = "module m;\r\n  wire a;\r\nendmodule\r\n"
        self.assertEqual(validate_source(code), code)

    def test_upload_extension_and_size(self):
        class F:
            def __init__(self, name, data):
                self.filename, self._d = name, data
            def read(self, n=-1):
                return self._d[:n]
        with self.assertRaises(ValidationError):
            verilog_analyzer.read_upload(F("evil.exe", b"module m; endmodule"))
        with self.assertRaises(ValidationError):
            verilog_analyzer.read_upload(F("a.v", b"\xff\xfe\x00bad"))
        self.assertIn("module", verilog_analyzer.read_upload(F("a.v", b"module m; endmodule")))

    def test_no_shell_true_in_source(self):
        for path in (ROOT / "modules").glob("*.py"):
            self.assertNotIn("shell=True", path.read_text(encoding="utf-8").replace("shell=False", ""),
                             f"{path.name} must not use shell=True")


class TestRunTool(unittest.TestCase):
    """Checks our safe subprocess wrapper using the Python interpreter as a stand-in program."""

    def test_captures_output(self):
        r = verilog_analyzer.run_tool([sys.executable, "-c", "print('hi')"], cwd=None, timeout=10)
        self.assertEqual((r["ran"], r["returncode"], r["stdout"].strip()), (True, 0, "hi"))

    def test_timeout_is_enforced(self):
        r = verilog_analyzer.run_tool([sys.executable, "-c", "import time; time.sleep(5)"], cwd=None, timeout=1)
        self.assertTrue(r["timed_out"])

    def test_missing_program_handled(self):
        r = verilog_analyzer.run_tool(["definitely-not-a-real-program-xyz"], cwd=None, timeout=5)
        self.assertFalse(r["ran"])


class TestParsers(unittest.TestCase):
    """Parsing code only - input lines are hand-written samples in the documented formats."""

    def test_iverilog_syntax_error(self):
        issues = parse_iverilog_output("design.v:5: syntax error\nI give up.\n")
        self.assertEqual([(i["category"], i["line"]) for i in issues], [(CAT_SYNTAX, 5)])

    def test_iverilog_error_and_warning(self):
        text = ("design.v:7: error: Unable to bind wire/reg/memory `foo' in `top'\n"
                "design.v:3: warning: implicit definition of wire 'bar'.\n")
        issues = parse_iverilog_output(text)
        self.assertEqual({i["kind"] for i in issues}, {"undeclared_identifier", "implicit_wire"})

    def test_yosys_latch(self):
        text = "Warning: Latch inferred for signal `\\latch_example.q' from process `\\latch_example.$proc$design.v:5$1'."
        issue = parse_yosys_output(text)[0]
        self.assertEqual((issue["category"], issue["signal"], issue["line"]), (CAT_LATCH, "q", 5))

    def test_yosys_multiple_drivers(self):
        issue = parse_yosys_output("Warning: multiple conflicting drivers for multi_driver.\\y:")[0]
        self.assertEqual((issue["category"], issue["signal"]), (CAT_MULTI, "y"))

    def test_yosys_syntax_error_both_formats(self):
        new = parse_yosys_output("design.v:4: ERROR: syntax error, unexpected TOK_ENDMODULE")[0]
        old = parse_yosys_output("ERROR: Parser error in line design.v:4: syntax error, unexpected X")[0]
        self.assertEqual((new["category"], new["line"]), (CAT_SYNTAX, 4))
        self.assertEqual((old["category"], old["line"]), (CAT_SYNTAX, 4))

    def test_simulation_failure_lines(self):
        sim = {"requested": True, "compile": {"ran": True, "returncode": 0, "stdout": "", "stderr": ""},
               "run": {"timed_out": False, "stdout": "FAIL: expected 1 got 0\nPASS: other\n", "stderr": ""}}
        issues = parse_simulation(sim)
        self.assertEqual([i["category"] for i in issues], [CAT_FUNC])

    def test_simulation_timeout(self):
        sim = {"requested": True, "compile": {"ran": True, "returncode": 0, "stdout": "", "stderr": ""},
               "run": {"timed_out": True, "stdout": "", "stderr": ""}}
        self.assertEqual(parse_simulation(sim)[0]["kind"], "sim_timeout")

    def test_merge_yosys_latch_with_static(self):
        code = (ROOT / "test_cases" / "latch_example.v").read_text()
        ys = {"ran": True, "timed_out": False, "returncode": 0, "stdout":
              "Warning: Latch inferred for signal `\\latch_example.q' from process `\\latch_example.$proc$design.v:5$1'.",
              "stderr": ""}
        issues = detect_issues(code, {"iverilog": None, "yosys": ys, "simulation": None})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["confidence"], "confirmed")
        self.assertIn("static-check", issues[0]["corroborated_by"])

    def test_clean_yosys_removes_static_latch_guess(self):
        code = (ROOT / "test_cases" / "latch_example.v").read_text()
        ys = {"ran": True, "timed_out": False, "returncode": 0, "stdout": "", "stderr": ""}
        self.assertEqual(detect_issues(code, {"iverilog": None, "yosys": ys, "simulation": None}), [])


class TestStaticChecks(unittest.TestCase):
    def test_manifest_static_expectations(self):
        for case in CASES.values():
            found = categories(static_checks(case["code"]))
            for expected in case["expected_static"]:
                self.assertIn(expected, found, case["id"])
            if not case["expected_static"]:
                self.assertEqual(found, set(), f"false positive on {case['id']}")

    def test_case_without_default_is_flagged(self):
        code = "module m(input [1:0] s, input a, output reg y);\nalways @(*) begin\ncase (s)\n2'b00: y = a;\n2'b01: y = 1'b0;\nendcase\nend\nendmodule\n"
        issue = static_checks(code)[0]
        self.assertEqual(issue["kind"], "case_without_default")

    def test_default_assignment_prevents_flag(self):
        code = "module m(input en, input d, output reg q);\nalways @(*) begin\nq = 1'b0;\nif (en) q = d;\nend\nendmodule\n"
        self.assertEqual(static_checks(code), [])

    def test_tristate_bus_not_flagged(self):
        code = "module m(input a, b, e1, e2, output y);\nassign y = e1 ? a : 1'bz;\nassign y = e2 ? b : 1'bz;\nendmodule\n"
        self.assertEqual(static_checks(code), [])

    def test_two_always_blocks_driving_same_reg(self):
        code = "module m(input clk, input a, b, output reg q);\nalways @(posedge clk) q <= a;\nalways @(posedge clk) q <= b;\nendmodule\n"
        self.assertIn(CAT_MULTI, categories(static_checks(code)))

    def test_unbalanced_begin_end(self):
        code = "module m(input a, output reg y);\nalways @(*) begin\ny = a;\nendmodule\n"
        self.assertIn(CAT_SYNTAX, categories(static_checks(code)))


class TestCorrections(unittest.TestCase):
    def test_original_code_not_modified(self):
        for case in CASES.values():
            before = case["code"]
            enrich_issues(detect_issues(before, {}), before)
            self.assertEqual(before, (ROOT / "test_cases" / case["file"]).read_text(encoding="utf-8"))

    def test_latch_fix_adds_default(self):
        code = CASES["latch"]["code"]
        issue = enrich_issues(detect_issues(code, {}), code)[0]
        self.assertIn("q = 1'b0;", issue["correction"]["corrected_code"])

    def test_syntax_fix_adds_semicolon(self):
        code = CASES["syntax"]["code"]
        issue = enrich_issues(detect_issues(code, {}), code)[0]
        self.assertIn("assign sum = a + b;", issue["correction"]["corrected_code"])

    def test_sensitivity_fix(self):
        code = CASES["comb"]["code"]
        issue = enrich_issues(detect_issues(code, {}), code)[0]
        self.assertIn("always @(*)", issue["correction"]["corrected_code"])

    def test_every_issue_has_all_sections(self):
        for case in CASES.values():
            for issue in enrich_issues(detect_issues(case["code"], {}), case["code"]):
                for key in ("meaning", "cause", "responsible_code", "concept", "beginner_tip"):
                    self.assertTrue(issue["explanation"][key])
                self.assertTrue(issue["correction"]["suggestion"])
                self.assertTrue(issue["correction"]["reason"])


class TestAI(unittest.TestCase):
    def _issues(self):
        code = CASES["latch"]["code"]
        return code, enrich_issues(detect_issues(code, {}), code)

    def test_fallback_without_key(self):
        code, issues = self._issues()
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "", "LLM_PROVIDER": ""}):
            info = ai_diagnosis.diagnose_with_ai(code, {}, issues)
        self.assertFalse(info["used"])
        self.assertEqual(issues[0]["explanation_source"], "rules")

    def test_valid_ai_answer_is_used(self):
        code, issues = self._issues()
        answer = {"summary": "s", "issues": {"ISSUE-1": {"meaning": "AI meaning", "cause": "See line 5."}}}
        self.assertEqual(ai_diagnosis.apply_ai_answer(answer, issues, code), 1)
        self.assertEqual(issues[0]["explanation"]["meaning"], "AI meaning")

    def test_invented_line_number_rejected(self):
        code, issues = self._issues()
        answer = {"issues": {"ISSUE-1": {"meaning": "Problem on line 99 of your code."}}}
        self.assertEqual(ai_diagnosis.apply_ai_answer(answer, issues, code), 0)
        self.assertEqual(issues[0]["explanation_source"], "rules")

    def test_unknown_issue_ids_ignored(self):
        code, issues = self._issues()
        self.assertEqual(ai_diagnosis.apply_ai_answer({"issues": {"ISSUE-42": {"meaning": "x"}}}, issues, code), 0)

    def test_parse_fenced_json(self):
        self.assertEqual(ai_diagnosis.parse_ai_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_network_error_falls_back(self):
        import requests
        code, issues = self._issues()
        env = {"LLM_API_KEY": "k", "LLM_PROVIDER": "anthropic", "LLM_MODEL": "m"}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(ai_diagnosis, "_call_llm", side_effect=requests.ConnectionError("x")):
            info = ai_diagnosis.diagnose_with_ai(code, {}, issues)
        self.assertFalse(info["used"])
        self.assertIn("failed", info["note"])

    def test_mocked_llm_end_to_end(self):
        code, issues = self._issues()
        reply = json.dumps({"summary": "Latch found.", "issues": {"ISSUE-1": {"meaning": "Mocked meaning"}}})
        env = {"LLM_API_KEY": "k", "LLM_PROVIDER": "anthropic", "LLM_MODEL": "m"}
        with mock.patch.dict("os.environ", env), mock.patch.object(ai_diagnosis, "_call_llm", return_value=reply):
            info = ai_diagnosis.diagnose_with_ai(code, {}, issues)
        self.assertTrue(info["used"])
        self.assertEqual(issues[0]["explanation_source"], "ai")


class TestWebApp(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_index_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Analyze Code", res.data)

    def test_health_and_samples(self):
        self.assertIn("tools", self.client.get("/api/health").get_json())
        self.assertEqual(len(self.client.get("/api/samples").get_json()), 5)

    def test_analyze_json(self):
        res = self.client.post("/api/analyze", json={"code": CASES["latch"]["code"], "use_ai": False})
        data = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(data["counts"]["total"], 1)

    def test_analyze_empty_is_400(self):
        res = self.client.post("/api/analyze", json={"code": "  "})
        self.assertEqual(res.status_code, 400)
        self.assertIn("empty", res.get_json()["error"])

    def test_upload_valid_and_invalid(self):
        ok = self.client.post("/api/upload", data={"file": (io.BytesIO(b"module m; endmodule"), "m.v")},
                              content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 200)
        bad = self.client.post("/api/upload", data={"file": (io.BytesIO(b"x"), "m.txt")},
                               content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)

    def test_report_page_escapes_html(self):
        code = "module m; endmodule // <script>alert(1)</script>"
        res = self.client.post("/report", data={"code": code, "use_ai": "0"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"<script>alert(1)</script>", res.data)


@unittest.skipUnless(HAVE_IVERILOG, "Icarus Verilog (iverilog + vvp) is not installed")
class TestRealIcarus(unittest.TestCase):
    def test_valid_design_compiles(self):
        result = run_analysis(CASES["valid"]["code"], use_ai=False)
        self.assertEqual(result["status"]["compilation"], "pass")

    def test_syntax_error_detected_by_iverilog(self):
        result = run_analysis(CASES["syntax"]["code"], use_ai=False)
        self.assertEqual(result["status"]["compilation"], "fail")
        self.assertTrue(any(i["category"] == CAT_SYNTAX and i["confidence"] == "confirmed" for i in result["issues"]))

    def test_testbench_detects_functional_mismatch(self):
        case = CASES["comb"]
        result = run_analysis(case["code"], case["testbench_code"], use_ai=False)
        self.assertIn(CAT_FUNC, categories(result["issues"]))


@unittest.skipUnless(HAVE_YOSYS, "Yosys is not installed")
class TestRealYosys(unittest.TestCase):
    def test_latch_confirmed(self):
        result = run_analysis(CASES["latch"]["code"], use_ai=False)
        self.assertTrue(any(i["category"] == CAT_LATCH and i["confidence"] == "confirmed" for i in result["issues"]))

    def test_multiple_drivers_confirmed(self):
        result = run_analysis(CASES["multi"]["code"], use_ai=False)
        self.assertIn(CAT_MULTI, categories(result["issues"]))

    def test_manifest_expectations_with_tools(self):
        for case in CASES.values():
            found = categories(run_analysis(case["code"], use_ai=False)["issues"])
            for expected in case["expected_with_tools"]:
                self.assertIn(expected, found, case["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
