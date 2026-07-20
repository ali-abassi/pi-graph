const boot = JSON.parse(document.getElementById("piw-boot").textContent);
const graph = boot.graph;
const byId = new Map(graph.nodes.map((node) => [node.id, node]));
const states = new Map(graph.nodes.map((node) => [node.id, "idle"]));
let selectedId = graph.nodes[0]?.id ?? null;
let session = null;
let eventCount = 0;
let polling = false;

const $ = (id) => document.getElementById(id);
const svg = $("graph");
const NS = "http://www.w3.org/2000/svg";

function svgEl(name, attrs = {}, text = "") {
  const el = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  if (text) el.textContent = text;
  return el;
}

function money(value) {
  if (!value) return "$0";
  return value < .01 ? `$${value.toFixed(4)}` : `$${value.toFixed(3)}`;
}

function kindLabel(kind) {
  return ({ command: "COMMAND", completion: "LLM", tooled: "TOOL", agent: "AGENT", qa: "QA" })[kind] ?? kind.toUpperCase();
}

function nodeBadges(node) {
  const badges = [];
  if (node.when) badges.push("route");
  if (node.judge) badges.push(`judge ≥${node.judge.score}`);
  if (node.gate) badges.push("gate");
  if (node.schema) badges.push("typed");
  if (node.retries) badges.push(`retry ×${node.retries}`);
  if (node.produces?.length) badges.push("artifact");
  return badges.slice(0, 3);
}

