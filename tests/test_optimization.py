from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import optimization_bundle as optimization  # noqa: E402
import optimization_runner as optimization_runtime  # noqa: E402


def digest(path: Path) -> str:
    return optimization.sha256_file(path)


def write_fixture(root: Path) -> tuple[Path, Path, Path]:
    workflow = root / "steps.yaml"
    workflow.write_text(yaml.safe_dump({
        "version": 1, "workflow": "demo",
        "steps": [{"id": "answer", "prompt": "answer precisely", "needs": []}],
    }, sort_keys=False), encoding="utf-8")
    dev = root / "dev.jsonl"
    dev.write_text('{"content":"one"}\n{"content":"two"}\n', encoding="utf-8")
    evaluator = root / "evaluator.py"
    evaluator.write_text("print('{}')\n", encoding="utf-8")
    parser = root / "parser.py"
    parser.write_text("print('{}')\n", encoding="utf-8")
    protected = root / "rubric.txt"
    protected.write_text("frozen rubric\n", encoding="utf-8")
    holdout = root / "private.jsonl"
    holdout.write_text('{"content":"secret"}\n', encoding="utf-8")
    contract = {
        "schema": "pi-graph.optimization-contract.v1", "evaluation_version": "eval-v1",
        "route": "candidate_loop",
        "objective": {
            "description": "improve answer quality", "unit": "workflow YAML",
            "primary_metric": "score", "direction": "maximize", "minimum_gain": 0.1,
            "definition_of_done": "strict measured gain", "non_goals": ["change evaluator"],
        },
        "boundaries": {
            "mutable": [{"mechanism": "answer_prompt", "pointer": "/steps/0/prompt"}],
            "protected_files": [{"path": "rubric.txt", "sha256": digest(protected)}],
            "authorized_effects": [], "rollback": "restore exact incumbent bytes", "network": "denied",
        },
        "development": {"path": "dev.jsonl", "sha256": digest(dev), "format": "jsonl", "count": 2},
        "holdout": {"sha256": digest(holdout), "format": "jsonl", "count": 1},
        "evaluation": {
            "evaluator": {"argv": [sys.executable, "evaluator.py"],
                          "sources": [{"path": "evaluator.py", "sha256": digest(evaluator)}], "timeout_seconds": 5},
            "parser": {"argv": [sys.executable, "parser.py"],
                       "sources": [{"path": "parser.py", "sha256": digest(parser)}], "timeout_seconds": 5},
            "hard_gates": [], "repeats": 1, "seeds": [1],
            "uncertainty": {"method": "none", "minimum_repeats": 1}, "tie_breakers": ["lower_cost"],
        },
        "execution": {
            "provider": "fixture", "model": "fixture/model", "thinking": "off", "tools": [],
            "environment": {"NO_NETWORK": "1"}, "dependency_files": [],
            "batch": {"parallel": 1, "require_all": True, "item_timeout_seconds": 10}, "cache": False,
        },
        "budgets": {
            "max_candidates": 3, "max_wall_seconds": 60, "max_tokens": 1000,
            "max_cost_usd": 1, "max_failures": 1, "max_consecutive_non_keeps": 2,
            "per_candidate": {"max_wall_seconds": 20, "max_tokens": 500, "max_cost_usd": 0.5, "max_failures": 0},
            "promotion": {"max_wall_seconds": 20, "max_tokens": 500, "max_cost_usd": 0.5},
        },
        "promotion": {"minimum_holdout_score": 0.5, "maximum_drop_from_selected_dev": 0.2,
                      "require_all_gates": True},
    }
    contract_path = root / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return workflow, contract_path, holdout


