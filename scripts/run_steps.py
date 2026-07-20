#!/usr/bin/env python3
"""Lean deterministic step runner — DAG edition.

Code owns the loop: one model call (or shell command) per step, a mechanical
gate decides pass/fail, an optional judge loop iterates toward a score
threshold, an optional QA pass independently validates the finished run.
Steps run as a dataflow DAG (a step fires the moment its dependencies are
done), passing model calls are content-cached, and every run emits a
token/cost ledger. No compile/certify/seal ceremony.

Usage:
  python3 run_steps.py steps.yaml
  python3 run_steps.py steps.yaml --from build --run-dir runs/<dir>
  python3 run_steps.py steps.yaml --verify --run-dir runs/<dir>
  python3 run_steps.py steps.yaml --regen personas,copy-home   # force those nodes fresh
  python3 run_steps.py steps.yaml --no-cache

steps.yaml:
  workflow: my-flow
  model: openai-codex/gpt-5.6-sol      # default model for steps
  thinking: medium                      # default thinking level
  workers: 4                            # max parallel steps (default 4)
  system: |                             # chain-hygiene system prompt (steps only;
    ...                                 # judges/qa never inherit; step system: "" opts out)
  cwd: .
  qa: { model: ..., prompt: ... {artifacts} ... }   # independent reviewer
  steps:
    - id: fetch
      cmd: ./scripts/fetch.sh > "$OUT"  # pure code step; env OUT, RUN, STEP
    - id: extract
      needs: [fetch]                    # explicit deps; {step.x} refs are added
      prompt: |                         # automatically; no needs key -> implicit
        {step.fetch} {prev} {run}       # dep on the previous listed step
      gate: test -s "$OUT"
      retries: 1
    - id: draft
      needs: [extract]
      prompt: ...
      judge: { model: ..., prompt: ... {out} ..., score: 8.5, max_iters: 3, keep_best: false }

Dependency rules:
  - `needs:` present -> exactly those (plus {step.x} refs in the prompt).
  - `needs:` absent  -> implicit dependency on the previous listed step.
  - `needs: []`      -> root (plus {step.x} refs).
  - deps must appear earlier in the list; cmd steps that read "$RUN/<id>.md"
    must declare that id in needs (the runner cannot see inside shell).

Cache: passing prompt-step outputs are cached in <yaml-dir>/cache/ keyed by
sha256(model|thinking|system|tools|agent|rendered prompt). A hit skips the
model call AND judge (gate still re-runs). Upstream changes alter the rendered
prompt -> automatic downstream invalidation. cmd steps are never cached.
--regen ids forces fresh generation for those steps this run.

Ledger: runs/<dir>/ledger.json + a table in log.md — per step: seconds,
tokens, real cost, cache status. A model response whose final stopReason is
not "stop" (aborted/error/truncated) fails the step even when pi exits 0.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "workflow.schema.json"

PI_BASE = [
    "pi", "-p", "--mode", "json", "--no-session",
    "--no-approve", "--offline",
    "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
]
SCORE_RE = re.compile(r'"score"\s*:\s*([0-9.]+)')
VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(pass|fail)"')
STEP_REF_RE = re.compile(r"\{step\.([A-Za-z0-9_-]+)\}")
PLACEHOLDER_RE = re.compile(r"\{(run|input|prev|step\.[A-Za-z0-9_-]+)\}")
LOG_LOCK = threading.Lock()


def validate_workflow_contract(spec: object, steps_file: Path) -> dict:
    """Fail closed on the published authoring contract before any node runs."""
    if not isinstance(spec, dict):
        raise SystemExit("workflow must be a YAML object")
    try:
        from jsonschema import Draft202012Validator

        contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError) as error:
        raise SystemExit(f"workflow schema validator unavailable: {error}") from error
    errors = sorted(
        Draft202012Validator(contract).iter_errors(spec),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        message = error.message
        if error.validator == "pattern":
            message = f"{error.instance!r} must match {error.validator_value!r}"
        raise SystemExit(
            f"workflow schema invalid at {location}: {message}\n"
            f"next: piw validate {steps_file}"
        )
    return spec


def log(run_dir: Path, line: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    entry = f"- {stamp} {line}"
    with LOG_LOCK:
        print(entry, flush=True)
        with (run_dir / "log.md").open("a") as fh:
            fh.write(entry + "\n")


# Optional structured event stream (--events <path>). Off unless requested, so
# existing callers are unaffected; log.md and the ledger are untouched either
# way. Consumers tail the file to follow a run step by step.
GIT_TIMEOUT_SECONDS = 20
HISTORY_ENABLED = True

EVENTS_LOCK = threading.Lock()
EVENTS_PATH: Path | None = None


def emit(kind: str, **fields) -> None:
    if EVENTS_PATH is None:
        return
    record = {"t": kind, "ts": time.time(), **fields}
    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
        with EVENTS_LOCK:
            with EVENTS_PATH.open("a") as fh:
                fh.write(line + "\n")
                fh.flush()
    except (OSError, TypeError, ValueError):
        pass  # telemetry must never take down a run


def parse_pi_events(stdout: str) -> tuple[str, dict, bool, str, str | None]:
    """Validate Pi JSONL and return text, usage, protocol status, detail, model.

    Records are split on "\\n" ONLY, per the pi JSONL contract
    (https://pi.dev/docs/latest/json). str.splitlines() also breaks on valid
    Unicode separators inside model output. Every non-empty line must be a Pi
    event, and completion requires agent_settled after the final assistant
    message so an automatic retry, compaction retry, or continuation cannot be
    mistaken for a finished node.
    """
    text = ""
    usage = {"input": 0, "output": 0, "total": 0, "cost": 0.0}
    events: list[dict] = []
    assistant_indices: list[int] = []
    assistants: list[dict] = []
    for line_number, line in enumerate(stdout.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError) as error:
            return text, usage, False, f"malformed Pi JSON event at line {line_number}: {error}", None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return text, usage, False, f"invalid Pi JSON event at line {line_number}", None
        events.append(event)
        if event.get("type") != "message_end":
            continue
        msg = event.get("message") or {}
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        assistant_indices.append(len(events) - 1)
        assistants.append(msg)
        u = msg.get("usage") or {}
        usage["input"] += u.get("input", 0)
        usage["output"] += u.get("output", 0)
        usage["total"] += u.get("totalTokens", 0)
        usage["cost"] += float(((u.get("cost") or {}).get("total")) or 0)
        blocks = [
            block.get("text", "") for block in (msg.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if blocks:
            text = "\n".join(blocks)
    if not assistants:
        return text, usage, False, "Pi JSON stream contained no assistant message_end", None
    if any(event.get("type") == "extension_error" for event in events):
        return text, usage, False, "Pi JSON stream contained extension_error", None
    failed_retries = [
        event for event in events
        if event.get("type") == "auto_retry_end" and event.get("success") is False
    ]
    if failed_retries:
        return text, usage, False, "Pi automatic retry exhausted", None
    settled = [index for index, event in enumerate(events) if event.get("type") == "agent_settled"]
    if not settled or settled[-1] < assistant_indices[-1]:
        return text, usage, False, "Pi JSON stream did not settle after the final assistant message", None
    final = assistants[-1]
    stop = final.get("stopReason")
    if stop != "stop" or final.get("errorMessage"):
        return text, usage, False, f"Pi final stopReason was {stop!r}", None
    provider, model = final.get("provider"), final.get("model")
    actual_model = f"{provider}/{model}" if provider and model else None
    return text, usage, True, "", actual_model


def call_pi(cfg: dict, spec: dict, prompt: str, cwd: Path) -> tuple[str, dict, bool, str, int]:
    """One pi invocation -> (text, usage, ok, detail, exit_code).
    Flavors: completion (default, isolated) / tools allowlist / agent: true
    (full default toolset + repo context files)."""
    agent = bool(cfg.get("agent"))
    cmd = list(PI_BASE)
    if not agent:
        cmd.append("--no-context-files")
    model = cfg.get("model", spec.get("model"))
    if not model:
        raise SystemExit(f"step '{cfg.get('id', 'judge/qa')}': no model (set model here or top-level)")
    cmd += ["--model", model, "--thinking", str(cfg.get("thinking", spec.get("thinking", "medium")))]
    system = cfg["system"] if "system" in cfg else (spec.get("system") if "id" in cfg else None)
    if system and not agent:
        cmd += ["--system-prompt", system]
    tools = cfg.get("tools")
    if tools:
        cmd += ["--tools", tools]
    elif not agent:
        cmd.append("--no-tools")
    cmd.append(prompt)
    limit = cfg.get("timeout", 1800 if agent else 900)
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True,
                              timeout=limit, check=False)
    except subprocess.TimeoutExpired:
        # A slow or hung provider call is a failed ATTEMPT, not a dead step.
        # This used to escape past the retry loop and be caught by the generic
        # handler in main(), so a step with retries: 2 died on the first
        # timeout having consumed none of them.
        return "", {"input": 0, "output": 0, "total": 0, "cost": 0.0}, False, (
            f"pi call exceeded {limit}s (no response). Raise `timeout:` on this "
            f"step if the work legitimately takes longer."
        ), 124
    text, usage, protocol_ok, protocol_detail, actual_model = parse_pi_events(proc.stdout)
    if proc.returncode != 0:
        return text, usage, False, f"pi exit {proc.returncode}: {proc.stderr[-1500:]}", proc.returncode
    if not protocol_ok:
        return text, usage, False, protocol_detail, 0
    if "/" in model and actual_model != model:
        return text, usage, False, f"Pi model pin drifted: expected {model}, received {actual_model or 'missing'}", 0
    if not text.strip():
        return text, usage, False, "empty output", 0
    return text, usage, True, "", 0


# Shell steps run in their own process group so a timeout can kill the whole
# tree. That also detaches them from the group `piw batch-cancel` terminates,
# so the runner has to forward termination to its children itself.
ACTIVE_GROUPS: set[int] = set()
ACTIVE_GROUPS_LOCK = threading.Lock()


def _kill_group(pgid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_children() -> None:
    with ACTIVE_GROUPS_LOCK:
        groups = sorted(ACTIVE_GROUPS)
    for pgid in groups:
        _kill_group(pgid, signal.SIGTERM)
    if groups:
        time.sleep(0.2)
        for pgid in groups:
            _kill_group(pgid, signal.SIGKILL)


class ShellTimeout(Exception):
    """A `cmd:`/`gate:` step exceeded its wall clock.

    This used to escape run_step entirely: retries were never consumed, no
    ledger entry was written, and graph.run_detail then fell back to
    "artifact exists => passed", so `piw detail` reported a timed-out step as
    PASSED and exited 0 while the runner exited 1. Carry it as a normal
    failure instead.
    """

    def __init__(self, limit: int, stderr: str = "") -> None:
        super().__init__(f"exceeded {limit}s")
        self.limit, self.stderr = limit, stderr


def run_shell(command: str, out_file: Path, run_dir: Path, step_id: str,
              cwd: Path, timeout: int = 900,
              workflow_dir: Path | None = None) -> subprocess.CompletedProcess:
    input_path = run_dir / "input.txt"
    env = {
        **os.environ,
        "OUT": str(out_file),
        "RUN": str(run_dir),
        "STEP": step_id,
        "INPUT": str(input_path),
        "PI_WORKFLOWS_INPUT": str(input_path),
        # Under `piw batch` the cwd is a per-item scratch workspace, so a step
        # that needs an asset shipped beside steps.yaml must address it here
        # rather than relatively.
        "WORKFLOW_DIR": str(workflow_dir or cwd),
    }
    # Own process group: on timeout, kill the whole tree. Without this a
    # backgrounded child outlives the runner.
    proc = subprocess.Popen(["bash", "-c", command], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = proc.pid
    with ACTIVE_GROUPS_LOCK:
        ACTIVE_GROUPS.add(pgid)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                out, err = proc.communicate()
        except (ProcessLookupError, PermissionError):
            out, err = "", ""
        raise ShellTimeout(timeout, (err or "")[-1500:])
    finally:
        with ACTIVE_GROUPS_LOCK:
            ACTIVE_GROUPS.discard(pgid)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _gate_ok(gate: str, out_file: Path, run_dir: Path, step_id: str,
             cwd: Path, workflow_dir: Path | None = None,
             timeout: int = 300) -> bool:
    """A gate that hangs is a failed gate, not a crashed run."""
    try:
        return run_shell(gate, out_file, run_dir, step_id, cwd, timeout,
                         workflow_dir).returncode == 0
    except ShellTimeout:
        return False


def render_prompt(template: str, run_dir: Path, prev_out: Path | None) -> str:
    """Substitute placeholders in ONE pass over the template.

    Substituting sequentially re-scanned already-inserted text, so a
    `{step.other}` sitting inside untrusted run input was expanded — inlining a
    sibling artifact the node never declared a dependency on and shipping it to
    the provider. `re.sub` never re-scans its own replacements, so one pass with
    a single alternation closes that. The untrusted fence is also neutralised in
    the inserted body so input cannot close it early.
    """
    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "run":
            return str(run_dir)
        if token == "input":
            input_path = run_dir / "input.txt"
            if not input_path.is_file():
                raise RuntimeError("prompt uses {input}, but this run has no --input or --input-file")
            body = input_path.read_text(encoding="utf-8", errors="replace")
            body = body.replace("</workflow-input>", "<\\/workflow-input>")
            return ('<workflow-input semantics="untrusted-data">\n'
                    + body + "\n</workflow-input>")
        if token == "prev":
            return prev_out.read_text() if prev_out and prev_out.exists() else ""
        name = token.split(".", 1)[1]
        artifact = run_dir / f"{name}.md"
        if not artifact.exists():
            raise RuntimeError(f"prompt references {{step.{name}}} but {artifact} does not exist")
        return artifact.read_text()

    return PLACEHOLDER_RE.sub(replace, template)


def cache_key(cfg: dict, spec: dict, prompt: str) -> str:
    """Fingerprint everything a cache hit skips.

    A hit re-runs the gate but skips the model call, the schema check, and the
    judge. So `judge` and `schema` MUST be part of the key: without them,
    raising a judge threshold (or tightening a schema) leaves the old artifact
    cached and the step passes a bar it was never held to.
    """
    parts = [str(cfg.get("model", spec.get("model"))),
             str(cfg.get("thinking", spec.get("thinking", "medium"))),
             str(cfg["system"] if "system" in cfg else spec.get("system", "")),
             str(cfg.get("tools", "")), str(bool(cfg.get("agent"))), prompt,
             json.dumps(cfg.get("judge"), sort_keys=True, default=str),
             json.dumps(cfg.get("schema"), sort_keys=True, default=str)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def retry_delay(step: dict, step_id: str, failed_attempt: int) -> float:
    """Return a bounded, replay-stable retry delay for one failed attempt."""
    base = float(step.get("retry_delay_seconds", 0))
    if step.get("retry_backoff", "fixed") == "exponential":
        base *= 2 ** max(0, failed_attempt - 1)
    jitter = float(step.get("retry_jitter", 0))
    if base and jitter:
        digest = hashlib.sha256(f"{step_id}:{failed_attempt}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        base *= 1 + ((unit * 2) - 1) * jitter
    return max(0.0, min(base, float(step.get("retry_max_delay_seconds", 300))))


def run_judge(judge: dict, spec: dict, candidate: str, run_dir: Path, sid: str,
              iteration: int, cwd: Path, usage_acc: dict) -> tuple[float | None, str]:
    # One pass: expanding {out} first meant candidate text containing {run}
    # was then itself expanded into the judge prompt.
    prompt = re.sub(r"\{(out|run)\}",
                    lambda m: candidate if m.group(1) == "out" else str(run_dir),
                    judge["prompt"])
    text, usage, ok, detail, _ = call_pi(judge, spec, prompt, cwd)
    for k in ("input", "output", "total"):
        usage_acc[k] += usage[k]
    usage_acc["cost"] += usage["cost"]
    (run_dir / f"{sid}.judge{iteration}.md").write_text(text or detail)
    match = SCORE_RE.search(text)
    score = float(match.group(1)) if (ok and match) else None
    return score, text or detail


def run_qa(spec: dict, ids: list[str], run_dir: Path, cwd: Path) -> tuple[bool, dict, float]:
    started = time.monotonic()
    qa = spec["qa"]
    parts = []
    for sid in ids:
        artifact = run_dir / f"{sid}.md"
        body = artifact.read_text() if artifact.exists() else "(missing artifact)"
        parts.append(f"### step: {sid}\n{body}")
    prompt = (qa["prompt"].replace("{run}", str(run_dir))
              .replace("{artifacts}", "\n\n".join(parts)))
    text, usage, ok, detail, _ = call_pi(qa, spec, prompt, cwd)
    (run_dir / "qa.md").write_text(text or detail)
    match = VERDICT_RE.search(text) if ok else None
    if not match:
        log(run_dir, f'QA: FAIL — {detail or "judge output unparseable"}')
        return False, usage, round(time.monotonic() - started, 1)
    verdict = match.group(1)
    log(run_dir, f"QA: verdict {verdict} · {usage['total']} tok ${usage['cost']:.4f} · report in {run_dir / 'qa.md'}")
    return verdict == "pass", usage, round(time.monotonic() - started, 1)


def verify_run(spec: dict, steps: list[dict], run_dir: Path, cwd: Path) -> int:
    git_commit(run_dir, "operator edits before verify")
    failures: list[str] = []
    for step in steps:
        sid = step["id"]
        out_file = run_dir / f"{sid}.md"
        if not out_file.exists():
            failures.append(f"{sid}: missing artifact {out_file.name}")
            continue
        gate = step.get("gate")
        if gate:
            try:
                g = run_shell(gate, out_file, run_dir, sid, cwd, 300)
                rc, detail = g.returncode, (g.stdout + g.stderr)
            except ShellTimeout as expired:
                rc, detail = 124, f"gate exceeded {expired.limit}s"
            if rc != 0:
                failures.append(f"{sid}: gate re-run failed (exit {rc}): "
                                f"{detail[-500:]}".rstrip())
    log_text = (run_dir / "log.md").read_text() if (run_dir / "log.md").exists() else ""
    if "run complete" not in log_text:
        failures.append("log.md: missing 'run complete' marker (run never finished)")
    missing = [s["id"] for s in steps if f" {s['id']} " not in log_text]
    if missing:
        failures.append(f"log.md: no execution record for step(s): {', '.join(missing)}")
    log(run_dir, f"verify: {len(steps)} step(s) checked · "
                 f"{'all mechanical checks passed' if not failures else f'{len(failures)} failure(s)'}")
    for failure in failures:
        log(run_dir, f"verify FAIL: {failure}")
    if failures:
        return 1
    if spec.get("qa"):
        qa_ok, _, _ = run_qa(spec, [s["id"] for s in steps], run_dir, cwd)
        return 0 if qa_ok else 1
    return 0


class Runner:
    def __init__(self, spec: dict, steps: list[dict], run_dir: Path, cwd: Path,
                 cache_dir: Path | None, regen: set[str], prev_map: dict[str, str | None],
                 workflow_dir: Path | None = None):
        self.spec, self.steps, self.run_dir, self.cwd = spec, steps, run_dir, cwd
        self.workflow_dir = workflow_dir or cwd
        self.cache_dir, self.regen, self.prev_map = cache_dir, regen, prev_map
        self.ledger: list[dict] = []
        self.ledger_lock = threading.Lock()

    def record(self, entry: dict) -> None:
        with self.ledger_lock:
            self.ledger.append(entry)

    def run_step(self, step: dict) -> bool:
        sid = step["id"]
        out_file = self.run_dir / f"{sid}.md"
        judge = step.get("judge")
        # `retries:` and `judge.max_iters` were mutually exclusive: declaring both
        # silently dropped retries entirely. Give the step whichever budget is
        # larger so neither knob is a no-op.
        attempts = int(step.get("retries", 1)) + 1
        if judge:
            attempts = max(attempts, int(judge.get("max_iters", 3)))
        t_start = time.monotonic()
        usage_acc = {"input": 0, "output": 0, "total": 0, "cost": 0.0}
        entry = {"id": sid, "model": None if step.get("cmd") else step.get("model", self.spec.get("model")),
                 "cached": False, "attempts": 0, "passed": False}
        emit("step_start", id=sid, model=entry["model"], max_attempts=attempts)

        prev_id = self.prev_map.get(sid)
        prev_out = self.run_dir / f"{prev_id}.md" if prev_id else None
        base_prompt = None
        key = None
        if step.get("prompt"):
            base_prompt = render_prompt(step["prompt"], self.run_dir, prev_out)
            if self.cache_dir and sid not in self.regen:
                key = cache_key(step, self.spec, base_prompt)
                hit = self.cache_dir / f"{key}.md"
                if hit.exists():
                    out_file.write_text(hit.read_text())
                    gate = step.get("gate")
                    if not gate or _gate_ok(gate, out_file, self.run_dir, sid, self.cwd, self.workflow_dir):
                        log(self.run_dir, f"{sid}: cache hit ({key[:8]}) — model + judge skipped")
                        # A cache hit still has to capture declared artifacts;
                        # otherwise `produces:` silently vanishes on exactly the
                        # runs the cache makes cheap.
                        produced = copy_produced(step, self.cwd, self.run_dir)
                        if produced:
                            emit("step_produced", id=sid, files=produced)
                        entry.update(cached=True, passed=True, seconds=round(time.monotonic() - t_start, 1), **usage_acc)
                        self.record(entry)
                        emit("step_cached", id=sid, key=key[:8], seconds=entry["seconds"])
                        return True
                    log(self.run_dir, f"{sid}: cache hit failed gate — regenerating")
            elif self.cache_dir:
                key = cache_key(step, self.spec, base_prompt)

        prompt = base_prompt
        best_score, best_text = -1.0, None
        passed = False
        for attempt in range(1, attempts + 1):
            entry["attempts"] = attempt
            t0 = time.monotonic()
            failure = ""
            failure_kind = ""
            emit("step_attempt", id=sid, attempt=attempt, max_attempts=attempts)
            if step.get("cmd"):
                limit = step.get("timeout", 900)
                try:
                    proc = run_shell(step["cmd"], out_file, self.run_dir, sid, self.cwd,
                                     limit, self.workflow_dir)
                except ShellTimeout as expired:
                    (self.run_dir / f"{sid}.stderr").write_text(expired.stderr)
                    ok, failure_kind = False, "command_timeout"
                    failure = (f"cmd exceeded {expired.limit}s and was terminated. "
                               f"Raise `timeout:` on this step if the work legitimately "
                               f"takes longer.")
                    proc = None
                if proc is not None:
                    # stdout is a convenience fallback, not an override: a step that
                    # wrote $OUT owns its artifact, and a stray echo must not replace it.
                    if not out_file.exists():
                        out_file.write_text(proc.stdout if proc.stdout.strip() else "")
                    elif out_file.stat().st_size == 0 and proc.stdout.strip():
                        out_file.write_text(proc.stdout)
                    (self.run_dir / f"{sid}.stderr").write_text(proc.stderr)
                    ok = proc.returncode == 0
                    if not ok:
                        failure_kind = "command_exit"
                        failure = f"cmd exited {proc.returncode}: {(proc.stdout + proc.stderr)[-2000:]}"
            else:
                text, usage, ok, detail, _ = call_pi(step, self.spec, prompt, self.cwd)
                for k in ("input", "output", "total"):
                    usage_acc[k] += usage[k]
                usage_acc["cost"] += usage["cost"]
                out_file.write_text(text)
                if not ok:
                    failure_kind = "model_error"
                    failure = detail
            if ok and step.get("gate"):
                try:
                    g = run_shell(step["gate"], out_file, self.run_dir, sid, self.cwd,
                                  300, self.workflow_dir)
                    gate_rc, gate_out = g.returncode, (g.stdout + g.stderr)
                except ShellTimeout as expired:
                    gate_rc, gate_out = 124, f"gate exceeded {expired.limit}s"
                ok = gate_rc == 0
                emit("step_gate", id=sid, attempt=attempt, passed=ok)
                if not ok:
                    failure_kind = "gate_failed"
                    failure = f"gate `{step['gate']}` exited {gate_rc}: {gate_out[-2000:]}"
            if ok and step.get("schema"):
                detail = check_step_schema(step, out_file)
                emit("step_schema", id=sid, attempt=attempt, passed=not detail)
                if detail:
                    ok, failure_kind, failure = False, "schema_failed", detail
            if ok and judge:
                score, verdict = run_judge(judge, self.spec, out_file.read_text(), self.run_dir,
                                           sid, attempt, self.cwd, usage_acc)
                if score is None:
                    ok, failure_kind, failure = (
                        False, "judge_below_target", 'judge output unparseable (needs "score": N)'
                    )
                else:
                    if score > best_score:
                        best_score, best_text = score, out_file.read_text()
                    threshold = float(judge.get("score", 8))
                    ok = score >= threshold
                    emit("step_judge", id=sid, attempt=attempt, max_attempts=attempts,
                         score=score, threshold=threshold, passed=ok)
                    if not ok:
                        failure_kind = "judge_below_target"
                        failure = (f"judge scored {score} < target {threshold}. Judge feedback:\n"
                                   f"{verdict[-2000:]}")
                    log(self.run_dir, f"{sid} attempt {attempt}/{attempts}: judge score {score} "
                                      f"(target {threshold})")
            dur = time.monotonic() - t0
            outcome = "PASS" if ok else "FAIL"
            reason = "" if ok or not failure else f" — {' '.join(failure.split())[:500]}"
            log(self.run_dir, f"{sid} attempt {attempt}/{attempts}: {outcome} ({dur:.0f}s){reason}")
            if ok:
                passed = True
                break
            if attempt < attempts:
                eligible = set(step.get("retry_on") or [
                    "command_exit", "command_timeout", "model_error", "gate_failed",
                    "schema_failed", "judge_below_target",
                ])
                if failure_kind not in eligible:
                    log(self.run_dir, f"{sid}: no retry for {failure_kind or 'unknown_failure'} "
                                      f"(eligible: {sorted(eligible)})")
                    emit("step_retry_skipped", id=sid, attempt=attempt,
                         failure_kind=failure_kind or "unknown_failure")
                    break
                delay = retry_delay(step, sid, attempt)
                emit("step_retry", id=sid, attempt=attempt, next_attempt=attempt + 1,
                     failure_kind=failure_kind or "unknown_failure", delay_seconds=round(delay, 3))
                log(self.run_dir, f"{sid}: retry {attempt + 1}/{attempts} after {delay:.3f}s "
                                  f"({failure_kind or 'unknown_failure'})")
                if delay:
                    time.sleep(delay)
            if attempt < attempts and base_prompt is not None:
                if out_file.exists():  # keep the rejected attempt diffable
                    (self.run_dir / f"{sid}.a{attempt}.md").write_text(out_file.read_text())
                prompt = (base_prompt
                          + f"\n\nPrevious attempt failed verification.\nFailure: {failure}\n"
                          + "Fix the problem and produce the corrected output in full.")
        if not passed and judge and judge.get("keep_best") and best_text is not None:
            out_file.write_text(best_text)
            log(self.run_dir, f"{sid}: below target after {attempts} iter(s), keeping best "
                              f"candidate (score {best_score})")
            passed = True
        if passed and key and self.cache_dir:
            self.cache_dir.mkdir(exist_ok=True)
            cache_file = self.cache_dir / f"{key}.md"
            temporary = self.cache_dir / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
            temporary.write_text(out_file.read_text())
            os.replace(temporary, cache_file)
        if passed:
            produced = copy_produced(step, self.cwd, self.run_dir)
            if produced:
                emit("step_produced", id=sid, files=produced)
        entry.update(passed=passed, seconds=round(time.monotonic() - t_start, 1), **usage_acc)
        # Why a step failed used to live only in log.md, so `piw detail` showed a
        # FAILED node whose command had visibly succeeded and never mentioned the
        # gate that rejected it. Carry the reason on the ledger entry.
        if not passed and failure:
            entry["failure"] = " ".join(failure.split())[:2000]
            entry["failure_kind"] = failure_kind or "unknown_failure"
        self.record(entry)
        emit("step_end", id=sid, passed=passed, seconds=entry["seconds"], attempts=entry["attempts"],
             cost=usage_acc["cost"], total=usage_acc["total"],
             input=usage_acc["input"], output=usage_acc["output"],
             failure=entry.get("failure", ""), failure_kind=entry.get("failure_kind", ""))
        return passed


class SchemaError(RuntimeError):
    """A step's output did not match its declared `schema:`."""


# Compact, dependency-free shape checking. Deliberately not full JSON Schema:
# a step contract is a flat set of field promises, and anything more elaborate
# belongs in a gate.
_SCHEMA_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list,
}


