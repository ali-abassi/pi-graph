from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNNER = SCRIPTS / "run_steps.py"
CLI = SCRIPTS / "piw.py"
sys.path.insert(0, str(SCRIPTS))

from run_bundle import (  # noqa: E402
    BundleError,
    RunBundle,
    atomic_write_json,
)


def workflow(path: Path, steps: list[dict], *, required_input: bool = False) -> None:
    value: dict = {"version": 1, "workflow": "durable-test", "steps": steps}
    if required_input:
        value["input"] = {"required": True, "description": "immutable test input"}
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def run_low_level(steps: Path, run_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), str(steps), "--run-dir", str(run_dir),
         "--no-cache", "--no-history", *extra],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PI_GRAPH_ROOTS": str(steps.parent)},
    )


class BundleKernelTests(unittest.TestCase):
    def initialize(self, root: Path, step_ids: list[str] | None = None) -> RunBundle:
        source = b"version: 1\nworkflow: kernel\nsteps: []\n"
        (root / "run").mkdir()
        bundle = RunBundle(root / "run").acquire()
        bundle.initialize(
            workflow_bytes=source, source_path=root / "steps.yaml",
            input_bytes=b"fixed", cwd=root, workflow_dir=root,
            step_ids=step_ids or ["one"], events_path=None,
        )
        return bundle

    def test_snapshot_and_projections_validate_against_published_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.initialize(root)
            bundle.record("run_started", run_status="running")
            manifest = json.loads((bundle.run_dir / "manifest.json").read_text())
            state = json.loads((bundle.run_dir / "state.json").read_text())
            event = json.loads((bundle.run_dir / "trace.jsonl").read_text().splitlines()[0])
            for filename, value in [
                ("run-bundle.schema.json", manifest),
                ("run-state.schema.json", state),
                ("trace-event.schema.json", event),
            ]:
                schema = json.loads((ROOT / "schemas" / filename).read_text())
                self.assertEqual(list(Draft202012Validator(schema).iter_errors(value)), [])
            self.assertEqual((bundle.run_dir / "workflow.yaml").read_bytes(),
                             b"version: 1\nworkflow: kernel\nsteps: []\n")
            self.assertEqual((bundle.run_dir / "input.txt").read_bytes(), b"fixed")
            bundle.close()

    def test_failed_atomic_replacement_preserves_previous_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            atomic_write_json(path, {"generation": 1})
            import run_bundle
            original = run_bundle.os.replace
            run_bundle.os.replace = lambda *_: (_ for _ in ()).throw(OSError("disk fault"))
            try:
                with self.assertRaisesRegex(OSError, "disk fault"):
                    atomic_write_json(path, {"generation": 2})
            finally:
                run_bundle.os.replace = original
            self.assertEqual(json.loads(path.read_text()), {"generation": 1})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_initialization_manifest_failure_recovers_from_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "run").mkdir()
            bundle = RunBundle(root / "run").acquire()
            import run_bundle
            original = run_bundle.atomic_write_json
            failed = False
            def fail_manifest_once(path: Path, value: object) -> None:
                nonlocal failed
                if path.name == "manifest.json" and not failed:
                    failed = True
                    raise OSError("manifest fault")
                original(path, value)
            run_bundle.atomic_write_json = fail_manifest_once
            try:
                with self.assertRaisesRegex(OSError, "manifest fault"):
                    bundle.initialize(
                        workflow_bytes=b"version: 1\nworkflow: init\nsteps: []\n",
                        source_path=root / "steps.yaml", input_bytes=None,
                        cwd=root, workflow_dir=root, step_ids=["one"], events_path=None,
                    )
            finally:
                run_bundle.atomic_write_json = original
                bundle.close()
            reopened = RunBundle(root / "run").acquire()
            reopened.load()
            self.assertEqual(reopened.state["trace_seq"], 0)
            self.assertEqual(reopened.manifest["status"], "initialized")
            self.assertFalse((root / "run" / ".bundle-bootstrap.json").exists())
            reopened.close()

    def test_manifest_projection_failure_never_reuses_committed_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.initialize(Path(raw))
            import run_bundle
            original = run_bundle.atomic_write_json
            failed = False
            def fail_manifest_once(path: Path, value: object) -> None:
                nonlocal failed
                if path.name == "manifest.json" and not failed:
                    failed = True
                    raise OSError("manifest fault")
                original(path, value)
            run_bundle.atomic_write_json = fail_manifest_once
            try:
                with self.assertRaisesRegex(BundleError, "reload"):
                    bundle.record("run_started", run_status="running")
                self.assertEqual(bundle.state["trace_seq"], 1)
                with self.assertRaisesRegex(BundleError, "reload"):
                    bundle.record("must_not_append")
            finally:
                run_bundle.atomic_write_json = original
                bundle.close()
            reopened = RunBundle(bundle.run_dir).acquire()
            reopened.load()
            reopened.record("after_reload")
            events = [json.loads(line) for line in reopened.trace_path.read_text().splitlines()]
            self.assertEqual([event["seq"] for event in events], [1, 2])
            reopened.close()

    def test_state_projection_failure_truncates_uncommitted_trace_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.initialize(Path(raw))
            import run_bundle
            original = run_bundle.atomic_write_json
            failed = False
            def fail_state_once(path: Path, value: object) -> None:
                nonlocal failed
                if path.name == "state.json" and not failed:
                    failed = True
                    raise OSError("state fault")
                original(path, value)
            run_bundle.atomic_write_json = fail_state_once
            try:
                with self.assertRaisesRegex(BundleError, "reload"):
                    bundle.record("uncommitted")
                self.assertEqual(bundle.state["trace_seq"], 0)
                with self.assertRaisesRegex(BundleError, "reload"):
                    bundle.record("must_not_reuse")
            finally:
                run_bundle.atomic_write_json = original
                bundle.close()
            reopened = RunBundle(bundle.run_dir).acquire()
            reopened.load()
            self.assertEqual(reopened.trace_path.read_text(), "")
            reopened.record("after_reload")
            events = [json.loads(line) for line in reopened.trace_path.read_text().splitlines()]
            self.assertEqual([event["seq"] for event in events], [1])
            reopened.close()

    def test_schema_invalid_committed_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.initialize(Path(raw))
            bundle.record("run_started", run_status="running")
            run_dir = bundle.run_dir
            bundle.close()
            event = json.loads((run_dir / "trace.jsonl").read_text())
            event["unexpected_committed_field"] = True
            (run_dir / "trace.jsonl").write_text(json.dumps(event) + "\n")
            reopened = RunBundle(run_dir).acquire()
            with self.assertRaisesRegex(BundleError, "schema invalid"):
                reopened.load()
            reopened.close()

    def test_owner_lock_excludes_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = self.initialize(root)
            before = sorted(p.name for p in first.run_dir.iterdir())
            with self.assertRaises(BundleError):
                RunBundle(first.run_dir).acquire()
            self.assertEqual(sorted(p.name for p in first.run_dir.iterdir()), before)
            first.close()
            second = RunBundle(first.run_dir).acquire()
            second.load()
            second.close()

    def test_parallel_transitions_have_contiguous_sequence_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.initialize(Path(raw), [f"s{i}" for i in range(20)])
            threads = [threading.Thread(
                target=bundle.record,
                args=("step_started",),
                kwargs={"step_id": f"s{i}", "step_status": "running"},
            ) for i in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            events = [json.loads(line) for line in
                      (bundle.run_dir / "trace.jsonl").read_text().splitlines()]
            self.assertEqual([event["seq"] for event in events], list(range(1, 21)))
            self.assertEqual(bundle.state["trace_seq"], 20)
            bundle.close()

    def test_torn_and_uncommitted_trace_tails_are_repaired_but_committed_loss_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.initialize(root)
            bundle.record("run_started", run_status="running")
            run_dir = bundle.run_dir
            bundle.close()
            with (run_dir / "trace.jsonl").open("ab") as handle:
                handle.write(b'{"schema":"pi-graph.trace-event.v1"')
            reopened = RunBundle(run_dir).acquire()
            reopened.load()
            self.assertEqual(len((run_dir / "trace.jsonl").read_text().splitlines()), 1)
            reopened.close()

            with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{malformed uncommitted json}\n")
            reopened = RunBundle(run_dir).acquire()
            reopened.load()
            self.assertEqual(len((run_dir / "trace.jsonl").read_text().splitlines()), 1)
            reopened.close()

            state = json.loads((run_dir / "state.json").read_text())
            state["trace_seq"] = 2
            atomic_write_json(run_dir / "state.json", state)
            broken = RunBundle(run_dir).acquire()
            with self.assertRaises(BundleError):
                broken.load()
            broken.close()


class DurableRunnerTests(unittest.TestCase):
    def test_source_drift_requires_force_and_input_drift_is_never_forceable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            run_dir = root / "run"
            marker = root / "marker"
            workflow(steps, [
                {"id": "one", "cmd": 'printf one > "$OUT"'},
                {"id": "two", "cmd": f'test -f "{marker}" || exit 7; printf two > "$OUT"'},
            ], required_input=True)
            first = run_low_level(steps, run_dir, "--input", "fixed")
            self.assertNotEqual(first.returncode, 0)

            changed = yaml.safe_load(steps.read_text())
            changed["steps"][1]["cmd"] = f'touch "{marker}"; printf two > "$OUT"'
            steps.write_text(yaml.safe_dump(changed, sort_keys=False))
            refused = run_low_level(steps, run_dir, "--resume")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("source drift", refused.stdout + refused.stderr)
            input_refused = run_low_level(
                steps, run_dir, "--resume", "--force-drift", "--input", "changed"
            )
            self.assertNotEqual(input_refused.returncode, 0)
            self.assertIn("immutable", input_refused.stdout + input_refused.stderr)
            forced = run_low_level(steps, run_dir, "--resume", "--force-drift")
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["resume_count"], 1)
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            self.assertIn("workflow_drift_forced", {event["type"] for event in events})

    def test_sigkill_releases_lock_and_resume_runs_only_unfinished_step(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            run_dir = root / "run"
            one_count, two_count, started = root / "one.count", root / "two.count", root / "started"
            workflow(steps, [
                {"id": "one", "cmd": f'echo x >> "{one_count}"; printf one > "$OUT"'},
                {"id": "two", "cmd": f'echo x >> "{two_count}"; touch "{started}"; sleep 1; printf two > "$OUT"'},
            ])
            process = subprocess.Popen(
                [sys.executable, str(RUNNER), str(steps), "--run-dir", str(run_dir),
                 "--no-cache", "--no-history"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "PI_GRAPH_ROOTS": str(root)},
            )
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            time.sleep(1.3)  # let the orphaned shell finish before explicit recovery
            resumed = run_low_level(steps, run_dir, "--resume")
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(len(one_count.read_text().splitlines()), 1)
            self.assertEqual(len(two_count.read_text().splitlines()), 2)
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["resume_count"], 1)

    def test_resume_preserves_cascaded_descendants_of_a_terminal_branch_skip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps, run_dir, marker = root / "steps.yaml", root / "run", root / "child-ran"
            workflow(steps, [
                {"id": "source", "cmd": "echo '{\"take\":false}' > \"$OUT\""},
                {"id": "branch", "needs": [], "from": "source",
                 "when": {"op": "equals", "path": "/take", "value": True},
                 "cmd": 'printf branch > "$OUT"'},
                {"id": "child", "needs": ["branch"],
                 "cmd": f'touch "{marker}"; printf child > "$OUT"'},
                {"id": "blocker", "needs": ["source"], "retries": 0, "cmd": "exit 5"},
            ])
            first = run_low_level(steps, run_dir)
            self.assertNotEqual(first.returncode, 0)
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["steps"]["child"]["status"], "skipped")
            changed = yaml.safe_load(steps.read_text())
            changed["steps"][3]["cmd"] = 'printf repaired > "$OUT"'
            steps.write_text(yaml.safe_dump(changed, sort_keys=False))
            resumed = run_low_level(steps, run_dir, "--resume", "--force-drift")
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertFalse(marker.exists(), "cascaded skipped child must remain terminal")

    def test_legacy_resume_is_refused_without_mutating_legacy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps, run_dir = root / "steps.yaml", root / "legacy"
            workflow(steps, [{"id": "one", "cmd": 'printf one > "$OUT"'}])
            run_dir.mkdir()
            (run_dir / "one.md").write_text("one")
            before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
            result = run_low_level(steps, run_dir, "--resume")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("legacy run has no durable resume boundary", result.stdout + result.stderr)
            self.assertEqual({p.name: p.read_bytes() for p in run_dir.iterdir()}, before)

    def test_cli_resume_json_is_one_document_and_legacy_readers_still_parse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            flow = root / "flow"
            flow.mkdir()
            steps = flow / "steps.yaml"
            marker = root / "ready"
            workflow(steps, [
                {"id": "one", "cmd": 'printf one > "$OUT"'},
                {"id": "two", "retries": 0,
                 "cmd": f'if ! test -f "{marker}"; then touch "{marker}"; exit 9; fi; printf two > "$OUT"'},
            ])
            first = subprocess.run(
                [sys.executable, str(CLI), "run", "flow", "--json", "--no-cache"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PI_GRAPH_ROOTS": str(root)},
            )
            self.assertNotEqual(first.returncode, 0)
            first_doc = json.loads(first.stdout)
            run_id = Path(first_doc["run_dir"]).name
            resumed = subprocess.run(
                [sys.executable, str(CLI), "resume", "flow", run_id, "--json"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PI_GRAPH_ROOTS": str(root)},
            )
            document = json.loads(resumed.stdout)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertTrue(document["ok"])
            detail = subprocess.run(
                [sys.executable, str(CLI), "detail", "flow", run_id, "--json"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PI_GRAPH_ROOTS": str(root)},
            )
            self.assertEqual(detail.returncode, 0, detail.stdout + detail.stderr)
            self.assertEqual(json.loads(detail.stdout)["run"]["id"], run_id)


if __name__ == "__main__":
    unittest.main()
