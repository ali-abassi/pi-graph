"""Pi Workflows — turn a deterministic-workflow steps.yaml into a renderable DAG.

This module is the visual layer for the engine installed with this product at
``~/.pi-workflows/scripts/run_steps.py``.
It does not execute anything; it parses a ``steps.yaml`` into nodes and edges so
the canvas can draw exactly the graph the runner will execute.

The dependency rules below are a faithful port of ``run_steps.build_deps``. If
they drift, the canvas lies about what will run — so ``tests/test_runner_contracts.py`` pins
them against the real skill file.

Layout lives in a sidecar ``steps.layout.json`` next to the yaml; steps.yaml is
never written, so hand-authored comments and formatting are safe.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

# Mirrors run_steps.STEP_REF_RE exactly.
STEP_REF_RE = re.compile(r"\{step\.([A-Za-z0-9_-]+)\}")

LAYOUT_NAME = "steps.layout.json"
QA_NODE_ID = "__qa__"

# Canvas geometry (also used by the auto-layout).
COLUMN_WIDTH = 300
ROW_HEIGHT = 150
ORIGIN_X = 60
ORIGIN_Y = 60

# Longest prompt/cmd body we ship to the browser, so a pathological step can't
# blow up the /graph response.
MAX_BODY = 8000


class WorkflowParseError(ValueError):
    """steps.yaml is malformed or references a step that does not exist yet."""


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_BODY:
        return text, False
    return text[:MAX_BODY], True


def _step_kind(step: dict[str, Any]) -> str:
    """Classify a step by flavor.

    Mirrors the skill's "weakest that works" ladder: cmd (code, zero variance)
    -> completion (one isolated pi call) -> tooled (allowlisted tools) ->
    agent (full tool loop).
    """
    if step.get("cmd"):
        return "command"
    if step.get("agent"):
        return "agent"
    if step.get("tools"):
        return "tooled"
    if step.get("prompt"):
        return "completion"
    return "unknown"


# How much of a node's behavior is guaranteed rather than trusted. Drives the
# left rule on the node card: fixed=solid, pinned=half, open=dashed.
DETERMINISM = {
    "command": "fixed",
    "completion": "pinned",
    "tooled": "pinned",
    "agent": "open",
    "qa": "pinned",
    "unknown": "open",
}


def build_deps(steps: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Port of run_steps.build_deps.

    Returns (deps, implicit) where ``implicit[sid]`` is the subset of deps that
    came from the implicit previous-step rule rather than an explicit ``needs:``
    or a ``{step.x}`` reference. The canvas draws those edges differently,
    because an omitted ``needs:`` silently chaining steps is the format's
    sharpest edge.
    """
    ids = [s["id"] for s in steps]
    deps: dict[str, set[str]] = {}
    implicit: dict[str, set[str]] = {}

    for index, step in enumerate(steps):
        sid = step["id"]
        found: set[str] = set(STEP_REF_RE.findall(step.get("prompt", "") or ""))
        inferred: set[str] = set()

        if "needs" in step:
            found |= set(step.get("needs") or [])
        elif index:
            inferred.add(ids[index - 1])

        # {prev} pulls in the previous step's artifact regardless of needs.
        prompt = step.get("prompt") or ""
        if prompt and "{prev}" in prompt and index:
            inferred.add(ids[index - 1])

        found |= inferred
        unknown = found - set(ids[:index])
        if unknown:
            raise WorkflowParseError(
                f"step '{sid}': depends on {sorted(unknown)} which do not appear earlier"
            )

        deps[sid] = found
        implicit[sid] = inferred - set(step.get("needs") or [])

    return deps, implicit


def _depth_layout(node_ids: list[str], deps: dict[str, set[str]]) -> dict[str, dict[str, int]]:
    """Auto-layout: column = topological depth, row = order within that depth."""
    depth: dict[str, int] = {}
    for sid in node_ids:  # already topologically ordered by construction
        parents = deps.get(sid) or set()
        depth[sid] = max((depth.get(p, 0) for p in parents), default=-1) + 1

    rows: dict[int, int] = {}
    positions: dict[str, dict[str, int]] = {}
    for sid in node_ids:
        column = depth[sid]
        row = rows.get(column, 0)
        rows[column] = row + 1
        positions[sid] = {
            "x": ORIGIN_X + column * COLUMN_WIDTH,
            "y": ORIGIN_Y + row * ROW_HEIGHT,
        }
    return positions