def validate_schema(document: Any, schema: dict) -> list[str]:
    """Return human-readable problems; empty means the output honours the contract."""
    problems: list[str] = []
    if not isinstance(document, dict):
        return [f"expected a JSON object, got {type(document).__name__}"]
    for field, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {"type": spec}
        optional = spec.get("optional") is True
        if field not in document:
            if not optional:
                problems.append(f"missing required field '{field}'")
            continue
        value = document[field]
        wanted = spec.get("type")
        if wanted:
            expected = _SCHEMA_TYPES.get(str(wanted))
            if expected is None:
                problems.append(f"field '{field}': unknown type {wanted!r} in schema")
                continue
            # bool is a subclass of int; keep JSON types distinct.
            ok = isinstance(value, expected) and not (
                expected in (int, (int, float)) and isinstance(value, bool)
            )
            if wanted == "boolean":
                ok = isinstance(value, bool)
            if not ok:
                problems.append(
                    f"field '{field}': expected {wanted}, got "
                    f"{type(value).__name__} ({json.dumps(value)[:60]})"
                )
                continue
        allowed = spec.get("enum")
        if allowed is not None and value not in allowed:
            problems.append(
                f"field '{field}': {json.dumps(value)} is not one of {json.dumps(allowed)}"
            )
    return problems


