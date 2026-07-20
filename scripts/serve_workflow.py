#!/usr/bin/env python3
"""Local, optional graph studio for one Pi Workflow.

The UI is a view and run surface over the same ``steps.yaml`` and runner used by
``piw``. It never becomes a second workflow engine or source of truth.
"""

from __future__ import annotations

import argparse
import json
import secrets
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

import graph as workflow_graph


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_steps.py"
UI_ROOT = ROOT / "ui"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SESSIONS = 24
MAX_ACTIVE = 4

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


def _read(path: Path, limit: int = 64_000) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return value if len(value) <= limit else value[:limit] + "\n\n… truncated"


def latest_snapshot() -> dict | None:
    runs_dir = CFG["steps"].parent / "runs"
    try:
        candidates = sorted(
            (path for path in runs_dir.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for run_dir in candidates:
        try:
            detail = workflow_graph.run_detail(CFG["steps"], run_dir)
        except (OSError, workflow_graph.WorkflowParseError):
            continue
        return {
            "detail": detail,
            "output": _read(run_dir / f"{CFG['output']}.md"),
        }
    return None


def _prune_sessions() -> None:
    """Keep recent evidence available without growing an unbounded process map."""
    with SESSIONS_LOCK:
        if len(SESSIONS) <= MAX_SESSIONS:
            return
        completed = [key for key, value in SESSIONS.items() if value["proc"].poll() is not None]
        for key in completed[: max(0, len(SESSIONS) - MAX_SESSIONS)]:
            SESSIONS.pop(key, None)


def start_run(content: str) -> str:
    _prune_sessions()
    with SESSIONS_LOCK:
        active = sum(1 for item in SESSIONS.values() if item["proc"].poll() is None)
        if active >= MAX_ACTIVE:
            raise RuntimeError(f"{MAX_ACTIVE} runs are already active; wait for one to finish")

    sid = uuid.uuid4().hex[:12]
    events_path = CFG["temp"] / f"{sid}.jsonl"
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
    session = {"proc": proc, "events": events_path, "output": [], "detail": None}
    with SESSIONS_LOCK:
        SESSIONS[sid] = session

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
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json; charset=utf-8")

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
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "run input exceeds 2 MiB"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            content = payload.get("content", "")
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

    temporary = tempfile.TemporaryDirectory(prefix="pi-workflows-ui-")
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
    print(f"Pi Workflows Studio · {graph['workflow']} · {url}", flush=True)
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
