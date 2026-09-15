"use strict";

const boot = JSON.parse(document.getElementById("piw-boot").textContent);
const NS = "http://www.w3.org/2000/svg";
const TRACE_PAGE = 60;
const TERMINAL_RUNS = new Set(["completed", "failed", "interrupted"]);
const STATUS = {
  idle: ["○", "idle"], initialized: ["○", "initialized"], pending: ["○", "pending"], not_run: ["○", "not run"],
  running: ["◉", "running"], passed: ["✓", "passed"], completed: ["✓", "completed"], cached: ["◆", "cached"],
  skipped: ["↷", "skipped"], failed: ["×", "failed"], interrupted: ["!", "interrupted"], legacy: ["◇", "legacy"],
  degraded: ["!", "degraded"], loading: ["…", "loading"], reconnecting: ["↻", "reconnecting"], unknown: ["?", "unknown"],
};

const $ = (id) => document.getElementById(id);
const app = {
  index: [], payload: null, graph: boot.graph, byId: new Map(), detailById: new Map(), states: new Map(),
  selectedRun: null, selectedNode: null, selectedTrace: null, traceShown: TRACE_PAGE,
  session: null, eventCount: 0, polling: false, indexTimer: null,
  refreshing: false, selectionGeneration: 0, zoom: 1, sessionTimer: null, stopped: false,
};

function svgEl(name, attrs = {}, text = "") {
  const element = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text) element.textContent = text;
  return element;
}

