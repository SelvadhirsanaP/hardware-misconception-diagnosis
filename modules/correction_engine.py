"""correction_engine.py - rule-based explanations and SUGGESTED corrections.

This module works without any AI. For each issue it builds:
  snippet     - the original lines involved
  explanation - what it means, why it happened, which hardware concept, a beginner tip
  correction  - advice + a corrected code template + the reason

The student's original code is NEVER changed: corrections are shown as separate text.
"""
import re

from modules.error_detector import (
    CAT_COMB, CAT_COMPILE, CAT_COMPILE_WARN, CAT_FUNC, CAT_INCOMPLETE, CAT_LATCH,
    CAT_MULTI, CAT_SIM, CAT_SYNTAX, CAT_SYNTH, CAT_UNDRIVEN, find_always_blocks, strip_comments,
)

BLOCK_KINDS = {"latch", "if_without_else", "case_without_default", "incomplete_sensitivity",
               "nonblocking_in_comb", "blocking_in_seq"}


# --------------------------------------------------------------- snippets
def _block_for_issue(code, issue):
    line = issue.get("line")
    if not line:
        return None
    candidates = [b for b in find_always_blocks(code) if b["start_line"] <= line <= b["end_line"]]
    return min(candidates, key=lambda b: b["end_line"] - b["start_line"]) if candidates else None


def get_snippet(code, issue, max_lines=14):
    """Return the original lines related to an issue (or None if no line is known)."""
    lines = code.splitlines()
    targets = issue.get("lines") or ([issue["line"]] if issue.get("line") else [])
    if not targets or not lines:
        return None
    block = _block_for_issue(code, issue) if (
        issue.get("kind") in BLOCK_KINDS or issue["category"] in (CAT_LATCH, CAT_INCOMPLETE)) else None
    if block:
        start, end = block["start_line"], block["end_line"]
    else:
        context = 2 if issue["category"] == CAT_SYNTAX else 1
        start, end = max(1, min(targets) - context), min(len(lines), max(targets) + context)
    end = min(end, start + max_lines - 1, len(lines))
    return {"start_line": start, "end_line": end, "highlight_lines": targets,
            "code": "\n".join(lines[start - 1:end])}


# ------------------------------------------------------------ explanations
def _where(issue):
    lines = issue.get("lines") or []
    if len(lines) > 1:
        return "Lines " + ", ".join(str(n) for n in lines)
    if issue.get("line"):
        return f"Line {issue['line']}"
    return "No exact line was reported by the tool"


