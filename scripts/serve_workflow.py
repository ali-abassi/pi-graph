#!/usr/bin/env python3
"""Local, optional graph studio for one Pi Workflow.

The UI is a view and run surface over the same ``steps.yaml`` and runner used by
``piw``. It never becomes a second workflow engine or source of truth.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jsonschema import Draft202012Validator, FormatChecker

import graph as workflow_graph


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_steps.py"
UI_ROOT = ROOT / "ui"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SESSIONS = 24
MAX_ACTIVE = 4
MAX_RUNS = 200
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_TRACE_EVENTS = 500
MAX_TRACE_EVENT_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
TERMINAL_STEP_STATUSES = {"passed", "failed", "skipped", "cached", "interrupted"}
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))
def _valid_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return True  # JSON Schema's type keyword owns non-string rejection.
    if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.tzinfo is not None


MANIFEST_VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas" / "run-bundle.schema.json").read_text()), format_checker=FORMAT_CHECKER)
STATE_VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas" / "run-state.schema.json").read_text()), format_checker=FORMAT_CHECKER)

CFG: dict = {}
SESSIONS: dict[str, dict] = {}
SESSIONS_LOCK = threading.Lock()


def _events(path: Path) -> list[dict]:
    """Read only complete JSONL records; the runner may be writing the tail."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    parsed = []
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict):
            parsed.append(event)
    return parsed


def _safe_bytes(path: Path, limit: int) -> tuple[bytes | None, str | None]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None, "not a regular file"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None, "file changed during safe open"
            if opened.st_size > limit:
                return None, f"exceeds {limit} byte read limit"
            raw = os.read(fd, limit + 1)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None, "missing"
    except OSError as error:
        return None, str(error)
    if len(raw) > limit:
        return None, f"exceeds {limit} byte read limit"
    return raw, None


def _safe_text_prefix(path: Path, limit: int = 8_000) -> tuple[str, bool]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return "", False
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return "", False
            raw = os.read(fd, limit + 1)
        finally:
            os.close(fd)
    except OSError:
        return "", False
    cut = opened.st_size > limit or len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), cut


def _read(path: Path, limit: int = 64_000) -> str:
    raw, error = _safe_bytes(path, limit)
    if raw is None:
        return "" if error == "missing" else f"[Studio withheld file: {error}]"
    return raw.decode("utf-8", errors="replace")


def _json_file(path: Path, limit: int = MAX_JSON_BYTES) -> tuple[dict | None, str | None]:
    raw, error = _safe_bytes(path, limit)
    if raw is None:
        return None, error
    if len(raw) > limit:
        return None, f"exceeds {limit} byte read limit"
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(value, dict):
        return None, "must be a JSON object"
    return value, None


def _run_dir(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError("invalid run id")
    runs_dir = CFG["steps"].parent / "runs"
    candidate = runs_dir / run_id
    if candidate.is_symlink() or not candidate.is_dir() or candidate.parent != runs_dir:
        raise ValueError("unknown run id")
    return candidate


def _schema_error(validator: Draft202012Validator, value: dict, label: str) -> str | None:
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(part) for part in first.path) or "<root>"
    return f"{label} schema at {where}: {first.message}"


def _fingerprinted_bytes(path: Path, record: dict, label: str) -> tuple[bytes | None, str | None]:
    raw, error = _safe_bytes(path, MAX_JSON_BYTES)
    if raw is None:
        return None, f"{label}: {error}"
    if record.get("bytes") != len(raw) or record.get("sha256") != hashlib.sha256(raw).hexdigest():
        return None, f"{label}: fingerprint mismatch"
    return raw, None