function statusParts(value) { return STATUS[value] || STATUS.unknown; }
function kindLabel(kind = "") { return ({ command: "COMMAND", completion: "LLM", tooled: "TOOL", agent: "AGENT", qa: "QA" })[kind] || kind.toUpperCase() || "NODE"; }
function compactId(value, n = 26) { return value.length > n ? `${value.slice(0, n - 1)}…` : value; }
function formatMoney(value) { const n = Number(value || 0); return n ? (n < .01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`) : "$0"; }
function formatTime(value) { if (!value) return "time unavailable"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function stringify(value) { try { return JSON.stringify(value, null, 2); } catch { return String(value); } }
function setText(id, value) { const element = $(id); if (element) element.textContent = value ?? "—"; }

function setGlobal(status, message) {
  const [glyph, label] = statusParts(status);
  $("globalState").dataset.status = status;
  setText("globalStateGlyph", glyph);
  setText("globalStateText", message || label);
}

function showError(message = "") {
  $("inspectorError").hidden = !message;
  setText("inspectorError", message);
}

function facts(target, rows) {
  const nodes = rows.map(([term, value]) => {
    const row = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = term;
    const dd = document.createElement("dd"); dd.textContent = value == null || value === "" ? "—" : String(value);
    row.append(dt, dd); return row;
  });
  target.replaceChildren(...nodes);
}

async function copyText(value, label) {
  try {
    await navigator.clipboard.writeText(value || "");
    setText("copyFeedback", `${label} copied`);
  } catch {
    setText("copyFeedback", `Could not copy ${label.toLowerCase()}`);
  }
}

function showTab(name, focus = false) {
  const tabs = [...document.querySelectorAll("[role=tab]")];
  for (const tab of tabs) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = $(`panel-${tab.dataset.tab}`);
    if (panel) panel.hidden = !selected;
    if (selected && focus) tab.focus();
  }
}

function runStatus(entry) { return entry.integrity === "degraded" ? "degraded" : entry.status || "unknown"; }

function renderRunList() {
  const query = $("runSearch").value.trim().toLowerCase();
  const rows = app.index.filter((run) => run.id.toLowerCase().includes(query));
  const buttons = rows.map((run) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "run-row"; button.dataset.runId = run.id;
    button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(run.id === app.selectedRun));
    const status = runStatus(run); const [glyph, label] = statusParts(status);
    const top = document.createElement("span"); top.className = "run-row-top";
    const id = document.createElement("strong"); id.textContent = run.id;
    const state = document.createElement("span"); state.className = "run-row-status"; state.dataset.status = status; state.textContent = `${glyph} ${label}`;
    top.append(id, state);
    const meta = document.createElement("span"); meta.className = "run-row-meta";
    meta.textContent = `${run.terminal}/${run.total} terminal · ${formatTime(run.updated_at)}`;
    button.append(top, meta);
    if (run.degraded_reason) { const reason = document.createElement("span"); reason.className = "run-row-reason"; reason.textContent = run.degraded_reason; button.append(reason); }
    button.addEventListener("click", () => selectRun(run.id));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      event.preventDefault(); const all = [...$("runList").querySelectorAll(".run-row")]; const index = all.indexOf(button);
      all[(index + (event.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]?.focus();
    });
    return button;
  });
  $("runList").replaceChildren(...buttons);
  $("runList").setAttribute("aria-busy", "false");
  setText("runCount", `${app.index.length} run${app.index.length === 1 ? "" : "s"}`);
  setText("mobileRunCount", String(app.index.length));
  $("runIndexState").hidden = rows.length > 0;
  setText("runIndexState", app.index.length ? "No runs match this search." : "No durable runs yet. Inspect the workflow or start a canonical run.");
}

async function refreshRuns({ keepSelection = true } = {}) {
  if (app.refreshing) return;
  app.refreshing = true;
  try {
    const response = await fetch("/api/runs", { cache: "no-store" });
    if (!response.ok) throw new Error(`run index returned ${response.status}`);
    const value = await response.json();
    app.index = Array.isArray(value.runs) ? value.runs : [];
    renderRunList();
    const urlRun = new URL(location.href).searchParams.get("run");
    const wanted = keepSelection && app.selectedRun ? app.selectedRun : urlRun || value.selected;
    if (wanted && app.index.some((item) => item.id === wanted)) {
      if (wanted !== app.selectedRun || !app.payload) await selectRun(wanted, false);
    } else if (!app.selectedRun) {
      renderEmptyHistory();
    }
    setGlobal(app.index.some((item) => item.status === "running") ? "running" : (app.selectedRun ? runStatus(app.index.find((item) => item.id === app.selectedRun) || {}) : "idle"), app.index.length ? `${app.index.length} durable run${app.index.length === 1 ? "" : "s"}` : "Ready to run");
  } catch (error) {
    setGlobal("reconnecting", "Durable history unavailable — retrying");
    $("runIndexState").hidden = false; setText("runIndexState", error.message);
  } finally {
    app.refreshing = false;
  }
}

function renderEmptyHistory() {
  app.payload = null; app.selectedRun = null; app.graph = boot.graph; app.detailById.clear(); app.states.clear();
  setText("contextRunId", "No run selected"); setText("contextStatus", "empty"); setText("contextMessage", "Inspect the current workflow graph or start a canonical run. No evidence is fabricated.");
  for (const id of ["contextProgress", "contextIntegrity", "contextTrace", "contextResumes", "contextWorkflowHash", "contextInputHash"]) setText(id, "—");
  $("resumeBox").hidden = true; setText("mobileRunId", "No run selected");
  renderGraph(); renderTrace(); selectNode(app.graph?.nodes?.[0]?.id || null); requestAnimationFrame(() => revealNode(app.selectedNode));
  $("graphState").hidden = Boolean(app.graph?.nodes?.length);
  if (!$("graphState").hidden) $("graphState").replaceChildren(Object.assign(document.createElement("strong"), { textContent: "No graph available" }));
}

async function selectRun(runId, updateUrl = true) {
  const generation = ++app.selectionGeneration;
  app.selectedRun = runId; app.selectedNode = null; app.selectedTrace = null; app.traceShown = TRACE_PAGE;
  renderRunList(); $("runContext").setAttribute("aria-busy", "true"); setGlobal("loading", `Opening ${runId}`); showError("");
  try {
    const response = await fetch(`/api/run?id=${encodeURIComponent(runId)}`, { cache: "no-store" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `run detail returned ${response.status}`);
    if (generation !== app.selectionGeneration || app.selectedRun !== runId) return;
    app.payload = value; app.graph = value.graph || { workflow: "Unavailable", nodes: [], edges: [] };
    app.detailById = new Map((value.detail?.steps || []).map((step) => [step.id, step]));
    app.states = new Map((value.detail?.steps || []).map((step) => [step.id, step.status || "not_run"]));
    if (updateUrl) { const url = new URL(location.href); url.searchParams.set("run", runId); history.replaceState({}, "", url); }
    renderContext(); renderGraph(); renderTrace(); renderInput();
    selectNode(app.graph.nodes?.[0]?.id || null); requestAnimationFrame(() => revealNode(app.selectedNode));
    if (value.trace?.length) selectTrace(value.trace[value.trace.length - 1], false);
    setGlobal(runStatus(value.run), `${runId} · ${statusParts(runStatus(value.run))[1]}`);
  } catch (error) {
    if (generation !== app.selectionGeneration) return;
    showError(error.message); setGlobal("degraded", `Could not open ${runId}`);
    setText("contextMessage", "The selected evidence could not be read safely. Refresh or inspect the run directory in the terminal.");
  } finally {
    if (generation === app.selectionGeneration) { $("runContext").setAttribute("aria-busy", "false"); closeRunRail(); }
  }
}

function renderContext() {
  const run = app.payload.run; const status = runStatus(run); const [glyph, label] = statusParts(status);
  setText("contextStatusGlyph", glyph); setText("contextRunId", run.id); setText("mobileRunId", run.id); setText("contextStatus", label);
  setText("contextProgress", `${run.terminal}/${run.total} terminal`); setText("contextIntegrity", run.integrity); setText("contextTrace", `${run.trace_seq} events`); setText("contextResumes", run.resume_count);
  setText("contextWorkflowHash", run.workflow?.sha256 ? compactId(run.workflow.sha256, 17) : "unavailable");
  setText("contextInputHash", run.input?.sha256 ? compactId(run.input.sha256, 17) : (run.input === null ? "none" : "unavailable"));
  if (run.integrity === "degraded") setText("contextMessage", `Evidence is degraded: ${run.degraded_reason || "bundle validation failed"}`);
  else if (run.legacy) setText("contextMessage", "Legacy evidence is readable, but it has no durable trace boundary or crash-resume guarantee.");
  else setText("contextMessage", `Frozen workflow snapshot · updated ${formatTime(run.updated_at)}${run.drift?.length ? ` · ${run.drift.length} audited source drift event(s)` : ""}`);
  $("resumeBox").hidden = !app.payload.resume?.eligible;
  setText("resumeReason", app.payload.resume?.reason || ""); setText("resumeCommand", app.payload.resume?.command || "");
}

function nodeBadges(node) {
  const badges = [];
  if (node.when) badges.push("route"); if (node.judge) badges.push(`judge ≥${node.judge.score}`); if (node.gate) badges.push("gate");
  if (node.schema) badges.push("typed"); if (node.retries) badges.push(`retry ×${node.retries}`); if (node.produces?.length) badges.push("artifact");
  return badges.slice(0, 3).join(" · ") || node.determinism || "fixed";
}

function renderGraph() {
  const svg = $("graph"); svg.replaceChildren();
  const graph = app.graph || { nodes: [], edges: [] }; app.byId = new Map((graph.nodes || []).map((node) => [node.id, node]));
  setText("graphTitle", graph.workflow || "Workflow");
  if (!graph.nodes?.length) { $("graphState").hidden = false; setText("graphState", app.payload?.run?.degraded_reason || "Frozen graph unavailable."); return; }
  $("graphState").hidden = true;
  const nodeW = 220, nodeH = 108, boundaryPad = 700;
  if (!graph._boundaryPadded) { graph.nodes.forEach((node) => { node.x = (node.x || 0) + boundaryPad; }); graph._boundaryPadded = true; }
  const maxX = Math.max(...graph.nodes.map((node) => node.x || 0)) + nodeW + boundaryPad;
  const maxY = Math.max(...graph.nodes.map((node) => node.y || 0)) + nodeH + 70;
  const width = Math.max(maxX, 760), height = Math.max(maxY, 360);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.dataset.baseWidth = String(width); svg.dataset.baseHeight = String(height);
  svg.setAttribute("width", width * app.zoom); svg.setAttribute("height", height * app.zoom);
  const defs = svgEl("defs"); const marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto" });
  marker.append(svgEl("path", { d: "M0 0L10 5L0 10z", fill: "#5a5851" })); defs.append(marker); svg.append(defs);
  for (const edge of graph.edges || []) {
    const source = app.byId.get(edge.source), target = app.byId.get(edge.target); if (!source || !target) continue;
    const x1 = source.x + nodeW, y1 = source.y + nodeH / 2, x2 = target.x, y2 = target.y + nodeH / 2, bend = Math.max(34, (x2 - x1) / 2);
    svg.append(svgEl("path", { d: `M${x1} ${y1}C${x1 + bend} ${y1},${x2 - bend} ${y2},${x2} ${y2}`, class: `edge${edge.implicit ? " implicit" : ""}${edge.conditional ? " conditional" : ""}`, "marker-end": "url(#arrow)" }));
  }
  for (const node of graph.nodes) {
    const status = app.states.get(node.id) || (app.payload ? "not_run" : "idle"); const [glyph, label] = statusParts(status);
    const group = svgEl("g", { class: `node-card ${node.determinism || "fixed"}${node.id === app.selectedNode ? " selected" : ""}`, transform: `translate(${node.x || 0} ${node.y || 0})`, tabindex: node.id === app.selectedNode || (!app.selectedNode && node === graph.nodes[0]) ? 0 : -1, role: "button", "aria-label": `${node.id}, ${kindLabel(node.kind)}, ${label}`, "data-id": node.id, "data-status": status });
    group.append(svgEl("rect", { class: "card", width: nodeW, height: nodeH, rx: 8 }));
    group.append(svgEl("line", { class: "rule", x1: 1, y1: 9, x2: 1, y2: nodeH - 9 }));
    group.append(svgEl("text", { class: "state-glyph", x: nodeW - 20, y: 22, "text-anchor": "middle" }, glyph));
    group.append(svgEl("text", { class: "meta", x: 16, y: 22 }, kindLabel(node.kind)));
    group.append(svgEl("text", { class: "title", x: 16, y: 49 }, compactId(node.id, 25)));
    group.append(svgEl("text", { class: "badge", x: 16, y: 75 }, nodeBadges(node)));
    group.append(svgEl("text", { class: "node-state-text", x: 16, y: 96 }, label.toUpperCase()));
    group.addEventListener("click", () => selectNode(node.id));
    group.addEventListener("keydown", (event) => nodeKey(event, node));
    svg.append(group);
  }
  applyNodeSearch();
}

function nodeKey(event, node) {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(node.id); return; }
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  const candidates = [...app.byId.values()].filter((other) => other.id !== node.id).map((other) => {
    const dx = (other.x || 0) - (node.x || 0), dy = (other.y || 0) - (node.y || 0);
    const valid = event.key === "ArrowRight" ? dx > 0 : event.key === "ArrowLeft" ? dx < 0 : event.key === "ArrowDown" ? dy > 0 : dy < 0;
    return { other, score: valid ? Math.abs(dx) + Math.abs(dy) * 1.4 : Infinity };
  }).sort((a, b) => a.score - b.score);
  if (candidates[0]?.score < Infinity) { selectNode(candidates[0].other.id); $("graph").querySelector(`[data-id="${CSS.escape(candidates[0].other.id)}"]`)?.focus(); }
}

function setGraphZoom(value) {
  app.zoom = Math.max(.6, Math.min(1.8, value));
  const svg = $("graph"); const width = Number(svg.dataset.baseWidth || 0), height = Number(svg.dataset.baseHeight || 0);
  $("graphViewport").classList.remove("fit");
  if (width && height) { svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("width", width * app.zoom); svg.setAttribute("height", height * app.zoom); }
}

function fitSmallGraph() {
  if (!app.byId.size || app.byId.size > 6) { $("graphViewport").classList.remove("fit"); return false; }
  if (app.zoom !== 1) return false;
  const svg = $("graph"), box = svg.getBBox(), padding = 24;
  svg.setAttribute("viewBox", `${box.x - padding} ${box.y - padding} ${box.width + padding * 2} ${box.height + padding * 2}`);
  $("graphViewport").classList.add("fit");
  return true;
}

function revealNode(id) {
  const node = app.byId.get(id); if (!node) return;
  if (fitSmallGraph()) return;
  const viewport = $("graphViewport");
  viewport.scrollTo({ left: Math.max(0, (node.x || 0) * app.zoom - viewport.clientWidth / 2 + 110 * app.zoom), top: Math.max(0, (node.y || 0) * app.zoom - viewport.clientHeight / 2 + 54 * app.zoom), behavior: "auto" });
}

function applyNodeSearch(reveal = false) {
  const query = $("nodeSearch").value.trim().toLowerCase(); let first = null;
  $("graph").querySelectorAll(".node-card").forEach((node) => { const match = !query || node.dataset.id.toLowerCase().includes(query); node.classList.toggle("filtered", !match); if (query && match && !first) first = node.dataset.id; });
  if (reveal && first) { setGraphZoom(Math.max(app.zoom, .85)); selectNode(first); requestAnimationFrame(() => revealNode(first)); }
}

function selectNode(id) {
  if (!id) return;
  app.selectedNode = id;
  $("graph").querySelectorAll(".node-card").forEach((node) => { const selected = node.dataset.id === id; node.classList.toggle("selected", selected); node.tabIndex = selected ? 0 : -1; });
  const node = app.byId.get(id), detail = app.detailById.get(id) || {}; if (!node) return;
  const status = app.states.get(id) || "not_run"; setText("nodeKind", kindLabel(node.kind)); setText("nodeName", id); setText("nodeStatus", `${statusParts(status)[0]} ${statusParts(status)[1]}`);
  setText("nodeSummary", node.when_text ? `Runs only when ${node.when_from}: ${node.when_text}.` : `${node.determinism || "fixed"} execution boundary with ${node.needs?.length || "no"} upstream ${node.needs?.length === 1 ? "dependency" : "dependencies"}.`);
  facts($("nodeFacts"), [["Runtime", kindLabel(node.kind)], ["Depends on", node.needs?.join(", ") || "root"], ["Model", node.model || "none"], ["Reasoning", node.thinking || "n/a"], ["Tools", node.tools || "none"], ["Gate", node.gate || "none"], ["Produces", node.produces?.join(", ") || "none"], ["Retries", node.retries ?? (node.judge ? node.judge.max_iters - 1 : 1)], ["Timeout", node.timeout ? `${node.timeout}s` : "default"]]);
  setText("nodeDeclaration", node.body || "—"); setText("evidenceNodeName", id);
  facts($("evidenceFacts"), [["Status", status], ["Reason", detail.reason || "none"], ["Failure", detail.failure || "none"], ["Failure kind", detail.failure_kind || "none"], ["Attempts", detail.attempts ?? "—"], ["Cached", detail.cached ? "yes" : "no"], ["Duration", detail.seconds == null ? "—" : `${detail.seconds}s`], ["Tokens", detail.tokens ?? 0], ["Cost", formatMoney(detail.cost)]]);
  setText("nodeOutput", detail.output || "No recorded output."); setText("nodeStderr", detail.stderr || "No recorded stderr."); setText("outputBound", detail.output_truncated || detail.output_truncated_by_studio ? "bounded" : "complete");
  const attempts = (detail.judge_attempts || []).map((attempt) => { const block = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = `Attempt ${attempt.n} · ${attempt.rejected ? "rejected" : "recorded"}`; const pre = document.createElement("pre"); pre.textContent = [attempt.output, attempt.judge].filter(Boolean).join("\n\n"); block.append(summary, pre); return block; });
  $("attemptList").replaceChildren(...(attempts.length ? attempts : [Object.assign(document.createElement("span"), { textContent: "No attempt evidence." })]));
  document.querySelectorAll(".trace-row").forEach((row) => row.classList.toggle("related", row.dataset.stepId === id));
}

function filteredTrace() {
  const query = $("traceSearch").value.trim().toLowerCase(); const trace = app.payload?.trace || [];
  return query ? trace.filter((event) => stringify(event).toLowerCase().includes(query)) : trace;
}

function renderTrace() {
  const trace = filteredTrace(), visible = trace.slice(0, app.traceShown); const rows = visible.map((event) => {
    const button = document.createElement("button"); button.type = "button"; button.className = "trace-row"; button.setAttribute("role", "option"); button.dataset.stepId = event.step_id || ""; button.setAttribute("aria-selected", String(app.selectedTrace?.seq === event.seq));
    const seq = document.createElement("span"); seq.className = "trace-seq"; seq.textContent = `#${event.seq}`;
    const type = document.createElement("strong"); type.textContent = event.type || event.t || "event";
    const step = document.createElement("span"); step.className = "trace-step"; step.textContent = event.step_id || "run";
    const time = document.createElement("time"); time.textContent = formatTime(event.timestamp || event.at);
    button.append(seq, type, step, time); button.addEventListener("click", () => selectTrace(event)); return button;
  });
  $("traceList").replaceChildren(...(rows.length ? rows : [Object.assign(document.createElement("div"), { className: "inline-state", textContent: app.payload ? "No committed events match." : "Select a durable run to inspect committed events." })]));
  $("traceList").setAttribute("aria-busy", "false");
  const total = app.payload?.trace_total || 0; setText("traceBound", `${total} committed`); setText("traceProgress", app.payload?.trace_limited ? `Showing ${Math.min(visible.length, app.payload.trace.length)} of ${total}; response is safely bounded.` : `Showing ${visible.length} of ${trace.length}`);
  $("loadMoreTrace").hidden = visible.length >= trace.length;
}

