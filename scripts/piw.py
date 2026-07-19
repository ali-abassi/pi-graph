"""piw — drive deterministic Pi Workflows from the terminal.

Built for agents as much as humans: every command prints compact, greppable
lines rather than verbose JSON, and every command takes --json when a machine
wants structure. Exit codes are meaningful, so `pygraph run x && ...` works.

The full loop an agent needs:

    piw ls                      what workflows exist
    piw graph <id>              the DAG, as text
    piw path <id>               the steps.yaml path (then edit it directly)
    piw validate <id>           check an edit before paying for a run
    piw run <id> --watch        run it, stream step results, exit non-zero on failure
    piw run <id> --node label   re-run one step fresh, upstream from cache
    piw runs <id>               run history
    piw show <id> <step>        what a step actually produced
    piw stats <id>              counters: pass rate, cost, cache hits

Runs go through the optional Loops daemon when it is up and resolves the same
workflow path, so the canvas lights up live. The standalone direct runner is
the fallback and has no Loops dependency.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

import control as loopd
import graph as pygraph

DAEMON = f"http://127.0.0.1:{loopd.DEFAULT_PORT}"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "workflow.schema.json"
ACTION_DIR = Path(__file__).resolve().parent.parent / "actions"
ACTION_REF_RE = re.compile(r"\{step\.([A-Za-z0-9_-]+)\}")

# Compact kind labels; the canvas shows icons, the terminal shows four chars.
KIND_LABEL = {
    "command": "cmd",
    "completion": "llm",
    "tooled": "llm+",
    "agent": "loop",
    "qa": "qa",
    "unknown": "?",
}

STATUS_MARK = {True: "ok", False: "FAIL"}


def out(line: str = "") -> None:
    print(line, flush=True)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def resolve(identifier: str) -> dict[str, Any] | None:
    """Find a workflow by exact id, or by unique substring of id or name."""
    candidate = Path(identifier).expanduser()
    if candidate.exists():
        path = (candidate / "steps.yaml" if candidate.is_dir() else candidate).resolve()
        if path.name != "steps.yaml" or not path.is_file():
            return None
        known = next((item for item in loopd.discover_workflows() if item["path"] == str(path)), None)
        if known:
            return known
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            spec = {}
        runs_dir = path.parent / "runs"
        runs = [item for item in runs_dir.iterdir() if item.is_dir()] if runs_dir.is_dir() else []
        return {
            "id": loopd.slugify(path.parent.name),
            "name": spec.get("workflow") or path.parent.name,
            "path": str(path),
            "cwd": str(path.parent),
            "runs_dir": str(runs_dir) if runs_dir.is_dir() else None,
            "run_count": len(runs),
            "last_run": None,
            "model": spec.get("model"),
        }
    workflows = loopd.discover_workflows()
    for workflow in workflows:
        if workflow["id"] == identifier:
            return workflow
    matches = [
        w for w in workflows
        if identifier in w["id"] or identifier == w["name"] or identifier in w["name"]
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(sorted(w["id"] for w in matches))
        raise SystemExit(fail(f"'{identifier}' is ambiguous: {names}"))
    return None


def need(identifier: str) -> dict[str, Any]:
    workflow = resolve(identifier)
    if not workflow:
        raise SystemExit(fail(f"no workflow matching '{identifier}' (try: piw ls)"))
    return workflow


def graph_for(workflow: dict[str, Any]) -> dict[str, Any]:
    try:
        return pygraph.parse_steps(Path(workflow["path"]))
    except pygraph.WorkflowParseError as error:
        raise SystemExit(fail(f"{workflow['id']}: {error}"))


def latest_run(workflow: dict[str, Any]) -> dict[str, Any] | None:
    runs = loopd.list_workflow_runs(workflow["id"], limit=1, runs_dir=workflow.get("runs_dir"))
    return runs[0] if runs else None


def read_ledger(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not run:
        return []
    ledger = run.get("ledger")
    if isinstance(ledger, list):
        return ledger
    return []


def money(value: float) -> str:
    if not value:
        return "$0"
    return f"${value:.4f}" if value < 0.01 else f"${value:.3f}"


def secs(value: float | None) -> str:
    if not value:
        return "0s"
    return f"{value:.1f}s" if value < 60 else f"{int(value // 60)}m{int(value % 60)}s"


# --------------------------------------------------------------------------- ls


def cmd_ls(args) -> int:
    workflows = loopd.discover_workflows()
    if args.json:
        out(json.dumps(workflows, indent=None, separators=(",", ":")))
        return 0
    if not workflows:
        out("no workflows found")
        return 0
    rows = []
    for workflow in workflows:
        try:
            graph = pygraph.parse_steps(Path(workflow["path"]))
            steps = str(len(graph["nodes"]))
        except (pygraph.WorkflowParseError, OSError):
            steps = "ERR"
        rows.append((workflow["id"], workflow["name"], steps, str(workflow["run_count"])))
    width = max(len(r[0]) for r in rows)
    out(f"{'ID'.ljust(width)}  {'NAME'.ljust(18)} STEPS RUNS")
    for identifier, name, steps, runs in rows:
        out(f"{identifier.ljust(width)}  {name[:18].ljust(18)} {steps.rjust(5)} {runs.rjust(4)}")
    return 0


# ------------------------------------------------------------------------ schema


def cmd_schema(args) -> int:
    """Expose the complete authoring contract to humans, agents, and editors."""
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return fail(f"workflow schema unavailable: {error}")
    if args.json:
        out(json.dumps(schema, separators=(",", ":")))
        return 0

    metadata = schema["x-pi-workflows"]
    out("Pi Workflows v1 · canonical file: steps.yaml")
    out(f"JSON Schema: {SCHEMA_PATH}")
    out()
    out("NODES")
    for node in metadata["nodeKinds"]:
        model = "model" if node["modelCall"] else "code"
        out(f"  {node['kind'].ljust(8)} {model.ljust(5)} · {node['selector']} · {node['purpose']}")
    out()
    out("GRAPH CAPABILITIES")
    for item in metadata["graphCapabilities"]:
        out(f"  {item['capability'].ljust(16)} {item['owner'].ljust(6)} · {item['selector']} · {item['purpose']}")
    out()
    out("PROMPT INPUTS")
    for name, meaning in metadata["runtimeInputs"]["prompt"].items():
        out(f"  {name.ljust(12)} {meaning}")
    out()
    out("COMMAND + GATE INPUTS")
    for name, meaning in metadata["runtimeInputs"]["commandAndGate"].items():
        out(f"  {name.ljust(20)} {meaning}")
    out()
    out("Judge: {out}, {run} · QA: {artifacts}, {run}")
    out("Full reference: docs/workflow-format.md · machine form: piw schema --json")
    return 0


# ---------------------------------------------------------------------- actions


def _action_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for path in sorted(ACTION_DIR.glob("*.yaml")):
        try:
            action = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            raise RuntimeError(f"action template {path.name} is unreadable: {error}") from error
        required = {
            "version", "id", "title", "description", "category", "inputs", "outputs", "failure",
            "effect", "retry_safe", "idempotency", "cost", "steps",
        }
        missing = required - set(action) if isinstance(action, dict) else required
        if missing or action.get("version") != 1 or not isinstance(action.get("steps"), list) or not action["steps"]:
            detail = f"missing {', '.join(sorted(missing))}" if missing else "invalid version or steps"
            raise RuntimeError(f"action template {path.name} is malformed: {detail}")
        identifier = str(action["id"])
        if path.stem != identifier or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identifier):
            raise RuntimeError(f"action template id must match its filename: {path.name}")
        if identifier in catalog:
            raise RuntimeError(f"duplicate action template: {identifier}")
        action["path"] = str(path)
        catalog[identifier] = action
    if not catalog:
        raise RuntimeError(f"no action templates found in {ACTION_DIR}")
    return catalog


def _source_reference(needs: list[str]) -> str:
    if not needs:
        return "{input}"
    if len(needs) == 1:
        return f"{{step.{needs[0]}}}"
    return "\n\n".join(f'<source step="{step}">\n{{step.{step}}}\n</source>' for step in needs)


def instantiate_action(action: dict[str, Any], prefix: str, external_needs: list[str]) -> list[dict[str, Any]]:
    """Expand a reusable action into ordinary v1 nodes with no runtime indirection."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", prefix):
        raise RuntimeError("--id must start with a letter or number and contain only letters, numbers, _ or -")
    raw_steps = copy.deepcopy(action["steps"])
    local_ids = [str(step.get("id") or "") for step in raw_steps]
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid) for sid in local_ids):
        raise RuntimeError(f"action {action['id']} has an invalid local step id")
    if len(set(local_ids)) != len(local_ids):
        raise RuntimeError(f"action {action['id']} has duplicate local step ids")
    id_map = {
        sid: prefix if len(local_ids) == 1 else f"{prefix}-{sid}"
        for sid in local_ids
    }
    source = _source_reference(external_needs)

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            text_value = value.replace("{{source}}", source).replace("{{prefix}}", prefix)
            text_value = ACTION_REF_RE.sub(
                lambda match: f"{{step.{id_map.get(match.group(1), match.group(1))}}}", text_value,
            )
            if "{{" in text_value or "}}" in text_value:
                raise RuntimeError(f"action {action['id']} contains an unknown template placeholder")
            return text_value
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    expanded: list[dict[str, Any]] = []
    for raw in raw_steps:
        local_id = str(raw["id"])
        internal_needs = list(raw.get("needs") or [])
        unknown = set(internal_needs) - set(local_ids)
        if unknown:
            raise RuntimeError(f"action {action['id']} references unknown local step(s): {', '.join(sorted(unknown))}")
        raw["id"] = id_map[local_id]
        raw["needs"] = [id_map[item] for item in internal_needs] if internal_needs else list(external_needs)
        expanded.append(expand(raw))
    return expanded


