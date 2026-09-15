from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "serve_workflow.py"
sys.path.insert(0, str(ROOT / "scripts"))
from run_bundle import RunBundle  # noqa: E402
import serve_workflow  # noqa: E402


@contextlib.contextmanager
def studio(steps: Path):
    env = {**os.environ, "PI_GRAPH_ROOTS": str(steps.parent), "LOOPS_PORT": "1"}
    process = subprocess.Popen(
        [sys.executable, str(SERVER), str(steps), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        match = re.search(r"(http://127\.0\.0\.1:\d+)$", line)
        if not match:
            raise AssertionError(line or (process.stderr.read() if process.stderr else "server failed"))
        yield match.group(1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def make_bundle(root: Path, run_id: str, workflow_bytes: bytes, step_ids: list[str], *, complete: bool) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    bundle = RunBundle(run_dir).acquire()
    bundle.initialize(
        workflow_bytes=workflow_bytes, source_path=root / "steps.yaml", input_bytes=b"frozen input\n",
        cwd=root, workflow_dir=root, step_ids=step_ids, events_path=None,
    )
    bundle.record("run_started", run_status="running")
    for sid in step_ids:
        bundle.record("step_started", step_id=sid, step_status="running")
        bundle.record("step_finished", step_id=sid, step_status="passed")
    if complete:
        bundle.record("run_completed", run_status="completed")
    bundle.close()
    return run_dir


class WorkflowUiTests(unittest.TestCase):
    def test_git_monitor_socket_does_not_hide_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = root / "steps.yaml"
            workflow.write_text("version: 1\nworkflow: git-monitor\nsteps:\n  - id: only\n    cmd: echo ok\n")
            run = make_bundle(root, "run-monitor", workflow.read_bytes(), ["only"], complete=True)
            (run / ".git").mkdir()
            with socket.socket(socket.AF_UNIX) as monitor:
                monitor.bind(str(run / ".git" / "monitor.ipc"))
                with studio(workflow) as base:
                    payload = get_json(f"{base}/api/run?id=run-monitor")
                    self.assertEqual(payload["run"]["integrity"], "durable")
                    self.assertEqual(payload["graph"]["workflow"], "git-monitor")

    def test_evidence_socket_outside_git_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with socket.socket(socket.AF_UNIX) as artifact:
                artifact.bind(str(root / "artifact.ipc"))
                self.assertEqual(serve_workflow._evidence_guard(root),
                                 "unsupported evidence file type: artifact.ipc")

    def test_studio_runs_the_canonical_engine_and_returns_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workflow = Path(raw)
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "ui-proof",
                "input": {"required": True, "description": "Name to preserve"},
                "steps": [{
                    "id": "copy",
                    "cmd": 'cat "$INPUT"',
                    "gate": 'test -s "$OUT"',
                }],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PI_GRAPH_ROOTS": raw, "LOOPS_PORT": "1"}
            process = subprocess.Popen(
                [sys.executable, str(SERVER), str(steps), "--port", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                match = re.search(r"(http://127\.0\.0\.1:\d+)$", line)
                self.assertIsNotNone(match, line)
                base = match.group(1)
                with urllib.request.urlopen(base, timeout=5) as response:
                    page = response.read().decode()
                    self.assertIn("Pi Graph Studio", page)
                    self.assertIn("ui-proof", page)
                    self.assertIn("Content-Security-Policy", response.headers)
                token = json.loads(re.search(
                    r'<script id="piw-boot" type="application/json">(.*?)</script>', page, re.S,
                ).group(1))["token"]

                request = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps({"content": "Ada"}).encode(),
                    headers={"Content-Type": "application/json", "X-Piw-Token": token},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    session = json.loads(response.read())["session"]

                payload = {}
                for _ in range(80):
                    with urllib.request.urlopen(f"{base}/api/status?session={session}&after=0", timeout=5) as response:
                        payload = json.loads(response.read())
                    if payload.get("done"):
                        break
                    time.sleep(0.05)
                self.assertTrue(payload.get("done"), payload)
                self.assertEqual(payload["exit"], 0, payload.get("error"))
                self.assertEqual(payload["output"], "Ada")
                self.assertTrue(payload["detail"]["run"]["ok"])
                self.assertIn("run_end", {event["t"] for event in payload["events"]})
                with urllib.request.urlopen(base, timeout=5) as response:
                    restored = response.read().decode()
                self.assertIn('"latest":{"detail"', restored)
                self.assertIn('"output":"Ada"', restored)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def test_historical_view_uses_frozen_snapshot_and_committed_trace_without_mutation(self) -> None:
        """A restartable read model must never substitute current source or repair evidence."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original = yaml.safe_dump({
                "version": 1, "workflow": "historical-proof",
                "steps": [{"id": "frozen_node", "cmd": 'printf frozen > "$OUT"'}],
            }, sort_keys=False).encode()
            steps = root / "steps.yaml"
            steps.write_bytes(original)
            run_dir = make_bundle(root, "run-frozen", original, ["frozen_node"], complete=True)
            committed = json.loads((run_dir / "state.json").read_text())["trace_seq"]
            with (run_dir / "trace.jsonl").open("ab") as handle:
                handle.write(b"{malformed uncommitted tail}\n")
            before = (run_dir / "trace.jsonl").read_bytes()
            before_mtime = (run_dir / "trace.jsonl").stat().st_mtime_ns
            steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "historical-proof",
                "steps": [{"id": "current_source_node", "cmd": 'printf current > "$OUT"'}],
            }, sort_keys=False))

            with studio(steps) as base:
                index = get_json(f"{base}/api/runs")
                self.assertEqual(index["version"], 1)
                self.assertEqual(index["runs"][0]["id"], "run-frozen")
                payload = get_json(f"{base}/api/run?id=run-frozen")
                self.assertEqual([node["id"] for node in payload["graph"]["nodes"]], ["frozen_node"])
                self.assertEqual(payload["run"]["status"], "completed")
                self.assertEqual(payload["run"]["integrity"], "durable")
                self.assertEqual(payload["trace_total"], committed)
                self.assertEqual([event["seq"] for event in payload["trace"]], list(range(1, committed + 1)))
                self.assertFalse(payload["resume"]["eligible"])
                self.assertLess(len(json.dumps(payload).encode()), 2 * 1024 * 1024)
                for path in ("../run-frozen", "run-frozen/../other", "."):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"{base}/api/run?id={urllib.parse.quote(path)}", timeout=5)
                    self.assertEqual(caught.exception.code, 400)

            self.assertEqual((run_dir / "trace.jsonl").read_bytes(), before)
            self.assertEqual((run_dir / "trace.jsonl").stat().st_mtime_ns, before_mtime)

    def test_degraded_and_legacy_runs_remain_visible_without_fabricated_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            workflow = yaml.safe_dump({
                "version": 1, "workflow": "integrity-proof",
                "steps": [{"id": "only", "cmd": 'printf ok > "$OUT"'}],
            }, sort_keys=False).encode()
            steps.write_bytes(workflow)
            corrupt = make_bundle(root, "run-corrupt", workflow, ["only"], complete=False)
            lines = (corrupt / "trace.jsonl").read_text().splitlines()
            event = json.loads(lines[0]); event["schema"] = "unsupported.trace.v99"
            lines[0] = json.dumps(event)
            (corrupt / "trace.jsonl").write_text("\n".join(lines) + "\n")

            schema_bad = make_bundle(root, "run-schema-bad", workflow, ["only"], complete=True)
            state = json.loads((schema_bad / "state.json").read_text())
            state["resume_count"] = [1]
            (schema_bad / "state.json").write_text(json.dumps(state) + "\n")

            empty_manifest = make_bundle(root, "run-empty-manifest", workflow, ["only"], complete=False)
            (empty_manifest / "manifest.json").write_text("{}\n")
            empty_state = make_bundle(root, "run-empty-state", workflow, ["only"], complete=False)
            (empty_state / "state.json").write_text("{}\n")
            invalid_date = make_bundle(root, "run-invalid-date", workflow, ["only"], complete=False)
            state = json.loads((invalid_date / "state.json").read_text())
            state["updated_at"] = "not-a-date"
            (invalid_date / "state.json").write_text(json.dumps(state) + "\n")
            fifo = make_bundle(root, "run-fifo", workflow, ["only"], complete=False)
            (fifo / "workflow.yaml").unlink()
            os.mkfifo(fifo / "workflow.yaml")

            symlinked = make_bundle(root, "run-symlink", workflow, ["only"], complete=False)
            secret = root / "outside-secret.txt"
            secret.write_text("must never enter Studio JSON")
            (symlinked / "input.txt").unlink()
            (symlinked / "input.txt").symlink_to(secret)

            partial = root / "runs" / "run-partial"
            partial.mkdir(parents=True)
            (partial / "workflow.yaml").write_bytes(workflow)
            (partial / "log.md").write_text("# initialization crashed\n")

            legacy = root / "runs" / "run-legacy"
            legacy.mkdir(parents=True)
            (legacy / "ledger.json").write_text("[]\n")
            (legacy / "log.md").write_text("# pre-bundle run\n")

            outside = root / "outside"
            outside.mkdir()
            (root / "runs" / "run-link").symlink_to(outside, target_is_directory=True)

            with studio(steps) as base:
                index = get_json(f"{base}/api/runs")
                by_id = {item["id"]: item for item in index["runs"]}
                self.assertEqual(by_id["run-corrupt"]["integrity"], "durable", by_id["run-corrupt"]["degraded_reason"])
                self.assertEqual(by_id["run-partial"]["integrity"], "degraded")
                self.assertEqual(by_id["run-schema-bad"]["integrity"], "degraded")
                self.assertEqual(by_id["run-schema-bad"]["resume_count"], 0)
                for run_id in ("run-empty-manifest", "run-empty-state", "run-invalid-date", "run-fifo", "run-symlink"):
                    self.assertEqual(by_id[run_id]["integrity"], "degraded", run_id)
                    self.assertFalse(get_json(f"{base}/api/run?id={run_id}")["resume"]["eligible"])
                self.assertEqual(by_id["run-legacy"]["integrity"], "legacy")
                self.assertNotIn("run-link", by_id)

                corrupt_payload = get_json(f"{base}/api/run?id=run-corrupt")
                self.assertFalse(corrupt_payload["ok"])
                self.assertEqual(corrupt_payload["run"]["integrity"], "degraded")
                self.assertIn("identity checks", corrupt_payload["run"]["degraded_reason"])
                self.assertFalse(corrupt_payload["resume"]["eligible"])

                symlink_payload = get_json(f"{base}/api/run?id=run-symlink")
                self.assertNotIn("must never enter Studio JSON", json.dumps(symlink_payload))
                self.assertIn("withheld", symlink_payload["input_text"])

                legacy_payload = get_json(f"{base}/api/run?id=run-legacy")
                self.assertEqual(legacy_payload["run"]["integrity"], "legacy")
                self.assertEqual(legacy_payload["trace_total"], 0)
                self.assertFalse(legacy_payload["resume"]["eligible"])

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/api/run?id=run-link", timeout=5)
                self.assertEqual(caught.exception.code, 400)

    def test_mutation_body_session_and_dom_injection_bounds_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "request-bounds",
                "steps": [{"id": "only", "cmd": 'printf ok > "$OUT"'}],
            }, sort_keys=False))
            with studio(steps) as base:
                page = urllib.request.urlopen(base, timeout=5).read().decode()
                token = json.loads(re.search(
                    r'<script id="piw-boot" type="application/json">(.*?)</script>', page,
                ).group(1))["token"]
                for body in (b"[]", b'{"content":"ok","extra":true}', b'{"wrong":"field"}'):
                    request = urllib.request.Request(
                        f"{base}/api/run", data=body, method="POST",
                        headers={"Content-Type": "application/json", "X-Piw-Token": token},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, 400)
                request = urllib.request.Request(
                    f"{base}/api/run", data=b"x" * (2 * 1024 * 1024 + 1), method="POST",
                    headers={"Content-Type": "application/json", "X-Piw-Token": token},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 413)

            class Done:
                @staticmethod
                def poll() -> int:
                    return 0
            saved = serve_workflow.SESSIONS
            try:
                serve_workflow.SESSIONS = {f"s{index}": {"proc": Done()} for index in range(serve_workflow.MAX_SESSIONS)}
                serve_workflow._prune_sessions()
                self.assertEqual(len(serve_workflow.SESSIONS), serve_workflow.MAX_SESSIONS - 1)
            finally:
                serve_workflow.SESSIONS = saved

            script = (ROOT / "ui" / "app.js").read_text()
            self.assertNotIn(".innerHTML", script)
            self.assertNotIn("insertAdjacentHTML", script)
            self.assertIn("app.sessionTimer = setTimeout(pollSession, delay)", script)
            self.assertIn("clearTimeout(app.sessionTimer)", script)

    def test_snapshot_parses_the_same_workflow_bytes_it_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = root / "steps.yaml"
            workflow.write_text(yaml.safe_dump({
                "version": 1, "workflow": "trusted",
                "steps": [{"id": "trusted", "cmd": 'printf ok > "$OUT"'}],
            }, sort_keys=False))
            run_dir = make_bundle(root, "run-byte-identity", workflow.read_bytes(), ["trusted"], complete=False)
            original = (run_dir / "workflow.yaml").read_bytes()
            attacker = yaml.safe_dump({
                "version": 1, "workflow": "attacker",
                "steps": [{"id": "attacker", "cmd": 'printf bad > "$OUT"'}],
            }, sort_keys=False).encode()
            saved_cfg = serve_workflow.CFG
            real_safe_bytes = serve_workflow._safe_bytes
            workflow_reads = 0

            def swapped_after_fingerprint(path: Path, limit: int):
                nonlocal workflow_reads
                if path == run_dir / "workflow.yaml":
                    workflow_reads += 1
                    return (original if workflow_reads == 1 else attacker), None
                return real_safe_bytes(path, limit)

            try:
                serve_workflow.CFG = {"steps": workflow, "output": "trusted", "temp": root}
                with mock.patch.object(serve_workflow, "_safe_bytes", side_effect=swapped_after_fingerprint):
                    payload = serve_workflow.run_snapshot("run-byte-identity")
                self.assertEqual(workflow_reads, 1)
                self.assertEqual(payload["graph"]["workflow"], "trusted")
                self.assertEqual([node["id"] for node in payload["graph"]["nodes"]], ["trusted"])
            finally:
                serve_workflow.CFG = saved_cfg

    def test_concurrent_launches_reserve_capacity_atomically(self) -> None:
        class Active:
            stdout: tuple = ()
            returncode = None

            @staticmethod
            def poll() -> None:
                return None

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text("version: 1\nworkflow: cap\nsteps: []\n")
            saved_sessions, saved_cfg = serve_workflow.SESSIONS, serve_workflow.CFG
            try:
                serve_workflow.SESSIONS = {
                    f"active-{index}": {"proc": Active()} for index in range(serve_workflow.MAX_ACTIVE - 1)
                }
                serve_workflow.CFG = {"temp": root, "steps": steps, "output": "only"}
                barrier = threading.Barrier(2)
                successes: list[str] = []
                errors: list[str] = []

                def slow_popen(*_args, **_kwargs):
                    time.sleep(0.1)
                    return Active()

                def launch() -> None:
                    barrier.wait()
                    try:
                        successes.append(serve_workflow.start_run(""))
                    except RuntimeError as error:
                        errors.append(str(error))

                with mock.patch.object(serve_workflow.subprocess, "Popen", side_effect=slow_popen):
                    threads = [threading.Thread(target=launch) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=2)
                self.assertEqual(len(successes), 1)
                self.assertEqual(len(errors), 1)
                self.assertIn("already active", errors[0])
                self.assertEqual(len(serve_workflow.SESSIONS), serve_workflow.MAX_ACTIVE)
            finally:
                serve_workflow.SESSIONS, serve_workflow.CFG = saved_sessions, saved_cfg

    def test_studio_refuses_a_rebound_host_and_never_leaks_the_run_token(self) -> None:
        """Binding to 127.0.0.1 does not stop DNS rebinding.

        Once an attacker domain resolves to loopback their page is same-origin,
        so SOP and CSP no longer apply and `GET /` would hand out the token that
        authorizes `POST /api/run` — which spends money and runs shell steps.
        Only a Host check stops it.
        """
        with tempfile.TemporaryDirectory() as raw:
            workflow = Path(raw)
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "host-guard",
                "input": {"required": True, "description": "Name"},
                "steps": [{"id": "copy", "cmd": 'cat "$INPUT"', "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PI_GRAPH_ROOTS": raw, "LOOPS_PORT": "1"}
            process = subprocess.Popen(
                [sys.executable, str(SERVER), str(steps), "--port", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                base = re.search(r"(http://127\.0\.0\.1:\d+)$", line).group(1)
                port = base.rsplit(":", 1)[1]

                for host in ("evil.example", f"evil.example:{port}", "attacker.test"):
                    request = urllib.request.Request(base, headers={"Host": host})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, 403, host)
                    self.assertNotIn("token", caught.exception.read().decode(), host)

                # A rebound origin must not reach the endpoint that spends money.
                run = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps({"content": "Ada"}).encode(),
                    headers={"Content-Type": "application/json",
                             "X-Piw-Token": "irrelevant", "Host": "evil.example"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(run, timeout=5)
                self.assertEqual(caught.exception.code, 403)

                # The legitimate loopback names still work and still boot.
                for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    request = urllib.request.Request(base, headers={"Host": host})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200, host)
                        self.assertIn("piw-boot", response.read().decode(), host)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