function selectTrace(event, openPanel = true) {
  app.selectedTrace = event; setText("traceEventTitle", `${event.type || event.t || "event"} #${event.seq}`);
  facts($("traceFacts"), [["Sequence", event.seq], ["Step", event.step_id || "run"], ["Committed at", formatTime(event.timestamp || event.at)], ["Schema", event.schema || "transient"]]);
  setText("tracePayload", stringify(event.payload || event));
  document.querySelectorAll(".trace-row").forEach((row) => row.setAttribute("aria-selected", String(row.querySelector(".trace-seq")?.textContent === `#${event.seq}`)));
  if (event.step_id && app.byId.has(event.step_id)) selectNode(event.step_id);
  if (openPanel) showTab("trace");
}

function renderInput() {
  const run = app.payload?.run || {}; facts($("inputFacts"), [["Run", run.id], ["Workflow digest", run.workflow?.sha256 || "unavailable"], ["Workflow bytes", run.workflow?.bytes ?? "—"], ["Input digest", run.input?.sha256 || (run.input === null ? "none" : "unavailable")], ["Input bytes", run.input?.bytes ?? 0], ["Integrity", run.integrity]]);
  setText("runInput", app.payload?.input_text || (run.input === null ? "No immutable input was supplied." : "Input unavailable."));
}