function renderGraph() {
  if (!graph.nodes.length) {
    $("emptyGraph").hidden = false;
    return;
  }
  const nodeW = 196;
  const nodeH = 92;
  const maxX = Math.max(...graph.nodes.map((node) => node.x)) + nodeW + 70;
  const maxY = Math.max(...graph.nodes.map((node) => node.y)) + nodeH + 70;
  svg.setAttribute("viewBox", `0 0 ${Math.max(maxX, 760)} ${Math.max(maxY, 330)}`);
  svg.setAttribute("width", Math.max(maxX, 760));
  svg.setAttribute("height", Math.max(maxY, 330));

  const defs = svgEl("defs");
  const marker = svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "5", markerHeight: "5", orient: "auto-start-reverse" });
  marker.append(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#5a5851" }));
  defs.append(marker);
  svg.append(defs);

  for (const edge of graph.edges) {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) continue;
    const x1 = source.x + nodeW;
    const y1 = source.y + nodeH / 2;
    const x2 = target.x;
    const y2 = target.y + nodeH / 2;
    const bend = Math.max(34, (x2 - x1) / 2);
    const path = svgEl("path", {
      d: `M${x1} ${y1} C${x1 + bend} ${y1},${x2 - bend} ${y2},${x2} ${y2}`,
      class: `edge${edge.implicit ? " implicit" : ""}${edge.conditional ? " conditional" : ""}`,
      "marker-end": "url(#arrow)",
    });
    svg.append(path);
    if (edge.conditional) {
      const label = (edge.label || "route").slice(0, 30);
      svg.append(svgEl("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 8, class: "edge-label", "text-anchor": "middle" }, label));
    }
  }

  for (const node of graph.nodes) {
    const group = svgEl("g", {
      class: `node-card ${node.determinism}${node.id === selectedId ? " selected" : ""}`,
      transform: `translate(${node.x} ${node.y})`, tabindex: "0", role: "button",
      "aria-label": `${node.id}, ${kindLabel(node.kind)} node`, "data-id": node.id, "data-status": "idle",
    });
    group.append(svgEl("rect", { class: "card", width: nodeW, height: nodeH, rx: "8" }));
    group.append(svgEl("line", { class: "rule", x1: "1", y1: "9", x2: "1", y2: nodeH - 9 }));
    group.append(svgEl("circle", { class: "state", cx: nodeW - 15, cy: 15, r: "4" }));
    group.append(svgEl("text", { class: "meta", x: "16", y: "22" }, kindLabel(node.kind)));
    group.append(svgEl("text", { class: "title", x: "16", y: "47" }, node.id.length > 21 ? `${node.id.slice(0, 19)}…` : node.id));
    const badges = nodeBadges(node).join("  ·  ") || node.determinism;
    group.append(svgEl("text", { class: "badge", x: "16", y: "72" }, badges));
    const select = () => selectNode(node.id);
    group.addEventListener("click", select);
    group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
    svg.append(group);
  }
}

function selectNode(id) {
  selectedId = id;
  svg.querySelectorAll(".node-card").forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
  const node = byId.get(id);
  if (!node) return;
  $("nodeKind").textContent = kindLabel(node.kind);
  $("nodeName").textContent = node.id;
  $("nodeStatus").textContent = states.get(id) || "idle";
  $("nodeSummary").textContent = node.when_text ? `Runs only when ${node.when_from}: ${node.when_text}.` : `${node.determinism} execution boundary with ${node.needs.length ? node.needs.length : "no"} upstream dependenc${node.needs.length === 1 ? "y" : "ies"}.`;
  const facts = [
    ["Runtime", kindLabel(node.kind)], ["Depends on", node.needs.join(", ") || "root"],
    ["Model", node.model || "none"], ["Reasoning", node.thinking || "n/a"],
    ["Tools", node.tools || (node.kind === "agent" ? "Pi defaults" : "none")],
    ["Gate", node.gate || "none"], ["Retries", String(node.retries ?? (node.judge ? node.judge.max_iters - 1 : 1))],
    ["Timeout", node.timeout ? `${node.timeout}s` : "default"],
  ];
  $("nodeFacts").replaceChildren(...facts.map(([term, value]) => {
    const row = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = term;
    const dd = document.createElement("dd"); dd.textContent = value;
    row.append(dt, dd); return row;
  }));
  $("nodeBody").textContent = node.body || "—";
}

function setState(mode, label) {
  const state = document.querySelector(".run-state");
  state.className = `run-state ${mode}`;
  $("statusText").textContent = label;
}

function setNodeState(id, state) {
  if (!byId.has(id)) return;
  states.set(id, state);
  const card = svg.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (card) card.dataset.status = state;
  if (id === selectedId) $("nodeStatus").textContent = state;
}

function showError(message) {
  const box = $("errorBox");
  box.textContent = message;
  box.hidden = !message;
}

function eventMessage(event) {
  switch (event.t) {
    case "run_start": return ["signal", `run started with ${event.workers} workers`, event.cache ? "cache on" : "cache off"];
    case "step_start": return ["signal", `${event.id} started`, event.model ? event.model.split("/").at(-1) : "code"];
    case "step_attempt": return ["", `${event.id} attempt ${event.attempt}/${event.max_attempts}`, ""];
    case "step_gate": return [event.passed ? "good" : "bad", `${event.id} gate ${event.passed ? "passed" : "failed"}`, "mechanical"];
    case "step_schema": return [event.passed ? "good" : "bad", `${event.id} output contract ${event.passed ? "passed" : "failed"}`, "typed"];
    case "step_judge": return [event.passed ? "good" : "bad", `${event.id} judge ${event.score}/${event.threshold}`, `attempt ${event.attempt}`];
    case "step_cached": return ["good", `${event.id} restored from cache`, `${event.seconds || 0}s`];
    case "step_skipped": return ["", `${event.id} skipped`, event.reason || "branch not taken"];
    case "step_end": return [event.passed ? "good" : "bad", `${event.id} ${event.passed ? "passed" : "failed"}`, `${event.seconds || 0}s · ${money(Number(event.cost || 0))}`];
    case "run_end": return [event.ok ? "good" : "bad", event.ok ? "run complete" : `run failed: ${(event.failed || []).join(", ")}`, "evidence sealed"];
    default: return ["", event.t.replaceAll("_", " "), event.id || ""];
  }
}

function applyEvent(event) {
  if (event.t === "step_start") setNodeState(event.id, "running");
  if (event.t === "step_cached") setNodeState(event.id, "cached");
  if (event.t === "step_skipped") setNodeState(event.id, "skipped");
  if (event.t === "step_end") setNodeState(event.id, event.passed ? "passed" : "failed");
  if (event.t === "run_start") $("runId").textContent = (event.run_dir || "RUNNING").split("/").at(-1);
  const [tone, message, meta] = eventMessage(event);
  const row = document.createElement("div"); row.className = `event ${tone}`;
  const time = document.createElement("time"); time.textContent = event.ts ? new Date(event.ts * 1000).toLocaleTimeString([], { hour12: false }) : "LIVE";
  const body = document.createElement("span"); body.textContent = message;
  const tail = document.createElement("span"); tail.className = "event-meta"; tail.textContent = meta;
  row.append(time, body, tail); $("eventFeed").append(row);
  $("eventFeed").scrollTop = $("eventFeed").scrollHeight;
}

function renderDetail(detail, output) {
  if (!detail) return;
  $("tokenCount").textContent = Number(detail.run.tokens || 0).toLocaleString();
  $("costCount").textContent = money(Number(detail.run.cost || 0));
  $("runOutput").textContent = output || "(no output artifact)";
  const costed = detail.steps.filter((step) => Number(step.cost) > 0).sort((a, b) => Number(b.cost) - Number(a.cost));
  const hot = costed[0];
  if (hot) {
    const el = $("hotspot");
    el.innerHTML = "";
    const title = document.createElement("b"); title.textContent = "OPTIMIZATION TARGET";
    const copy = document.createElement("span"); copy.textContent = `${hot.id} used ${money(Number(hot.cost))} and ${Number(hot.tokens || 0).toLocaleString()} tokens. Inspect its prompt, reasoning level, and cache key first.`;
    el.append(title, copy); el.hidden = false;
  }
}

function restoreLatest(latest) {
  if (!latest?.detail) return;
  const detail = latest.detail;
  for (const step of detail.steps || []) setNodeState(step.id, step.status || "idle");
  renderDetail(detail, latest.output);
  $("runId").textContent = detail.run.id;
  $("eventFeed").replaceChildren();
  for (const entry of detail.log || []) {
    const row = document.createElement("div"); row.className = "event muted";
    const time = document.createElement("time"); time.textContent = entry.at || "—";
    const body = document.createElement("span"); body.textContent = entry.text;
    const tail = document.createElement("span"); tail.className = "event-meta";
    row.append(time, body, tail); $("eventFeed").append(row);
  }
  setState(detail.run.ok ? "passed" : "failed", detail.run.ok ? "Latest run passed" : "Latest run failed");
  if (selectedId) selectNode(selectedId);
}

async function poll() {
  if (!session || polling) return;
  polling = true;
  try {
    const response = await fetch(`/api/status?session=${encodeURIComponent(session)}&after=${eventCount}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Status request failed");
    for (const event of payload.events) applyEvent(event);
    eventCount = payload.event_count;
    if (payload.done) {
      $("runButton").disabled = false;
      setState(payload.exit === 0 ? "passed" : "failed", payload.exit === 0 ? "Run passed" : "Run failed");
      renderDetail(payload.detail, payload.output);
      if (payload.error) showError(payload.error);
      session = null;
      return;
    }
    window.setTimeout(poll, 650);
  } catch (error) {
    $("runButton").disabled = false;
    setState("failed", "Connection failed");
    showError(String(error));
    session = null;
  } finally {
    polling = false;
  }
}

async function runWorkflow() {
  if (session) return;
  showError("");
  eventCount = 0;
  states.forEach((_, id) => setNodeState(id, "idle"));
  $("eventFeed").replaceChildren();
  $("runOutput").textContent = "Workflow is running…";
  $("hotspot").hidden = true;
  $("runButton").disabled = true;
  setState("running", "Workflow running");
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Piw-Token": boot.token },
      body: JSON.stringify({ content: $("workflowInput").value }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Run could not start");
    session = payload.session;
    window.setTimeout(poll, 100);
  } catch (error) {
    $("runButton").disabled = false;
    setState("failed", "Could not start");
    showError(String(error));
  }
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${name}Panel`));
}

$("workflowName").textContent = graph.workflow;
$("workflowPath").textContent = graph.path;
$("nodeCount").textContent = graph.nodes.length;
$("workerCount").textContent = graph.workers;
$("workflowInput").value = boot.default_input || "";
$("inputDescription").textContent = graph.input?.description || "This workflow accepts optional text input.";
const updateBytes = () => $("inputBytes").textContent = `${new Blob([$("workflowInput").value]).size.toLocaleString()} B`;
$("workflowInput").addEventListener("input", updateBytes); updateBytes();
$("runButton").addEventListener("click", runWorkflow);
document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runWorkflow(); });
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
$("copyBody").addEventListener("click", () => navigator.clipboard.writeText($("nodeBody").textContent));
$("copyOutput").addEventListener("click", () => navigator.clipboard.writeText($("runOutput").textContent));

renderGraph();
if (selectedId) selectNode(selectedId);
restoreLatest(boot.latest);
