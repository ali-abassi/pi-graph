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
    def run_cli(self, *arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "LOOPS_PORT": "1",
            "PI_WORKFLOWS_STATE_DIR": str(Path(tempfile.gettempdir()) / "piw-batch-test-state"),
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
                    "cmd": 'if grep -q slow "$INPUT"; then sleep 0.3; fi; cat "$INPUT"',
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
                "--parallel", "1", "--item-timeout", "0.1", "--require-all", "--json",
            )
            self.assertEqual(first.returncode, 1, first.stderr + first.stdout)
            first_summary = json.loads(first.stdout)
            self.assertEqual(first_summary["passed"], 1)
            self.assertEqual(first_summary["failed"], 1)

            resumed = self.run_cli(
                "batch", str(steps), "--inputs", str(corpus), "--resume", str(batch),
                "--parallel", "1", "--item-timeout", "2", "--require-all", "--json",
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


if __name__ == "__main__":
    unittest.main()