def check_step_schema(step: dict, out_file: Path) -> str:
    """Empty string when the step honours its contract, else a failure message."""
    schema = step.get("schema")
    if not schema or not isinstance(schema, dict):
        return ""
    raw = out_file.read_text() if out_file.exists() else ""
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        return (f"output is not JSON ({error}). The schema requires an object with: "
                f"{', '.join(sorted(schema))}")
    problems = validate_schema(document, schema)
    if not problems:
        return ""
    return ("output does not match the declared schema:\n- " + "\n- ".join(problems)
            + f"\nRequired shape: {json.dumps(schema)}")


class WhenError(RuntimeError):
    """A `when:` condition could not be evaluated (bad grammar or unreadable source)."""


# Condition vocabulary for `when:`. Kept deliberately small and explicit so a
# reader can tell what will run without executing anything.
_LEAF_OPS = {
    "exists", "missing", "type_is", "equals", "not_equals",
    "less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal",
    "contains", "in",
}
_GROUP_OPS = {"all", "any", "not"}
_TYPE_NAMES = {
    "string": str, "number": (int, float), "boolean": bool,
    "object": dict, "array": list, "null": type(None),
}
_MISSING = object()


def _pointer(document: Any, path: str) -> Any:
    """Resolve an RFC 6901 JSON Pointer; returns _MISSING when absent."""
    if path in ("", "/"):
        return document
    if not path.startswith("/"):
        raise WhenError(f"path must start with '/': {path!r}")
    current = document
    for raw in path.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.lstrip("-").isdigit():
                return _MISSING
            index = int(token)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def eval_condition(node: Any, document: Any, depth: int = 0) -> bool:
    """Evaluate one `when:` condition against a step's parsed JSON output."""
    if depth > 8:
        raise WhenError("condition nested deeper than 8 levels")
    if not isinstance(node, dict) or "op" not in node:
        raise WhenError(f"condition must be a mapping with an 'op': {node!r}")
    op = node["op"]

    if op in _GROUP_OPS:
        if op == "not":
            inner = node.get("of")
            if isinstance(inner, list):
                if len(inner) != 1:
                    raise WhenError("'not' takes exactly one condition")
                inner = inner[0]
            return not eval_condition(inner, document, depth + 1)
        clauses = node.get("of")
        if not isinstance(clauses, list) or not clauses:
            raise WhenError(f"'{op}' needs a non-empty 'of' list")
        if len(clauses) > 50:
            raise WhenError(f"'{op}' takes at most 50 conditions")
        results = (eval_condition(clause, document, depth + 1) for clause in clauses)
        return all(results) if op == "all" else any(results)

    if op not in _LEAF_OPS:
        raise WhenError(f"unknown op {op!r}; expected one of {sorted(_LEAF_OPS | _GROUP_OPS)}")

    actual = _pointer(document, str(node.get("path", "")))
    if op == "exists":
        return actual is not _MISSING
    if op == "missing":
        return actual is _MISSING
    if actual is _MISSING:
        # Any comparison against an absent value is false rather than an error:
        # a branch on a field the model did not emit simply does not fire.
        return False

    expected = node.get("value")
    if op == "type_is":
        wanted = _TYPE_NAMES.get(str(expected))
        if wanted is None:
            raise WhenError(f"type_is needs one of {sorted(_TYPE_NAMES)}")
        # bool is a subclass of int; keep the JSON types distinct.
        if wanted is _TYPE_NAMES["number"]:
            return isinstance(actual, (int, float)) and not isinstance(actual, bool)
        if wanted is bool:
            return isinstance(actual, bool)
        return isinstance(actual, wanted)
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op in ("less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal"):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise WhenError(f"{op} needs a numeric 'value'")
        if op == "less_than":
            return actual < expected
        if op == "less_than_or_equal":
            return actual <= expected
        if op == "greater_than":
            return actual > expected
        return actual >= expected
    if op == "contains":
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, (list, tuple)):
            return expected in actual
        return False
    if op == "in":
        if not isinstance(expected, (list, tuple)):
            raise WhenError("'in' needs a list 'value'")
        return actual in expected
    raise WhenError(f"unhandled op {op!r}")