def load_layout(steps_path: Path) -> dict[str, dict[str, int]]:
    path = steps_path.parent / LAYOUT_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}
    positions = data.get("positions")
    if not isinstance(positions, dict):
        return {}
    clean: dict[str, dict[str, int]] = {}
    for sid, point in positions.items():
        if isinstance(point, dict) and isinstance(point.get("x"), (int, float)):
            clean[str(sid)] = {"x": int(point["x"]), "y": int(point.get("y", 0))}
    return clean


def save_layout(steps_path: Path, positions: dict[str, Any]) -> None:
    """Persist node positions beside the yaml. Never touches steps.yaml."""
    clean: dict[str, dict[str, int]] = {}
    for sid, point in (positions or {}).items():
        if isinstance(point, dict) and isinstance(point.get("x"), (int, float)):
            clean[str(sid)] = {"x": int(point["x"]), "y": int(point.get("y", 0))}
    path = steps_path.parent / LAYOUT_NAME
    path.write_text(json.dumps({"version": 1, "positions": clean}, indent=1), encoding="utf-8")


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
LOG_LINE_RE = re.compile(r"^- (\d{2}:\d{2}:\d{2}) (.*)$")


def _read(path: Path, limit: int = MAX_BODY) -> tuple[str, bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    return _truncate(text) if len(text) > limit else (text, False)


def run_detail(steps_path: Path, run_dir: Path) -> dict[str, Any]:
    """Everything known about one run, assembled for a run-detail view.

    The runner already leaves a rich trail on disk — per-step output and stderr,
    a cost ledger, a timestamped log, rejected judge attempts, and a git commit
    per step — so this needs no engine changes; it just gathers it.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise WorkflowParseError(f"no such run: {run_dir}")

    graph = parse_steps(steps_path)
    by_id = {node["id"]: node for node in graph["nodes"]}

    ledger: dict[str, dict[str, Any]] = {}
    try:
        entries = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
        for entry in entries if isinstance(entries, list) else []:
            ledger[entry.get("id")] = entry
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    log_lines: list[dict[str, str]] = []
    log_text, _ = _read(run_dir / "log.md", 200_000)
    for line in log_text.splitlines():
        match = LOG_LINE_RE.match(line)
        if match:
            log_lines.append({"at": match.group(1), "text": match.group(2)})

    steps: list[dict[str, Any]] = []
    for node in graph["nodes"]:
        sid = node["id"]
        entry = ledger.get(sid) or {}
        output, output_cut = _read(run_dir / f"{sid}.md")
        stderr, _ = _read(run_dir / f"{sid}.stderr", 4000)

        # Rejected judge attempts are snapshotted as <id>.aN.md, the judge's own
        # report as <id>.judgeN.md — together they are the revise-loop evidence.
        attempts = []
        for number in range(1, 12):
            body, cut = _read(run_dir / f"{sid}.a{number}.md")
            report, _ = _read(run_dir / f"{sid}.judge{number}.md", 4000)
            if not body and not report:
                continue
            # The runner snapshots <id>.aN.md only for attempts that FAILED, so a
            # judge report with no snapshot belongs to the attempt that passed.
            attempts.append({
                "n": number, "output": body, "truncated": cut,
                "judge": report, "rejected": bool(body),
            })

        if sid == QA_NODE_ID:
            # QA is not a ledger step; its verdict lives in qa.md.
            verdict, _ = _read(run_dir / "qa.md", 20_000)
            status = ("passed" if '"verdict"' in verdict and '"pass"' in verdict
                      else "failed" if verdict.strip() else "not_run")
        elif sid in ledger:
            status = "cached" if entry.get("cached") else ("passed" if entry.get("passed") else "failed")
        elif (run_dir / f"{sid}.md").exists():
            status = "passed"
        else:
            status = "not_run"

        try:
            resolved = resolve_prompt(steps_path, run_dir, sid)["resolved"] if not node.get("synthetic") else ""
        except (WorkflowParseError, OSError):
            resolved = ""

        steps.append({
            "id": sid,
            "kind": node["kind"],
            "determinism": node["determinism"],
            "model": entry.get("model") or node.get("model"),
            "status": status,
            "cached": bool(entry.get("cached")),
            "attempts": entry.get("attempts"),
            "seconds": entry.get("seconds"),
            "cost": entry.get("cost", 0.0),
            "tokens_in": entry.get("input", 0),
            "tokens_out": entry.get("output", 0),
            "tokens": entry.get("total", 0),
            "gate": node.get("gate"),
            "judge": node.get("judge"),
            "sent": resolved,
            "output": output,
            "output_truncated": output_cut,
            "stderr": stderr,
            "judge_attempts": attempts,
            "previews": _previews(node, run_dir, steps_path.parent),
            # Recorded by the runner so inspection can say why a node failed
            # instead of leaving the reason in log.md.
            "failure": entry.get("failure", ""),
            "failure_kind": entry.get("failure_kind", ""),
        })

    qa_text, _ = _read(run_dir / "qa.md", 20_000)
    commits = []
    try:
        result = subprocess.run(
            ["git", "-C", str(run_dir), "log", "--format=%h\x1f%s"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for line in result.stdout.splitlines():
            sha, _, message = line.partition("\x1f")
            if sha:
                commits.append({"sha": sha, "message": message})
    except (OSError, subprocess.SubprocessError):
        pass

    total_cost = sum(float(e.get("cost") or 0) for e in ledger.values())
    counts: dict[str, int] = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1

    return {
        "run": {
            "id": run_dir.name,
            "path": str(run_dir),
            "workflow": graph["workflow"],
            "started_at": log_lines[0]["at"] if log_lines else "",
            "finished_at": log_lines[-1]["at"] if log_lines else "",
            "seconds": round(sum(float(e.get("seconds") or 0) for e in ledger.values()), 1),
            "cost": round(total_cost, 6),
            "tokens": sum(int(e.get("total") or 0) for e in ledger.values()),
            "counts": counts,
            "ok": counts.get("failed", 0) == 0 and bool(ledger),
            "complete": bool(ledger),
        },
        "steps": steps,
        "log": log_lines,
        "qa": qa_text or None,
        "commits": commits,
    }


def _previews(node: dict[str, Any], run_dir: Path, workflow_dir: Path) -> list[str]:
    """Image artifacts to show for a step.

    A step may declare `preview:` in steps.yaml (a UI-only key the runner
    ignores); otherwise any image the step's output text points at is used, since
    generated media usually lands in the workflow directory rather than the run
    directory.
    """
    candidates: list[str] = []
    declared = node.get("preview")
    if isinstance(declared, str):
        candidates.append(declared)
    elif isinstance(declared, list):
        candidates.extend(str(item) for item in declared)

    found: list[str] = []
    for candidate in candidates:
        for base in (run_dir, workflow_dir):
            path = (base / candidate).resolve()
            try:
                path.relative_to(base.resolve())
            except ValueError:
                continue
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                found.append(str(path))
                break
    return found


def _when_source(step: dict[str, Any], dep_ids: set[str]) -> str | None:
    """Which step a `when:` reads, mirroring run_steps.evaluate_when exactly.

    Explicit `from:` wins; then a single declared `needs:`; then a single
    inferred dependency. A prompt that inlines {step.x} adds a data dependency
    which must not make the routing source ambiguous.
    """
    if step.get("from"):
        return str(step["from"])
    declared = [str(item) for item in (step.get("needs") or [])]
    candidates = declared if len(declared) == 1 else sorted(dep_ids)
    return candidates[0] if len(candidates) == 1 else None


_OP_WORDS = {
    "equals": "==", "not_equals": "!=", "less_than": "<", "less_than_or_equal": "<=",
    "greater_than": ">", "greater_than_or_equal": ">=", "contains": "contains",
    "in": "in", "exists": "exists", "missing": "is missing", "type_is": "is a",
}


def describe_condition(node: Any, depth: int = 0) -> str:
    """Render a `when:` condition as a readable one-liner for the canvas.

    `{"op":"equals","path":"/kind","value":"docs"}` -> `kind == "docs"`.
    """
    if not isinstance(node, dict) or "op" not in node:
        return "invalid condition"
    op = node["op"]
    if op in ("all", "any"):
        clauses = node.get("of") or []
        joiner = " and " if op == "all" else " or "
        body = joiner.join(describe_condition(clause, depth + 1) for clause in clauses)
        return f"({body})" if depth and len(clauses) > 1 else body
    if op == "not":
        inner = node.get("of")
        if isinstance(inner, list):
            inner = inner[0] if inner else None
        return f"not {describe_condition(inner, depth + 1)}"

    field = str(node.get("path", "")).lstrip("/").replace("/", ".") or "value"
    word = _OP_WORDS.get(op, op)
    if op in ("exists", "missing"):
        return f"{field} {word}"
    value = node.get("value")
    rendered = f'"{value}"' if isinstance(value, str) else json.dumps(value)
    return f"{field} {word} {rendered}"


def _sequence_offset(text: str) -> int:
    """How far this file indents list items under `steps:` (commonly 0 or 2)."""
    in_steps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("steps:"):
            in_steps = True
            continue
        if in_steps and stripped.startswith("- "):
            return len(line) - len(line.lstrip())
        if in_steps and stripped and not line.startswith((" ", "\t", "-")):
            break  # left the steps block without finding an item
    return 2


def resolve_prompt(steps_path: Path, run_dir: Path, step_id: str) -> dict[str, Any]:
    """Reproduce the exact text the runner sent for this step in a given run.

    Mirrors run_steps.render_prompt: {run} -> the run dir, {step.x} -> that
    step's artifact, {prev} -> the previous listed step's artifact. Unlike the
    runner this never raises on a missing artifact — a step that has not run yet
    still shows its template with a clear marker, which is the point of being
    able to inspect a node before paying to run it.
    """
    spec = yaml.safe_load(steps_path.read_text(encoding="utf-8")) or {}
    steps = spec.get("steps") or []
    ids = [s.get("id") for s in steps]
    if step_id not in ids:
        raise WorkflowParseError(f"unknown step: {step_id}")

    index = ids.index(step_id)
    step = steps[index]
    template = step.get("prompt") or step.get("cmd") or ""
    missing: list[str] = []

    def artifact(name: str) -> str | None:
        path = run_dir / f"{name}.md"
        if not path.is_file():
            missing.append(name)
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            missing.append(name)
            return None

    text = str(template).replace("{run}", str(run_dir))

    def step_ref(match: re.Match) -> str:
        name = match.group(1)
        body = artifact(name)
        return body if body is not None else f"<{{step.{name}}} not produced yet>"

    text = STEP_REF_RE.sub(step_ref, text)
    if "{prev}" in text:
        prev_id = ids[index - 1] if index else None
        body = artifact(prev_id) if prev_id else ""
        text = text.replace("{prev}", body if body is not None else "<{prev} not produced yet>")

    body, truncated = _truncate(text)
    return {
        "id": step_id,
        "resolved": body,
        "truncated": truncated,
        "missing": sorted(set(missing)),
        "is_command": bool(step.get("cmd")),
    }


# Fields the canvas may edit. Everything else in steps.yaml stays hand-authored;
# this is a deliberately small surface so the file remains the source of truth.
EDITABLE = {
    "model", "thinking", "prompt", "cmd", "gate", "tools", "retries", "timeout",
    "retry_on", "retry_delay_seconds", "retry_backoff", "retry_max_delay_seconds", "retry_jitter",
    # Routing and declared outputs are editable too, so an agent can build a
    # branching workflow without hand-writing yaml.
    "when", "from", "produces",
    # A step's output contract, so an agent can act on validate's advice.
    "schema", "judge",
}


def update_step(steps_path: Path, step_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Patch one step in steps.yaml, preserving comments, order and formatting.

    Uses ruamel's round-trip loader so a hand-authored file survives an edit from
    the canvas. Setting a value to None or "" removes the key, so a step can fall
    back to the workflow default (e.g. clearing a per-step model).
    """
    try:
        from ruamel.yaml import YAML
    except ImportError as error:  # pragma: no cover - depends on environment
        raise WorkflowParseError(
            "editing needs ruamel.yaml (pip install ruamel.yaml); steps.yaml left untouched"
        ) from error

    unknown = set(changes) - EDITABLE
    if unknown:
        raise WorkflowParseError(f"not editable: {', '.join(sorted(unknown))}")

    original = steps_path.read_text(encoding="utf-8")

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    # Keep long prompts as readable block scalars rather than one folded line.
    yaml_rt.width = 4096
    # Match the file's own list indentation instead of imposing ruamel's default,
    # which would reflow every step in a hand-authored workflow.
    offset = _sequence_offset(original)
    yaml_rt.indent(mapping=2, sequence=offset + 2, offset=offset)

    document = yaml_rt.load(original)
    if not document or "steps" not in document:
        raise WorkflowParseError("steps.yaml has no steps")

    target = next((s for s in document["steps"] if s.get("id") == step_id), None)
    if target is None:
        raise WorkflowParseError(f"unknown step: {step_id}")

    # A step must keep exactly one of cmd/prompt, which the runner enforces at load.
    for key, value in changes.items():
        if value in (None, ""):
            target.pop(key, None)
            continue
        if key in ("retries", "timeout"):
            coerced: Any = int(value)
        elif key in ("retry_delay_seconds", "retry_max_delay_seconds", "retry_jitter"):
            coerced = float(value)
        elif key == "prompt":
            from ruamel.yaml.scalarstring import LiteralScalarString
            text = str(value)
            coerced = LiteralScalarString(text if text.endswith("\n") else text + "\n")
        else:
            coerced = value

        if key in target:
            target[key] = coerced
        else:
            # Appending would land the key after any trailing blank line or
            # comment attached to the last key, visually detaching it from its
            # step. Insert straight after `id` instead.
            keys = list(target.keys())
            position = keys.index("id") + 1 if "id" in keys else 0
            target.insert(position, key, coerced)

    if bool(target.get("cmd")) == bool(target.get("prompt")):
        raise WorkflowParseError(
            f"step '{step_id}' must have exactly one of cmd or prompt after the edit"
        )

    # Write via a temp file so a crash mid-write cannot truncate the workflow.
    temp = steps_path.with_suffix(steps_path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        yaml_rt.dump(document, handle)
    temp.replace(steps_path)
    return {"id": step_id, "changed": sorted(changes)}


def append_steps(steps_path: Path, steps: list[dict[str, Any]]) -> None:
    """Append already-validated ordinary nodes while preserving the YAML file."""
    try:
        from ruamel.yaml import YAML
    except ImportError as error:  # pragma: no cover - depends on environment
        raise WorkflowParseError(
            "editing needs ruamel.yaml (pip install ruamel.yaml); steps.yaml left untouched"
        ) from error
    if not steps:
        raise WorkflowParseError("no action steps to append")

    original = steps_path.read_text(encoding="utf-8")
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096
    offset = _sequence_offset(original)
    yaml_rt.indent(mapping=2, sequence=offset + 2, offset=offset)
    document = yaml_rt.load(original)
    if not document or not isinstance(document.get("steps"), list):
        raise WorkflowParseError("steps.yaml has no steps")
    for step in steps:
        document["steps"].append(step)

    temp = steps_path.with_suffix(steps_path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        yaml_rt.dump(document, handle)
    temp.replace(steps_path)


def parse_steps(steps_path: Path) -> dict[str, Any]:
    """Parse a steps.yaml into {nodes, edges} for the canvas."""
    steps_path = Path(steps_path)
    try:
        spec = yaml.safe_load(steps_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise WorkflowParseError(f"invalid yaml: {error}") from error
    if not isinstance(spec, dict):
        raise WorkflowParseError("steps.yaml must be a mapping")

    version = spec.get("version", 1)
    if version != 1:
        raise WorkflowParseError(f"unsupported workflow version: {version!r} (expected 1)")
    workers = spec.get("workers", 4)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise WorkflowParseError("workers must be an integer from 1 to 16")

    raw_steps = spec.get("steps") or []
    if not isinstance(raw_steps, list):
        raise WorkflowParseError("'steps' must be a list")
    if not raw_steps:
        raise WorkflowParseError("steps.yaml has no steps")

    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_steps:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise WorkflowParseError("every step needs an 'id'")
        sid = str(entry["id"])
        if bool(entry.get("cmd")) == bool(entry.get("prompt")):
            raise WorkflowParseError(f"step '{sid}' must have exactly one of cmd or prompt")
        if entry.get("cmd") and entry.get("agent"):
            raise WorkflowParseError(f"step '{sid}': agent applies to prompt steps only")
        if "needs" in entry and (
            not isinstance(entry["needs"], list)
            or not all(isinstance(value, str) and value for value in entry["needs"])
        ):
            raise WorkflowParseError(f"step '{sid}': needs must be a list of earlier step ids")
        if sid in seen:
            raise WorkflowParseError(f"duplicate step id: {sid}")
        seen.add(sid)
        steps.append(entry)

    deps, implicit = build_deps(steps)

    default_model = spec.get("model")
    default_thinking = spec.get("thinking", "medium")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for step in steps:
        sid = step["id"]
        kind = _step_kind(step)
        body_source = step.get("cmd") if kind == "command" else step.get("prompt")
        body, truncated = _truncate(str(body_source or ""))

        judge = step.get("judge") if isinstance(step.get("judge"), dict) else None
        condition = step.get("when")
        node: dict[str, Any] = {
            "id": sid,
            "kind": kind,
            "determinism": DETERMINISM.get(kind, "open"),
            # A guarded step routes: it runs only when its condition holds.
            "when": condition,
            "when_from": _when_source(step, deps[sid]) if condition else None,
            "when_text": describe_condition(condition) if condition else None,
            "produces": ([step["produces"]] if isinstance(step.get("produces"), str)
                         else list(step.get("produces") or [])),
            # The step's declared output contract, if any.
            "schema": step.get("schema") if isinstance(step.get("schema"), dict) else None,
            # cmd steps never call a model, so they never carry one.
            "model": None if kind == "command" else (step.get("model") or default_model),
            "thinking": None if kind == "command" else step.get("thinking", default_thinking),
            "needs": sorted(deps[sid]),
            "explicit_needs": list(step.get("needs") or []) if "needs" in step else None,
            "body": body,
            "body_truncated": truncated,
            "gate": step.get("gate"),
            "judge": (
                {
                    "score": judge.get("score"),
                    "max_iters": judge.get("max_iters", 3),
                    "model": judge.get("model") or default_model,
                    "keep_best": bool(judge.get("keep_best")),
                }
                if judge
                else None
            ),
            "retries": step.get("retries"),
            "timeout": step.get("timeout"),
            "tools": step.get("tools"),
        }
        nodes.append(node)

        for parent in sorted(deps[sid]):
            # The edge carrying the routing decision is drawn as a labelled
            # conditional branch rather than a plain dependency.
            routes = bool(condition) and parent == node["when_from"]
            edges.append(
                {
                    "id": f"e-{parent}-{sid}",
                    "source": parent,
                    "target": sid,
                    "implicit": parent in implicit[sid],
                    "conditional": routes,
                    "label": node["when_text"] if routes else None,
                }
            )

    # Top-level qa: runs after the last step over {artifacts}, so it hangs off
    # every terminal node.
    qa = spec.get("qa") if isinstance(spec.get("qa"), dict) else None
    if qa and steps:
        has_children = {edge["source"] for edge in edges}
        terminals = [step["id"] for step in steps if step["id"] not in has_children]
        qa_body, qa_truncated = _truncate(str(qa.get("prompt") or ""))
        nodes.append(
            {
                "id": QA_NODE_ID,
                "kind": "qa",
                "determinism": DETERMINISM["qa"],
                "model": qa.get("model") or default_model,
                "thinking": qa.get("thinking", default_thinking),
                "needs": terminals,
                "explicit_needs": None,
                "when": None,
                "when_from": None,
                "when_text": None,
                "produces": [],
                "schema": None,
                "body": qa_body,
                "body_truncated": qa_truncated,
                "gate": None,
                "judge": None,
                "retries": None,
                "timeout": qa.get("timeout"),
                "tools": qa.get("tools"),
                "synthetic": True,
            }
        )
        for parent in terminals:
            edges.append(
                {
                    "id": f"e-{parent}-{QA_NODE_ID}",
                    "source": parent,
                    "target": QA_NODE_ID,
                    "implicit": False,
                    "conditional": False,
                    "label": None,
                }
            )

    node_ids = [node["id"] for node in nodes]
    layout_deps = {node["id"]: set(node["needs"]) for node in nodes}
    auto = _depth_layout(node_ids, layout_deps)
    saved = load_layout(steps_path)
    for node in nodes:
        point = saved.get(node["id"]) or auto[node["id"]]
        node["x"] = point["x"]
        node["y"] = point["y"]

    return {
        "workflow": spec.get("workflow") or steps_path.parent.name,
        "path": str(steps_path),
        "cwd": str(steps_path.parent),
        "model": default_model,
        "thinking": default_thinking,
        "workers": workers,
        "input": spec.get("input") if isinstance(spec.get("input"), dict) else None,
        "has_qa": bool(qa),
        "layout_source": "sidecar" if saved else "auto",
        "nodes": nodes,
        "edges": edges,
    }