function isMobileRail() { return matchMedia("(max-width: 900px)").matches; }
function syncRunRail() {
  const rail = $("runRail"), mobile = isMobileRail(), open = rail.classList.contains("open");
  rail.inert = mobile && !open;
  rail.setAttribute("aria-hidden", String(mobile && !open));
  if (!mobile) { rail.classList.remove("open"); rail.inert = false; rail.setAttribute("aria-hidden", "false"); $("railScrim").hidden = true; }
}
function openRunRail() { const rail = $("runRail"); rail.classList.add("open"); rail.inert = false; rail.setAttribute("aria-hidden", "false"); $("railScrim").hidden = false; $("openRunsButton").setAttribute("aria-expanded", "true"); requestAnimationFrame(() => $("closeRunsButton").focus()); }
function closeRunRail(restoreFocus = false) { const rail = $("runRail"); rail.classList.remove("open"); $("railScrim").hidden = true; $("openRunsButton").setAttribute("aria-expanded", "false"); syncRunRail(); if (restoreFocus && isMobileRail()) $("openRunsButton").focus(); }

function validRunInput() {
  if (!boot.graph.input?.required || $("workflowInput").value.trim()) return true;
  setText("launchState", "Enter an input before starting this workflow.");
  $("workflowInput").focus();
  return false;
}