def rule_based_explanation(issue):
    """Beginner-friendly explanation written from fixed rules (no AI)."""
    cat, kind, sig = issue["category"], issue.get("kind"), issue.get("signal") or "the signal"
    where = _where(issue)
    if cat in (CAT_LATCH, CAT_INCOMPLETE):
        branch = ("a 'case' statement without a 'default' branch" if kind == "case_without_default"
                  else "an 'if' without a matching 'else'")
        return {
            "meaning": f"Signal '{sig}' keeps its old value in some situations, so the hardware needs memory. "
                       "A latch (a level-sensitive memory cell) is created.",
            "cause": f"Inside a combinational always block, '{sig}' is not assigned on every possible path. "
                     f"Here the cause looks like {branch}.",
            "responsible_code": f"{where}: the always block that assigns '{sig}'.",
            "concept": "Combinational logic must give an output for EVERY input combination. "
                       "If some paths leave the output unassigned, synthesis infers a latch.",
            "beginner_tip": "Ask: 'if none of my conditions are true, what should this output be?' "
                            "Then write that answer as a default value or an else/default branch.",
        }
    if cat == CAT_MULTI:
        return {
            "meaning": f"More than one piece of logic tries to drive signal '{sig}' at the same time.",
            "cause": "Two or more 'assign' statements or always blocks write to the same signal.",
            "responsible_code": f"{where}: each of these lines drives '{sig}'.",
            "concept": "In hardware a wire has exactly one driver (unless it is a deliberate tri-state bus). "
                       "Two drivers can fight and create a short circuit or an unknown 'x' value.",
            "beginner_tip": "Think of a wire as a pipe with ONE tap. If two sources must share it, "
                            "add a multiplexer that chooses one source.",
        }
    if cat == CAT_SYNTAX:
        return {
            "meaning": "The compiler could not understand the code because it breaks Verilog's grammar rules.",
            "cause": "Typical causes: a missing ';', a missing 'end'/'endmodule', or unbalanced brackets. "
                     "The reported line is where the compiler got confused - the real mistake is often "
                     "on the line just before it.",
            "responsible_code": f"{where} (check the previous line too).",
            "concept": "Verilog statements end with ';' and blocks must be closed with matching keywords.",
            "beginner_tip": "Look at the line before the reported one: is every statement finished with ';'?",
        }
    if cat == CAT_COMPILE or cat == CAT_COMPILE_WARN:
        if kind == "undeclared_identifier":
            return {"meaning": "A name is used but was never declared.",
                    "cause": "The signal is misspelled or you forgot to declare it as input/output/wire/reg.",
                    "responsible_code": f"{where}.", "concept": "Every signal must be declared before use.",
                    "beginner_tip": "Compare the spelling with your declarations (Verilog is case-sensitive)."}
        if kind == "invalid_lvalue":
            return {"meaning": "You assign a value inside an always block to a signal declared as a 'wire'.",
                    "cause": "Signals assigned inside always blocks must be of type 'reg' (or 'logic' in SystemVerilog).",
                    "responsible_code": f"{where}.", "concept": "wire = driven by assign/ports; reg = assigned in always blocks.",
                    "beginner_tip": "'reg' does not always mean a flip-flop; it only means 'assigned inside always'."}
        if kind == "reg_assign_continuous":
            return {"meaning": "A 'reg' signal is driven with an 'assign' statement.",
                    "cause": "'assign' can only drive wires.", "responsible_code": f"{where}.",
                    "concept": "wire vs reg data types.",
                    "beginner_tip": "Use 'assign' with wires and 'always' with regs."}
        if kind == "implicit_wire":
            return {"meaning": "A name that was never declared is silently treated as a 1-bit wire.",
                    "cause": "A typo or missing declaration. Verilog creates an implicit wire instead of an error.",
                    "responsible_code": f"{where}.", "concept": "Implicit nets hide typos.",
                    "beginner_tip": "Declare every signal explicitly (or add `default_nettype none)."}
        return {"meaning": "The compiler reported a problem with this code.",
                "cause": "See the exact compiler message shown above.", "responsible_code": f"{where}.",
                "concept": "Compilation checks that the code follows Verilog rules.",
                "beginner_tip": "Fix the first error first; later errors are often caused by it."}
    if cat == CAT_COMB:
        if kind == "incomplete_sensitivity":
            return {"meaning": "The always block does not list every signal it reads.",
                    "cause": "An explicit sensitivity list like @(a) was used while the block also reads other signals.",
                    "responsible_code": f"{where}: the always header.",
                    "concept": "Simulation only re-runs the block when a listed signal changes; real hardware "
                               "reacts to ALL inputs. The mismatch is a classic simulation-vs-synthesis bug.",
                    "beginner_tip": "For combinational logic write always @(*) so the tool adds all inputs for you."}
        if kind == "nonblocking_in_comb":
            return {"meaning": "Non-blocking assignment (<=) used in combinational logic.",
                    "cause": "<= schedules the update for later, which can give surprising simulation results in combinational blocks.",
                    "responsible_code": f"{where}.", "concept": "Blocking (=) for combinational, non-blocking (<=) for clocked logic.",
                    "beginner_tip": "Rule of thumb: always @(*) -> '='; always @(posedge clk) -> '<='."}
        return {"meaning": "Blocking assignment (=) used in clocked logic.",
                "cause": "Mixing '=' into a clocked block can make registers update in the wrong order.",
                "responsible_code": f"{where}.", "concept": "Race conditions between flip-flops in simulation.",
                "beginner_tip": "Use '<=' for every register in a clocked always block."}
    if cat == CAT_UNDRIVEN:
        return {"meaning": f"Signal '{sig}' is read but nothing drives it.",
                "cause": "It was declared but never assigned, or an input is not connected.",
                "responsible_code": f"{where}.", "concept": "Floating signals have unknown value in hardware.",
                "beginner_tip": "Make sure every wire has exactly one driver."}
    if cat == CAT_FUNC:
        return {"meaning": "Your testbench says the circuit's output differs from what you expected.",
                "cause": "The design behaves differently from the expectation written in the testbench. "
                         "The tools cannot know the intended behaviour - only your testbench/specification can.",
                "responsible_code": "See the testbench message and compare it with the design logic.",
                "concept": "Functional verification compares actual outputs with expected outputs.",
                "beginner_tip": "Trace the failing input values through your design by hand."}
    if cat == CAT_SIM:
        return {"meaning": "The simulation did not complete normally.", "cause": issue["message"],
                "responsible_code": "The testbench.", "concept": "A testbench must end with $finish.",
                "beginner_tip": "Add '$finish;' at the end of your initial block."}
    return {"meaning": "The synthesis tool printed a warning.", "cause": "See the exact tool message.",
            "responsible_code": f"{where}.", "concept": "Synthesis warnings often point to unintended hardware.",
            "beginner_tip": "Read the warning text carefully and search for the signal name in your code."}


