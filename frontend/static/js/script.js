// script.js - talks to the Flask backend and draws the results.
// All text from the server is inserted with textContent (never innerHTML), so it cannot inject HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const codeInput = $("codeInput"), tbInput = $("tbInput"), gutter = $("gutter");

/* ---------- small DOM helper ---------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/* ---------- editor line numbers ---------- */
function updateGutter() {
  const count = codeInput.value.split("\n").length;
  let text = "";
  for (let i = 1; i <= count; i++) text += i + "\n";
  gutter.textContent = text.trimEnd();
}
codeInput.addEventListener("input", updateGutter);
codeInput.addEventListener("scroll", () => { gutter.scrollTop = codeInput.scrollTop; });
codeInput.addEventListener("keydown", (e) => {          // Tab key inserts spaces
  if (e.key === "Tab") {
    e.preventDefault();
    const s = codeInput.selectionStart;
    codeInput.setRangeText("    ", s, codeInput.selectionEnd, "end");
    updateGutter();
  }
});

function showError(message) {
  const box = $("errorBox");
  box.textContent = message || "";
  box.hidden = !message;
}

/* ---------- tool status chips ---------- */
async function loadHealth() {
  try {
    const data = await (await fetch("/api/health")).json();
    const chips = $("toolChips");
    chips.textContent = "";
    for (const [name, info] of Object.entries(data.tools)) {
      const chip = el("span", "chip " + (info.available ? "on" : "off"),
        name + (info.available ? " ✔" : " not found"));
      chip.title = info.available ? (info.version || "") : "Install it - see README";
      chips.appendChild(chip);
    }
    const ai = el("span", "chip " + (data.ai.configured ? "on" : "off"),
      data.ai.configured ? "AI: " + data.ai.provider : "AI: rule-based mode");
    chips.appendChild(ai);
  } catch (err) { /* server not reachable - ignore */ }
}

/* ---------- samples ---------- */
let samples = [];
async function loadSamples() {
  try {
    samples = await (await fetch("/api/samples")).json();
    const select = $("sampleSelect");
    samples.forEach((s, i) => select.appendChild(new Option(s.title, String(i))));
  } catch (err) { /* ignore */ }
}
$("sampleSelect").addEventListener("change", (e) => {
  const sample = samples[Number(e.target.value)];
  if (!sample) { $("sampleHint").textContent = ""; return; }
  codeInput.value = sample.code;
  tbInput.value = sample.testbench_code || "";
  if (sample.testbench_code) document.querySelector(".tb-details").open = true;
  $("sampleHint").textContent = "Expected: " + sample.expected_behavior;
  updateGutter();
});

/* ---------- upload (validated by the server) ---------- */
$("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  showError("");
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Upload failed");
    codeInput.value = data.code;
    updateGutter();
  } catch (err) { showError(err.message); }
  e.target.value = "";
});

/* ---------- clear ---------- */
$("clearBtn").addEventListener("click", () => {
  codeInput.value = ""; tbInput.value = ""; $("sampleSelect").value = ""; $("sampleHint").textContent = "";
  $("results").replaceChildren(el("p", "placeholder", "Results will appear here after you click Analyze Code."));
  for (const id of ["stCompile", "stSynth", "stIssues", "stConf"]) { $(id).textContent = "—"; $(id).className = "stat-value"; }
  $("reportForm").hidden = true;
  showError(""); updateGutter();
});

/* ---------- analyze ---------- */
$("analyzeBtn").addEventListener("click", async () => {
  showError("");
  const btn = $("analyzeBtn");
  btn.disabled = true; btn.textContent = "Analyzing…";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: codeInput.value, testbench: tbInput.value, use_ai: $("useAi").checked }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Analysis failed");
    renderResults(data);
  } catch (err) { showError(err.message); }
  finally { btn.disabled = false; btn.textContent = "Analyze Code"; }
});

/* ---------- rendering ---------- */
const STATUS_TEXT = { pass: "Passed", fail: "Failed", not_run: "Not run", timeout: "Timed out", not_requested: "n/a", heuristic: "Heuristic only" };

function setStat(id, text, cls) { const n = $(id); n.textContent = text; n.className = "stat-value " + (cls || ""); }

function codeBlock(snippet) {
  const pre = el("pre", "code");
  snippet.code.split("\n").forEach((line, i) => {
    const num = snippet.start_line + i;
    const row = el("span", snippet.highlight_lines.includes(num) ? "hl" : "");
    row.appendChild(el("span", "ln", String(num).padStart(3) + "  "));
    row.appendChild(document.createTextNode(line));
    pre.appendChild(row);
  });
  return pre;
}

