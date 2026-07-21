from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "piw.py"


def five_step_workflow() -> dict:
    return {
        "version": 1,
        "workflow": "exact-five",
        "workers": 2,
        "input": {"required": True, "description": "one corpus record"},
        "steps": [
            {"id": "one", "cmd": 'cat "$INPUT"', "gate": 'test -s "$OUT"'},
            {"id": "two", "needs": ["one"], "cmd": 'sed "s/^/two:/" "$RUN/one.md"', "gate": 'grep -q "^two:" "$OUT"'},
            {"id": "three", "needs": ["two"], "cmd": 'sed "s/^/three:/" "$RUN/two.md"', "gate": 'grep -q "^three:two:" "$OUT"'},
            {"id": "four", "needs": ["three"], "cmd": 'sed "s/^/four:/" "$RUN/three.md"', "gate": 'grep -q "^four:three:two:" "$OUT"'},
            {"id": "five", "needs": ["four"], "cmd": 'sed "s/^/five:/" "$RUN/four.md"', "gate": 'grep -q "^five:four:three:two:" "$OUT"'},
        ],
    }


class BatchTests(unittest.TestCase):
    def run_cli(self, *arguments: str, timeout: float = 60,
                env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "LOOPS_PORT": "1",
            "PI_GRAPH_STATE_DIR": str(Path(tempfile.gettempdir()) / "piw-batch-test-state"),
            **(env_overrides or {}),
        }
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            text=True, capture_output=True, check=False, timeout=timeout, env=env,
        )

    def test_batch_runs_every_declared_step_for_each_immutable_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump(five_step_workflow(), sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": f"item-{index}", "content": f"value-{index}"}) + "\n"
                for index in range(1, 13)
            ), encoding="utf-8")
            batch = root / "receipt"

            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "4", "--require-all", "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["passed"], 12)
            self.assertEqual(summary["contract_complete"], 12)
            self.assertEqual(summary["all_steps_passed"], 12)
            self.assertEqual(summary["not_run"], 0)
            self.assertNotIn("results", summary)
            details = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
            self.assertEqual(
                details["results"][0]["expected_steps"],
                ["one", "two", "three", "four", "five"],
            )
            for item in details["results"]:
                run_dir = Path(item["run_dir"])
                self.assertEqual(item["passed_steps"], item["expected_steps"])
                self.assertTrue((run_dir / "events.jsonl").is_file())
                self.assertTrue((run_dir / "ledger.json").is_file())
                self.assertFalse((run_dir / ".git").exists())
                source = f"value-{item['index']}"
                self.assertEqual((run_dir / "input.txt").read_text(encoding="utf-8"), source)
                self.assertEqual((run_dir / "five.md").read_text(encoding="utf-8"), f"five:four:three:two:{source}")

    def test_resume_skips_passed_items_and_retries_only_incomplete_items(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "resume-bulk",
                "input": {"required": True, "description": "one timed item"},
                "steps": [{
                    "id": "copy",
                    # The slow item must be unambiguously slower than the timeout,
                    # and the fast one unambiguously faster. This used to give the
                    # fast item 100ms total, which is less than interpreter startup,
                    # so it failed on any loaded machine.
                    "cmd": 'if grep -q slow "$INPUT"; then sleep 5; fi; cat "$INPUT"',
                    "gate": 'test -s "$OUT"',
                    "retries": 0,
                }],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"id": "fast", "content": "fast"}) + "\n"
                + json.dumps({"id": "slow", "content": "slow"}) + "\n",
                encoding="utf-8",
            )
            batch = root / "receipt"

            first = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "1", "--item-timeout", "2", "--require-all", "--json",
            )
            self.assertEqual(first.returncode, 1, first.stderr + first.stdout)
            first_summary = json.loads(first.stdout)
            self.assertEqual(first_summary["passed"], 1)
            self.assertEqual(first_summary["failed"], 1)

            resumed = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--resume", str(batch),
                "--parallel", "1", "--item-timeout", "15", "--require-all", "--json",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
            summary = json.loads(resumed.stdout)
            self.assertEqual(summary["passed"], 2)
            details = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
            by_id = {item["id"]: item for item in details["results"]}
            self.assertEqual(by_id["fast"]["attempt"], 1)
            self.assertEqual(by_id["slow"]["attempt"], 2)

    def test_resume_refuses_a_changed_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump(five_step_workflow(), sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text(json.dumps({"id": "one", "content": "before"}) + "\n", encoding="utf-8")
            batch = root / "receipt"
            first = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch), "--json",
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)

            corpus.write_text(json.dumps({"id": "one", "content": "after"}) + "\n", encoding="utf-8")
            resumed = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--resume", str(batch), "--json",
            )
            self.assertEqual(resumed.returncode, 2)
            self.assertIn("frozen batch contract changed", resumed.stdout)

    def test_duplicate_item_ids_fail_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump(five_step_workflow(), sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"id": "same", "content": "one"}) + "\n"
                + json.dumps({"id": "same", "content": "two"}) + "\n",
                encoding="utf-8",
            )
            result = self.run_cli("batch", str(steps), "--inputs", str(corpus), "--json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate item id", result.stdout)

    def test_require_all_fails_an_item_when_a_declared_branch_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "strict-route",
                "input": {"required": True, "description": "route fixture"},
                "steps": [
                    {
                        "id": "classify", "cmd": "printf '{\"kind\":\"skip\"}'",
                        "schema": {"kind": {"type": "string", "enum": ["skip"]}},
                        "gate": 'grep -q kind "$OUT"',
                    },
                    {
                        "id": "branch", "needs": ["classify"], "from": "classify",
                        "when": {"op": "equals", "path": "/kind", "value": "run"},
                        "cmd": "printf ran", "gate": 'test -s "$OUT"',
                    },
                ],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text(json.dumps({"id": "one", "content": "route"}) + "\n", encoding="utf-8")
            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(root / "batch"),
                "--require-all", "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            receipt = json.loads((root / "batch" / "batch.json").read_text(encoding="utf-8"))["results"][0]
            self.assertTrue(receipt["contract_complete"])
            self.assertFalse(receipt["all_steps_passed"])
            self.assertEqual(receipt["skipped_steps"], ["branch"])

    def test_failure_ceiling_stops_dispatching_the_remaining_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "fail-fast-bulk",
                "input": {"required": True, "description": "failure fixture"},
                "steps": [{"id": "fail", "cmd": "exit 9", "retries": 0}],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": str(index), "content": str(index)}) + "\n"
                for index in range(20)
            ), encoding="utf-8")
            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(root / "batch"),
                "--parallel", "2", "--stop-after-failures", "1", "--json",
            )
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "stopped")
            self.assertGreaterEqual(summary["failed"], 1)
            self.assertGreaterEqual(summary["not_run"], 18)

    def test_detached_batch_finishes_without_holding_the_call_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump(five_step_workflow(), sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": str(index), "content": f"value-{index}"}) + "\n"
                for index in range(3)
            ), encoding="utf-8")
            batch = root / "detached"
            launched = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "2", "--require-all", "--detach", "--json",
            )
            self.assertEqual(launched.returncode, 0, launched.stderr + launched.stdout)
            launch = json.loads(launched.stdout)
            self.assertEqual(launch["status"], "starting")
            deadline = time.monotonic() + 10
            state = None
            while time.monotonic() < deadline:
                checked = self.run_cli("batch-status", str(batch), "--json")
                if checked.stdout:
                    state = json.loads(checked.stdout)
                    if state["status"] == "completed":
                        break
                time.sleep(0.05)
            self.assertIsNotNone(state)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["passed"], 3)
            self.assertEqual(state["all_steps_passed"], 3)
            # "completed" is written before the detached worker exits; wait for
            # the process to die or its final artifact writes race the tempdir
            # cleanup below.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(launch["pid"], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("detached batch process did not exit after completion")

    def test_parallel_shared_workspace_steps_require_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            corpus = root / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"id": "one", "content": "one"}) + "\n"
                + json.dumps({"id": "two", "content": "two"}) + "\n",
                encoding="utf-8",
            )
            risky_specs = {
                "agent": {
                    "id": "mutate", "prompt": "Make the requested change.", "agent": True,
                    "gate": "test -s \"$OUT\"",
                },
                "produces": {
                    "id": "export", "cmd": "printf artifact > shared.txt; printf ok",
                    "produces": ["shared.txt"], "gate": "test -s \"$OUT\"",
                },
            }
            for name, step in risky_specs.items():
                with self.subTest(name=name):
                    workflow_dir = root / name
                    workflow_dir.mkdir()
                    steps = workflow_dir / "steps.yaml"
                    steps.write_text(yaml.safe_dump({
                        "version": 1, "workflow": f"risky-{name}", "steps": [step],
                    }, sort_keys=False), encoding="utf-8")
                    # Items are isolated by default now, so these no longer need
                    # to be refused — they simply cannot see each other.
                    isolated = self.run_cli(
                        "batch", str(steps), "--inputs", str(corpus), "--parallel", "2", "--json",
                    )
                    self.assertNotIn(
                        "may race in the shared workflow workspace",
                        isolated.stderr + isolated.stdout,
                    )

            # Opting back into one shared directory is allowed, but warns.
            allowed = self.run_cli(
                "batch", str(root / "produces" / "steps.yaml"), "--inputs", str(corpus),
                "--parallel", "2", "--allow-shared-workspace", "--out", str(root / "allowed"),
                "--json",
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr + allowed.stdout)
            self.assertIn("race in one directory", allowed.stderr)

    def test_parallel_items_never_share_a_working_directory(self) -> None:
        """Every item used to run in the same cwd.

        With --parallel 4, four items each wrote ./scratch.txt and all four
        exported the winner's content while the batch reported ok/passed —
        silent corruption behind a green receipt.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "isolation",
                "input": {"required": True, "description": "one item"},
                "steps": [{
                    "id": "work",
                    "cmd": 'cat "$INPUT" > scratch.txt\nsleep 0.3\ncp scratch.txt "$OUT"',
                    "gate": 'test -s "$OUT"',
                }],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": name, "content": name}) + "\n"
                for name in ("item-A", "item-B", "item-C", "item-D")
            ), encoding="utf-8")

            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--parallel", "4",
                "--require-all", "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            batch_dir = Path(json.loads(result.stdout)["batch_dir"])
            produced = {
                path.read_text(encoding="utf-8").strip()
                for path in batch_dir.glob("items/*/attempts/001/work.md")
            }
            self.assertEqual(
                produced, {"item-A", "item-B", "item-C", "item-D"},
                f"items overwrote each other in a shared workspace: {sorted(produced)}",
            )

    def test_cancel_stops_every_active_item_group_and_persists_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            effect = root / "effect.txt"
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "cancel-active-items",
                "input": {"required": True, "description": "cancellation fixture"},
                "steps": [{
                    "id": "slow",
                    "cmd": f"printf '%s' \"$$\" > \"$RUN/shell.pid\"; sleep 30; printf effect >> {effect}",
                    "gate": 'test -s "$OUT"',
                    "retries": 0,
                }],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": str(index), "content": str(index)}) + "\n"
                for index in range(6)
            ), encoding="utf-8")
            batch = root / "detached"
            launched = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "2", "--detach", "--json",
            )
            self.assertEqual(launched.returncode, 0, launched.stderr + launched.stdout)

            deadline = time.monotonic() + 10
            pid_files: list[Path] = []
            while time.monotonic() < deadline:
                pid_files = list((batch / "items").glob("*/attempts/*/shell.pid"))
                if len(pid_files) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(len(pid_files), 2, "two item runners never became active")
            child_pids = [int(path.read_text(encoding="utf-8")) for path in pid_files]

            cancelled = self.run_cli("batch-cancel", str(batch), "--json")
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr + cancelled.stdout)
            self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelling")

            state = None
            while time.monotonic() < deadline:
                checked = self.run_cli("batch-status", str(batch), "--json")
                if checked.stdout:
                    state = json.loads(checked.stdout)
                    if state["status"] == "cancelled":
                        break
                time.sleep(0.05)
            self.assertIsNotNone(state)
            self.assertEqual(state["status"], "cancelled")
            self.assertFalse(state["ok"])
            self.assertEqual(state["cancelled"], 2)
            self.assertEqual(state["not_run"], 4)
            self.assertEqual(self.run_cli("batch-status", str(batch), "--json").returncode, 1)
            results = json.loads((batch / "batch.json").read_text(encoding="utf-8"))["results"]
            self.assertEqual({result["status"] for result in results}, {"cancelled"})
            self.assertTrue(all(result["exit"] == 130 for result in results))
            self.assertFalse(effect.exists(), "a cancelled item reached its external effect")

            for child_pid in child_pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_output_export_is_exact_cardinality_and_corpus_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "ordered-output",
                "input": {"required": True, "description": "ordered fixture"},
                "steps": [{
                    "id": "selected",
                    "cmd": 'grep -q slow "$INPUT" && sleep 0.2 || true; cat "$INPUT"',
                    "gate": 'test -s "$OUT"',
                }, {
                    "id": "after", "needs": ["selected"], "cmd": "printf done",
                }],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join([
                json.dumps({"id": "first", "content": "slow first"}) + "\n",
                json.dumps({"id": "second", "content": "fast second"}) + "\n",
                json.dumps({"id": "third", "content": "fast third"}) + "\n",
            ]), encoding="utf-8")
            batch = root / "batch"
            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "3", "--output-step", "selected", "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["output_rows"], 3)
            self.assertEqual(receipt["outputs_exported"], 3)
            rows = [json.loads(line) for line in (batch / "outputs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["id"] for row in rows], ["first", "second", "third"])
            self.assertEqual([row["output"] for row in rows], ["slow first", "fast second", "fast third"])
            manifest = json.loads((batch / "outputs.manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["results_sha256"], receipt["outputs_sha256"])

            rejected = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--output-step", "missing", "--json",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("references unknown step", rejected.stdout)

    def test_recorded_token_budget_stops_new_dispatch_and_reports_overshoot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_pi = fake_bin / "pi"
            fake_pi.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\","
                "\"provider\":\"test\",\"model\":\"luna\",\"stopReason\":\"stop\","
                "\"content\":[{\"type\":\"text\",\"text\":\"ok\"}],"
                "\"usage\":{\"input\":5,\"output\":2,\"totalTokens\":7,"
                "\"cost\":{\"total\":0.001}}}}'\n"
                "printf '%s\\n' '{\"type\":\"agent_settled\"}'\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "token-budget", "model": "test/luna",
                "input": {"required": True, "description": "budget fixture"},
                "steps": [{"id": "call", "prompt": "Return ok for {input}", "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            corpus = root / "corpus.jsonl"
            corpus.write_text("".join(
                json.dumps({"id": str(index), "content": str(index)}) + "\n" for index in range(6)
            ), encoding="utf-8")
            batch = root / "batch"
            result = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--out", str(batch),
                "--parallel", "1", "--max-tokens", "10", "--json",
                env_overrides={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "budget_exhausted")
            self.assertEqual(receipt["completed"], 2)
            self.assertEqual(receipt["not_run"], 4)
            self.assertEqual(receipt["tokens"], 14)
            self.assertEqual(receipt["token_overshoot"], 4)
            self.assertIn("token budget reached", receipt["stop_reason"])


if __name__ == "__main__":
    unittest.main()