async function requestRun() {
  const response = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json", "X-Piw-Token": boot.token }, body: JSON.stringify({ content: $("workflowInput").value }) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Could not start run");
  return value;
}

async function startRun() {
  if (app.polling) return;
  if (!validRunInput()) return;
  app.stopped = false; clearTimeout(app.sessionTimer); app.sessionTimer = null;
  app.polling = true; $("launchButton").disabled = true; $("runButton").disabled = true; setText("launchState", "Starting the canonical runner…"); setGlobal("running", "Starting canonical run");
  try {
    const value = await requestRun();
    app.session = value.session; app.eventCount = 0; setText("launchState", `Run session ${value.session} started. Waiting for durable evidence…`); pollSession();
  } catch (error) {
    app.polling = false; $("launchButton").disabled = false; $("runButton").disabled = false; setText("launchState", error.message); setGlobal("failed", "Run could not start");
  }
}

function scheduleSessionPoll(delay) {
  clearTimeout(app.sessionTimer);
  if (!app.stopped) app.sessionTimer = setTimeout(pollSession, delay);
}

async function pollSession() {
  if (!app.session || app.stopped) return;
  try {
    const response = await fetch(`/api/status?session=${encodeURIComponent(app.session)}&after=${app.eventCount}`, { cache: "no-store" }); const value = await response.json();
    if (!response.ok) throw new Error(value.error || "Live session unavailable"); app.eventCount = value.event_count || app.eventCount;
    setText("launchState", value.done ? (value.exit === 0 ? "Runner finished. Opening durable evidence…" : value.error || "Runner failed; opening recorded evidence…") : `${app.eventCount} transient event${app.eventCount === 1 ? "" : "s"}; durable history is authoritative.`);
    if (!value.done) { scheduleSessionPoll(650); return; }
    app.sessionTimer = null; app.polling = false; $("launchButton").disabled = false; $("runButton").disabled = false;
    await refreshRuns({ keepSelection: false });
    if (value.detail?.run?.id && app.index.some((item) => item.id === value.detail.run.id)) await selectRun(value.detail.run.id);
  } catch (error) {
    if (app.stopped) return;
    setText("launchState", `${error.message}. Reconnecting through durable history…`); await refreshRuns(); scheduleSessionPoll(1200);
  }
}