class OptimizationBundleTests(unittest.TestCase):
    def init_bundle(self, root: Path) -> tuple[optimization.OptimizationBundle, Path, Path]:
        workflow, contract, holdout = write_fixture(root)
        bundle = optimization.OptimizationBundle.init(
            workflow, contract, root / "experiment", holdout_path=holdout,
            experiment_id="exp-1", product_root=ROOT)
        return bundle, workflow, holdout

    def test_init_freezes_contract_and_development_without_reading_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, contract, holdout = write_fixture(root)
            real_safe = optimization._safe_bytes

            def deny_holdout(path: Path, limit: int = optimization.MAX_FILE_BYTES) -> bytes:
                if Path(path).resolve(strict=False) == holdout.resolve():
                    raise AssertionError("holdout was read during init")
                return real_safe(path, limit)

            with mock.patch.object(optimization, "_safe_bytes", side_effect=deny_holdout):
                bundle = optimization.OptimizationBundle.init(
                    workflow, contract, root / "experiment", holdout_path=holdout,
                    experiment_id="exp-1", product_root=ROOT)
            self.assertFalse(any("holdout" in path.name for path in (bundle.path / "artifacts").iterdir()))
            self.assertEqual(list((bundle.path / "private").iterdir()), [])
            self.assertEqual(bundle.state()["holdout_uses"], 0)
            bundle.verify_integrity()

    def test_failed_baseline_gate_emits_no_score(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow, contract_path, holdout = write_fixture(root)
            contract = json.loads(contract_path.read_text())
            contract["evaluation"]["hard_gates"] = [{"id": "reject", "argv": [shutil.which("false") or "/usr/bin/false"],
                                                         "timeout_seconds": 5}]
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            bundle = optimization.OptimizationBundle.init(
                workflow, contract_path, root / "experiment", holdout_path=holdout,
                experiment_id="exp-1", product_root=ROOT)
            with self.assertRaisesRegex(optimization.OptimizationError, "baseline failed"):
                optimization_runtime.run_baseline(bundle)
            records = bundle.ledger.replay()
            self.assertFalse(any(row["type"] == "baseline_evaluated" for row in records))
            self.assertFalse(any("metrics" in row["payload"] for row in records if row["type"] == "failed"))

    def test_single_writer_lock_is_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle, _, _ = self.init_bundle(Path(raw))
            with bundle.lock():
                with self.assertRaises(optimization.OptimizationBusy):
                    with bundle.lock():
                        pass

    def test_ledger_recovers_incomplete_tail_and_rejects_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle, _, _ = self.init_bundle(Path(raw))
            committed = bundle.ledger.replay()
            with bundle.ledger.path.open("ab") as stream:
                stream.write(b'{"partial"')
            self.assertEqual(bundle.ledger.replay(), committed)
            with bundle.lock():
                bundle.ledger.append("failed", {"code": "fixture", "reason": "expected"})
            self.assertEqual(len(bundle.ledger.replay()), len(committed) + 1)
            committed_after_failure = bundle.ledger.replay()
            with bundle.ledger.path.open("ab") as stream:
                stream.write(b'{"seq":999,"forged":true}\n')
            self.assertEqual(bundle.ledger.replay(), committed_after_failure)
            with bundle.lock():
                bundle.ledger.append("failed", {"code": "fixture", "reason": "truncate forged tail"})
            self.assertNotIn(b'"seq":999', bundle.ledger.path.read_bytes())
            lines = bundle.ledger.path.read_bytes().splitlines(keepends=True)
            first = json.loads(lines[0])
            first["actor"] = "attacker"
            lines[0] = optimization.canonical(first)
            bundle.ledger.path.write_bytes(b"".join(lines))
            with self.assertRaisesRegex(optimization.OptimizationError, "hash mismatch|torn"):
                bundle.ledger.replay()

    def test_frozen_source_and_development_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, _, _ = self.init_bundle(root)
            (root / "evaluator.py").write_text("print('changed')\n", encoding="utf-8")
            with self.assertRaisesRegex(optimization.OptimizationError, "source fingerprint drift"):
                bundle.verify_integrity()

    def test_candidate_must_change_one_declared_path_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, _, _ = self.init_bundle(root)
            active = bundle.path / "artifacts" / "active.yaml"
            parent_hash = digest(active)
            accepted = root / "accepted.yaml"
            value = yaml.safe_load(active.read_text())
            value["steps"][0]["prompt"] = "answer with cited evidence"
            accepted.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            details = optimization.validate_candidate(bundle, accepted, parent_hash, mechanism="answer_prompt")
            self.assertEqual(details["mechanism"], "answer_prompt")
            with self.assertRaisesRegex(optimization.OptimizationError, "parent hash"):
                optimization.validate_candidate(bundle, accepted, "0" * 64)

            bundled = root / "bundled.yaml"
            value["workflow"] = "also-changed"
            bundled.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(optimization.OptimizationError, "exactly one"):
                optimization.validate_candidate(bundle, bundled, parent_hash)

    def test_apply_then_restore_is_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, _, _ = self.init_bundle(root)
            active = bundle.path / "artifacts" / "active.yaml"
            before = active.read_bytes()
            value = yaml.safe_load(active.read_text())
            value["steps"][0]["prompt"] = "new prompt"
            candidate = root / "candidate.yaml"
            candidate.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            bundle.apply_candidate(candidate, "c1")
            self.assertNotEqual(active.read_bytes(), before)
            restored = bundle.restore("c1")
            self.assertEqual(active.read_bytes(), before)
            self.assertEqual(restored, optimization.sha256_bytes(before))

    def test_candidate_cannot_keep_after_mutating_a_protected_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, _, _ = self.init_bundle(root)
            metrics = {"schema": "pi-graph.optimization-metrics.v1", "primary": 0.5,
                       "coverage": {"expected": 2, "scored": 2}, "gates": {},
                       "usage": {"tokens": 0, "cost_usd": 0, "wall_seconds": 0},
                       "uncertainty": {"low": 0.5, "high": 0.5}}
            metrics_path = bundle.path / "evaluations" / "baseline" / "development" / "aggregate-metrics.json"
            optimization.atomic_write(metrics_path, optimization.canonical(metrics))
            baseline_hash = digest(bundle.path / "artifacts" / "baseline.yaml")
            with bundle.lock():
                bundle.ledger.append("baseline_evaluated", {"candidate_id": None, "dataset": "development",
                    "metrics": metrics, "gates_passed": True, "usage": metrics["usage"], "evidence": []})
                bundle.ledger.append("baseline_verified", {"artifact_sha256": baseline_hash,
                    "snapshot_sha256": baseline_hash, "metrics": metrics})
                bundle.write_state()
            candidate = root / "candidate.yaml"
            value = yaml.safe_load((bundle.path / "artifacts" / "active.yaml").read_text())
            value["steps"][0]["prompt"] = "malicious candidate"
            candidate.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            malicious_metrics = {**metrics, "primary": 1.0, "uncertainty": {"low": 1.0, "high": 1.0}}

            def mutate_protected(*_args, **_kwargs):
                (root / "rubric.txt").write_text("owned\n", encoding="utf-8")
                return malicious_metrics, [], True

            with mock.patch("optimization_runner.evaluate_arm", side_effect=mutate_protected):
                with self.assertRaisesRegex(optimization.OptimizationError, "source fingerprint drift"):
                    optimization_runtime.run_candidate(bundle, candidate, "baseline", "answer_prompt", "attack")
            self.assertEqual((root / "rubric.txt").read_text(), "frozen rubric\n")
            self.assertEqual((bundle.path / "artifacts" / "active.yaml").read_bytes(),
                             (bundle.path / "artifacts" / "baseline.yaml").read_bytes())
            self.assertFalse(any(row["type"] == "incumbent_committed" for row in bundle.ledger.replay()))

    def test_resume_rolls_back_an_interrupted_candidate_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, _, _ = self.init_bundle(root)
            metrics = {"schema": "pi-graph.optimization-metrics.v1", "primary": 0.5,
                       "coverage": {"expected": 2, "scored": 2}, "gates": {},
                       "usage": {"tokens": 0, "cost_usd": 0, "wall_seconds": 0},
                       "uncertainty": {"low": 0.5, "high": 0.5}}
            metrics_path = bundle.path / "evaluations" / "baseline" / "development" / "aggregate-metrics.json"
            optimization.atomic_write(metrics_path, optimization.canonical(metrics))
            baseline_hash = digest(bundle.path / "artifacts" / "baseline.yaml")
            with bundle.lock():
                bundle.ledger.append("baseline_evaluated", {"candidate_id": None, "dataset": "development",
                    "metrics": metrics, "gates_passed": True, "usage": metrics["usage"], "evidence": []})
                bundle.ledger.append("baseline_verified", {"artifact_sha256": baseline_hash,
                    "snapshot_sha256": baseline_hash, "metrics": metrics})
                bundle.write_state()
                candidate = root / "candidate.yaml"
                value = yaml.safe_load((bundle.path / "artifacts" / "active.yaml").read_text())
                value["steps"][0]["prompt"] = "interrupted candidate"
                candidate.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
                bundle.ledger.append("candidate_declared", {"candidate_id": "c001", "parent_id": "baseline",
                    "hypothesis": "interrupt me", "mechanism": "answer_prompt"})
                parent = bundle.snapshot_active("c001")
                bundle.ledger.append("snapshot_created", {"candidate_id": "c001", "artifact_sha256": digest(parent),
                    "snapshot": {"path": str(parent.relative_to(bundle.path)), "sha256": digest(parent)}})
                artifact_hash, semantic_hash = bundle.apply_candidate(candidate, "c001")
                bundle.ledger.append("candidate_mutated", {"candidate_id": "c001",
                    "artifact_sha256": artifact_hash, "semantic_sha256": semantic_hash,
                    "diff_sha256": "1" * 64})
            with mock.patch("optimization_runner._run", side_effect=AssertionError("must not redispatch")):
                state = optimization_runtime.resume(bundle)
            self.assertEqual(state["status"], "searching")
            self.assertEqual(state["resume_count"], 1)
            self.assertEqual((bundle.path / "artifacts" / "active.yaml").read_bytes(),
                             (bundle.path / "artifacts" / "baseline.yaml").read_bytes())
            self.assertFalse(any(row["type"] == "arm_evaluated" for row in bundle.ledger.replay()))

    def test_process_output_and_non_finite_json_fail_closed(self) -> None:
        with self.assertRaisesRegex(optimization.OptimizationError, "output exceeds"):
            optimization_runtime._run(
                [sys.executable, "-c", f"print('x'*{optimization_runtime.MAX_PROCESS_OUTPUT + 1})"],
                cwd=ROOT, env={**os.environ}, timeout=10)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "metrics.json"
            path.write_text('{"primary":NaN}', encoding="utf-8")
            with self.assertRaises(optimization.OptimizationError):
                optimization._load_json(path)
            for label, constant in (("development", "NaN"), ("private holdout", "Infinity")):
                with self.subTest(label=label):
                    with self.assertRaisesRegex(optimization.OptimizationError, "invalid JSONL"):
                        optimization._count_jsonl(f'{{"value":{constant}}}\n'.encode(), label)
            workflow, contract, holdout = write_fixture(Path(raw))
            bad_dev = Path(raw) / "dev.jsonl"
            bad_dev.write_text('{"id":"a","value":NaN}\n', encoding="utf-8")
            contract_value = json.loads(contract.read_text())
            contract_value["development"]["sha256"] = digest(bad_dev)
            contract_value["development"]["count"] = 1
            contract.write_text(json.dumps(contract_value), encoding="utf-8")
            with self.assertRaisesRegex(optimization.OptimizationError, "development corpus has invalid JSONL"):
                optimization.OptimizationBundle.init(workflow, contract, Path(raw) / "bad-experiment",
                                                     holdout_path=holdout, experiment_id="bad-jsonl",
                                                     product_root=ROOT)

    def test_budget_and_plateau_stop_reasons_are_finite(self) -> None:
        state = {
            "consecutive_non_keeps": 0,
            "budget": {"candidates_dispatched": 0,
                       "usage": {"tokens": 0, "cost_usd": 0, "wall_seconds": 0, "failures": 0}},
        }
        budgets = {"max_candidates": 2, "max_wall_seconds": 10, "max_tokens": 100,
                   "max_cost_usd": 1, "max_failures": 0, "max_consecutive_non_keeps": 2}
        self.assertIsNone(optimization.stop_reason(state, budgets, elapsed_wall_seconds=0))
        state["budget"]["usage"]["failures"] = 1
        self.assertEqual(optimization.stop_reason(state, budgets, elapsed_wall_seconds=0), "failures")
        state["budget"]["usage"]["failures"] = 0
        state["consecutive_non_keeps"] = 2
        self.assertEqual(optimization.stop_reason(state, budgets, elapsed_wall_seconds=0), "plateau")


class OptimizationLifecycleTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/piw.py"), *arguments, "--json"],
            cwd=ROOT, capture_output=True, text=True, check=False,
            env={**os.environ, "PI_GRAPH_HOME": "/nonexistent"},
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"CLI did not emit one JSON document: {error}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result, payload

    def test_cli_baseline_candidate_stop_promote_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = root / "steps.yaml"
            workflow.write_text(yaml.safe_dump({
                "version": 1, "workflow": "optimization-fixture",
                "steps": [{"id": "answer", "cmd": 'printf "%s" "$INPUT" > "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            dev = root / "dev.jsonl"
            dev.write_text('{"content":"one"}\n{"content":"two"}\n', encoding="utf-8")
            holdout = root / "holdout.jsonl"
            holdout.write_text('{"content":"hidden"}\n', encoding="utf-8")
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "import pathlib,sys\n"
                "text=pathlib.Path(sys.argv[1]).read_text()\n"
                "improved=': improved' in text\n"
                "print(0.75 if improved and sys.argv[2]=='holdout' else 0.8 if improved else 0.5)\n",
                encoding="utf-8")
            parser = root / "parser.py"
            parser.write_text(
                "import json,pathlib,sys\n"
                "score=float(sys.stdin.read().strip())\n"
                "count=1 if sys.argv[2]=='holdout' else 2\n"
                "value={'schema':'pi-graph.optimization-metrics.v1','primary':score,"
                "'coverage':{'expected':count,'scored':count},'gates':{},"
                "'usage':{'tokens':0,'cost_usd':0,'wall_seconds':0},"
                "'uncertainty':{'low':score,'high':score}}\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps(value))\n",
                encoding="utf-8")
            protected = root / "rubric.txt"
            protected.write_text("frozen\n", encoding="utf-8")
            contract = {
                "schema": "pi-graph.optimization-contract.v1", "evaluation_version": "fixture-v1",
                "route": "candidate_loop",
                "objective": {"description": "improve fixture", "unit": "workflow", "primary_metric": "score",
                              "direction": "maximize", "minimum_gain": 0.1,
                              "definition_of_done": "measured gain", "non_goals": ["external effects"]},
                "boundaries": {"mutable": [{"mechanism": "command", "pointer": "/steps/0/cmd"}],
                    "protected_files": [{"path": "rubric.txt", "sha256": digest(protected)}],
                    "authorized_effects": [], "rollback": "exact bytes", "network": "denied"},
                "development": {"path": "dev.jsonl", "sha256": digest(dev), "format": "jsonl", "count": 2},
                "holdout": {"sha256": digest(holdout), "format": "jsonl", "count": 1},
                "evaluation": {
                    "evaluator": {"argv": [sys.executable, "evaluator.py", "{artifact}", "{phase}"],
                        "sources": [{"path": "evaluator.py", "sha256": digest(evaluator)}], "timeout_seconds": 10},
                    "parser": {"argv": [sys.executable, "parser.py", "{metrics_output}", "{phase}"],
                        "sources": [{"path": "parser.py", "sha256": digest(parser)}], "timeout_seconds": 10},
                    "hard_gates": [], "repeats": 1, "seeds": [1],
                    "uncertainty": {"method": "none", "minimum_repeats": 1}, "tie_breakers": ["lower_cost"]},
                "execution": {"provider": "fixture", "model": "fixture/model", "thinking": "off", "tools": [],
                    "environment": {"FIXTURE": "1"}, "dependency_files": [],
                    "batch": {"parallel": 1, "require_all": True, "item_timeout_seconds": 10}, "cache": False},
                "budgets": {"max_candidates": 2, "max_wall_seconds": 120, "max_tokens": 1000,
                    "max_cost_usd": 1, "max_failures": 1, "max_consecutive_non_keeps": 2,
                    "per_candidate": {"max_wall_seconds": 30, "max_tokens": 500, "max_cost_usd": 0.5, "max_failures": 0},
                    "promotion": {"max_wall_seconds": 30, "max_tokens": 500, "max_cost_usd": 0.5}},
                "promotion": {"minimum_holdout_score": 0.7, "maximum_drop_from_selected_dev": 0.1,
                              "require_all_gates": True},
            }
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            experiment = root / "experiment"

            result, payload = self.run_cli("optimize", "init", str(workflow), "--contract", str(contract_path),
                                           "--out", str(experiment))
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["state"], "created")
            result, payload = self.run_cli("optimize", "baseline", str(experiment))
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["state"], "baseline_verified")
            baseline_batch = json.loads((experiment / "evaluations" / "baseline" / "development" /
                                          "repeat-1" / "batch" / "batch-manifest.json").read_text())
            self.assertEqual(Path(baseline_batch["workflow_path"]).name, "active.yaml")

            candidate = root / "candidate.yaml"
            value = yaml.safe_load(workflow.read_text())
            value["steps"][0]["cmd"] += "; : improved"
            candidate.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            result, payload = self.run_cli("optimize", "candidate", str(experiment), "--file", str(candidate),
                                           "--parent", "baseline", "--mechanism", "command",
                                           "--hypothesis", "fixture score should improve")
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["result"]["decision"], "keep")
            self.assertEqual(payload["state"], "searching")
            self.assertFalse((experiment / "private" / "holdout.jsonl").exists())

            rejected = root / "rejected.yaml"
            value["steps"][0]["cmd"] = 'printf "%s" "$INPUT" > "$OUT"; : worse'
            rejected.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            result, payload = self.run_cli("optimize", "candidate", str(experiment), "--file", str(rejected),
                                           "--parent", "c001", "--mechanism", "command",
                                           "--hypothesis", "fixture score might improve again")
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["result"]["decision"], "discard")
            self.assertIn(": improved", (experiment / "artifacts" / "active.yaml").read_text())

            result, payload = self.run_cli("optimize", "stop", str(experiment), "--reason", "candidate budget complete")
            self.assertEqual(result.returncode, 0, payload)
            result, payload = self.run_cli("optimize", "promote", str(experiment),
                                           "--holdout-file", str(holdout))
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["result"]["outcome"], "promoted")
            self.assertEqual(payload["result"]["holdout"]["uses"], 1)
            result, payload = self.run_cli("optimize", "receipt", str(experiment))
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["result"]["outcome"], "promoted")
            result, payload = self.run_cli("optimize", "promote", str(experiment),
                                           "--holdout-file", str(root / "does-not-exist.jsonl"))
            self.assertEqual(result.returncode, 2)
            self.assertIn("illegal from state promoted", payload["error"]["message"])
            self.assertEqual(workflow.read_text(), yaml.safe_dump({
                "version": 1, "workflow": "optimization-fixture",
                "steps": [{"id": "answer", "cmd": 'printf "%s" "$INPUT" > "$OUT"'}],
            }, sort_keys=False))

    def test_promote_without_a_dev_winner_retains_baseline_and_writes_receipt(self) -> None:
        # Regression: the terminal event used status "retained_incumbent" before
        # the event schema allowed it, so ledger validation failed and no
        # receipt was ever written on the no-winner path.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = root / "steps.yaml"
            workflow.write_text(yaml.safe_dump({
                "version": 1, "workflow": "retained-fixture",
                "steps": [{"id": "answer", "cmd": 'printf "%s" "$INPUT" > "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            dev = root / "dev.jsonl"
            dev.write_text('{"content":"one"}\n{"content":"two"}\n', encoding="utf-8")
            holdout = root / "holdout.jsonl"
            holdout.write_text('{"content":"hidden"}\n', encoding="utf-8")
            evaluator = root / "evaluator.py"
            evaluator.write_text("print(0.5)\n", encoding="utf-8")
            parser = root / "parser.py"
            parser.write_text(
                "import json,sys\n"
                "score=float(sys.stdin.read().strip())\n"
                "print(json.dumps({'schema':'pi-graph.optimization-metrics.v1','primary':score,"
                "'coverage':{'expected':2,'scored':2},'gates':{},"
                "'usage':{'tokens':0,'cost_usd':0,'wall_seconds':0},"
                "'uncertainty':{'low':score,'high':score}}))\n", encoding="utf-8")
            contract = {
                "schema": "pi-graph.optimization-contract.v1", "evaluation_version": "retained-v1",
                "route": "candidate_loop",
                "objective": {"description": "improve fixture", "unit": "workflow", "primary_metric": "score",
                              "direction": "maximize", "minimum_gain": 0.1,
                              "definition_of_done": "measured gain", "non_goals": []},
                "boundaries": {"mutable": [{"mechanism": "command", "pointer": "/steps/0/cmd"}],
                    "protected_files": [], "authorized_effects": [], "rollback": "exact bytes",
                    "network": "denied"},
                "development": {"path": "dev.jsonl", "sha256": digest(dev), "format": "jsonl", "count": 2},
                "holdout": {"sha256": digest(holdout), "format": "jsonl", "count": 1},
                "evaluation": {
                    "evaluator": {"argv": [sys.executable, "evaluator.py"],
                        "sources": [{"path": "evaluator.py", "sha256": digest(evaluator)}], "timeout_seconds": 10},
                    "parser": {"argv": [sys.executable, "parser.py"],
                        "sources": [{"path": "parser.py", "sha256": digest(parser)}], "timeout_seconds": 10},
                    "hard_gates": [], "repeats": 1, "seeds": [1],
                    "uncertainty": {"method": "none", "minimum_repeats": 1}, "tie_breakers": ["lower_cost"]},
                "execution": {"provider": "fixture", "model": "fixture/model", "thinking": "off", "tools": [],
                    "environment": {}, "dependency_files": [],
                    "batch": {"parallel": 1, "require_all": True, "item_timeout_seconds": 10}, "cache": False},
                "budgets": {"max_candidates": 2, "max_wall_seconds": 120, "max_tokens": 1000,
                    "max_cost_usd": 1, "max_failures": 1, "max_consecutive_non_keeps": 2,
                    "per_candidate": {"max_wall_seconds": 30, "max_tokens": 500, "max_cost_usd": 0.5, "max_failures": 0},
                    "promotion": {"max_wall_seconds": 30, "max_tokens": 500, "max_cost_usd": 0.5}},
                "promotion": {"minimum_holdout_score": 0.7, "maximum_drop_from_selected_dev": 0.1,
                              "require_all_gates": True},
            }
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            experiment = root / "experiment"
            result, payload = self.run_cli("optimize", "init", str(workflow),
                                           "--contract", str(contract_path), "--out", str(experiment))
            self.assertEqual(result.returncode, 0, payload)
            result, payload = self.run_cli("optimize", "baseline", str(experiment))
            self.assertEqual(result.returncode, 0, payload)
            result, payload = self.run_cli("optimize", "stop", str(experiment), "--reason", "nothing to search")
            self.assertEqual(result.returncode, 0, payload)
            result, payload = self.run_cli("optimize", "promote", str(experiment))
            self.assertEqual(result.returncode, 1, payload)
            self.assertEqual(payload["state"], "retained_incumbent")
            self.assertEqual(payload["next"], ["receipt"])
            self.assertEqual((experiment / "private" / "holdout.jsonl").exists(), False)
            result, payload = self.run_cli("optimize", "receipt", str(experiment))
            self.assertEqual(result.returncode, 1, payload)  # non-promoted outcomes exit 1
            self.assertEqual(payload["result"]["outcome"], "retained_baseline")
            self.assertEqual(payload["result"]["selected"]["id"], "baseline")


if __name__ == "__main__":
    unittest.main()
