"""error_detector.py - turns tool output and simple static checks into a list of "issues".

Every issue has:
  confidence = "confirmed"  -> reported by a real tool (Icarus Verilog, Yosys, or the
                                student's own testbench output)
               "potential"  -> found by our own simple text checks; may be a false alarm
  source     = which tool / check found it

Static checks are deliberately simple (regular expressions, not a full Verilog parser).
They help when a tool is not installed and add hints, but they never replace the tools.
"""
import re

from modules.verilog_analyzer import strip_comments

CAT_SYNTAX = "Syntax error"
CAT_COMPILE = "Compilation error"
CAT_COMPILE_WARN = "Compiler warning"
CAT_LATCH = "Unintended latch"
CAT_MULTI = "Multiple drivers"
CAT_INCOMPLETE = "Incomplete combinational assignment"
CAT_COMB = "Potential combinational logic issue"
CAT_SYNTH = "Synthesis warning"
CAT_UNDRIVEN = "Undriven signal"
CAT_FUNC = "Functional mismatch (testbench)"
CAT_SIM = "Simulation problem"

KEYWORDS = {
    "if", "else", "case", "casez", "casex", "endcase", "default", "begin", "end", "for", "while",
    "repeat", "forever", "assign", "always", "initial", "module", "endmodule", "input", "output",
    "inout", "wire", "reg", "integer", "parameter", "localparam", "posedge", "negedge", "or", "and",
    "not", "signed", "logic", "function", "endfunction", "task", "endtask", "generate", "endgenerate",
}


def make_issue(category, severity, confidence, source, message, line=None, lines=None,
               signal=None, kind="generic", raw=None, extra=None):
    """Create one issue dictionary (the id is added later)."""
    return {
        "id": None, "category": category, "severity": severity, "confidence": confidence,
        "source": source, "message": message, "line": line,
        "lines": lines or ([line] if line else []), "signal": signal, "kind": kind,
        "raw": raw, "corroborated_by": [], "extra": extra or {},
    }


# =====================================================================
# Tiny statement parser (used by the static checks)
# =====================================================================
def _ws(t, p):
    while p < len(t) and t[p].isspace():
        p += 1
    return p


def _kw(t, p, word):
    """True if the keyword `word` starts exactly at position p."""
    end = p + len(word)
    if not t.startswith(word, p):
        return False
    if end < len(t) and (t[end].isalnum() or t[end] == "_"):
        return False
    return p == 0 or not (t[p - 1].isalnum() or t[p - 1] == "_")