function renderIssue(issue) {
  const card = el("article", "card issue sev-" + issue.severity);
  const head = el("div", "issue-head");
  head.appendChild(el("span", "badge b-" + issue.severity, issue.severity));
  head.appendChild(el("span", "badge " + (issue.confidence === "confirmed" ? "b-confirmed" : "b-potential"),
    issue.confidence === "confirmed" ? "Confirmed by " + issue.source : "Potential (heuristic)"));
  head.appendChild(el("strong", "", issue.category));
  if (issue.lines.length) head.appendChild(el("span", "muted", "line " + issue.lines.join(", ")));
  card.appendChild(head);
  card.appendChild(el("p", "", issue.message));

  if (issue.snippet) { card.appendChild(el("h4", "", "Original code")); card.appendChild(codeBlock(issue.snippet)); }

  card.appendChild(el("h4", "", "Explanation" + (issue.explanation_source === "ai" ? " (AI)" : " (built-in rules)")));
  const ul = el("ul");
  [["What it means", "meaning"], ["Why it happened", "cause"], ["Responsible code", "responsible_code"],
   ["Hardware concept", "concept"], ["Beginner tip", "beginner_tip"]].forEach(([label, key]) => {
    const li = el("li"); li.appendChild(el("b", "", label + ": ")); li.appendChild(document.createTextNode(issue.explanation[key] || ""));
    ul.appendChild(li);
  });
  card.appendChild(ul);

  card.appendChild(el("h4", "", "Suggested correction"));
  card.appendChild(el("p", "", issue.correction.suggestion));
  if (issue.correction.corrected_code) {
    const pre = el("pre", "code fixed", issue.correction.corrected_code); card.appendChild(pre);
  } else { card.appendChild(el("p", "muted", "No automatic code template for this item.")); }
  const reason = el("p"); reason.appendChild(el("b", "", "Reason: ")); reason.appendChild(document.createTextNode(issue.correction.reason || ""));
  card.appendChild(reason);
  card.appendChild(el("p", "hint", issue.correction.note));
  return card;
}

function renderResults(data) {
  const s = data.status, c = data.counts;
  setStat("stCompile", STATUS_TEXT[s.compilation], s.compilation === "pass" ? "pass" : s.compilation === "fail" ? "fail" : "warn");
  setStat("stSynth", STATUS_TEXT[s.synthesis], s.synthesis === "pass" ? "pass" : s.synthesis === "fail" ? "fail" : "warn");
  setStat("stIssues", String(c.total), c.errors ? "fail" : c.total ? "warn" : "pass");
  setStat("stConf", c.confirmed + " / " + c.potential);

  const root = $("results");
  root.replaceChildren();

  const summary = el("div", "card");
  summary.appendChild(el("h3", "", "Summary"));
  summary.appendChild(el("p", "", data.summary));
  data.warnings.forEach((w) => summary.appendChild(el("p", "warn-line", "⚠ " + w)));
  const pills = el("div", "checks");
  data.checks.forEach((ch) => pills.appendChild(el("span", "check-pill " + ch.status, ch.name + ": " + (STATUS_TEXT[ch.status] || ch.status))));
  summary.appendChild(pills);
  const cats = Object.entries(c.by_category).map(([k, v]) => k + " (" + v + ")").join(" · ");
  summary.appendChild(el("p", "muted", "Errors: " + c.errors + " · Warnings: " + c.warnings + " · Info: " + c.info + (cats ? " · " + cats : "")));
  summary.appendChild(el("p", "hint", "AI: " + data.ai.note));
  summary.appendChild(el("p", "hint", data.disclaimer));
  root.appendChild(summary);

  if (!data.issues.length) root.appendChild(el("div", "card ok-box", "✔ No issues were detected by the checks that ran."));
  data.issues.forEach((i) => root.appendChild(renderIssue(i)));

  const raw = el("details", "card raw");
  raw.appendChild(el("summary", "", "Raw tool output"));
  [["iverilog", data.raw_reports.iverilog], ["yosys", data.raw_reports.yosys]].forEach(([name, rep]) => {
    raw.appendChild(el("h4", "", name));
    raw.appendChild(rep ? el("pre", "code", rep.command + "\n" + rep.stdout + rep.stderr) : el("p", "muted", "Tool not run."));
  });
  const sim = data.raw_reports.simulation;
  if (sim && sim.run) { raw.appendChild(el("h4", "", "testbench simulation")); raw.appendChild(el("pre", "code", sim.run.stdout + sim.run.stderr)); }
  root.appendChild(raw);

  $("reportCode").value = codeInput.value; $("reportTb").value = tbInput.value; $("reportAi").value = $("useAi").checked ? "1" : "0";
  $("reportForm").hidden = false;
}

updateGutter(); loadHealth(); loadSamples();