def _evidence_guard(run_dir: Path) -> str | None:
    total = 0
    try:
        for root, dirs, files in os.walk(run_dir, followlinks=False):
            root_path = Path(root)
            for name in [*dirs, *files]:
                path = root_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    return f"unsafe evidence symlink: {path.relative_to(run_dir)}"
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if info.st_size > MAX_JSON_BYTES:
                        return f"evidence file exceeds read limit: {path.relative_to(run_dir)}"
                    if total > MAX_EVIDENCE_BYTES:
                        return "run evidence exceeds cumulative read limit"
                elif not stat.S_ISDIR(info.st_mode):
                    return f"unsupported evidence file type: {path.relative_to(run_dir)}"
    except OSError as error:
        return f"evidence inspection failed: {error}"
    return None


def _run_summary(run_dir: Path) -> dict:
    manifest, manifest_error = _json_file(run_dir / "manifest.json")
    state, state_error = _json_file(run_dir / "state.json")
    has_snapshot = (run_dir / "workflow.yaml").is_file()
    legacy_evidence = any((run_dir / name).exists() for name in ("ledger.json", "log.md"))
    reasons: list[str] = []
    workflow_raw: bytes | None = None
    input_raw: bytes | None = None
    unsafe_evidence = _evidence_guard(run_dir)
    if unsafe_evidence:
        reasons.append(unsafe_evidence)
    legacy = manifest is None and state is None and not has_snapshot and legacy_evidence
    if legacy:
        integrity = "degraded" if reasons else "legacy"
        status = "legacy"
        steps: dict = {}
        trace_seq = 0
        updated_at = ""
        resume_count = 0
    else:
        if manifest_error:
            reasons.append(f"manifest: {manifest_error}")
        if state_error:
            reasons.append(f"state: {state_error}")
        if not has_snapshot:
            reasons.append("workflow snapshot: missing")
        if manifest is not None:
            error = _schema_error(MANIFEST_VALIDATOR, manifest, "manifest")
            if error:
                reasons.append(error)
        if state is not None:
            error = _schema_error(STATE_VALIDATOR, state, "state")
            if error:
                reasons.append(error)
        if manifest and manifest.get("run_id") != run_dir.name:
            reasons.append("manifest: run id mismatch")
        if state and state.get("run_id") != run_dir.name:
            reasons.append("state: run id mismatch")
        if manifest is not None:
            workflow_record = manifest.get("workflow")
            if isinstance(workflow_record, dict):
                workflow_raw, error = _fingerprinted_bytes(
                    run_dir / "workflow.yaml", workflow_record, "workflow snapshot")
                if error:
                    reasons.append(error)
            input_record = manifest.get("input")
            if isinstance(input_record, dict):
                input_raw, error = _fingerprinted_bytes(
                    run_dir / "input.txt", input_record, "immutable input")
                if error:
                    reasons.append(error)
            elif input_record is None and (run_dir / "input.txt").exists():
                reasons.append("immutable input: unexpected file")
        integrity = "degraded" if reasons else "durable"
        steps = state.get("steps", {}) if isinstance(state, dict) and isinstance(state.get("steps"), dict) else {}
        status = str(state.get("status") if state else manifest.get("status") if manifest else "unknown")
        trace_seq = state.get("trace_seq", 0) if state else 0
        if not isinstance(trace_seq, int) or trace_seq < 0:
            reasons.append("state: invalid committed trace sequence")
            integrity, trace_seq = "degraded", 0
        updated_at = str((state or {}).get("updated_at") or (manifest or {}).get("updated_at") or "")
        recorded_resume_count = (state or {}).get("resume_count", 0)
        resume_count = (recorded_resume_count
                        if isinstance(recorded_resume_count, int) and not isinstance(recorded_resume_count, bool)
                        and recorded_resume_count >= 0 else 0)
    terminal = sum(1 for item in steps.values()
                   if isinstance(item, dict) and item.get("status") in TERMINAL_STEP_STATUSES)
    try:
        mtime = run_dir.stat().st_mtime
    except OSError:
        mtime = 0
    return {
        "id": run_dir.name,
        "status": status,
        "integrity": integrity,
        "terminal": terminal,
        "total": len(steps),
        "updated_at": updated_at,
        "trace_seq": trace_seq,
        "resume_count": resume_count,
        "legacy": legacy,
        "degraded_reason": "; ".join(reasons),
        "_mtime": mtime,
        "_manifest": manifest,
        "_state": state,
        "_unsafe_evidence": unsafe_evidence,
        "_workflow_raw": workflow_raw,
        "_input_raw": input_raw,
    }