def evaluate_when(step: dict, run_dir: Path, dep_ids: set[str]) -> tuple[bool, str]:
    """Decide whether a `when:`-guarded step should run.

    The condition reads the JSON output of a source step -- `from:` when given,
    otherwise the step's single dependency. Code decides, never a model, so the
    same inputs always take the same path.
    """
    condition = step.get("when")
    source = step.get("from")
    if not source:
        # Prefer the declared `needs:` over the full dependency set: a prompt
        # that inlines {step.x} picks up x as a data dependency, which should not
        # make the routing source ambiguous. `needs: [classify]` means "I route
        # off classify" even when the prompt also reads two other artifacts.
        declared = [str(item) for item in (step.get("needs") or [])]
        candidates = declared if len(declared) == 1 else sorted(dep_ids)
        if len(candidates) != 1:
            raise WhenError(
                f"step '{step['id']}': `when:` cannot tell which step to read "
                f"(candidates: {candidates or ['none']}). Add `from: <step>`."
            )
        source = candidates[0]
    artifact = run_dir / f"{source}.md"
    if not artifact.exists():
        raise WhenError(f"step '{step['id']}': `when:` reads '{source}' which produced no artifact")
    raw = artifact.read_text()
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise WhenError(
            f"step '{step['id']}': `when:` needs '{source}' to emit JSON ({error}). "
            f"Add a gate like: python3 -c \"import json;json.load(open('$OUT'))\""
        ) from error
    result = eval_condition(condition, document)
    return result, f"when({source}) -> {'true' if result else 'false'}"


