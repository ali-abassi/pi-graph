from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "piw.py"
SCHEMA = ROOT / "schemas" / "optimization-contract.schema.json"


def run_cli(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *argv], capture_output=True, text=True,
        cwd=str(cwd) if cwd else None, timeout=120, check=False,
    )


def make_workflow(root: Path) -> Path:
    """A minimal valid workflow directory with two corpora beside steps.yaml."""
    created = run_cli("create", "demo", "--dir", str(root / "demo"))
    assert created.returncode == 0, created.stderr
    (root / "demo" / "dev.jsonl").write_text(
        '{"content":"item one"}\n{"content":"item two"}\n', encoding="utf-8")
    (root / "demo" / "holdout.jsonl").write_text(
        '{"content":"holdout one"}\n', encoding="utf-8")
    return root / "demo"


class JsonErrorEnvelopeTests(unittest.TestCase):
    def test_unknown_workflow_emits_structured_error_on_stdout(self) -> None:
        done = run_cli("detail", "definitely-not-a-workflow", "--json")
        self.assertEqual(done.returncode, 2)
        payload = json.loads(done.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["schema"], "pi-graph.error.v1")
        self.assertIn("message", payload["error"])

    def test_human_mode_keeps_stderr_only(self) -> None:
        done = run_cli("detail", "definitely-not-a-workflow")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        self.assertIn("error:", done.stderr)

    def test_typoed_subcommand_suggests_near_match(self) -> None:
        done = run_cli("runn", "--json")
        self.assertEqual(done.returncode, 2)
        payload = json.loads(done.stdout)
        self.assertIn("did you mean 'run'", payload["error"]["message"])


