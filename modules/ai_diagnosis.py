"""ai_diagnosis.py - OPTIONAL AI explanations on top of the rule-based ones.

How it stays honest:
  * The AI never creates issues. It only explains the issues we already found
    (identified by ISSUE-n ids) using the real code and real tool output.
  * Its answer must be JSON; unknown ids and non-text values are ignored.
  * If the AI text mentions a line number that is not part of that issue's code
    snippet, the AI text for that issue is discarded and the rule-based text is kept.
  * With no API key (or on any error) the rule-based explanations are used.
"""
import json
import re

import requests

import config

AI_FIELDS = ("meaning", "cause", "concept", "beginner_tip", "fix_reason")

SYSTEM_PROMPT = (
    "You are a patient teaching assistant for students learning Verilog HDL. "
    "You are given the student's code (with line numbers), the REAL output of Icarus Verilog / Yosys, "
    "and a list of issues already detected. Explain ONLY those issues. "
    "Never invent compiler errors, tool output, issues or line numbers. "
    "Only mention line numbers that appear in the issue's 'lines' or 'snippet_lines'. "
    "If something is only a 'potential' finding, say it is a possibility, not a certainty. "
    "Reply with JSON only (no markdown fences) in this shape: "
    '{"summary": "2-3 sentence overview", "issues": {"ISSUE-1": {"meaning": "...", "cause": "...", '
    '"concept": "hardware concept involved", "beginner_tip": "...", "fix_reason": "why the suggested fix works"}}}'
)


def ai_status():
    """Is an AI provider configured?"""
    s = config.llm_settings()
    ready = bool(s["api_key"] and s["provider"] in ("anthropic", "openai") and s["model"])
    return {"configured": ready, "provider": s["provider"] or None, "model": s["model"] or None}


def _numbered(code):
    return "\n".join(f"{n:4d}| {line}" for n, line in enumerate(code.splitlines(), 1))


def build_prompt(code, reports, issues):
    """Assemble the user message sent to the LLM."""
    def tool_text(name):
        rep = reports.get(name)
        if not rep:
            return "(tool not run)"
        return ((rep.get("stdout") or "") + (rep.get("stderr") or ""))[:6000] or "(no output)"

    sim = reports.get("simulation") or {}
    payload = {
        "issues": [
            {"id": i["id"], "category": i["category"], "confidence": i["confidence"], "source": i["source"],
             "line": i["line"], "lines": i["lines"], "signal": i["signal"], "message": i["message"],
             "snippet_lines": [i["snippet"]["start_line"], i["snippet"]["end_line"]] if i.get("snippet") else [],
             "suggested_fix": (i.get("correction") or {}).get("corrected_code")}
            for i in issues
        ]
    }
    return (
        "STUDENT CODE:\n" + _numbered(code) +
        "\n\nICARUS VERILOG OUTPUT:\n" + tool_text("iverilog") +
        "\n\nYOSYS OUTPUT:\n" + tool_text("yosys") +
        "\n\nTESTBENCH OUTPUT:\n" + (((sim.get("run") or {}).get("stdout") or "(none)")[:3000]) +
        "\n\nDETECTED ISSUES (JSON):\n" + json.dumps(payload, indent=1)
    )


def _call_llm(prompt, settings):
    """Send the prompt to the configured provider and return the reply text."""
    timeout = settings["timeout"]
    if settings["provider"] == "anthropic":
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": settings["model"], "max_tokens": 3000, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout)
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
    resp = requests.post(  # OpenAI-compatible
        settings["base_url"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}", "content-type": "application/json"},
        json={"model": settings["model"], "max_tokens": 3000,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]},
        timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_ai_json(text):
    """Parse the model reply, tolerating ```json fences."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in AI reply")
    return json.loads(cleaned[start:end + 1])


def _lines_ok(texts, issue, total_lines):
    """True if every 'line N' mentioned is one this issue legitimately covers."""
    allowed = set(issue.get("lines") or [])
    if issue.get("snippet"):
        allowed |= set(range(issue["snippet"]["start_line"], issue["snippet"]["end_line"] + 1))
    for text in texts:
        for m in re.finditer(r"\blines?\s+(\d+)(?:\s*(?:-|to|and)\s*(\d+))?", text, re.I):
            for g in (m.group(1), m.group(2)):
                if g and (int(g) > total_lines or (allowed and int(g) not in allowed)):
                    return False
    return True


def apply_ai_answer(answer, issues, code):
    """Copy validated AI text into the issues. Returns how many issues were enriched."""
    total = len(code.splitlines())
    items = answer.get("issues") if isinstance(answer, dict) else None
    if not isinstance(items, dict):
        return 0
    count = 0
    for issue in issues:
        entry = items.get(issue["id"])
        if not isinstance(entry, dict):
            continue
        clean = {k: entry[k].strip() for k in AI_FIELDS if isinstance(entry.get(k), str) and entry[k].strip()}
        if not clean or not _lines_ok(list(clean.values()), issue, total):
            continue
        exp = issue["explanation"]
        for key in ("meaning", "cause", "concept", "beginner_tip"):
            if key in clean:
                exp[key] = clean[key]
        if "fix_reason" in clean:
            issue["correction"]["reason"] = clean["fix_reason"]
        issue["explanation_source"] = "ai"
        count += 1
    return count


def diagnose_with_ai(code, reports, issues):
    """Try to improve explanations with the LLM. Never raises: falls back to rules."""
    info = {"used": False, "provider": None, "summary": None, "note": None}
    settings = config.llm_settings()
    if not ai_status()["configured"]:
        info["note"] = ("No AI key configured - showing built-in rule-based explanations. "
                        "Set LLM_PROVIDER, LLM_API_KEY and LLM_MODEL in .env to enable AI explanations.")
        return info
    if not issues:
        info["note"] = "No issues to explain, so the AI was not called."
        return info
    try:
        answer = parse_ai_json(_call_llm(build_prompt(code, reports, issues), settings))
        enriched = apply_ai_answer(answer, issues, code)
        info.update(used=enriched > 0, provider=settings["provider"])
        if isinstance(answer.get("summary"), str):
            info["summary"] = answer["summary"].strip()
        info["note"] = (f"AI explained {enriched} of {len(issues)} issue(s); the rest use rule-based text."
                        if enriched else "The AI reply could not be validated - rule-based text is shown.")
    except requests.RequestException as exc:
        info["note"] = f"AI request failed ({type(exc).__name__}) - showing rule-based explanations."
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        info["note"] = "The AI reply was not valid JSON - showing rule-based explanations."
    return info