def copy_produced(step: dict, cwd: Path, run_dir: Path) -> list[str]:
    """Copy a step's declared `produces:` files into the run dir.

    Generated media is written relative to the workflow cwd, so without this every
    run overwrites the last and there is no per-run history. Declaring the files
    keeps a copy beside the run's other artifacts.
    """
    declared = step.get("produces")
    if not declared:
        return []
    if isinstance(declared, str):
        declared = [declared]
    copied: list[str] = []
    target_root = run_dir / "produced"
    for entry in declared:
        source = (cwd / str(entry)).resolve()
        try:
            source.relative_to(cwd.resolve())
        except ValueError:
            continue  # never copy from outside the workflow directory
        if not source.is_file():
            continue
        destination = (target_root / str(entry)).resolve()
        try:
            destination.relative_to(target_root.resolve())
        except ValueError:
            continue  # never write outside the run's produced/ directory
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, destination)
            copied.append(str(entry))
        except OSError:
            continue
    return copied


def build_deps(steps: list[dict]) -> tuple[dict[str, set[str]], dict[str, str | None]]:
    ids = [s["id"] for s in steps]
    deps: dict[str, set[str]] = {}
    prev_map: dict[str, str | None] = {}
    for i, step in enumerate(steps):
        sid = step["id"]
        prev_map[sid] = ids[i - 1] if i else None
        d: set[str] = set(STEP_REF_RE.findall(step.get("prompt", "")))
        if "needs" in step:
            d |= set(step.get("needs") or [])
        elif i:
            d.add(ids[i - 1])
        if step.get("prompt") and "{prev}" in step["prompt"] and i:
            d.add(ids[i - 1])
        earlier = set(ids[:i])
        unknown = d - earlier
        if unknown:
            raise SystemExit(f"step '{sid}': depends on {sorted(unknown)} which do not appear earlier")
        deps[sid] = d
    return deps, prev_map