def run_index() -> dict:
    runs_dir = CFG["steps"].parent / "runs"
    try:
        candidates = [path for path in runs_dir.iterdir()
                      if path.is_dir() and not path.is_symlink() and RUN_ID_RE.fullmatch(path.name)]
    except OSError:
        candidates = []
    summaries = [_run_summary(path) for path in candidates]
    summaries.sort(key=lambda item: (item["updated_at"], item["_mtime"], item["id"]), reverse=True)
    result = [{key: value for key, value in item.items() if not key.startswith("_")}
              for item in summaries[:MAX_RUNS]]
    return {"ok": True, "version": 1, "runs": result,
            "selected": result[0]["id"] if result else None,
            "limited": len(summaries) > MAX_RUNS}


def _committed_trace(run_dir: Path, summary: dict) -> tuple[list[dict], bool, str | None]:
    committed = summary["trace_seq"]
    if not committed:
        return [], False, None
    events: list[dict] = []
    path = run_dir / "trace.jsonl"
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES:
            return [], committed > 0, "trace is unsafe or exceeds the cumulative read limit"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return [], committed > 0, "trace changed during safe open"
            for expected in range(1, min(committed, MAX_TRACE_EVENTS) + 1):
                line = handle.readline(MAX_TRACE_EVENT_BYTES + 1)
                if not line or not line.endswith(b"\n"):
                    return events, committed > len(events), f"committed trace is torn at event {expected}"
                if len(line) > MAX_TRACE_EVENT_BYTES:
                    return events, True, f"trace event {expected} exceeds read limit"
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, ValueError) as error:
                    return events, committed > len(events), f"committed trace event {expected} is invalid: {error}"
                if (not isinstance(event, dict) or event.get("schema") != "pi-graph.trace-event.v1"
                        or event.get("seq") != expected or event.get("run_id") != run_dir.name):
                    return events, committed > len(events), f"committed trace event {expected} failed identity checks"
                events.append(event)
    except OSError as error:
        return [], committed > 0, f"trace: {error}"
    return events, committed > len(events), None


def _bound_detail(detail: dict | None, budget: int = 750_000) -> dict | None:
    if not detail:
        return detail
    remaining = budget
    for step in detail.get("steps", []):
        for key, cap in (("sent", 4000), ("output", 12_000), ("stderr", 4000)):
            value = step.get(key)
            if not isinstance(value, str):
                continue
            allowed = max(0, min(cap, remaining))
            if len(value) > allowed:
                step[key] = value[:allowed] + "\n\n… truncated by Studio"
                step[f"{key}_truncated"] = True
            remaining -= min(len(value), allowed)
        attempts = step.get("judge_attempts")
        if isinstance(attempts, list):
            step["judge_attempts"] = attempts[:4]
            for attempt in step["judge_attempts"]:
                if not isinstance(attempt, dict):
                    continue
                for key in ("output", "judge"):
                    if isinstance(attempt.get(key), str) and len(attempt[key]) > 4000:
                        attempt[key] = attempt[key][:4000] + "\n\n… truncated by Studio"
                        attempt["truncated"] = True
    if isinstance(detail.get("log"), list):
        detail["log"] = detail["log"][-500:]
    return detail