# ------------------------------------------------------------ corrections
def _strip_line_comment(line):
    return line.split("//")[0]


def _fix_default_assignment(code, issue):
    """Insert a default assignment right after the 'begin' of the always block."""
    block = _block_for_issue(code, issue)
    sig = issue.get("signal")
    if not block or not sig:
        return None
    lines = code.splitlines()[block["start_line"] - 1:block["end_line"]]
    text = "\n".join(lines)
    op = "<=" if re.search(r"\b%s\b\s*(\[[^\]]*\])?\s*<=" % re.escape(sig), text) else "="
    for i, line in enumerate(lines):
        if re.search(r"\bbegin\b", _strip_line_comment(line)):
            nxt = lines[i + 1] if i + 1 < len(lines) else line
            indent = re.match(r"\s*", nxt).group(0) or (re.match(r"\s*", line).group(0) + "    ")
            lines.insert(i + 1, f"{indent}{sig} {op} 1'b0;  // default value: prevents a latch")
            return "\n".join(lines)
    return None


def _fix_sensitivity(code, issue):
    block = _block_for_issue(code, issue)
    if not block:
        return None
    lines = code.splitlines()[block["start_line"] - 1:block["end_line"]]
    lines[0] = re.sub(r"always\s*@\s*\([^)]*\)", "always @(*)", lines[0], count=1)
    return "\n".join(lines)


_NONBLOCKING = re.compile(r"(?m)(^\s*|;\s*|\belse\s+|\)\s*)([A-Za-z_]\w*(?:\[[^\]]*\])?)\s*<=")
_BLOCKING = re.compile(r"(?m)(^\s*|;\s*|\belse\s+|\)\s*)([A-Za-z_]\w*(?:\[[^\]]*\])?)\s*=(?![=])")


def _fix_assign_style(code, issue, to_blocking):
    block = _block_for_issue(code, issue)
    if not block:
        return None
    text = "\n".join(code.splitlines()[block["start_line"] - 1:block["end_line"]])
    if to_blocking:
        return _NONBLOCKING.sub(lambda m: f"{m.group(1)}{m.group(2)} =", text)
    return _BLOCKING.sub(lambda m: f"{m.group(1)}{m.group(2)} <=", text)


def _needs_semicolon(line):
    s = strip_comments(line).strip()
    if not s or s[-1] in ";,({:":
        return False
    if s.startswith("`") or re.search(r"\b(begin|end|else|endcase|endmodule|endfunction|endtask|generate|endgenerate)$", s):
        return False
    if s.endswith(")") and re.match(r"(if|else|for|while|always|case|casez|casex|initial|repeat)\b|@", s):
        return False
    return True


def _fix_semicolon(code, issue):
    lines = code.splitlines()
    snippet = get_snippet(code, issue)
    if not snippet or not issue.get("line"):
        return None
    target = None
    for number in (issue["line"], issue["line"] - 1, issue["line"] - 2):  # reported line, then back
        if 1 <= number <= len(lines) and strip_comments(lines[number - 1]).strip():
            if _needs_semicolon(lines[number - 1]):
                target = number
            break
    if target is None or not (snippet["start_line"] <= target <= snippet["end_line"]):
        return None
    text = lines[target - 1]
    head, sep, tail = text.partition("//")
    lines[target - 1] = head.rstrip() + ";" + ((" " + sep + tail) if sep else "")
    return "\n".join(lines[snippet["start_line"] - 1:snippet["end_line"]]), target


def _find_decl_fix(code, name):
    """Find a declaration of `name` as wire/output and return (line_no, fixed_text)."""
    for n, line in enumerate(code.splitlines(), 1):
        s = _strip_line_comment(line)
        if re.search(r"\b%s\b" % re.escape(name), s) and re.search(r"\b(output|wire)\b", s):
            fixed = re.sub(r"\boutput\s+wire\b", "output reg", s)
            fixed = re.sub(r"\boutput\b(?!\s+reg)", "output reg", fixed) if fixed == s else fixed
            fixed = re.sub(r"\bwire\b", "reg", fixed) if fixed == s else fixed
            return n, fixed.rstrip()
    return None