def descendants(deps: dict[str, set[str]], roots: set[str]) -> set[str]:
    out = set(roots)
    changed = True
    while changed:
        changed = False
        for sid, d in deps.items():
            if sid not in out and d & out:
                out.add(sid)
                changed = True
    return out


def git_commit(run_dir: Path, message: str) -> None:
    """Every run dir is a git repo; every step completion is a commit.
    Model output, merges, and operator hand-edits all become diffable history
    (`git -C <run> log --stat`). Silently no-ops if git is unavailable."""
    if not HISTORY_ENABLED:
        return

    # Every call is bounded and gets a closed stdin. History is a nice-to-have;
    # a git that stalls (index lock, credential/askpass prompt, slow filesystem)
    # must never be able to hang the workflow itself. On timeout we simply skip
    # the commit, which is the same no-op as git being absent.
    def git(*args: str, check: bool = True) -> None:
        subprocess.run(
            ["git", "-C", str(run_dir), *args],
            check=check, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )

    try:
        if not (run_dir / ".git").exists():
            git("init", "-q")
            git("config", "user.email", "runner@local")
            git("config", "user.name", "run_steps")
            # Git may otherwise spawn background auto-maintenance after commit.
            # A caller can legitimately delete a completed run immediately;
            # background pack writes race that cleanup on Linux/Python 3.13.
            git("config", "gc.auto", "0")
            git("config", "maintenance.auto", "false")
        git("add", "-A")
        git("commit", "-q", "-m", message, check=False)  # empty commit -> nonzero, fine
    except (OSError, subprocess.SubprocessError):
        pass  # includes TimeoutExpired


