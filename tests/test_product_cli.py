from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_steps.py"
CLI = ROOT / "scripts" / "piw.py"


class ProductCliTests(unittest.TestCase):
    def test_schema_exposes_node_kinds_and_every_runtime_input(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "schema", "--json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        schema = json.loads(result.stdout)
        metadata = schema["x-pi-workflows"]
        self.assertEqual(
            [node["kind"] for node in metadata["nodeKinds"]],
            ["command", "llm", "tool", "agent", "qa"],
        )
        self.assertEqual(
            set(metadata["runtimeInputs"]["prompt"]),
            {"{input}", "{step.ID}", "{prev}", "{run}"},
        )
        self.assertEqual(
            set(metadata["runtimeInputs"]["commandAndGate"]),
            {"$INPUT", "$PI_WORKFLOWS_INPUT", "$OUT", "$RUN", "$STEP"},
        )

    def test_validate_rejects_ambiguous_node_kind_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            steps = Path(raw) / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "ambiguous",
                "steps": [{"id": "both", "cmd": "true", "prompt": "do it"}],
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CLI), "validate", str(steps), "--json"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one of cmd or prompt", result.stdout)

    def test_concurrent_runs_keep_inputs_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "workflow": "input-isolation",
                "input": {"required": True, "description": "test value"},
                "steps": [{
                    "id": "copy",
                    "cmd": 'sleep 0.1; cat "$INPUT"',
                    "gate": 'test "$(cat "$OUT")" = "$(cat "$INPUT")"',
                }],
            }, sort_keys=False), encoding="utf-8")

            first = subprocess.Popen(
                [sys.executable, str(RUNNER), str(steps), "--input", "alpha"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            second = subprocess.Popen(
                [sys.executable, str(RUNNER), str(steps), "--input", "beta"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            first_output = first.communicate(timeout=30)
            second_output = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_output)
            self.assertEqual(second.returncode, 0, second_output)

            runs = sorted((root / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            observed = {(run / "input.txt").read_text(): (run / "copy.md").read_text() for run in runs}
            self.assertEqual(observed, {"alpha": "alpha", "beta": "beta"})
            self.assertFalse((root / ".loops-input.txt").exists())

    def test_create_emits_a_valid_explicit_input_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "triage"
            env = {**os.environ, "PI_WORKFLOWS_ROOTS": raw}
            created = subprocess.run(
                [sys.executable, str(CLI), "create", "Triage", "--dir", str(target), "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            payload = json.loads(created.stdout)
            self.assertEqual(payload["id"], "triage")

            spec = yaml.safe_load((target / "steps.yaml").read_text(encoding="utf-8"))
            self.assertEqual(spec["version"], 1)
            self.assertEqual(spec["input"]["required"], True)
            self.assertIn("{input}", spec["steps"][0]["prompt"])

            validated = subprocess.run(
                [sys.executable, str(CLI), "validate", str(target / "steps.yaml"), "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["holds"])

    def test_required_input_fails_before_any_step_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "workflow": "required-input",
                "input": {"required": True},
                "steps": [{"id": "never", "cmd": "exit 99"}],
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(RUNNER), str(steps)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires --input", result.stderr)

    def test_run_json_is_one_machine_parseable_document(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "json-output",
                "input": {"required": True, "description": "value"},
                "steps": [{"id": "copy", "cmd": 'cat "$INPUT"', "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            env = {
                **os.environ,
                "PI_WORKFLOWS_ROOTS": raw,
                "LOOPS_PORT": "1",
                "PI_WORKFLOWS_STATE_DIR": str(root / "state"),
            }
            result = subprocess.run(
                [sys.executable, str(CLI), "run", str(steps), "--input", "Ada", "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["passed"], 1)

    def test_final_qa_usage_is_in_the_machine_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_pi = fake_bin / "pi"
            args_path = root / "pi-args.txt"
            fake_pi.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_PI_ARGS\"\n"
                "printf '%s\\n' '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\","
                "\"provider\":\"test\",\"model\":\"luna\",\"stopReason\":\"stop\",\"content\":[{\"type\":\"text\","
                "\"text\":\"{\\\"verdict\\\":\\\"pass\\\",\\\"issues\\\":[]}\"}],"
                "\"usage\":{\"input\":5,\"output\":2,\"totalTokens\":7,"
                "\"cost\":{\"total\":0.001}}}}'\n"
                "printf '%s\\n' '{\"type\":\"agent_settled\"}'\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "qa-ledger",
                "model": "test/luna",
                "steps": [{"id": "artifact", "cmd": "printf artifact"}],
                "qa": {"model": "test/luna", "prompt": "Review: {artifacts}"},
            }, sort_keys=False), encoding="utf-8")
            env = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_PI_ARGS": str(args_path),
            }
            result = subprocess.run(
                [sys.executable, str(RUNNER), str(steps)],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            ledger_path = next((root / "runs").glob("*/ledger.json"))
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            qa = next(entry for entry in ledger if entry["id"] == "__qa__")
            self.assertEqual(qa["total"], 7)
            self.assertEqual(qa["cost"], 0.001)
            self.assertTrue(qa["passed"])
            pi_args = args_path.read_text(encoding="utf-8").splitlines()
            for flag in ("--no-session", "--no-approve", "--offline", "--no-extensions", "--no-skills"):
                self.assertIn(flag, pi_args)

    def test_model_step_rejects_an_unsettled_pi_json_stream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_pi = fake_bin / "pi"
            fake_pi.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\","
                "\"provider\":\"test\",\"model\":\"luna\",\"stopReason\":\"stop\","
                "\"content\":[{\"type\":\"text\",\"text\":\"looks finished\"}]}}'\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "unsettled",
                "model": "test/luna",
                "steps": [{"id": "draft", "prompt": "Return text", "retries": 0}],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                [sys.executable, str(RUNNER), str(steps)],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not settle", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