class ModelsCommandTests(unittest.TestCase):
    def _run_with_fake_pi(self, *argv: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as raw:
            fake_bin = Path(raw) / "bin"
            fake_bin.mkdir()
            (fake_bin / "pi").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'provider      model                     context'\n"
                "printf '%s\\n' 'testco       alpha                     1M'\n"
                "printf '%s\\n' 'testco       beta                      8K'\n",
                encoding="utf-8",
            )
            (fake_bin / "pi").chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
            return subprocess.run(
                [sys.executable, str(CLI), *argv], capture_output=True, text=True,
                env=env, timeout=60, check=False,
            )

    def test_models_lists_ids_and_skips_the_header_row(self) -> None:
        done = self._run_with_fake_pi("models", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        ids = [item["id"] for item in payload["models"]]
        self.assertEqual(ids, ["testco/alpha", "testco/beta"])

    def test_check_known_and_unknown_ids(self) -> None:
        good = json.loads(self._run_with_fake_pi("models", "--check", "testco/alpha", "--json").stdout)
        self.assertTrue(good["check"]["known"])
        bad_done = self._run_with_fake_pi("models", "--check", "testco/alphaa", "--json")
        self.assertEqual(bad_done.returncode, 1)
        bad = json.loads(bad_done.stdout)
        self.assertFalse(bad["check"]["known"])
        self.assertEqual(bad["check"]["suggestion"], "testco/alpha")


class SetHintTests(unittest.TestCase):
    def test_set_unknown_step_lists_valid_step_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            demo = make_workflow(Path(raw))
            done = run_cli("set", str(demo / "steps.yaml"), "nosuchstep", "--model", "x/y")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("(steps: ", done.stderr)
            self.assertIn("produce", done.stderr)


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_contract_passes_init_and_hides_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            demo = make_workflow(Path(raw))
            scaffolded = run_cli(
                "optimize", "scaffold", str(demo / "steps.yaml"),
                "--inputs", str(demo / "dev.jsonl"), "--holdout", str(demo / "holdout.jsonl"),
                "--json",
            )
            self.assertEqual(scaffolded.returncode, 0, scaffolded.stderr)
            payload = json.loads(scaffolded.stdout)
            self.assertTrue(payload["ok"])
            contract_path = Path(payload["result"]["contract_path"])
            self.assertTrue((contract_path.parent / "optimization-eval.py").is_file())
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertIn("produce-prompt", [m["mechanism"] for m in contract["boundaries"]["mutable"]])

            from jsonschema import Draft202012Validator
            schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(contract)

            # The private holdout contributes only digest metadata: neither its
            # path nor its content may appear in the contract.
            self.assertNotIn("holdout.jsonl", contract_path.read_text(encoding="utf-8"))

            init = run_cli(
                "optimize", "init", str(demo / "steps.yaml"),
                "--contract", str(contract_path), "--json",
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            init_payload = json.loads(init.stdout)
            self.assertTrue(init_payload["ok"])
            experiment = Path(init_payload["paths"]["experiment"])
            self.assertTrue((experiment / "contract.json").is_file())
            self.assertTrue((experiment / "artifacts" / "frozen-sources").is_dir())

    def test_scaffold_refuses_to_overwrite_and_rejects_outside_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            demo = make_workflow(Path(raw))
            (Path(raw) / "elsewhere.jsonl").write_text('{"content":"x"}\n', encoding="utf-8")
            outside = run_cli(
                "optimize", "scaffold", str(demo / "steps.yaml"),
                "--inputs", str(Path(raw) / "elsewhere.jsonl"), "--holdout", str(demo / "holdout.jsonl"),
                "--json",
            )
            self.assertEqual(outside.returncode, 2)
            self.assertIn("inside the contract directory", json.loads(outside.stdout)["error"]["message"])
            first = run_cli(
                "optimize", "scaffold", str(demo / "steps.yaml"),
                "--inputs", str(demo / "dev.jsonl"), "--holdout", str(demo / "holdout.jsonl"),
                "--json",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            again = run_cli(
                "optimize", "scaffold", str(demo / "steps.yaml"),
                "--inputs", str(demo / "dev.jsonl"), "--holdout", str(demo / "holdout.jsonl"),
                "--json",
            )
            self.assertEqual(again.returncode, 2)
            self.assertIn("refusing to overwrite", json.loads(again.stdout)["error"]["message"])


class DefaultEvaluatorTests(unittest.TestCase):
    EVAL = ROOT / "scripts" / "optimization_eval.py"

    def _batch_receipt(self, **overrides) -> dict:
        receipt = {
            "ok": True, "status": "completed", "total": 4, "completed": 4, "passed": 3,
            "failed": 1, "cancelled": 0, "not_run": 0, "contract_complete": 4,
            "all_steps_passed": 4, "tokens": 1200, "cost": 0.02, "wall_s": 12.5,
            "results": [],
        }
        receipt.update(overrides)
        return receipt

    def _summarize(self, receipt: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(receipt, handle)
            path = handle.name
        try:
            return subprocess.run(
                [sys.executable, str(self.EVAL), "summarize", "--batch-json", path],
                capture_output=True, text=True, timeout=60, check=False,
            )
        finally:
            os.unlink(path)

    def test_summarize_derives_pass_rate_and_usage(self) -> None:
        done = self._summarize(self._batch_receipt())
        self.assertEqual(done.returncode == 0, True, done.stderr)
        metrics = json.loads(done.stdout)
        self.assertAlmostEqual(metrics["primary"], 0.75)
        self.assertEqual(metrics["coverage"], {"expected": 4, "scored": 4})
        self.assertEqual(metrics["usage"], {"tokens": 1200, "cost_usd": 0.02, "wall_seconds": 12.5})
        self.assertEqual(metrics["uncertainty"], {"low": 0.75, "high": 0.75})

    def test_summarize_refuses_partial_batches(self) -> None:
        done = self._summarize(self._batch_receipt(not_run=1, completed=3, status="failed"))
        self.assertNotEqual(done.returncode, 0)

    def test_parse_round_trips_summarize_output(self) -> None:
        summarized = self._summarize(self._batch_receipt())
        parsed = subprocess.run(
            [sys.executable, str(self.EVAL), "parse"], input=summarized.stdout,
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(parsed.returncode == 0, True, parsed.stderr)
        self.assertEqual(json.loads(parsed.stdout), json.loads(summarized.stdout))

    def test_parse_rejects_garbage(self) -> None:
        parsed = subprocess.run(
            [sys.executable, str(self.EVAL), "parse"], input="not json",
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertNotEqual(parsed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