def write_ledger(runner: Runner, run_dir: Path) -> None:
    # `--from` only records the steps it re-ran, so writing the file outright
    # deleted every earlier step's entry and the run then under-reported its own
    # cost forever. Merge over whatever is already on disk.
    existing: dict[str, dict] = {}
    ledger_path = run_dir / "ledger.json"
    if ledger_path.exists():
        try:
            for entry in json.loads(ledger_path.read_text()) or []:
                if isinstance(entry, dict) and entry.get("id"):
                    existing[str(entry["id"])] = entry
        except (OSError, ValueError):
            existing = {}
    for entry in runner.ledger:
        existing[str(entry["id"])] = entry
    ledger = sorted(existing.values(), key=lambda e: e["id"])
    ledger_path.write_text(json.dumps(ledger, indent=1))
    tot_tok = sum(e.get("total", 0) for e in ledger)
    tot_cost = sum(e.get("cost", 0.0) for e in ledger)
    tot_s = sum(e.get("seconds", 0) for e in ledger)
    rows = [f"  {e['id']:<18} {'cache' if e.get('cached') else (e.get('model') or 'cmd').split('/')[-1]:<16}"
            f" {e.get('seconds', 0):>6.1f}s {e.get('total', 0):>8} tok  ${e.get('cost', 0.0):.4f}"
            for e in ledger]
    log(run_dir, "ledger:\n" + "\n".join(rows) +
        f"\n  TOTAL {tot_s:.0f}s compute · {tot_tok} tok · ${tot_cost:.4f} · ledger.json written")


def _on_terminate(signum, _frame):  # noqa: ANN001 - stdlib signal handler API
    """Forward termination to shell children before exiting.

    `piw batch-cancel` kills the item's process group; shell steps live in their
    own group so a timeout can kill their whole tree, which means they would
    otherwise survive the cancel as orphans.
    """
    terminate_children()
    sys.exit(130 if signum == signal.SIGINT else 143)