function bind() {
  $("workflowInput").value = boot.default_input || ""; setText("inputBytes", `${new Blob([$("workflowInput").value]).size} B`);
  $("workflowInput").addEventListener("input", () => setText("inputBytes", `${new Blob([$("workflowInput").value]).size} B`));
  $("runButton").addEventListener("click", () => showTab("new", true)); $("launchButton").addEventListener("click", startRun);
  $("refreshButton").addEventListener("click", () => refreshRuns()); $("runSearch").addEventListener("input", renderRunList); $("nodeSearch").addEventListener("input", () => applyNodeSearch(true));
  $("traceSearch").addEventListener("input", () => { app.traceShown = TRACE_PAGE; renderTrace(); }); $("loadMoreTrace").addEventListener("click", () => { app.traceShown += TRACE_PAGE; renderTrace(); });
  $("zoomOutButton").addEventListener("click", () => setGraphZoom(app.zoom - .15)); $("zoomInButton").addEventListener("click", () => setGraphZoom(app.zoom + .15));
  $("fitGraphButton").addEventListener("click", () => { if ((app.graph?.nodes?.length || 0) <= 50) $("graphViewport").classList.add("fit"); else { setGraphZoom(.85); revealNode(app.selectedNode || app.graph.nodes[0]?.id); setText("copyFeedback", "Large graph fitted to the selected node; search or arrow keys move through the DAG"); } });
  $("resetGraphButton").addEventListener("click", () => { setGraphZoom(1); $("graphViewport").scrollTo({ left: 0, top: 0, behavior: "smooth" }); });
  $("openRunsButton").addEventListener("click", openRunRail); $("closeRunsButton").addEventListener("click", () => closeRunRail(true)); $("railScrim").addEventListener("click", () => closeRunRail(true));
  $("copyResume").addEventListener("click", () => copyText($("resumeCommand").textContent, "Resume command")); $("copyDeclaration").addEventListener("click", () => copyText($("nodeDeclaration").textContent, "Declaration"));
  $("copyOutput").addEventListener("click", () => copyText($("nodeOutput").textContent, "Output")); $("copyTrace").addEventListener("click", () => copyText($("tracePayload").textContent, "Trace event")); $("copyInput").addEventListener("click", () => copyText($("runInput").textContent, "Input"));
  const tabs = [...document.querySelectorAll("[role=tab]")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const index = tabs.indexOf(tab); const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length; showTab(tabs[next].dataset.tab, true); });
  }
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && $("runRail").classList.contains("open")) { event.preventDefault(); closeRunRail(true); } else if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); showTab("new"); startRun(); } });
  addEventListener("resize", syncRunRail);
  addEventListener("pagehide", () => { app.stopped = true; app.polling = false; clearInterval(app.indexTimer); clearTimeout(app.sessionTimer); app.sessionTimer = null; app.selectionGeneration += 1; });
  syncRunRail();
}

bind();
renderEmptyHistory();
refreshRuns({ keepSelection: false });
app.indexTimer = setInterval(() => refreshRuns(), 3000);