def _skip_parens(t, p):
    """p is at '('. Return the index just after the matching ')'."""
    depth = 0
    for i in range(p, len(t)):
        if t[i] == "(":
            depth += 1
        elif t[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(t)


def _match_end(t, p, opens, close):
    """p is at an opening word (begin/case). Return the index after its matching end word."""
    pattern = re.compile(r"\b(?:%s|%s)\b" % ("|".join(opens), close))
    depth = 0
    for m in pattern.finditer(t, p):
        if m.group(0) == close:
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            depth += 1
    return len(t)


_SIMPLE_ASSIGN = re.compile(r"\s*(?:#\s*\d+\s*)?([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(<=|=)(?!=)")


def _empty(end):
    return {"def": set(), "pos": set(), "part": set(), "ops": set(), "end": end}


def _merge(into, other):
    for key in ("pos", "part", "ops"):
        into[key] |= other[key]


def _parse_stmt(t, pos, depth=0):
    """Parse one statement starting at pos.

    Returns a dict:
      def  = signals assigned on EVERY path through the statement
      pos  = signals assigned on SOME path
      part = signals assigned with an index (a[3] = ...) - only part of the signal
      ops  = assignment operators used ("=" and/or "<=")
      end  = index after the statement
    """
    pos = _ws(t, pos)
    if depth > 40 or pos >= len(t):
        return _empty(len(t))

    if _kw(t, pos, "begin"):
        end = _match_end(t, pos, ("begin",), "end")
        inner = re.sub(r"^\s*:\s*\w+", "", t[pos + 5:max(pos + 5, end - 3)])
        result = _empty(end)
        p = 0
        while True:
            p = _ws(inner, p)
            if p >= len(inner):
                break
            sub = _parse_stmt(inner, p, depth + 1)
            result["def"] |= sub["def"]
            _merge(result, sub)
            p = sub["end"] if sub["end"] > p else p + 1
        return result

    if _kw(t, pos, "if"):
        lp = t.find("(", pos)
        rp = _skip_parens(t, lp) if lp != -1 else len(t)
        then = _parse_stmt(t, rp, depth + 1)
        result = _empty(then["end"])
        _merge(result, then)
        m = re.compile(r"\s*else\b").match(t, then["end"])
        if m:
            other = _parse_stmt(t, m.end(), depth + 1)
            result["def"] = then["def"] & other["def"]
            _merge(result, other)
            result["end"] = other["end"]
        return result

    if _kw(t, pos, "case") or _kw(t, pos, "casez") or _kw(t, pos, "casex"):
        lp = t.find("(", pos)
        start = _skip_parens(t, lp) if lp != -1 else pos
        end = _match_end(t, pos, ("case", "casez", "casex"), "endcase")
        inner = t[start:max(start, end - len("endcase"))]
        result = _empty(end)
        sets, has_default, p = [], False, 0
        while True:
            p = _ws(inner, p)
            if p >= len(inner):
                break
            colon = inner.find(":", p)
            if colon == -1:
                break
            if inner[p:colon].strip().startswith("default"):
                has_default = True
            sub = _parse_stmt(inner, colon + 1, depth + 1)
            sets.append(sub["def"])
            _merge(result, sub)
            p = sub["end"] if sub["end"] > colon + 1 else colon + 1
        if has_default and sets:
            result["def"] = set.intersection(*sets)
        return result

    if any(_kw(t, pos, w) for w in ("for", "while", "repeat", "forever")):
        lp = t.find("(", pos)
        rp = _skip_parens(t, lp) if lp != -1 else pos + 3
        body = _parse_stmt(t, rp, depth + 1)
        result = _empty(body["end"])
        _merge(result, body)  # a loop might run zero times, so nothing is "definite"
        return result

    # simple statement:  name = expr;   or   name <= expr;
    semi = t.find(";", pos)
    end = len(t) if semi == -1 else semi + 1
    result = _empty(end)
    m = _SIMPLE_ASSIGN.match(t[pos:end])
    if m and m.group(1) not in KEYWORDS:
        name = m.group(1)
        result["def"].add(name)
        result["pos"].add(name)
        result["ops"].add(m.group(3))
        if m.group(2):
            result["part"].add(name)
    return result


# =====================================================================
# Finding modules and always blocks
# =====================================================================
_ALWAYS = re.compile(r"\balways(?:_comb|_ff|_latch)?\b")
_SENS = re.compile(r"\s*@\s*(\(([^)]*)\)|\*)")
_MODULE = re.compile(r"\bmodule\b.*?\bendmodule\b", re.S)
_ASSIGN = re.compile(
    r"\bassign\b\s*(?:\([^)]*\)\s*)?(?:#\s*\(?\s*\w+\s*\)?\s*)?([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*=\s*([^;]*);")
_DECL = re.compile(
    r"\b(?:input|output|inout|wire|reg|logic)\b\s*(?:reg|wire|logic|signed)?\s*(?:\[[^\]]*\])?\s*"
    r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)")
_DECL_WORDS = {"input", "output", "inout", "wire", "reg", "logic", "signed"}


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def _module_spans(clean):
    spans = [(m.start(), m.end()) for m in _MODULE.finditer(clean)]
    return spans or [(0, len(clean))]


def _declared(module_text):
    names = set()
    for m in _DECL.finditer(module_text):
        for tok in re.split(r"\s*,\s*", m.group(1)):
            if tok and tok not in _DECL_WORDS and tok not in KEYWORDS:
                names.add(tok)
    return names


def _integers(module_text):
    names = set()
    for m in re.finditer(r"\b(?:integer|genvar)\b\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)", module_text):
        names |= set(re.split(r"\s*,\s*", m.group(1)))
    return names


def find_always_blocks(code):
    """Return a list of always blocks with their line range and what they assign."""
    clean = strip_comments(code)
    spans = _module_spans(clean)
    blocks = []
    for m in _ALWAYS.finditer(clean):
        header_end, sens, star = m.end(), None, False
        sm = _SENS.match(clean, m.end())
        if sm:
            header_end = sm.end()
            if sm.group(1) == "*":
                star = True
            else:
                sens = sm.group(2)
                star = sens.strip() == "*"
        try:
            parsed = _parse_stmt(clean, header_end)
        except Exception:  # a parsing surprise must never crash the app
            continue
        keyword = m.group(0)
        if keyword == "always_ff" or (sens and re.search(r"\b(posedge|negedge)\b", sens)):
            kind = "seq"
        elif keyword == "always_latch":
            kind = "latch"
        elif keyword == "always_comb" or star or sens is not None:
            kind = "comb"
        else:
            kind = "other"
        mod = next((i for i, (a, b) in enumerate(spans) if a <= m.start() < b), 0)
        blocks.append({
            "start_line": _line_of(clean, m.start()),
            "end_line": _line_of(clean, max(m.start(), parsed["end"] - 1)),
            "kind": kind, "sens": None if star else sens, "star": star,
            "body": clean[header_end:parsed["end"]], "module": mod,
            "definite": parsed["def"], "possible": parsed["pos"],
            "partial": parsed["part"], "ops": parsed["ops"],
        })
    return blocks


# =====================================================================
# Static (heuristic) checks
# =====================================================================
def static_checks(code):
    """Simple text-based checks. Everything returned here is only 'potential'."""
    try:
        return _static_checks(code)
    except Exception:  # heuristics must never break the analysis
        return []


def _static_checks(code):
    clean = strip_comments(code)
    issues = _balance_checks(clean)
    if issues:  # structure is broken, further heuristics would be unreliable
        return issues

    spans = _module_spans(clean)
    blocks = find_always_blocks(code)
    mod_text = [clean[a:b] for a, b in spans]
    declared = [_declared(t) for t in mod_text]
    integers = [_integers(t) for t in mod_text]

    for b in blocks:
        idx = b["module"]
        if b["kind"] == "comb":
            issues += _latch_candidates(b, integers[idx])
            if b["sens"] is not None:
                issues += _sensitivity_check(b, declared[idx], integers[idx])
            if "<=" in b["ops"]:
                issues.append(make_issue(
                    CAT_COMB, "warning", "potential", "static-check",
                    "Non-blocking assignment (<=) used inside a combinational always block. "
                    "Combinational logic normally uses blocking assignments (=).",
                    line=b["start_line"], kind="nonblocking_in_comb"))
        elif b["kind"] == "seq" and "=" in b["ops"]:
            issues.append(make_issue(
                CAT_COMB, "info", "potential", "static-check",
                "Blocking assignment (=) used inside a clocked always block. "
                "Registers (flip-flops) normally use non-blocking assignments (<=).",
                line=b["start_line"], kind="blocking_in_seq"))

    issues += _multiple_driver_candidates(clean, spans, blocks)
    return issues


def _balance_checks(clean):
    """Unbalanced begin/end, module/endmodule, case/endcase, parentheses; assign without ';'."""
    issues = []
    pairs = [(r"\bbegin\b", r"\bend\b", "begin", "end"),
             (r"\bmodule\b", r"\bendmodule\b", "module", "endmodule"),
             (r"\bcase[zx]?\b", r"\bendcase\b", "case", "endcase")]
    for open_re, close_re, o, c in pairs:
        n_open, n_close = len(re.findall(open_re, clean)), len(re.findall(close_re, clean))
        if n_open != n_close:
            issues.append(make_issue(
                CAT_SYNTAX, "error", "potential", "static-check",
                f"Unbalanced '{o}'/'{c}': found {n_open} '{o}' but {n_close} '{c}'.", kind="unbalanced"))
    if clean.count("(") != clean.count(")"):
        issues.append(make_issue(CAT_SYNTAX, "error", "potential", "static-check",
                                 "Unbalanced parentheses: the number of '(' and ')' differ.", kind="unbalanced"))
    for m in re.finditer(r"\bassign\b", clean):
        semi = clean.find(";", m.end())
        segment = clean[m.start(): len(clean) if semi == -1 else semi]
        nxt = re.search(r"\n\s*(assign|always|endmodule|initial|wire|reg|input|output|module|parameter)\b", segment)
        if nxt:
            # nxt.start() is the newline that ends the statement's last line
            culprit = _line_of(clean, m.start() + nxt.start())
            issues.append(make_issue(
                CAT_SYNTAX, "error", "potential", "static-check",
                f"A statement starting with 'assign' seems to be missing a ';' (line {culprit}).",
                line=culprit, kind="missing_semicolon"))
    return issues


def _latch_candidates(block, integers):
    result = []
    names = sorted(block["possible"] - block["definite"] - integers)
    has_case = re.search(r"\bcase[zx]?\b", block["body"]) is not None
    has_default = re.search(r"\bdefault\b", block["body"]) is not None
    for name in names:
        kind = "case_without_default" if has_case and not has_default else "if_without_else"
        result.append(make_issue(
            CAT_INCOMPLETE, "warning", "potential", "static-check",
            f"Signal '{name}' is not assigned on every path through this combinational always block "
            f"(lines {block['start_line']}-{block['end_line']}). Hardware must remember its old value, "
            "which creates a latch.",
            line=block["start_line"], signal=name, kind=kind,
            extra={"block": (block["start_line"], block["end_line"])}))
    return result


def _sensitivity_check(block, declared, integers):
    listed = set(re.findall(r"[A-Za-z_]\w*", block["sens"] or "")) - {"or", "and"}
    words = set(re.findall(r"[A-Za-z_]\w*", block["body"]))
    read = (words & declared) - block["possible"] - integers - KEYWORDS
    missing = sorted(read - listed)
    if not missing:
        return []
    return [make_issue(
        CAT_COMB, "warning", "potential", "static-check",
        f"The sensitivity list ({', '.join(sorted(listed)) or 'empty'}) is missing signals that the block reads: "
        f"{', '.join(missing)}. Simulation will not react when they change, but synthesis ignores the list, "
        "so simulation and real hardware can disagree.",
        line=block["start_line"], kind="incomplete_sensitivity", extra={"missing": missing})]


def _multiple_driver_candidates(clean, spans, blocks):
    issues = []
    for mod_index, (a, b) in enumerate(spans):
        sites = {}      # signal -> list of (line, rhs or None)
        partial = set()
        tristate = set()
        for m in _ASSIGN.finditer(clean, a, b):
            name = m.group(1)
            if m.group(2):
                partial.add(name)
            if re.search(r"'[sS]?[bBhHdDoO]\s*[zZ]", m.group(3)):
                tristate.add(name)
            sites.setdefault(name, []).append((_line_of(clean, m.start()), " ".join(m.group(3).split())))
        for blk in blocks:
            if blk["module"] != mod_index or blk["kind"] not in ("comb", "seq", "latch"):
                continue
            partial |= blk["partial"]
            for name in blk["possible"]:
                sites.setdefault(name, []).append((blk["start_line"], None))
        for name, found in sorted(sites.items()):
            lines = sorted({ln for ln, _ in found})
            if len(lines) < 2 or name in partial or name in tristate:
                continue
            issues.append(make_issue(
                CAT_MULTI, "error", "potential", "static-check",
                f"Signal '{name}' is driven from {len(lines)} places (lines {', '.join(map(str, lines))}). "
                "In real hardware two outputs cannot drive the same wire.",
                line=lines[0], lines=lines, signal=name, kind="multiple_drivers",
                extra={"rhs": [r for _, r in found if r]}))
    return issues


# =====================================================================
# Parsers for real tool output
# =====================================================================
_IV_LINE = re.compile(r"^(?P<file>.*?\.s?v):(?P<line>\d+):\s*(?:(?P<level>error|warning|sorry):\s*)?(?P<msg>.*)$")


def parse_iverilog_output(text, prefix=""):
    """Parse Icarus Verilog compiler output (stdout + stderr)."""
    issues = []
    for raw in text.splitlines():
        m = _IV_LINE.match(raw.strip())
        if not m:
            continue
        level, msg, fname = m.group("level"), m.group("msg").strip(), m.group("file")
        in_design = fname.replace("\\", "/").split("/")[-1] == "design.v"
        line = int(m.group("line")) if in_design else None
        note = "" if in_design else "[testbench] "
        if not msg and not level:
            continue
        if level is None and "syntax error" in msg or msg == "syntax error":
            issues.append(make_issue(CAT_SYNTAX, "error", "confirmed", "iverilog" + prefix,
                                     note + "syntax error", line=line, kind="syntax", raw=raw.strip()))
        elif level == "error":
            kind = "generic"
            if "Unable to bind" in msg:
                kind = "undeclared_identifier"
            elif "not a valid l-value" in msg:
                kind = "invalid_lvalue"
            elif "cannot be driven by primitives or continuous assignment" in msg:
                kind = "reg_assign_continuous"
            category = CAT_SYNTAX if "syntax error" in msg else CAT_COMPILE
            issues.append(make_issue(category, "error", "confirmed", "iverilog" + prefix,
                                     note + msg, line=line, kind=kind, raw=raw.strip()))
        elif level in ("warning", "sorry"):
            kind = "implicit_wire" if "implicit definition of wire" in msg else "generic"
            issues.append(make_issue(CAT_COMPILE_WARN, "warning", "confirmed", "iverilog" + prefix,
                                     note + msg, line=line, kind=kind, raw=raw.strip()))
    return issues


_YS_LATCH = re.compile(r"Latch inferred for signal `(?P<sig>[^']+)' from process `(?P<proc>[^']+)'")
_YS_PROC_LINE = re.compile(r":(\d+)\$\d+\s*$")
_YS_MULTI = re.compile(r"multiple conflicting drivers for (?P<sig>[^\s:]+)")
_YS_NODRIVER = re.compile(r"Wire (?P<sig>\S+) is used but has no driver")
_YS_FILE = re.compile(
    r"(?P<file>\S*\.s?v):(?P<line>\d+)(?:\.\d+(?:-\d+\.\d+)?)?:\s*(?P<lvl>ERROR|Warning|WARNING):\s*(?P<msg>.*)")
_YS_PARSER_OLD = re.compile(r"ERROR: Parser error in line (?P<file>\S+?\.s?v):(?P<line>\d+):\s*(?P<msg>.*)")


def _short_signal(name):
    """'\\top.\\q' or 'top.q' -> 'q'."""
    return name.replace("\\", "").split(".")[-1]


def parse_yosys_output(text):
    """Parse Yosys log output for the warnings/errors we care about."""
    issues, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        m = _YS_LATCH.search(line)
        if m:
            sig = _short_signal(m.group("sig"))
            pm = _YS_PROC_LINE.search(m.group("proc"))
            if sig in seen:
                continue
            seen.add(sig)
            issues.append(make_issue(
                CAT_LATCH, "warning", "confirmed", "yosys",
                f"Yosys inferred a latch for signal '{sig}'.",
                line=int(pm.group(1)) if pm else None, signal=sig, kind="latch", raw=line))
            continue
        m = _YS_MULTI.search(line)
        if m:
            sig = _short_signal(m.group("sig"))
            if ("multi", sig) in seen:
                continue
            seen.add(("multi", sig))
            issues.append(make_issue(
                CAT_MULTI, "error", "confirmed", "yosys",
                f"Yosys found multiple conflicting drivers for signal '{sig}'.",
                signal=sig, kind="multiple_drivers", raw=line))
            continue
        m = _YS_NODRIVER.search(line)
        if m:
            sig = _short_signal(m.group("sig"))
            issues.append(make_issue(
                CAT_UNDRIVEN, "warning", "confirmed", "yosys",
                f"Signal '{sig}' is used but nothing drives it.", signal=sig, kind="undriven", raw=line))
            continue
        m = _YS_PARSER_OLD.search(line) or _YS_FILE.search(line)
        if m:
            msg = m.group("msg").strip()
            is_error = "lvl" not in m.groupdict() or m.group("lvl") == "ERROR"
            syntax = "syntax error" in msg.lower()
            if is_error:
                issues.append(make_issue(
                    CAT_SYNTAX if syntax else CAT_COMPILE, "error", "confirmed", "yosys", msg,
                    line=int(m.group("line")), kind="syntax" if syntax else "generic", raw=line))
            else:
                issues.append(make_issue(CAT_SYNTH, "warning", "confirmed", "yosys", msg,
                                         line=int(m.group("line")), raw=line))
            continue
        if line.startswith("ERROR:"):
            issues.append(make_issue(CAT_COMPILE, "error", "confirmed", "yosys",
                                     line[len("ERROR:"):].strip(), raw=line))
        elif line.startswith("Warning:") and "Found and reported" not in line:
            issues.append(make_issue(CAT_SYNTH, "warning", "confirmed", "yosys",
                                     line[len("Warning:"):].strip(), raw=line))
    return issues


_FAIL_WORDS = re.compile(r"\b(fail|failed|error|mismatch)\b", re.I)


def parse_simulation(sim):
    """Look at the student's testbench output. Needs expected-behaviour checks inside the testbench."""
    issues = []
    if not sim or not sim.get("requested"):
        return issues
    comp, run = sim.get("compile"), sim.get("run")
    if comp and comp.get("ran") and comp.get("returncode") != 0:
        issues += parse_iverilog_output(comp["stdout"] + "\n" + comp["stderr"], prefix="(testbench)")
        if not issues:
            issues.append(make_issue(CAT_SIM, "error", "confirmed", "simulation",
                                     "The design + testbench did not compile."))
    if run:
        if run.get("timed_out"):
            issues.append(make_issue(
                CAT_SIM, "warning", "confirmed", "simulation",
                "The simulation did not finish before the time limit. A testbench needs '$finish;' "
                "(or the design has an endless loop).", kind="sim_timeout"))
        for raw in (run.get("stdout") or "").splitlines():
            if _FAIL_WORDS.search(raw):
                issues.append(make_issue(
                    CAT_FUNC, "error", "confirmed", "simulation",
                    f"Testbench reported a problem: {raw.strip()}", kind="functional", raw=raw.strip()))
    return issues


# =====================================================================
# Putting it all together
# =====================================================================
def _ran_ok(report):
    return bool(report) and report.get("ran") and not report.get("timed_out") and report.get("returncode") == 0


def detect_issues(code, reports):
    """Combine tool findings and static checks into one de-duplicated list."""
    iv, ys = reports.get("iverilog"), reports.get("yosys")
    iv_issues = parse_iverilog_output((iv["stdout"] + "\n" + iv["stderr"])) if iv and iv.get("ran") else []
    ys_issues = parse_yosys_output(ys["stdout"] + "\n" + ys["stderr"]) if ys and ys.get("ran") else []
    sim_issues = parse_simulation(reports.get("simulation"))
    static = static_checks(code)

    for rep, name in ((iv, "Icarus Verilog"), (ys, "Yosys")):
        if rep and rep.get("timed_out"):
            iv_issues.append(make_issue(CAT_COMPILE, "warning", "confirmed", name.split()[0].lower(),
                                        f"{name} did not finish before the time limit.", kind="timeout"))

    confirmed = iv_issues + ys_issues
    # 1) the same syntax error reported by both tools -> keep one
    iv_syntax = [i for i in iv_issues if i["category"] == CAT_SYNTAX]
    if iv_syntax:
        for y in [i for i in ys_issues if i["category"] == CAT_SYNTAX]:
            confirmed.remove(y)
            iv_syntax[0]["corroborated_by"].append("yosys")
    # 2) a confirmed syntax error makes the heuristic checks unreliable
    has_confirmed_syntax = any(i["category"] == CAT_SYNTAX for i in confirmed)
    if has_confirmed_syntax:
        static = []
    yosys_clean = _ran_ok(ys)

    # 3) merge heuristic findings into matching tool findings
    for issue in list(confirmed):
        if issue["category"] == CAT_LATCH:
            for s in [x for x in static if x["category"] == CAT_INCOMPLETE and x["signal"] == issue["signal"]]:
                issue["kind"] = s["kind"]
                issue["line"] = issue["line"] or s["line"]
                issue["lines"] = issue["lines"] or [issue["line"]]
                issue["corroborated_by"].append("static-check")
                static.remove(s)
        if issue["category"] == CAT_MULTI:
            for s in [x for x in static if x["category"] == CAT_MULTI and x["signal"] == issue["signal"]]:
                issue["line"], issue["lines"], issue["extra"] = s["line"], s["lines"], s["extra"]
                issue["corroborated_by"].append("static-check")
                static.remove(s)
    # 4) Yosys finished cleanly and did not report a latch -> drop the heuristic latch guess
    if yosys_clean:
        static = [s for s in static if s["category"] != CAT_INCOMPLETE]
    # 5) hide a heuristic syntax guess if a tool already reported syntax problems (handled above)

    issues = confirmed + sim_issues + static
    issues.sort(key=lambda i: (i["line"] is None, i["line"] or 0, i["category"]))
    for n, issue in enumerate(issues, 1):
        issue["id"] = f"ISSUE-{n}"
    return issues