def main() -> int:
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_terminate)
        except (ValueError, OSError):
            pass  # not on the main thread; the parent still kills the group
    ap = argparse.ArgumentParser(description="Lean deterministic step runner (DAG)")
    ap.add_argument("steps_file", type=Path)
    ap.add_argument("--from", dest="from_id", help="re-run this step and all dependents (requires --run-dir)")
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--regen", default="", help="comma-separated step ids to force fresh (bypass cache read)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-history", action="store_true",
                    help="skip per-step Git commits (bulk runs retain events and ledgers)")
    ap.add_argument("--workflow-dir", type=Path,
                    help="directory holding steps.yaml; exported to steps as $WORKFLOW_DIR "
                         "so they can reach shipped assets when --cwd is an isolated workspace")
    ap.add_argument("--cwd", type=Path,
                    help="execution cwd override for a frozen workflow snapshot")
    input_group = ap.add_mutually_exclusive_group()
    input_group.add_argument("--input", help="immutable text input copied into this run")
    input_group.add_argument("--input-file", type=Path, help="immutable file input copied into this run")
    ap.add_argument("--events", type=Path, default=None,
                    help="append a JSONL event stream here so a UI can follow the run live")
    args = ap.parse_args()

    global HISTORY_ENABLED
    HISTORY_ENABLED = not args.no_history

    if args.events:
        global EVENTS_PATH
        args.events.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_PATH = args.events

    spec = validate_workflow_contract(yaml.safe_load(args.steps_file.read_text()), args.steps_file)
    steps = spec.get("steps") or []
    if not steps:
        raise SystemExit("no steps defined")
    ids = [s["id"] for s in steps]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate step ids")
    for step in steps:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", str(step.get("id", ""))):
            raise SystemExit(
                f"step id {step.get('id')!r} must match [A-Za-z0-9][A-Za-z0-9_-]* "
                "(ids become artifact filenames inside the run directory)"
            )
        if bool(step.get("cmd")) == bool(step.get("prompt")):
            raise SystemExit(f"step '{step['id']}': exactly one of cmd or prompt required")
        if step.get("agent") and step.get("cmd"):
            raise SystemExit(f"step '{step['id']}': agent applies to prompt steps only")
    deps, prev_map = build_deps(steps)
    if args.from_id and args.from_id not in ids:
        raise SystemExit(f"unknown --from step '{args.from_id}'")
    if (args.from_id or args.verify) and not args.run_dir:
        raise SystemExit("--from/--verify require --run-dir")

    cwd = args.cwd.expanduser().resolve() if args.cwd else (
        args.steps_file.parent / spec.get("cwd", ".")
    ).resolve()
    if not cwd.is_dir():
        raise SystemExit(f"workflow cwd not found: {cwd}")
    workflow = spec.get("workflow", args.steps_file.stem)
    run_dir = (args.run_dir or args.steps_file.parent / "runs" /
               f"{workflow}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}").resolve()

    if args.verify:
        if not run_dir.is_dir():
            raise SystemExit(f"run dir not found: {run_dir}")
        return verify_run(spec, steps, run_dir, cwd)

    if args.run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Run dirs are named to the second, so two runs started in the same second
        # collided into one directory and silently clobbered each other's
        # artifacts and ledger. mkdir(exist_ok=False) is atomic, so racing
        # processes each land in their own directory.
        base = run_dir
        attempt = 1
        while True:
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                attempt += 1
                run_dir = base.parent / f"{base.name}-{attempt}"

    input_path = run_dir / "input.txt"
    if args.input is not None:
        input_path.write_text(args.input, encoding="utf-8")
    elif args.input_file is not None:
        source = args.input_file.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"input file not found: {source}")
        input_path.write_bytes(source.read_bytes())
    input_contract = spec.get("input") if isinstance(spec.get("input"), dict) else {}
    if input_contract.get("required") and not input_path.is_file():
        raise SystemExit("this workflow requires --input or --input-file")
    cache_dir = None if args.no_cache else args.steps_file.parent / "cache"
    regen = {s for s in args.regen.split(",") if s}
    workers = int(spec.get("workers", 4))

    # resume: --from X re-runs X + descendants; everything else must already exist
    todo = set(ids)
    if args.from_id:
        redo = descendants(deps, {args.from_id})
        for sid in ids:
            if sid not in redo:
                if not (run_dir / f"{sid}.md").exists():
                    raise SystemExit(f"cannot resume: missing prior artifact for '{sid}'")
        todo = redo
        regen |= redo  # resumed steps must not silently reuse stale cache of themselves

    log(run_dir, f"run start · workflow={workflow} · steps={len(todo)}/{len(steps)} · "
                 f"workers={workers} · cache={'off' if not cache_dir else 'on'} · dir={run_dir}")
    emit("run_start", workflow=workflow, run_dir=str(run_dir), workers=workers,
         cache=bool(cache_dir), regen=sorted(regen),
         todo=[sid for sid in ids if sid in todo],
         steps=[{"id": s["id"], "needs": sorted(deps[s["id"]])} for s in steps])

    workflow_dir = (args.workflow_dir.expanduser().resolve()
                    if args.workflow_dir else cwd)
    runner = Runner(spec, steps, run_dir, cwd, cache_dir, regen, prev_map,
                    workflow_dir=workflow_dir)
    by_id = {s["id"]: s for s in steps}
    done: set[str] = {sid for sid in ids if sid not in todo}
    failed: set[str] = set()
    skipped: set[str] = set()
    futures: dict[cf.Future, str] = {}

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        def cascade_skip(sid: str, reason: str) -> None:
            for descendant in descendants(deps, {sid}) - {sid}:
                if descendant in todo and descendant not in done and descendant not in skipped:
                    skipped.add(descendant)
                    log(run_dir, f"{descendant}: skipped ({reason})")
                    emit("step_skipped", id=descendant, reason=reason)

        def dispatch_ready() -> None:
            for sid in ids:
                if not (sid in todo and sid not in done and sid not in failed and sid not in skipped
                        and sid not in futures.values() and deps[sid] <= done):
                    continue
                step = by_id[sid]
                if "when" in step:
                    # Conditions are evaluated by code against a dependency's JSON
                    # output, never by a model, so the same inputs always take the
                    # same path. A false condition SKIPS the step -- it is a route
                    # not taken, not a failure.
                    try:
                        should_run, detail = evaluate_when(step, run_dir, deps[sid])
                    except WhenError as error:
                        failed.add(sid)
                        log(run_dir, f"{sid}: ERROR {error}")
                        emit("step_end", id=sid, passed=False, error=str(error))
                        cascade_skip(sid, f"depends on failed '{sid}'")
                        continue
                    emit("step_when", id=sid, passed=should_run, detail=detail)
                    if not should_run:
                        skipped.add(sid)
                        log(run_dir, f"{sid}: skipped ({detail})")
                        emit("step_skipped", id=sid, reason=detail)
                        cascade_skip(sid, f"branch not taken at '{sid}'")
                        continue
                futures[pool.submit(runner.run_step, step)] = sid

        dispatch_ready()
        while futures:
            complete, _ = cf.wait(list(futures), return_when=cf.FIRST_COMPLETED)
            for fut in complete:
                sid = futures.pop(fut)
                try:
                    ok = fut.result()
                except Exception as exc:  # rendering/infra error
                    ok = False
                    log(run_dir, f"{sid}: ERROR {exc}")
                    emit("step_end", id=sid, passed=False, error=str(exc))
                (done if ok else failed).add(sid)
                git_commit(run_dir, f"{sid}: {'PASS' if ok else 'FAIL'}")
                if not ok:
                    cascade_skip(sid, f"depends on failed '{sid}'")
            dispatch_ready()

    if failed:
        write_ledger(runner, run_dir)
        log(run_dir, f"HALT · failed: {sorted(failed)} · skipped: {sorted(skipped)} · artifacts in {run_dir}")
        emit("run_end", ok=False, failed=sorted(failed), skipped=sorted(skipped))
        print(f"\nFAILED step(s) {sorted(failed)}. Fix and rerun with:\n"
              f"  python3 {shlex.quote(sys.argv[0])} {shlex.quote(str(args.steps_file))} "
              f"--from {sorted(failed)[0]} --run-dir {shlex.quote(str(run_dir))}", file=sys.stderr)
        return 1

    # Report what actually ran: with `when:` guards, "all N passed" would be a lie
    # whenever a branch was not taken.
    summary = f"{len(done & todo)} step(s) passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    log(run_dir, f"run complete · {summary}")
    if spec.get("qa"):
        qa = spec.get("qa") or {}
        qa_model = qa.get("model") or spec.get("model")
        emit("step_start", id="__qa__", model=qa_model, max_attempts=1)
        qa_ok, qa_usage, qa_seconds = run_qa(spec, ids, run_dir, cwd)
        runner.record({
            "id": "__qa__", "model": qa_model, "cached": False, "attempts": 1,
            "passed": qa_ok, "seconds": qa_seconds, **qa_usage,
        })
        git_commit(run_dir, f"QA: {'pass' if qa_ok else 'fail'}")
        emit("step_end", id="__qa__", passed=qa_ok, seconds=qa_seconds, attempts=1,
             cost=qa_usage["cost"], total=qa_usage["total"],
             input=qa_usage["input"], output=qa_usage["output"])
        emit("qa", passed=qa_ok)
        write_ledger(runner, run_dir)
        if not qa_ok:
            emit("run_end", ok=False, failed=["__qa__"], skipped=[])
            print(f"\nQA FAILED. Report: {run_dir / 'qa.md'}\n"
                  f"Fix, rerun the offending step with --from or --regen, then re-check with:\n"
                  f"  python3 {shlex.quote(sys.argv[0])} {shlex.quote(str(args.steps_file))} "
                  f"--verify --run-dir {shlex.quote(str(run_dir))}", file=sys.stderr)
            return 1
    else:
        write_ledger(runner, run_dir)
    emit("run_end", ok=True, failed=[], skipped=sorted(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