def build_correction(issue, code):
    """Return {suggestion, corrected_code, reason, note}. corrected_code may be None."""
    cat, kind, sig = issue["category"], issue.get("kind"), issue.get("signal")
    note = "Suggestion only - your original code was not changed. Check that it matches your design intent."
    out = {"suggestion": None, "corrected_code": None, "reason": None, "note": note}

    if cat in (CAT_LATCH, CAT_INCOMPLETE):
        out["suggestion"] = (f"Give '{sig}' a value on every path: add a default assignment at the top of the block "
                             "(shown below), or add the missing 'else' / 'default:' branch.")
        out["corrected_code"] = _fix_default_assignment(code, issue)
        out["reason"] = ("With a default value, '%s' always has a defined value, so no memory is needed "
                         "and the circuit stays purely combinational." % sig)
    elif cat == CAT_MULTI:
        rhs = (issue.get("extra") or {}).get("rhs") or []
        out["suggestion"] = (f"Keep exactly ONE driver for '{sig}'. Delete the extra driver or combine the sources "
                             "with a multiplexer.")
        if len(rhs) >= 2:
            out["corrected_code"] = (f"// 'sel' is a NEW control input you must add to choose the source.\n"
                                     f"assign {sig} = sel ? ({rhs[1]}) : ({rhs[0]});")
        else:
            out["corrected_code"] = (f"// Keep ONE driver for '{sig}'. Merge all sources into one block, e.g.:\n"
                                     f"always @(*) begin\n    if (sel) {sig} = <source_2>;\n    else     {sig} = <source_1>;\nend")
        out["reason"] = "A wire can only have one driver; a multiplexer makes the choice explicit."
    elif cat == CAT_SYNTAX:
        fixed = _fix_semicolon(code, issue)
        if fixed:
            out["suggestion"] = f"Line {fixed[1]} looks like a statement that is missing its ';'."
            out["corrected_code"] = fixed[0]
            out["reason"] = "Every Verilog statement must end with a semicolon."
        else:
            out["suggestion"] = ("Check the reported line and the line before it for a missing ';', "
                                 "unmatched begin/end, or unbalanced brackets.")
            out["reason"] = "The tool could not tell exactly which token is wrong, so no automatic template is offered."
    elif kind == "undeclared_identifier" or kind == "implicit_wire":
        m = re.search(r"`([^']+)'|wire '?`?(\w+)", issue["message"] + " " + (issue.get("raw") or ""))
        name = (m.group(1) or m.group(2)) if m else "name"
        out["suggestion"] = f"Declare '{name}' (or fix the spelling if it is a typo)."
        out["corrected_code"] = f"wire {name};   // use 'reg {name};' if it is assigned inside an always block"
        out["reason"] = "Declaring signals explicitly prevents silent typos."
    elif kind == "invalid_lvalue":
        m = re.search(r"(\w+) is not a valid l-value", issue["message"])
        fix = _find_decl_fix(code, m.group(1)) if m else None
        out["suggestion"] = "Declare the signal as 'reg' because it is assigned inside an always block."
        if fix:
            out["corrected_code"] = f"// line {fix[0]}:\n{fix[1]}"
        out["reason"] = "Only 'reg' (or 'logic') signals can be assigned inside always blocks."
    elif kind == "reg_assign_continuous":
        out["suggestion"] = "Declare the signal as 'wire' when using 'assign', or drive it from an always block."
        out["reason"] = "'assign' drives wires; always blocks drive regs."
    elif kind == "incomplete_sensitivity":
        out["suggestion"] = "Use @(*) so every signal read in the block triggers it."
        out["corrected_code"] = _fix_sensitivity(code, issue)
        out["reason"] = "@(*) makes simulation match the hardware that synthesis builds."
    elif kind == "nonblocking_in_comb":
        out["suggestion"] = "Use blocking assignments (=) in combinational always blocks."
        out["corrected_code"] = _fix_assign_style(code, issue, to_blocking=True)
        out["reason"] = "Blocking assignments evaluate in order, like the combinational logic you intend."
    elif kind == "blocking_in_seq":
        out["suggestion"] = "Use non-blocking assignments (<=) for registers in clocked always blocks."
        out["corrected_code"] = _fix_assign_style(code, issue, to_blocking=False)
        out["reason"] = "Non-blocking updates all flip-flops 'at once', avoiding race conditions."
    elif cat == CAT_FUNC:
        out["suggestion"] = ("Compare the failing case with your intended behaviour. No automatic fix is possible: "
                             "the tools do not know what the circuit is SUPPOSED to do - only your testbench does.")
        out["reason"] = "Functional errors need a specification or reference model."
    else:
        out["suggestion"] = "Read the exact tool message above; no automatic correction is available for this item."
        out["reason"] = "The rule-based engine has no template for this message."
    return out


def enrich_issues(issues, code):
    """Attach snippet, explanation and correction to every issue (original code untouched)."""
    for issue in issues:
        issue["snippet"] = get_snippet(code, issue)
        issue["explanation"] = rule_based_explanation(issue)
        issue["explanation_source"] = "rules"
        issue["correction"] = build_correction(issue, code)
        fixed = issue["correction"].get("corrected_code")
        if isinstance(fixed, tuple):  # (_fix_semicolon returns a tuple)
            issue["correction"]["corrected_code"] = fixed[0]
    return issues