def _validate_candidate(spec: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
        contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError) as error:
        raise RuntimeError(f"workflow schema validator unavailable: {error}") from error
    errors = sorted(
        Draft202012Validator(contract).iter_errors(spec),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise RuntimeError(f"action would make the workflow invalid at {location}: {error.message}")
    try:
        pygraph.build_deps(spec.get("steps") or [])
    except (pygraph.WorkflowParseError, KeyError, TypeError) as error:
        raise RuntimeError(f"action would make the graph invalid: {error}") from error


def cmd_actions(args) -> int:
    catalog = _action_catalog()
    if args.action:
        action = catalog.get(args.action)
        if not action:
            return fail(f"unknown action '{args.action}' (try: piw actions)")
        if args.json:
            out(json.dumps(action, separators=(",", ":")))
        else:
            out(f"{action['id']} · {action['title']}")
            out(action["description"])
            out(f"category: {action['category']} · nodes: {len(action['steps'])}")
            out(f"input: {action['inputs']}")
            out(f"output: {action['outputs']}")
            out(f"failure: {action['failure']}")
            out(f"effect: {action['effect']} · retry safe: {action['retry_safe']} · idempotency: {action['idempotency']}")
            out(f"cost: {action['cost']}")
            out()
            out(yaml.safe_dump({"steps": action["steps"]}, sort_keys=False, width=100).rstrip())
        return 0
    rows = [{key: value for key, value in action.items() if key != "steps"} | {"nodes": len(action["steps"])}
            for action in catalog.values()]
    if args.json:
        out(json.dumps(rows, separators=(",", ":")))
        return 0
    width = max(len(action["id"]) for action in catalog.values())
    out(f"{'ACTION'.ljust(width)}  NODES  CATEGORY       PURPOSE")
    for action in catalog.values():
        out(f"{action['id'].ljust(width)}  {str(len(action['steps'])).rjust(5)}  "
            f"{str(action['category'])[:14].ljust(14)} {action['description']}")
    out("\nAdd one: piw add <workflow> <action> --id <prefix> [--needs step,step]")
    return 0


def cmd_add_action(args) -> int:
    workflow = need(args.workflow)
    action = _action_catalog().get(args.action)
    if not action:
        return fail(f"unknown action '{args.action}' (try: piw actions)")
    graph = graph_for(workflow)
    existing = [node["id"] for node in graph["nodes"] if not node.get("synthetic")]
    needs = [item for item in (args.needs or "").split(",") if item]
    unknown = set(needs) - set(existing)
    if unknown:
        return fail(f"--needs references unknown step(s): {', '.join(sorted(unknown))}")
    try:
        steps = instantiate_action(action, args.id or action["id"], needs)
        collisions = set(existing) & {step["id"] for step in steps}
        if collisions:
            return fail(f"action step id already exists: {', '.join(sorted(collisions))} (choose --id)")
        path = Path(workflow["path"])
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        candidate = copy.deepcopy(spec)
        candidate["steps"] = [*(candidate.get("steps") or []), *steps]
        _validate_candidate(candidate)
        pygraph.append_steps(path, steps)
    except (OSError, yaml.YAMLError, RuntimeError, pygraph.WorkflowParseError) as error:
        return fail(str(error))
    payload = {
        "ok": True, "action": action["id"], "workflow": workflow["id"],
        "added": [step["id"] for step in steps], "path": workflow["path"],
        "next": f"piw validate {shlex.quote(workflow['path'])}",
    }
    if args.json:
        out(json.dumps(payload, separators=(",", ":")))
    else:
        out(f"added {action['id']} · {', '.join(payload['added'])}")
        out(f"next: {payload['next']}")
    return 0


# ------------------------------------------------------------------------ graph


def cmd_graph(args) -> int:
    workflow = need(args.workflow)
    graph = graph_for(workflow)
    if args.json:
        out(json.dumps(graph, separators=(",", ":")))
        return 0

    header = f"{graph['workflow']} · {len(graph['nodes'])} steps · workers {graph['workers']}"
    if graph["model"]:
        header += f" · {graph['model']}"
    out(header)
    out(graph["path"])
    out()

    children: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        children.setdefault(edge["source"], []).append(edge["target"])

    width = max(len(n["id"]) for n in graph["nodes"])
    for node in graph["nodes"]:
        badges = []
        if node["gate"]:
            badges.append("gate")
        if node["judge"]:
            badges.append(f"judge>={node['judge']['score']}x{node['judge']['max_iters']}")
        if node["retries"]:
            badges.append(f"retry{node['retries']}")
        if node["tools"]:
            badges.append(f"tools:{node['tools']}")
        flow = f"{','.join(node['needs']) or '-'} -> {','.join(children.get(node['id'], [])) or '-'}"
        model = node["model"] or ""
        out(
            f"  {node['id'].ljust(width)}  {KIND_LABEL.get(node['kind'], '?').ljust(4)} "
            f"{node['determinism'].ljust(6)} {flow.ljust(34)} {' '.join(badges)}"
            + (f"  [{model}]" if model and args.verbose else "")
        )
        # Routing is the most important thing about a guarded step; give it its
        # own line rather than burying it in the badge list.
        if node.get("when_text"):
            out(f"  {' ' * width}  └ runs only when {node['when_from']}: {node['when_text']}")
    return 0


# --------------------------------------------------------------------- validate


def _condition_fields(condition: Any) -> list[str]:
    """Every JSON-Pointer field a `when:` condition reads, dotted."""
    if not isinstance(condition, dict):
        return []
    if condition.get("op") in ("all", "any", "not"):
        clauses = condition.get("of")
        clauses = clauses if isinstance(clauses, list) else [clauses]
        found: list[str] = []
        for item in clauses:
            found.extend(_condition_fields(item))
        return found
    path = str(condition.get("path", "")).lstrip("/").replace("/", ".")
    return [path] if path else []


def cmd_validate(args) -> int:
    """Answer "is this workflow sound?" as clauses an agent can branch on.

    Shape borrowed from planr's `plan audit`: a list of {clause, pass, open}
    plus a top-level `holds`, and every finding carries a `fix` naming the step
    and the exact command to re-check. Prose tells an agent something is wrong;
    this tells it what to run next.
    """
    workflow = need(args.workflow)
    recheck = f"piw validate {workflow['id']}"

    try:
        graph = pygraph.parse_steps(Path(workflow["path"]))
    except pygraph.WorkflowParseError as error:
        verdict = {
            "id": workflow["id"], "holds": False, "reason": "unparseable",
            "clauses": [{"clause": "parses", "pass": False,
                         "open": [{"step": None, "message": str(error),
                                   "fix": f"edit {workflow['path']}, then re-run `{recheck}`"}]}],
            "next": f"edit {workflow['path']} to fix the error above, then re-run `{recheck}`",
        }
        if args.json:
            out(json.dumps(verdict, separators=(",", ":")))
        else:
            out(f"INVALID {workflow['id']}: {error}")
            out(f"next: {verdict['next']}")
        return 1

    ids = {n["id"] for n in graph["nodes"]}
    has_parent = {e["target"] for e in graph["edges"]}
    has_child = {e["source"] for e in graph["edges"]}
    real = [n for n in graph["nodes"] if not n.get("synthetic")]

    def finding(step, message, fix):
        return {"step": step, "message": message, "fix": f"{fix}, then re-run `{recheck}`"}

    clauses: list[dict[str, Any]] = []

    def clause(name, open_items, severity="error"):
        clauses.append({"clause": name, "pass": not open_items,
                        "severity": severity, "open": open_items})

    # Every step is one the runner will accept.
    clause("steps are well formed", [
        finding(n["id"], "neither cmd nor prompt — the runner rejects this at load",
                f"give `{n['id']}` exactly one of cmd: or prompt: in {workflow['path']}")
        for n in real if n["kind"] == "unknown"
    ])

    raw_spec = yaml.safe_load(Path(workflow["path"]).read_text(encoding="utf-8")) or {}

    # The JSON Schema is the public authoring contract. Semantic graph checks
    # below cannot substitute for rejecting unknown fields, illegal field
    # combinations, or wrong boundary types before a paid run.
    try:
        from jsonschema import Draft202012Validator
        contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        schema_errors = sorted(
            Draft202012Validator(contract).iter_errors(raw_spec),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
    except (ImportError, OSError, ValueError) as error:
        return fail(f"workflow schema validator unavailable: {error} (run ./install.sh)")

    schema_findings = []
    for error in schema_errors[:20]:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        schema_findings.append(finding(
            str(error.absolute_path[1]) if len(error.absolute_path) > 1 and error.absolute_path[0] == "steps" else None,
            f"{location}: {error.message}",
            f"make `{location}` match `piw schema --json` in {workflow['path']}",
        ))
    if len(schema_errors) > 20:
        schema_findings.append(finding(
            None,
            f"{len(schema_errors) - 20} additional schema violations omitted",
            f"fix the first schema violations in {workflow['path']}",
        ))
    clause("matches the versioned JSON Schema", schema_findings)

    uses_input = any("{input}" in str(node.get("body") or "") for node in real)
    input_contract = raw_spec.get("input")
    input_findings: list[dict[str, Any]] = []
    if uses_input and not isinstance(input_contract, dict):
        input_findings.append(finding(
            None,
            "a step uses {input}, but the workflow has no explicit top-level input contract",
            f"add `input: {{required: true, description: ...}}` to {workflow['path']}",
        ))
    elif isinstance(input_contract, dict) and not isinstance(input_contract.get("required", False), bool):
        input_findings.append(finding(
            None,
            "input.required must be true or false",
            f"fix the input contract in {workflow['path']}",
        ))
    clause("workflow input is explicit and run-isolated", input_findings)

    # Gates decide, judges smell-test. A judge with nothing mechanical under it
    # is a model grading itself.
    clause("model steps are mechanically checked", [
        finding(n["id"], "judge without a gate — nothing mechanical decides pass/fail",
                f"add a gate: to `{n['id']}` (a bash check on \"$OUT\")")
        for n in real if n["judge"] and not n["gate"]
    ] + [
        finding(n["id"], "agent step without a gate — nothing checks the effect it produced",
                f"add a gate: to `{n['id']}` that checks the effect, not the transcript")
        for n in real if n["kind"] == "agent" and not n["gate"]
    ])

    # A guarded step reads its source's JSON. If nothing pins the SHAPE of that
    # JSON, a model emitting {"type": "bug"} instead of {"kind": "bug"} makes the
    # condition quietly evaluate false: the branch never fires and the run still
    # reports success. Silent misrouting is the worst failure this system has, so
    # it is caught here rather than in production.
    routing: list[dict[str, Any]] = []
    unpinned: list[dict[str, Any]] = []
    for node in real:
        if not node.get("when_from"):
            continue
        source = next((n for n in real if n["id"] == node["when_from"]), None)
        if not source:
            continue
        schema = source.get("schema")
        if not schema:
            unpinned.append(finding(
                source["id"],
                f"`{node['id']}` routes on this step's output, but its shape is never pinned — "
                f"a wrong field name would route silently instead of failing",
                f"declare a schema: on `{source['id']}` naming the fields "
                f"`{node['id']}` reads ({', '.join(_condition_fields(node['when'])) or 'the routing field'})"))
            continue
        for field in _condition_fields(node["when"]):
            root = field.split(".")[0]
            if root and root not in schema:
                routing.append(finding(
                    source["id"],
                    f"`{node['id']}` routes on `{field}`, which `{source['id']}` "
                    f"does not promise (schema declares: {', '.join(sorted(schema)) or 'nothing'})",
                    f"add `{root}` to the schema: on `{source['id']}`, "
                    f"or change the when: on `{node['id']}` to a declared field"))
    clause("routing reads fields the source promises", routing)
    clause("routing sources pin their output shape", unpinned, severity="advice")

    clause("no orphan steps", [
        finding(n["id"], "orphan — no incoming or outgoing edges",
                f"add needs: to `{n['id']}`, or remove it")
        for n in real if len(ids) > 1 and n["id"] not in has_parent and n["id"] not in has_child
    ])

    # A steps.yaml copied from a template and never specialised produces a
    # confident-looking run that does nothing. Catch it before it costs money.
    placeholders = [
        finding(n["id"], "looks like unfilled scaffolding (placeholder text in the body)",
                f"replace the placeholder body of `{n['id']}` with the real prompt or command")
        for n in real
        if any(mark in (n.get("body") or "").lower()
               for mark in ("todo", "tbd", "your prompt here", "lorem ipsum", "<placeholder>", "fill me"))
    ]
    clause("no unfilled scaffolding", placeholders)

    errors = [c for c in clauses if not c["pass"] and c["severity"] == "error"]
    advice = [c for c in clauses if not c["pass"] and c["severity"] == "advice"]
    open_all = [item for c in errors for item in c["open"]]
    holds = not errors
    # One next action: fix the first real defect, else take the first piece of
    # advice, else run it.
    if errors:
        next_action = errors[0]["open"][0]["fix"]
    elif advice:
        next_action = advice[0]["open"][0]["fix"]
    else:
        next_action = f"piw run {workflow['id']}"

    verdict = {
        "id": workflow["id"],
        "holds": holds,
        "steps": len(real),
        "clauses": clauses,
        "next": next_action,
    }
    verdict["advice"] = [item for c in advice for item in c["open"]]
    if not holds:
        verdict["reason"] = errors[0]["clause"]

    if args.json:
        out(json.dumps(verdict, separators=(",", ":")))
        return 0 if holds else 1

    if holds:
        note = f" · {len(verdict['advice'])} advisory" if verdict["advice"] else ""
        out(f"OK {workflow['id']} · {len(real)} steps · {len(clauses)} clauses{note}")
        for item in verdict["advice"]:
            out(f"  [note] {item['step'] or '-'}: {item['message']}")
        out(f"next: {next_action}")
        return 0
    out(f"FAILS {workflow['id']} · {len(open_all)} issue(s)")
    for entry in clauses:
        mark = "ok  " if entry["pass"] else ("FAIL" if entry["severity"] == "error" else "note")
        out(f"  [{mark}] {entry['clause']}")
        for item in entry["open"]:
            out(f"         {item['step'] or '-'}: {item['message']}")
            out(f"         fix: {item['fix']}")
    out(f"next: {next_action}")
    return 1


# -------------------------------------------------------------------------- run


def daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DAEMON}/api/health", timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def daemon_token() -> str | None:
    """The CSRF token is injected into the served HTML; scrape it for POSTs."""
    try:
        with urllib.request.urlopen(DAEMON, timeout=3) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None
    marker = 'const TOKEN = "'
    start = html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = html.find('"', start)
    return html[start:end] if end > start else None


def daemon_workflow_path(workflow_id: str) -> str | None:
    """The steps.yaml the daemon would run for this id, or None."""
    try:
        with urllib.request.urlopen(f"{DAEMON}/api/workflows", timeout=5) as response:
            listing = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for item in listing.get("workflows") or []:
        if item.get("id") == workflow_id:
            return item.get("path")
    return None


def start_via_daemon(
    workflow: dict[str, Any], regen: list[str], no_cache: bool,
    input_text: str | None = None, input_file: str | None = None,
) -> Path | None:
    """Run through the daemon so the canvas lights up live — but only if the
    daemon resolves this id to the SAME file we just inspected.

    The daemon discovers workflows in its own scope, so delegating blindly could
    run a different workflow that happens to share an id. An agent that edited
    one file must never end up running another.
    """
    workflow_id = workflow["id"]
    if daemon_workflow_path(workflow_id) != workflow["path"]:
        return None
    token = daemon_token()
    if not token:
        return None
    payload = json.dumps({
        "regen": regen,
        "no_cache": no_cache,
        **({"input": input_text} if input_text is not None else {}),
        **({"input_file": input_file} if input_file else {}),
    }).encode()
    request = urllib.request.Request(
        f"{DAEMON}/api/workflows/{workflow_id}/run",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Loops-Token": token,
            "Origin": DAEMON,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    run_id = body.get("run_id")
    events_path = body.get("events_path")
    if isinstance(events_path, str) and events_path:
        return Path(events_path)
    return (loopd.PYGRAPH_EVENTS_DIR / f"{run_id}.jsonl") if run_id else None


def start_direct(
    workflow: dict[str, Any], regen: list[str], no_cache: bool,
    input_text: str | None = None, input_file: str | None = None,
) -> tuple[subprocess.Popen, Path]:
    # Unique per invocation, not per second: a timestamp made two runs started in
    # the same second share one event file, so each reported the other's steps.
    events = loopd.PYGRAPH_EVENTS_DIR / f"cli-{uuid.uuid4().hex[:12]}.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch()
    command = [
        sys.executable, str(loopd.WORKFLOW_RUNNER), str(workflow["path"]),
        "--events", str(events),
    ]
    if regen:
        command += ["--regen", ",".join(regen)]
    if no_cache:
        command += ["--no-cache"]
    if input_text is not None:
        command += ["--input", input_text]
    if input_file:
        command += ["--input-file", input_file]
    process = subprocess.Popen(
        command, cwd=workflow["cwd"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    return process, events


def follow(
    events_path: Path,
    quiet: bool,
    timeout: float,
    process: subprocess.Popen | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Tail the JSONL event stream, printing one line per finished step."""
    offset = 0
    started: dict[str, float] = {}
    totals = {"cost": 0.0, "tokens": 0, "passed": 0, "failed": 0, "cached": 0, "skipped": 0}
    run_dir = ""
    deadline = time.monotonic() + timeout
    width = 12

    while time.monotonic() < deadline:
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        except OSError:
            time.sleep(0.2)
            continue

        # Only consume whole lines; the runner may be mid-write.
        if chunk and not chunk.endswith("\n"):
            cut = chunk.rfind("\n")
            if cut < 0:
                offset -= len(chunk.encode("utf-8"))
                chunk = ""
            else:
                offset -= len(chunk[cut + 1:].encode("utf-8"))
                chunk = chunk[: cut + 1]

        # "\n" only: splitlines() would fragment a record containing U+2028 etc.
        for line in chunk.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("t")
            sid = str(event.get("id", ""))

            if kind == "run_start":
                run_dir = event.get("run_dir", "")
                if not quiet:
                    out(f"run {event.get('workflow')} · {len(event.get('todo') or [])} steps")
            elif kind == "step_start":
                started[sid] = time.monotonic()
            elif kind == "step_judge" and not quiet:
                out(f"  {sid.ljust(width)} judge {event.get('score')} "
                    f"(need {event.get('threshold')}) attempt {event.get('attempt')}")
            elif kind == "step_cached":
                totals["cached"] += 1
                totals["passed"] += 1
                if not quiet:
                    out(f"  {sid.ljust(width)} cache  {secs(event.get('seconds'))}")
            elif kind == "step_end":
                ok = bool(event.get("passed"))
                totals["passed" if ok else "failed"] += 1
                totals["cost"] += float(event.get("cost") or 0)
                totals["tokens"] += int(event.get("total") or 0)
                if not quiet:
                    attempts = event.get("attempts") or 1
                    suffix = f" x{attempts}" if attempts > 1 else ""
                    out(f"  {sid.ljust(width)} {STATUS_MARK[ok].ljust(6)} "
                        f"{secs(event.get('seconds'))} {money(float(event.get('cost') or 0))}{suffix}")
            elif kind == "step_skipped":
                totals["skipped"] += 1
                if not quiet:
                    out(f"  {sid.ljust(width)} skip   {event.get('reason', '')}")
            elif kind == "run_end":
                return bool(event.get("ok")), {**totals, "run_dir": run_dir, "failed_ids": event.get("failed") or []}
        if process is not None and process.poll() is not None:
            return False, {**totals, "run_dir": run_dir, "failed_ids": ["<runner-exited>"]}
        time.sleep(0.2)

    return False, {**totals, "run_dir": run_dir, "failed_ids": ["<timeout>"]}


def cmd_run(args) -> int:
    workflow = need(args.workflow)
    graph = graph_for(workflow)
    regen = [n for n in (args.node or []) if n]
    unknown = [n for n in regen if n not in {node["id"] for node in graph["nodes"]}]
    if unknown:
        return fail(f"unknown step(s): {', '.join(unknown)}")

    if args.input is not None and args.input_file:
        return fail("choose --input or --input-file, not both")
    input_file = None
    if args.input_file:
        candidate = Path(args.input_file).expanduser().resolve()
        if not candidate.is_file():
            return fail(f"input file not found: {candidate}")
        input_file = str(candidate)
    process = None
    events_path = start_via_daemon(
        workflow, regen, args.no_cache, args.input, input_file,
    ) if daemon_up() else None
    if events_path is None:
        process, events_path = start_direct(workflow, regen, args.no_cache, args.input, input_file)

    # Machine output must be one JSON document. Streaming decorative progress
    # before it made `piw run --json | jq ...` fail even when the run passed.
    ok, totals = follow(events_path, quiet=args.quiet or args.json, timeout=args.timeout, process=process)

    if process is not None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    summary = {
        "ok": ok,
        "passed": totals["passed"],
        "failed": totals["failed"],
        "cached": totals["cached"],
        "skipped": totals["skipped"],
        "cost": round(totals["cost"], 6),
        "tokens": totals["tokens"],
        "run_dir": totals["run_dir"],
        "failed_ids": totals["failed_ids"],
    }
    if args.json:
        out(json.dumps(summary, separators=(",", ":")))
    else:
        verdict = "RUN ok" if ok else f"RUN FAILED {','.join(summary['failed_ids'])}"
        out(f"{verdict} · {totals['passed']} passed"
            + (f", {totals['failed']} failed" if totals["failed"] else "")
            + (f", {totals['cached']} cached" if totals["cached"] else "")
            + (f", {totals['skipped']} skipped" if totals["skipped"] else "")
            + f" · {money(totals['cost'])} · {totals['tokens']} tok")
        if summary["run_dir"]:
            out(f"run={Path(summary['run_dir']).name}")
    return 0 if ok else 1


# ------------------------------------------------------------------------- runs


def cmd_runs(args) -> int:
    workflow = need(args.workflow)
    runs = loopd.list_workflow_runs(workflow["id"], limit=args.limit, runs_dir=workflow.get("runs_dir"))
    if args.json:
        out(json.dumps(runs, separators=(",", ":")))
        return 0
    if not runs:
        out("no runs yet")
        return 0
    for run in runs:
        ledger = read_ledger(run)
        cost = sum(float(e.get("cost") or 0) for e in ledger)
        tokens = sum(int(e.get("total") or 0) for e in ledger)
        passed = sum(1 for e in ledger if e.get("passed"))
        out(f"{run['id']}  {run['status'].ljust(11)} {passed}/{len(ledger)} "
            f"{money(cost).rjust(8)} {str(tokens).rjust(7)} tok")
    return 0


# ------------------------------------------------------------------------- show


def cmd_show(args) -> int:
    workflow = need(args.workflow)
    run = latest_run(workflow) if not args.run else None
    if args.run:
        runs = loopd.list_workflow_runs(workflow["id"], limit=200, runs_dir=workflow.get("runs_dir"))
        run = next((r for r in runs if r["id"] == args.run or args.run in r["id"]), None)
    if not run:
        return fail("no matching run (try: pygraph runs <id>)")

    run_dir = Path(run["path"])
    if not args.step:
        names = sorted(p.name for p in run_dir.iterdir() if p.is_file())
        out("\n".join(names))
        return 0

    if args.resolved:
        # What the runner actually sent, after {step.x} / {prev} / {run} substitution.
        try:
            info = pygraph.resolve_prompt(Path(workflow["path"]), Path(run["path"]), args.step)
        except pygraph.WorkflowParseError as error:
            return fail(str(error))
        if args.json:
            out(json.dumps(info, separators=(",", ":")))
            return 0
        if info["missing"]:
            print(f"warning: upstream artifacts missing: {', '.join(info['missing'])}", file=sys.stderr)
        out(info["resolved"].rstrip())
        return 0

    candidate = run_dir / (args.step if "." in args.step else f"{args.step}.md")
    # Guard against ../ escaping the run directory.
    try:
        candidate = candidate.resolve()
        candidate.relative_to(run_dir.resolve())
    except (ValueError, OSError):
        return fail("invalid step name")
    if not candidate.is_file():
        return fail(f"no artifact '{candidate.name}' in {run_dir.name}")
    out(candidate.read_text(encoding="utf-8", errors="replace").rstrip())
    return 0


# ------------------------------------------------------------------------ stats


def cmd_stats(args) -> int:
    workflow = need(args.workflow)
    runs = loopd.list_workflow_runs(workflow["id"], limit=args.limit, runs_dir=workflow.get("runs_dir"))
    per_step: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    total_tokens = 0
    complete = 0

    for run in runs:
        ledger = read_ledger(run)
        if not ledger:
            continue
        complete += 1
        for entry in ledger:
            stat = per_step.setdefault(
                entry["id"], {"runs": 0, "passed": 0, "cached": 0, "cost": 0.0, "tokens": 0, "seconds": 0.0},
            )
            stat["runs"] += 1
            stat["passed"] += 1 if entry.get("passed") else 0
            stat["cached"] += 1 if entry.get("cached") else 0
            stat["cost"] += float(entry.get("cost") or 0)
            stat["tokens"] += int(entry.get("total") or 0)
            stat["seconds"] += float(entry.get("seconds") or 0)
            total_cost += float(entry.get("cost") or 0)
            total_tokens += int(entry.get("total") or 0)

    summary = {
        "id": workflow["id"],
        "runs": len(runs),
        "with_ledger": complete,
        "cost": round(total_cost, 6),
        "tokens": total_tokens,
        "steps": per_step,
    }
    if args.json:
        out(json.dumps(summary, separators=(",", ":")))
        return 0

    out(f"{workflow['name']} · {len(runs)} run(s) · {money(total_cost)} · {total_tokens} tok")
    if not per_step:
        out("no ledgers yet (runs that never completed write none)")
        return 0
    width = max(len(s) for s in per_step)
    out(f"  {'STEP'.ljust(width)}  PASS  CACHE  {'COST'.rjust(8)}  {'TOK'.rjust(7)}  AVG")
    # Most expensive step first: the skill's own advice is that it is the next
    # optimisation target.
    for step, stat in sorted(per_step.items(), key=lambda kv: -kv[1]["cost"]):
        avg = stat["seconds"] / stat["runs"] if stat["runs"] else 0
        out(f"  {step.ljust(width)}  {stat['passed']}/{stat['runs']}   "
            f"{stat['cached']}/{stat['runs']}  {money(stat['cost']).rjust(8)}  "
            f"{str(stat['tokens']).rjust(7)}  {secs(avg)}")
    return 0


# ------------------------------------------------------------------- run detail


def cmd_detail(args) -> int:
    workflow = need(args.workflow)
    runs = loopd.list_workflow_runs(workflow["id"], limit=200, runs_dir=workflow.get("runs_dir"))
    if not runs:
        return fail("no runs yet")
    run = (next((r for r in runs if r["id"] == args.run or args.run in r["id"]), None)
           if args.run else runs[0])
    if not run:
        return fail(f"no run matching '{args.run}'")

    try:
        detail = pygraph.run_detail(Path(workflow["path"]), Path(run["path"]))
    except pygraph.WorkflowParseError as error:
        return fail(str(error))

    if args.json:
        out(json.dumps(detail, separators=(",", ":")))
        return 0 if detail["run"]["ok"] else 1

    info = detail["run"]
    counts = " · ".join(f"{n} {status}" for status, n in sorted(info["counts"].items()))
    out(f"{info['workflow']} · {info['id']}")
    out(f"{'OK' if info['ok'] else 'FAILED'} · {counts} · {secs(info['seconds'])} · "
        f"{money(info['cost'])} · {info['tokens']} tok")
    out()

    for step in detail["steps"]:
        if step["status"] == "not_run" and not args.all:
            continue
        head = [step["status"].upper(), KIND_LABEL.get(step["kind"], step["kind"])]
        if step["seconds"]:
            head.append(secs(step["seconds"]))
        if step["cost"]:
            head.append(money(step["cost"]))
        if step["tokens"]:
            head.append(f"{step['tokens']} tok")
        if (step["attempts"] or 1) > 1:
            head.append(f"{step['attempts']} attempts")
        out(f"── {step['id']}  [{' · '.join(head)}]")
        if step["model"]:
            out(f"   model: {step['model']}")
        for attempt in step["judge_attempts"]:
            label = "rejected" if attempt.get("rejected") else "judged"
            verdict = " ".join(attempt["judge"].split())[:110]
            out(f"   {label} attempt {attempt['n']}: {verdict}")
        if step["previews"]:
            for image in step["previews"]:
                out(f"   produced: {image}")
        if args.io and step["sent"]:
            out("   --- sent ---")
            out(textwrap.indent(step["sent"].strip()[:2000], "   | "))
        if step["output"]:
            body = step["output"].strip()
            out("   --- output ---")
            out(textwrap.indent(body if args.io else body[:400], "   | "))
        if step["stderr"].strip():
            out(textwrap.indent(f"stderr: {step['stderr'].strip()[:400]}", "   ! "))
        out()

    if detail["qa"]:
        out(f"QA: {detail['qa'].strip()[:400]}")
    return 0 if info["ok"] else 1


# ------------------------------------------------------------------------ evals


def _skill_script(name: str) -> Path:
    return Path(loopd.WORKFLOW_RUNNER).parent / name


def _run_skill(script: str, workflow: dict[str, Any], extra: list[str]) -> int:
    path = _skill_script(script)
    if not path.is_file():
        return fail(f"{script} not found at {path}")
    command = [sys.executable, str(path), str(workflow["path"]), *extra]
    if "--json" not in extra:
        out(f"$ {' '.join(command[1:])}")
    return subprocess.run(command, cwd=workflow["cwd"], check=False).returncode


def cmd_batch(args) -> int:
    """Run one frozen workflow contract across an isolated input corpus."""
    workflow = need(args.workflow)
    inputs = str(Path(args.inputs).expanduser().resolve())
    extra = ["--inputs", inputs, "--input-file", args.input_file,
             "--parallel", str(args.parallel), "--item-timeout", str(args.item_timeout)]
    if args.limit:
        extra += ["--limit", str(args.limit)]
    if args.out:
        extra += ["--out", str(Path(args.out).expanduser().resolve())]
    if args.resume:
        extra += ["--resume", str(Path(args.resume).expanduser().resolve())]
    if args.require_all:
        extra.append("--require-all")
    if args.stop_after_failures:
        extra += ["--stop-after-failures", str(args.stop_after_failures)]
    if args.max_tokens:
        extra += ["--max-tokens", str(args.max_tokens)]
    if args.max_cost:
        extra += ["--max-cost", str(args.max_cost)]
    if args.output_step:
        extra += ["--output-step", args.output_step]
    if args.git_history:
        extra.append("--git-history")
    if args.allow_shared_workspace:
        extra.append("--allow-shared-workspace")
    if args.detach:
        extra.append("--detach")
    if args.json:
        extra.append("--json")
    return _run_skill("run_batch.py", workflow, extra)


def _batch_state(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"batch directory not found: {path}")
    progress_path = path / "progress.json"
    controller_path = path / "controller.json"
    source = progress_path if progress_path.is_file() else controller_path
    if not source.is_file():
        raise ValueError(f"batch has no controller or progress receipt: {path}")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"malformed batch receipt: {source}")
    controller = {}
    if controller_path.is_file():
        candidate = json.loads(controller_path.read_text(encoding="utf-8"))
        controller = candidate if isinstance(candidate, dict) else {}
    pid = int(controller.get("pid") or value.get("pid") or 0)
    alive = False
    if pid > 1:
        try:
            os.kill(pid, 0)
            alive = True
        except (OSError, ProcessLookupError):
            pass
    status = str(value.get("status") or controller.get("status") or "unknown")
    if controller.get("status") in {"cancelling", "cancelled"}:
        status = str(controller["status"])
    if status in {"starting", "running", "cancelling"} and not alive:
        status = "interrupted"
    return {
        **value,
        "status": status,
        "pid": pid or None,
        "alive": alive,
        "batch_dir": str(path),
        "report": str(path / "batch-report.md") if (path / "batch-report.md").is_file() else None,
        "log": str(path / "controller.log") if (path / "controller.log").is_file() else None,
    }


def cmd_batch_status(args) -> int:
    try:
        state = _batch_state(Path(args.batch))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return fail(str(error))
    if args.json:
        out(json.dumps(state, separators=(",", ":")))
    else:
        counts = " · ".join(
            f"{state.get(key, 0)} {key.replace('_', ' ')}"
            for key in ("passed", "failed", "not_run") if key in state
        )
        out(f"{state['status']} · {counts or 'waiting for first receipt'} · {state['batch_dir']}")
        if state.get("report"):
            out(f"report: {state['report']}")
        elif state.get("log"):
            out(f"log: {state['log']}")
    return 0 if state["status"] not in {"interrupted", "stopped"} and state.get("ok") is not False else 1


def cmd_batch_cancel(args) -> int:
    try:
        path = Path(args.batch).expanduser().resolve()
        state = _batch_state(path)
        pid = int(state.get("pid") or 0)
        if pid <= 1 or not state.get("alive"):
            return fail(f"batch controller is not running: {path}")
        controller = {
            "pid": pid, "status": "cancelling", "batch_dir": str(path),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        (path / "controller.json").write_text(json.dumps(controller, indent=2) + "\n", encoding="utf-8")
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return fail(str(error))
    if args.json:
        out(json.dumps({"ok": True, **controller}, separators=(",", ":")))
    else:
        out(f"cancelling batch · pid={pid} · {path}")
    return 0


def cmd_eval(args) -> int:
    """Compare models on the same corpus (wraps the skill's eval_models.py).

    Only the workflow's default model is swapped; per-step pins, judges and QA
    stay fixed, so the evaluator is held constant while the generator varies.
    """
    workflow = need(args.workflow)
    extra = ["--inputs", args.inputs, "--input-file", args.input_file, "--models", args.models]
    if args.parallel:
        extra += ["--parallel", str(args.parallel)]
    if args.limit:
        extra += ["--limit", str(args.limit)]
    return _run_skill("eval_models.py", workflow, extra)


def cmd_reports(args) -> int:
    """Find batch/eval reports this workflow has produced."""
    workflow = need(args.workflow)
    root = Path(workflow["cwd"])
    found: list[dict[str, Any]] = []
    for pattern, kind in (("batch-*/batch-report.md", "batch"), ("eval-*/eval-report.md", "eval")):
        for path in sorted(root.glob(pattern), reverse=True):
            found.append({"kind": kind, "path": str(path),
                          "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
    if args.json:
        out(json.dumps(found, separators=(",", ":")))
        return 0
    if not found:
        out("no batch or eval reports yet (see: piw batch / piw eval)")
        return 0
    for report in found[: args.limit]:
        out(f"{report['kind']:<6} {report['modified']}  {report['path']}")
    if args.show and found:
        out()
        out(Path(found[0]["path"]).read_text(encoding="utf-8", errors="replace"))
    return 0


# -------------------------------------------------------------------------- set


def cmd_set(args) -> int:
    workflow = need(args.workflow)
    changes: dict[str, Any] = {}
    for key in ("model", "thinking", "gate", "tools"):
        value = getattr(args, key)
        if value is not None:
            # `--model ''` clears the key so the step falls back to the default.
            changes[key] = value
    for key in ("retries", "timeout"):
        value = getattr(args, key)
        if value is None:
            continue
        if value == "":
            changes[key] = ""  # remove the key
        elif str(value).strip().lstrip("-").isdigit():
            changes[key] = int(value)
        else:
            return fail(f"--{key} must be a whole number (or '' to clear)")
    for key in ("retry_delay_seconds", "retry_max_delay_seconds", "retry_jitter"):
        value = getattr(args, key)
        if value is None:
            continue
        if value == "":
            changes[key] = ""
            continue
        try:
            changes[key] = float(value)
        except ValueError:
            return fail(f"--{key.replace('_', '-')} must be a number (or '' to clear)")
    if args.retry_backoff is not None:
        changes["retry_backoff"] = args.retry_backoff
    if args.retry_on is not None:
        allowed = {"command_exit", "model_error", "gate_failed", "schema_failed", "judge_below_target"}
        values = [item for item in args.retry_on.split(",") if item]
        unknown = set(values) - allowed
        if unknown:
            return fail(f"--retry-on has unknown failure class(es): {', '.join(sorted(unknown))}")
        changes["retry_on"] = values if values else ""
    if args.prompt_file:
        source = Path(args.prompt_file)
        if not source.is_file():
            return fail(f"no such file: {source}")
        changes["prompt"] = source.read_text(encoding="utf-8")
    if args.when is not None:
        if args.when == "":
            changes["when"] = ""  # clears the guard
        else:
            try:
                condition = json.loads(args.when)
            except ValueError as error:
                return fail(f"--when must be JSON: {error}")
            if not isinstance(condition, dict) or "op" not in condition:
                return fail('--when needs an object with an "op", e.g. '
                            '\'{"op":"equals","path":"/kind","value":"bug"}\'')
            changes["when"] = condition
    if args.schema is not None:
        if args.schema == "":
            changes["schema"] = ""
        else:
            try:
                shape = json.loads(args.schema)
            except ValueError as error:
                return fail(f"--schema must be JSON: {error}")
            if not isinstance(shape, dict) or not shape:
                return fail('--schema needs a non-empty object, e.g. '
                            '\'{"kind":{"type":"string","enum":["bug","feature"]}}\'')
            changes["schema"] = shape
    if args.from_step is not None:
        changes["from"] = args.from_step
    if args.produces is not None:
        changes["produces"] = ([p for p in args.produces.split(",") if p] if args.produces else "")
    if not changes:
        return fail("nothing to change (see: pygraph set --help)")

    try:
        result = pygraph.update_step(Path(workflow["path"]), args.step, changes)
    except pygraph.WorkflowParseError as error:
        return fail(str(error))

    if args.json:
        out(json.dumps({"ok": True, **result}, separators=(",", ":")))
    else:
        out(f"updated {args.step}: {', '.join(result['changed'])}")
        out(workflow["path"])
    return 0


# ------------------------------------------------------------------------- path


def cmd_path(args) -> int:
    workflow = need(args.workflow)
    out(workflow["path"] if not args.dir else workflow["cwd"])
    return 0


# --------------------------------------------------------------- product ops


def cmd_doctor(args) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import jsonschema  # type: ignore[import-not-found]  # noqa: F401
        import ruamel.yaml  # type: ignore[import-not-found]  # noqa: F401
        dependencies = True
        dependency_detail = (
            f"PyYAML {getattr(yaml, '__version__', '?')} · ruamel.yaml available · "
            "jsonschema available"
        )
    except ImportError as error:
        dependencies = False
        dependency_detail = str(error)
    check("dependencies", dependencies, dependency_detail)
    check("runner", loopd.WORKFLOW_RUNNER.is_file(), str(loopd.WORKFLOW_RUNNER))

    pi_bin = shutil.which("pi")
    pi_version = "missing"
    pi_ok = False
    if pi_bin:
        try:
            result = subprocess.run([pi_bin, "--version"], capture_output=True, text=True, timeout=5, check=False)
            pi_version = (result.stdout or result.stderr).strip().splitlines()[0]
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", pi_version)
            parsed = tuple(int(part) for part in match.groups()) if match else ()
            pi_ok = parsed >= (0, 80, 10)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    check("pi", pi_ok, f"{pi_bin or 'not on PATH'} · {pi_version}")
    product_root = Path(__file__).resolve().parent.parent
    pi_package_ok = False
    settings = Path.home() / ".pi" / "agent" / "settings.json"
    try:
        package_settings = json.loads(settings.read_text(encoding="utf-8")) or {}
        sources = [value.get("source") if isinstance(value, dict) else value
                   for value in package_settings.get("packages") or []]
        registered = {
            (settings.parent / value).expanduser().resolve()
            for value in sources
            if isinstance(value, str) and not value.startswith(("npm:", "git:", "http:", "https:", "ssh:"))
        }
        pi_package_ok = product_root in registered
    except (OSError, ValueError, TypeError):
        pass
    check("pi-package", pi_package_ok, str(product_root))

    pi_skill_ok = False
    pi_skill_detail = "skill:pi-workflows unavailable"
    if pi_bin and pi_ok:
        try:
            result = subprocess.run(
                [pi_bin, "--mode", "rpc", "--no-session"],
                input='{"type":"get_commands","id":"pi-workflows-doctor"}\n',
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "PI_OFFLINE": "1"},
            )
            for line in result.stdout.split("\n"):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if event.get("id") != "pi-workflows-doctor" or not event.get("success"):
                    continue
                commands = ((event.get("data") or {}).get("commands") or [])
                pi_skill_ok = any(command.get("name") == "skill:pi-workflows" for command in commands)
                if pi_skill_ok:
                    pi_skill_detail = "skill:pi-workflows loaded"
                break
        except (OSError, subprocess.SubprocessError, TypeError):
            pass
    check("pi-skill", pi_skill_ok, pi_skill_detail)

    loops_ok = daemon_up()
    check("loops", loops_ok, f"{DAEMON} · {'connected' if loops_ok else 'optional integration unavailable'}", required=False)
    codex = Path.home() / ".agents" / "skills" / "pi-workflows" / "SKILL.md"
    claude = Path.home() / ".claude" / "skills" / "pi-workflows" / "SKILL.md"
    check("codex-skill", codex.is_file(), str(codex), required=False)
    check("claude-skill", claude.is_file(), str(claude), required=False)

    core_ok = all(item["ok"] for item in checks if item["required"])
    integrations_ok = all(item["ok"] for item in checks if not item["required"])
    payload = {
        "ok": core_ok,
        "integrations_ok": integrations_ok,
        "product_root": str(product_root),
        "checks": checks,
    }
    if args.json:
        out(json.dumps(payload, separators=(",", ":")))
    else:
        out(f"Pi Workflows · {'ready' if core_ok else 'not ready'}")
        for item in checks:
            mark = "ok" if item["ok"] else ("FAIL" if item["required"] else "note")
            out(f"  [{mark.ljust(4)}] {item['name']} · {item['detail']}")
    return 0 if core_ok else 1


def cmd_create(args) -> int:
    name = args.name.strip()
    if not name:
        return fail("workflow name required")
    identifier = loopd.slugify(name)
    directory = Path(args.dir or identifier).expanduser().resolve()
    steps_path = directory / "steps.yaml"
    if steps_path.exists():
        return fail(f"refusing to overwrite {steps_path}")
    action = None
    if args.action:
        try:
            action = _action_catalog().get(args.action)
        except RuntimeError as error:
            return fail(str(error))
        if not action:
            return fail(f"unknown action '{args.action}' (try: piw actions)")
    steps = instantiate_action(action, action["id"], []) if action else [
        {
            "id": "produce",
            "prompt": (
                "Complete this work item. Return only the requested artifact, with no meta commentary.\n\n"
                "{input}"
            ),
            "gate": 'test -s "$OUT"',
            "retries": 1,
        },
        {
            "id": "verify",
            "needs": ["produce"],
            "cmd": 'test -s "$RUN/produce.md" && cp "$RUN/produce.md" "$OUT"',
            "gate": 'test -s "$OUT"',
        },
    ]
    qa_prompt = (
        "Review the completed workflow artifact against the original input. "
        "Output JSON only: {\"verdict\": \"pass\"|\"fail\", \"issues\": [\"...\"]}\n"
        "{artifacts}"
    )
    if action:
        qa_prompt = (
            f"Review whether the completed artifacts satisfy the `{action['id']}` action contract. "
            "Do not require the workflow to perform work outside this action's declared purpose.\n"
            f"Input contract: {action['inputs']}\n"
            f"Output contract: {action['outputs']}\n"
            f"Failure contract: {action['failure']}\n"
            "Output JSON only: {\"verdict\": \"pass\"|\"fail\", \"issues\": [\"...\"]}\n"
            "{artifacts}"
        )
    spec = {
        "version": 1,
        "workflow": identifier,
        "model": args.model,
        "thinking": args.thinking,
        "workers": args.workers,
        "input": {"required": True, "description": "One immutable unit of work for this run"},
        "qa": {
            "model": args.qa_model,
            "thinking": "low",
            "prompt": qa_prompt,
        },
        "steps": steps,
    }
    try:
        _validate_candidate(spec)
    except RuntimeError as error:
        return fail(str(error))
    directory.mkdir(parents=True, exist_ok=True)
    steps_path.write_text(yaml.safe_dump(spec, sort_keys=False, width=100), encoding="utf-8")
    payload = {
        "ok": True, "id": identifier, "path": str(steps_path),
        "action": action["id"] if action else None,
        "next": f"piw validate {shlex.quote(str(steps_path))}",
    }
    if args.json:
        out(json.dumps(payload, separators=(",", ":")))
    else:
        out(f"created {identifier} · {steps_path}")
        out(f"next: {payload['next']}")
    return 0


def _run_loops(args: list[str]) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("loops")
    if not executable:
        raise RuntimeError("Loops is not installed; core workflows still run, but scheduling requires the Loops adapter")
    return subprocess.run([executable, *args], capture_output=True, text=True, timeout=30, check=False)


def cmd_schedule(args) -> int:
    workflow = need(args.workflow)
    if args.interval_minutes is not None and args.interval_minutes < 1:
        return fail("--interval-minutes must be at least 1")
    if args.daily and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args.daily):
        return fail("--daily must be local time as HH:MM")
    if args.timeout < 1:
        return fail("--timeout must be at least 1 second")
    if args.stop_after is not None and args.stop_after < 1:
        return fail("--stop-after must be at least 1")
    validation = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "validate", workflow["path"], "--json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if validation.returncode != 0:
        print(validation.stdout or validation.stderr, file=sys.stderr, end="")
        return fail("workflow validation failed; automation was not created")
    piw_bin = (Path(__file__).resolve().parent.parent / "bin" / "piw").resolve()
    command = " ".join([
        "PI_WORKFLOWS_AUTOMATION=1",
        shlex.quote(str(piw_bin)),
        "run",
        shlex.quote(workflow["path"]),
        "--json",
    ])
    loop_id = args.id or loopd.slugify(f"piw-{workflow['name']}")[:63]
    command_args = [
        "create", "--id", loop_id, "--name", args.name or f"Pi Workflow · {workflow['name']}",
        "--instructions", f"Validate and run {workflow['path']}; preserve its run ledger and artifacts.",
        "--command", command,
        "--goal", f"Run Pi Workflow {workflow['name']} on its declared schedule.",
        "--workspace", workflow["cwd"],
        "--timeout", str(args.timeout),
    ]
    command_args += ["--interval", str(args.interval_minutes * 60)] if args.interval_minutes else ["--daily", args.daily]
    if args.stop_after:
        command_args += ["--stop-after", str(args.stop_after)]
    result = _run_loops(command_args)
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return fail(output or "Loops rejected the automation")
    if args.json:
        out(output)
    else:
        out(f"scheduled {loop_id} · {workflow['name']}")
        out(output)
    return 0


def cmd_ui(args) -> int:
    """Open the optional local graph studio over the canonical workflow file."""
    if args.json:
        return fail("ui is an interactive browser surface and does not support --json")
    workflow = need(args.workflow)
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "serve_workflow.py"),
        workflow["path"],
        "--port", str(args.port),
    ]
    if args.input_file:
        command.extend(["--input-file", str(Path(args.input_file).expanduser().resolve())])
    if args.output:
        command.extend(["--output", args.output])
    if not args.no_open:
        command.append("--open")
    return subprocess.run(command, cwd=workflow["cwd"], check=False).returncode


def cmd_automations(args) -> int:
    result = _run_loops(["list"])
    if result.returncode != 0:
        return fail((result.stderr or result.stdout).strip())
    try:
        rows = [row for row in json.loads(result.stdout) if "PI_WORKFLOWS_AUTOMATION=1" in str(row.get("action_payload", ""))]
    except (ValueError, TypeError, AttributeError):
        return fail("Loops returned malformed automation data")
    if args.json:
        out(json.dumps(rows, separators=(",", ":")))
    elif not rows:
        out("no Pi Workflow automations")
    else:
        for row in rows:
            out(f"{row['id']}  {row['status'].ljust(9)} {row['schedule_type']}={row['schedule_value']}  {row['name']}")
    return 0


def cmd_automation(args) -> int:
    result = _run_loops([args.action, args.id])
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return fail(output or f"Loops {args.action} failed")
    out(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piw",
        description="Drive deterministic workflows: list, inspect, validate, run, review.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text, workflow=True):
        node = sub.add_parser(name, help=help_text)
        if workflow:
            node.add_argument("workflow", help="workflow id, or any unique substring of it")
        node.add_argument("--json", action="store_true", help="machine-readable output")
        return node

    add("ls", "list workflows", workflow=False)

    add("schema", "show every workflow field, node kind, and runtime input", workflow=False)

    actions = add("actions", "list or inspect reusable action-node templates", workflow=False)
    actions.add_argument("action", nargs="?", help="action id to inspect")

    add_action = add("add", "expand a reusable action into ordinary workflow nodes")
    add_action.add_argument("action", help="action id from `piw actions`")
    add_action.add_argument("--id", help="runtime id or prefix (default: action id)")
    add_action.add_argument("--needs", help="comma-separated existing steps supplying the action input")

    add("doctor", "verify the standalone product and optional integrations", workflow=False)

    create = add("create", "scaffold a valid input-to-artifact workflow", workflow=False)
    create.add_argument("name")
    create.add_argument("--dir", help="target directory (default: ./<workflow-name>)")
    create.add_argument("--action", help="start from one reusable action instead of the generic two-step scaffold")
    create.add_argument("--model", default=os.environ.get("PI_WORKFLOWS_MODEL", "openai-codex/gpt-5.6-luna"))
    create.add_argument("--qa-model", default=os.environ.get("PI_WORKFLOWS_QA_MODEL", "openai-codex/gpt-5.6-terra"))
    create.add_argument("--thinking", choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"], default="low")
    create.add_argument("--workers", type=int, choices=range(1, 17), default=4)

    graph = add("graph", "print the DAG")
    graph.add_argument("-v", "--verbose", action="store_true", help="include per-step models")

    add("validate", "check the yaml without running it")

    run = add("run", "run a workflow and stream step results")
    run.add_argument("--node", action="append", help="force this step fresh (repeatable); upstream comes from cache")
    run.add_argument("--no-cache", action="store_true", help="bypass the cache entirely")
    run_input = run.add_mutually_exclusive_group()
    run_input.add_argument("--input", help="immutable text input for this run")
    run_input.add_argument("--input-file", help="immutable file input for this run")
    run.add_argument("-q", "--quiet", action="store_true", help="summary line only")
    run.add_argument("--timeout", type=float, default=3600, help="seconds to follow the run (default 3600)")

    ui = add("ui", "open the optional local graph studio")
    ui.add_argument("--input-file", help="prefill the immutable run input")
    ui.add_argument("--output", help="step whose artifact is shown after a run")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--no-open", action="store_true", help="serve without opening a browser")

    runs = add("runs", "list past runs")
    runs.add_argument("-n", "--limit", type=int, default=20)

    detail = add("detail", "full per-step breakdown of one run")
    detail.add_argument("run", nargs="?", help="run id (default: most recent)")
    detail.add_argument("--io", action="store_true", help="include full sent/received bodies")
    detail.add_argument("--all", action="store_true", help="include steps that never ran")

    batch = add("batch", "run the exact workflow across an isolated input corpus")
    batch.add_argument("--inputs", required=True, help="corpus: .jsonl, a directory, or a lines file")
    batch.add_argument("--input-file", default="input.txt", help="immutable per-item filename")
    batch.add_argument("--parallel", type=int, choices=range(1, 33), default=4,
                       help="concurrent items, separate from workflow workers")
    batch.add_argument("--limit", type=int)
    output = batch.add_mutually_exclusive_group()
    output.add_argument("--out", help="new batch directory")
    output.add_argument("--resume", help="resume a batch with the same graph and corpus")
    batch.add_argument("--require-all", action="store_true",
                       help="fail an item if any declared step is skipped")
    batch.add_argument("--stop-after-failures", type=int,
                       help="stop dispatching new items after N failures")
    batch.add_argument("--max-tokens", type=int,
                      help="stop dispatch after recorded attempt usage reaches N tokens")
    batch.add_argument("--max-cost", type=float,
                      help="stop dispatch after recorded attempt usage reaches this dollar cost")
    batch.add_argument("--output-step", help="export this step as ordered outputs.jsonl")
    batch.add_argument("--item-timeout", type=float, default=3600,
                       help="hard wall timeout per item (default 3600s)")
    batch.add_argument("--git-history", action="store_true",
                       help="retain per-step Git commits for every item")
    batch.add_argument("--allow-shared-workspace", action="store_true",
                       help="allow parallel agent/effect steps after independently ensuring concurrency safety")
    batch.add_argument("--detach", action="store_true",
                       help="run in the background and return a status command")

    batch_status = add("batch-status", "inspect a running or completed bulk job", workflow=False)
    batch_status.add_argument("batch", help="batch directory returned by piw batch --detach")

    batch_cancel = add("batch-cancel", "stop a detached bulk job", workflow=False)
    batch_cancel.add_argument("batch", help="batch directory returned by piw batch --detach")

    evaluate = add("eval", "compare models over a corpus (judges held fixed)")
    evaluate.add_argument("--inputs", required=True)
    evaluate.add_argument("--input-file", required=True)
    evaluate.add_argument("--models", required=True, help="comma-separated model ids")
    evaluate.add_argument("--parallel", type=int)
    evaluate.add_argument("--limit", type=int)

    reports = add("reports", "list batch and eval reports")
    reports.add_argument("-n", "--limit", type=int, default=10)
    reports.add_argument("--show", action="store_true", help="print the newest report")

    show = add("show", "print a step's artifact from a run")
    show.add_argument("step", nargs="?", help="step id (omit to list the run's files)")
    show.add_argument("--run", help="run id (default: most recent)")
    show.add_argument("--resolved", action="store_true",
                      help="print what was actually sent (templating applied) instead of the output")

    setter = add("set", "edit a step in steps.yaml (comments and formatting preserved)")
    setter.add_argument("step", help="step id")
    setter.add_argument("--model", help="model id, or '' to fall back to the workflow default")
    # "" is allowed so a step can be reverted to the workflow default, matching
    # the --model/--gate/--tools convention.
    setter.add_argument("--thinking", metavar="LEVEL",
                        choices=["", "off", "minimal", "low", "medium", "high", "xhigh", "max"],
                        help="off|minimal|low|medium|high|xhigh|max, or '' to clear")
    setter.add_argument("--gate", help="gate command, or '' to remove the gate")
    setter.add_argument("--tools", help="comma-separated tool allowlist, or '' to clear")
    # Strings, not ints: '' has to be accepted so a step can be reverted to the
    # workflow default, matching every other --flag here.
    setter.add_argument("--retries", metavar="N", help="attempts on failure; '' clears")
    setter.add_argument("--timeout", metavar="SECONDS", help="per-step timeout; '' clears")
    setter.add_argument("--retry-on", help="comma-separated eligible failure classes; '' clears")
    setter.add_argument("--retry-delay-seconds", help="base retry delay; '' clears")
    setter.add_argument("--retry-backoff", choices=["", "fixed", "exponential"], help="retry pacing; '' clears")
    setter.add_argument("--retry-max-delay-seconds", help="retry delay ceiling; '' clears")
    setter.add_argument("--retry-jitter", help="deterministic jitter fraction 0..1; '' clears")
    setter.add_argument("--prompt-file", help="replace the step's prompt with this file's contents")
    setter.add_argument("--when", metavar="JSON",
                        help='routing condition, e.g. \'{"op":"equals","path":"/kind","value":"bug"}\''
                             " (ops: equals not_equals greater_than less_than contains in exists"
                             " missing type_is, grouped by all/any/not); '' removes the guard")
    setter.add_argument("--from", dest="from_step", metavar="STEP",
                        help="which step's JSON output `when` reads; '' clears it")
    setter.add_argument("--schema", metavar="JSON",
                        help='output contract, e.g. \'{"kind":{"type":"string","enum":["bug","feature"]}}\''
                             " (types: string number integer boolean object array; optional: true); '' clears")
    setter.add_argument("--produces", metavar="PATHS",
                        help="comma-separated files to copy into the run dir; '' clears")

    stats = add("stats", "aggregate pass rate, cost and cache counters")
    stats.add_argument("-n", "--limit", type=int, default=50)

    path = add("path", "print the steps.yaml path")
    path.add_argument("-d", "--dir", action="store_true", help="print the working directory instead")

    schedule = add("schedule", "create a durable Loops automation for this workflow")
    schedule.add_argument("--id")
    schedule.add_argument("--name")
    schedule_group = schedule.add_mutually_exclusive_group(required=True)
    schedule_group.add_argument("--interval-minutes", type=int)
    schedule_group.add_argument("--daily", help="local time HH:MM")
    schedule.add_argument("--timeout", type=int, default=3600, help="per-run timeout in seconds")
    schedule.add_argument("--stop-after", type=int)

    add("automations", "list Pi Workflow automations", workflow=False)
    automation = add("automation", "control one Pi Workflow automation", workflow=False)
    automation.add_argument("action", choices=["show", "pause", "resume", "run", "delete"])
    automation.add_argument("id")

    return parser


COMMANDS = {
    "ls": cmd_ls, "schema": cmd_schema, "graph": cmd_graph, "validate": cmd_validate, "run": cmd_run,
    "actions": cmd_actions, "add": cmd_add_action,
    "ui": cmd_ui,
    "runs": cmd_runs, "show": cmd_show, "stats": cmd_stats, "path": cmd_path,
    "set": cmd_set, "detail": cmd_detail, "batch": cmd_batch,
    "batch-status": cmd_batch_status, "batch-cancel": cmd_batch_cancel, "eval": cmd_eval,
    "reports": cmd_reports, "doctor": cmd_doctor, "create": cmd_create,
    "schedule": cmd_schedule, "automations": cmd_automations,
    "automation": cmd_automation,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except RuntimeError as error:
        return fail(str(error))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