def run_snapshot(run_id: str) -> dict:
    run_dir = _run_dir(run_id)
    summary = _run_summary(run_dir)
    manifest = summary.pop("_manifest", None)
    state = summary.pop("_state", None)
    unsafe_evidence = summary.pop("_unsafe_evidence", None)
    workflow_raw = summary.pop("_workflow_raw", None)
    input_raw = summary.pop("_input_raw", None)
    summary.pop("_mtime", None)
    snapshot = run_dir / "workflow.yaml"
    graph_path = CFG["steps"] if summary["legacy"] else snapshot
    graph_error = unsafe_evidence
    try:
        if summary["legacy"]:
            graph_raw, safe_error = _safe_bytes(graph_path, MAX_JSON_BYTES)
        else:
            graph_raw, safe_error = workflow_raw, None if workflow_raw is not None else "fingerprint unavailable"
        if unsafe_evidence or graph_raw is None:
            graph = None
            graph_error = unsafe_evidence or f"workflow snapshot: {safe_error}"
        else:
            graph = workflow_graph.parse_steps_text(
                graph_raw.decode("utf-8", errors="replace"), graph_path)
    except (OSError, workflow_graph.WorkflowParseError) as error:
        graph, graph_error = None, str(error)
    try:
        detail = (_bound_detail(workflow_graph.run_detail(
            graph_path, run_dir, resolve_prompts=False,
            parsed_graph=graph, read_file=_safe_text_prefix))
                  if graph and not unsafe_evidence else None)
    except (OSError, workflow_graph.WorkflowParseError) as error:
        detail, graph_error = None, str(error)
    if detail and state:
        state_steps = state.get("steps", {})
        for item in detail.get("steps", []):
            durable = state_steps.get(item.get("id"), {}) if isinstance(state_steps, dict) else {}
            if isinstance(durable, dict) and durable.get("status"):
                item["status"] = durable["status"]
                if durable.get("reason"):
                    item["reason"] = durable["reason"]
    trace, trace_limited, trace_error = _committed_trace(run_dir, summary)
    errors = [value for value in (summary.get("degraded_reason"), graph_error, trace_error) if value]
    if errors:
        summary["integrity"] = "degraded"
        summary["degraded_reason"] = "; ".join(errors)
    eligible = summary["integrity"] == "durable" and summary["status"] == "interrupted"
    resume = {
        "eligible": eligible,
        "command": f"piw resume {CFG['steps']} {run_id}" if eligible else "",
        "reason": "interrupted durable run" if eligible else "durable interrupted runs only",
    }
    workflow_info = manifest.get("workflow") if isinstance(manifest, dict) else None
    input_info = manifest.get("input") if isinstance(manifest, dict) else None
    summary["workflow"] = workflow_info
    summary["input"] = input_info
    summary["drift"] = manifest.get("drift", []) if isinstance(manifest, dict) else []
    return {
        "ok": summary["integrity"] != "degraded",
        "version": 1,
        "run": summary,
        "graph": graph,
        "detail": detail,
        "trace": trace,
        "trace_total": summary["trace_seq"],
        "trace_limited": trace_limited,
        "resume": resume,
        "input_text": (_read(run_dir / "input.txt") if summary["legacy"]
                       else input_raw.decode("utf-8", errors="replace") if input_raw is not None
                       else "" if summary.get("input") is None
                       else "[Studio withheld immutable input: fingerprint validation failed]"),
    }


def latest_snapshot() -> dict | None:
    index = run_index()
    selected = index.get("selected")
    if not selected:
        return None
    try:
        snapshot = run_snapshot(selected)
        # Keep the original boot projection for older Studio clients while the
        # first-class UI hydrates through the durable endpoints.
        return {"detail": snapshot.get("detail"),
                "output": _read(_run_dir(selected) / f"{CFG['output']}.md"),
                **snapshot}
    except (OSError, ValueError):
        return None


def _prune_sessions() -> None:
    """Keep recent evidence available without growing an unbounded process map."""
    with SESSIONS_LOCK:
        if len(SESSIONS) < MAX_SESSIONS:
            return
        completed = [key for key, value in SESSIONS.items()
                     if value.get("proc") is not None and value["proc"].poll() is not None]
        for key in completed[: len(SESSIONS) - MAX_SESSIONS + 1]:
            SESSIONS.pop(key, None)


def start_run(content: str) -> str:
    sid = uuid.uuid4().hex[:12]
    events_path = CFG["temp"] / f"{sid}.jsonl"
    session = {"proc": None, "events": events_path, "output": [], "detail": None}

    # Capacity checks and reservation are one transaction. A None process is a
    # reserved active slot, so concurrent launch requests cannot both pass the
    # check and overfill MAX_ACTIVE before Popen returns.
    with SESSIONS_LOCK:
        if len(SESSIONS) >= MAX_SESSIONS:
            completed = [key for key, value in SESSIONS.items()
                         if value.get("proc") is not None and value["proc"].poll() is not None]
            for key in completed[: len(SESSIONS) - MAX_SESSIONS + 1]:
                SESSIONS.pop(key, None)
        active = sum(1 for item in SESSIONS.values()
                     if item.get("proc") is None or item["proc"].poll() is None)
        if active >= MAX_ACTIVE:
            raise RuntimeError(f"{MAX_ACTIVE} runs are already active; wait for one to finish")
        if len(SESSIONS) >= MAX_SESSIONS:
            raise RuntimeError(f"Studio session history is full at {MAX_SESSIONS}; restart the local server")
        SESSIONS[sid] = session

    try:
        events_path.touch()
        command = [sys.executable, str(RUNNER), str(CFG["steps"]), "--events", str(events_path)]
        if content:
            command.extend(["--input", content])
        proc = subprocess.Popen(
            command,
            cwd=CFG["steps"].parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        with SESSIONS_LOCK:
            SESSIONS.pop(sid, None)
        events_path.unlink(missing_ok=True)
        raise
    with SESSIONS_LOCK:
        session["proc"] = proc

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            session["output"].append(line)
            if len(session["output"]) > 1000:
                del session["output"][:250]

    threading.Thread(target=pump, daemon=True, name=f"piw-ui-{sid}").start()
    return sid


def session_status(sid: str, after: int) -> dict:
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if not session:
        raise KeyError("unknown or expired session")

    events = _events(session["events"])
    done = session["proc"].poll() is not None
    response = {
        "events": events[max(0, after):],
        "event_count": len(events),
        "done": done,
        "exit": session["proc"].returncode if done else None,
    }
    if not done:
        return response

    run_start = next((item for item in events if item.get("t") == "run_start"), None)
    run_dir = Path(run_start["run_dir"]) if run_start and run_start.get("run_dir") else None
    if run_dir and run_dir.is_dir():
        if session["detail"] is None:
            try:
                session["detail"] = workflow_graph.run_detail(CFG["steps"], run_dir)
            except (OSError, workflow_graph.WorkflowParseError) as error:
                session["detail"] = {"error": str(error)}
        detail = session["detail"]
        response["detail"] = detail if "error" not in detail else None
        output_id = CFG["output"]
        response["output"] = _read(run_dir / f"{output_id}.md")
        if "error" in detail:
            response["error"] = detail["error"]

    if session["proc"].returncode and "error" not in response:
        response["error"] = "".join(session["output"])[-5000:].strip() or "workflow failed"
    return response


class Handler(BaseHTTPRequestHandler):
    server_version = "PiWorkflowsStudio/1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        if len(body) > MAX_JSON_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            body = json.dumps({"ok": False, "error": "Studio response exceeds 2 MiB bound"},
                              separators=(",", ":")).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _asset(self, name: str, content_type: str) -> None:
        path = UI_ROOT / name
        if not path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
            return
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    def _host_ok(self) -> bool:
        """Reject DNS rebinding.

        Binding to 127.0.0.1 does not help once an attacker's domain resolves
        there: their page becomes same-origin, so SOP and CSP stop applying and
        `GET /` would hand out the run token. Only a Host check survives that.
        """
        host = self.headers.get("Host", "")
        if host.startswith("[") and "]" in host:      # [::1] or [::1]:8787
            name = host[1:host.index("]")]
        else:                                          # localhost or 127.0.0.1:8787
            name = host.split(":", 1)[0]
        if name in {"127.0.0.1", "localhost", "::1"}:
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "invalid Host header"})
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            boot = json.dumps({
                "graph": CFG["graph"],
                "token": CFG["token"],
                "default_input": CFG["default_input"],
                "latest": latest_snapshot(),
            }, separators=(",", ":")).replace("</", "<\\/")
            page = (UI_ROOT / "index.html").read_text(encoding="utf-8").replace("__PIW_BOOT__", boot)
            self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/styles.css":
            self._asset("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/assets/app.js":
            self._asset("app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/api/runs":
            self._json(HTTPStatus.OK, run_index())
            return
        if parsed.path == "/api/run":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [""])[0]
            try:
                self._json(HTTPStatus.OK, run_snapshot(run_id))
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            except (OSError, workflow_graph.WorkflowParseError) as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": str(error)})
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            sid = (query.get("session") or [""])[0]
            try:
                after = max(0, int((query.get("after") or ["0"])[0]))
                self._json(HTTPStatus.OK, session_status(sid, after))
            except (KeyError, ValueError) as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._host_ok():
            return
        if urlparse(self.path).path != "/api/run":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get("X-Piw-Token") != CFG["token"]:
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid run token"})
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_REQUEST_BYTES:
            # Drain only a narrowly-over-limit body so ordinary clients receive
            # the 413 instead of a reset; never trust or allocate an arbitrary claim.
            if MAX_REQUEST_BYTES < length <= MAX_REQUEST_BYTES + 64 * 1024:
                self.rfile.read(length)
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "run input exceeds 2 MiB"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if set(payload) != {"content"}:
                raise ValueError("request body must contain exactly one content field")
            content = payload["content"]
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            sid = start_run(content)
        except (json.JSONDecodeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except (OSError, RuntimeError) as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        self._json(HTTPStatus.ACCEPTED, {"session": sid})


def validate_workflow(steps: Path) -> dict:
    graph = workflow_graph.parse_steps(steps)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "piw.py"), "validate", str(steps), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        verdict = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(result.stderr.strip() or "workflow validation returned invalid output") from error
    if result.returncode or not verdict.get("holds"):
        issue = verdict.get("next") or verdict.get("reason") or "workflow validation failed"
        raise RuntimeError(issue)
    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Optional local graph studio for one Pi Workflow")
    parser.add_argument("steps_file", type=Path)
    parser.add_argument("--input-file", type=Path, help="prefill the immutable run input")
    parser.add_argument("--output", help="step whose artifact is shown after a run")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="open the studio in the default browser")
    args = parser.parse_args()

    steps = args.steps_file.expanduser().resolve()
    if not steps.is_file():
        parser.error(f"workflow not found: {steps}")
    try:
        graph = validate_workflow(steps)
    except (OSError, RuntimeError, workflow_graph.WorkflowParseError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    output = args.output or next((node["id"] for node in reversed(graph["nodes"]) if not node.get("synthetic")), "")
    if output not in {node["id"] for node in graph["nodes"] if not node.get("synthetic")}:
        print(f"error: unknown output step: {output}", file=sys.stderr)
        return 2
    default_input = ""
    if args.input_file:
        try:
            default_input = args.input_file.expanduser().read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read input file: {error}", file=sys.stderr)
            return 2

    temporary = tempfile.TemporaryDirectory(prefix="pi-graph-ui-")
    CFG.update({
        "steps": steps,
        "graph": graph,
        "output": output,
        "default_input": default_input,
        "token": secrets.token_urlsafe(24),
        "temp": Path(temporary.name),
        "temporary": temporary,
    })
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Pi Graph Studio · {graph['workflow']} · {url}", flush=True)
    print(f"source of truth: {steps}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for session in list(SESSIONS.values()):
            if session["proc"].poll() is None:
                session["proc"].terminate()
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
