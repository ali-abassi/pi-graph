from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "piw.py"
FAKE_PI = ROOT / "tests" / "fixtures" / "fake_pi.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from optimization_bundle import semantic_workflow  # noqa: E402


class FakePiJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        shutil.copy2(FAKE_PI, self.fake_bin / "pi")
        (self.fake_bin / "pi").chmod(0o755)
        self.argv_log = self.root / "pi-argv.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PI_GRAPH_HOME": str(self.root / "pi-graph-home"),
            "PI_GRAPH_STATE_DIR": str(self.root / "pi-graph-state"),
            "FAKE_PI_ARGV_LOG": str(self.argv_log),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str, rules: list[dict] | None = None,
                timeout: int = 180) -> tuple[subprocess.CompletedProcess[str], dict]:
        env = dict(self.env)
        env["FAKE_PI_RULES_JSON"] = json.dumps(rules or [])
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments, "--json"], cwd=self.root,
            capture_output=True, text=True, env=env, timeout=timeout, check=False,
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError as error:
            self.fail(f"CLI emitted invalid JSON: {error}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result, payload

    def write_workflow(self, value: dict, name: str = "workflow") -> Path:
        directory = self.root / name
        directory.mkdir()
        path = directory / "steps.yaml"
        path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
        return path

    def argv_rows(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]

    def test_reliable_repo_change_action_runs_all_six_gated_stages(self) -> None:
        repository = self.root / "target-repository"
        result, created = self.run_cli(
            "create", "reliable-change", "--dir", str(repository),
            "--action", "repo-change",
        )
        self.assertEqual(result.returncode, 0, created)
        workflow = repository / "steps.yaml"
        request = repository / "request.md"
        request.write_text("Add a durable marker file and verify it.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repository, check=True)
        subprocess.run(["git", "add", "steps.yaml", "request.md"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture baseline"], cwd=repository, check=True)

        strict, strict_payload = self.run_cli("validate", str(workflow), "--strict")
        self.assertEqual(strict.returncode, 0, strict_payload)
        marker = repository / "durable-marker.txt"
        rules = [
            {"contains": ["Review whether the completed artifacts satisfy the `repo-change` action contract"],
             "text": '{"verdict":"pass","issues":[]}'},
            {"contains": ["Plan the smallest safe repository change"],
             "text": '{"summary":"add marker","files":["durable-marker.txt"],"checks":["test -s durable-marker.txt"],"risks":[]}'},
            {"contains": ["Implement the scoped request"],
             "write": {"path": str(marker), "text": "durable\n"},
             "text": '{"changed_files":["durable-marker.txt"],"checks":["test -s durable-marker.txt: pass"],"summary":"added marker"}'},
            {"contains": ["Independently test the current repository change"],
             "text": '{"checks":["test -s durable-marker.txt"],"results":["PASS: marker exists"],"verdict":"pass"}'},
            {"contains": ["Independently review the repository diff"],
             "text": '{"verdict":"pass","issues":[],"evidence":["durable-marker.txt is present in git status"]}'},
            {"contains": ["Apply only repairs required"],
             "text": '{"addressed":["no-repair-needed"],"checks":["git diff --check: pass"],"summary":"review passed"}'},
        ]
        run, payload = self.run_cli(
            "run", str(workflow), "--input-file", str(request), rules=rules,
        )
        self.assertEqual(run.returncode, 0, payload)
        run_dir = Path(payload["run_dir"])
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(
            list(state["steps"]),
            [
                "repo-change-plan", "repo-change-implement", "repo-change-test",
                "repo-change-review", "repo-change-repair", "repo-change-verify",
            ],
        )
        self.assertTrue(all(step["status"] == "passed" for step in state["steps"].values()))
        final = json.loads((run_dir / "repo-change-verify.md").read_text(encoding="utf-8"))
        self.assertEqual(final["verdict"], "pass")
        self.assertTrue(any("durable-marker.txt" in item for item in final["changed"]))

    def test_model_tool_agent_judge_and_qa_journey(self) -> None:
        workflow = self.write_workflow({
            "version": 1, "workflow": "protocol-journey", "model": "fixture/luna",
            "thinking": "low", "input": {"required": True, "description": "fixture input"},
            "qa": {"model": "fixture/judge", "prompt": "QA_SENTINEL {artifacts}"},
            "steps": [
                {"id": "draft", "prompt": "GENERATOR_SENTINEL {input}",
                 "gate": 'grep -q "DRAFT" "$OUT"',
                 "judge": {"model": "fixture/judge", "prompt": "JUDGE_SENTINEL {out}",
                           "score": 8, "max_iters": 2}},
                {"id": "tool", "needs": ["draft"], "prompt": "TOOL_SENTINEL {step.draft}",
                 "tools": "read", "gate": 'grep -q "TOOL_OK" "$OUT"'},
                {"id": "agent", "needs": ["tool"], "prompt": "AGENT_SENTINEL {step.tool}",
                 "agent": True, "gate": 'grep -q "AGENT_OK" "$OUT"'},
            ],
        })
        rules = [
            {"contains": ["QA_SENTINEL"], "text": '{"verdict":"pass","issues":[]}'},
            {"contains": ["JUDGE_SENTINEL", "DRAFT2"], "text": '{"score":9,"feedback":"good"}'},
            {"contains": ["JUDGE_SENTINEL", "DRAFT1"], "text": '{"score":3,"feedback":"improve"}'},
            {"contains": ["Previous attempt failed verification"], "text": "DRAFT2"},
            {"contains": ["GENERATOR_SENTINEL"], "text": "DRAFT1"},
            {"contains": ["TOOL_SENTINEL"], "kind": "tool", "text": "TOOL_OK"},
            {"contains": ["AGENT_SENTINEL"], "text": "AGENT_OK"},
        ]
        result, payload = self.run_cli("run", str(workflow), "--input", "hello", rules=rules)
        self.assertEqual(result.returncode, 0, payload)
        run_dir = Path(payload["run_dir"])
        ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual({entry["id"] for entry in ledger}, {"draft", "tool", "agent", "__qa__"})
        self.assertTrue((run_dir / "draft.a1.md").is_file())
        self.assertEqual((run_dir / "draft.md").read_text(encoding="utf-8").strip(), "DRAFT2")
        rows = self.argv_rows()
        tool_argv = next(row for row in rows if "TOOL_SENTINEL" in row[-1])
        agent_argv = next(row for row in rows if "AGENT_SENTINEL" in row[-1])
        self.assertEqual(tool_argv[tool_argv.index("--tools") + 1], "read")
        self.assertNotIn("--no-context-files", agent_argv)
        self.assertNotIn("--no-tools", agent_argv)

        result, path_payload = self.run_cli("path", str(workflow), rules=rules)
        self.assertEqual(result.returncode, 0, path_payload)
        self.assertEqual(Path(path_payload["path"]).resolve(), workflow.resolve())
        result, files = self.run_cli("show", str(workflow), "--run", run_dir.name, rules=rules)
        self.assertEqual(result.returncode, 0, files)
        self.assertIn("draft.md", files["files"])
        result, artifact = self.run_cli(
            "show", str(workflow), "draft", "--run", run_dir.name, rules=rules)
        self.assertEqual(result.returncode, 0, artifact)
        self.assertEqual(artifact["content"], "DRAFT2")

    def test_scheduler_wrappers_never_pass_through_non_json_output(self) -> None:
        loops = self.fake_bin / "loops"
        loops.write_text("#!/bin/sh\nprintf '%s\\n' 'plain scheduler reply'\n", encoding="utf-8")
        loops.chmod(0o755)
        workflow = self.write_workflow({
            "version": 1, "workflow": "scheduled",
            "steps": [{"id": "tick", "cmd": 'printf "ok" > "$OUT"',
                       "gate": 'test -s "$OUT"'}],
        }, name="scheduled")
        result, scheduled = self.run_cli(
            "schedule", str(workflow), "--interval-minutes", "5")
        self.assertEqual(result.returncode, 0, scheduled)
        self.assertEqual(scheduled["scheduler"], {"raw": "plain scheduler reply"})
        result, controlled = self.run_cli("automation", "show", "scheduled")
        self.assertEqual(result.returncode, 0, controlled)
        self.assertEqual(controlled["result"], {"raw": "plain scheduler reply"})
        loops.write_text("#!/bin/sh\nprintf '%s\\n' '{\"value\":NaN}'\n", encoding="utf-8")
        result, strict = self.run_cli("automation", "show", "scheduled")
        self.assertEqual(result.returncode, 0, strict)
        self.assertEqual(strict["result"], {"raw": '{"value":NaN}'})
        loops.write_text("#!/bin/sh\nprintf '%s\\n' '{\"value\":1e999}'\n", encoding="utf-8")
        result, overflow = self.run_cli("automation", "show", "scheduled")
        self.assertEqual(result.returncode, 0, overflow)
        self.assertEqual(overflow["result"], {"raw": '{"value":1e999}'})

    def test_eval_returns_paired_evidence_and_refuses_small_sample_certainty(self) -> None:
        workflow = self.write_workflow({
            "version": 1, "workflow": "eval-journey", "model": "fixture/luna",
            "input": {"required": True, "description": "one item"},
            "steps": [{"id": "answer", "prompt": "EVAL_SENTINEL {input}",
                       "gate": 'grep -q "PASS" "$OUT"'}],
        }, name="eval")
        corpus = workflow.parent / "corpus.jsonl"
        corpus.write_text('{"id":"one","content":"one"}\n'
                          '{"id":"two","content":"two"}\n', encoding="utf-8")
        result, payload = self.run_cli(
            "eval", str(workflow), "--inputs", str(corpus),
            "--models", "fixture/luna,fixture/judge",
            rules=[{"contains": ["EVAL_SENTINEL"], "text": "PASS"}], timeout=240)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["paired_comparisons"][0]["paired_items"], 2)
        self.assertTrue(payload["paired_comparisons"][0]["quality"]["non_regressing"])
        self.assertEqual(payload["models"][0]["warnings"][0]["code"], "small_corpus")
        self.assertEqual(payload["recommendation"]["decision"], "inconclusive")
        result, failed = self.run_cli(
            "eval", str(workflow), "--inputs", str(workflow.parent / "missing.jsonl"),
            "--models", "fixture/luna,fixture/judge", timeout=240)
        self.assertEqual(result.returncode, 2, failed)
        self.assertEqual(failed["schema"], "pi-graph.eval.v1")
        self.assertEqual(failed["error"]["code"], "E_EVAL")
        result, duplicate = self.run_cli(
            "eval", str(workflow), "--inputs", str(corpus),
            "--models", "fixture/luna,fixture/luna", timeout=240)
        self.assertEqual(result.returncode, 2, duplicate)
        self.assertIn("unique", duplicate["error"]["message"])

    def test_model_protocol_failures_are_classified_and_evidenced(self) -> None:
        cases = ["model_error", "unsettled", "malformed", "extension_error", "auto_retry_error", "blank"]
        for kind in cases:
            with self.subTest(kind=kind):
                workflow = self.write_workflow({
                    "version": 1, "workflow": f"failure-{kind}", "model": "fixture/luna",
                    "steps": [{"id": "answer", "prompt": f"FAIL_{kind}", "gate": 'test -s "$OUT"'}],
                }, name=f"failure-{kind}")
                result, payload = self.run_cli(
                    "run", str(workflow), rules=[{"contains": [f"FAIL_{kind}"], "kind": kind}])
                self.assertEqual(result.returncode, 1, payload)
                detail_result, detail = self.run_cli("detail", str(workflow), Path(payload["run_dir"]).name)
                self.assertEqual(detail_result.returncode, 1)
                self.assertEqual(detail["steps"][0]["failure_kind"], "model_error")

    def test_scaffolded_optimizer_full_journey_without_tokens(self) -> None:
        workflow = self.write_workflow({
            "version": 1, "workflow": "optimizer-journey", "model": "fixture/luna",
            "thinking": "low", "input": {"required": True, "description": "one word"},
            "steps": [{"id": "answer", "prompt": "BASE_PROMPT {input}",
                       "gate": 'grep -q "PASS" "$OUT"'}],
        }, name="optimizer")
        dev = workflow.parent / "dev.jsonl"
        holdout = workflow.parent / "holdout.jsonl"
        dev.write_text('{"content":"one"}\n{"content":"two"}\n', encoding="utf-8")
        holdout.write_text('{"content":"hidden"}\n', encoding="utf-8")
        rules = [
            {"contains": ["BROKEN_PROMPT"], "text": "FAIL"},
            {"contains": ["IMPROVED_PROMPT"], "text": "PASS improved"},
            {"contains": ["BASE_PROMPT"], "text": "PASS baseline"},
        ]
        result, scaffold = self.run_cli(
            "optimize", "scaffold", str(workflow), "--inputs", str(dev),
            "--holdout", str(holdout), rules=rules)
        self.assertEqual(result.returncode, 0, scaffold)
        contract_path = Path(scaffold["result"]["contract_path"])
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["objective"]["minimum_gain"] = 0
        contract["execution"]["environment"]["FAKE_PI_RULES_JSON"] = json.dumps(rules)
        contract["execution"]["environment"]["FAKE_PI_ARGV_LOG"] = str(self.argv_log)
        contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        experiment = workflow.parent / "experiment"
        result, initialized = self.run_cli(
            "optimize", "init", str(workflow), "--contract", str(contract_path),
            "--out", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, initialized)
        result, baseline = self.run_cli("optimize", "baseline", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, baseline)
        self.assertEqual(baseline["result"]["metrics"]["primary"], 1)

        schema_invalid = self.root / "schema-invalid.yaml"
        result, checkout = self.run_cli(
            "optimize", "checkout", str(experiment), "--out", str(schema_invalid), rules=rules)
        self.assertEqual(result.returncode, 0, checkout)
        invalid_value = yaml.safe_load(schema_invalid.read_text(encoding="utf-8"))
        invalid_value["steps"][0]["prompt"] = ""
        schema_invalid.write_text(yaml.safe_dump(invalid_value, sort_keys=False), encoding="utf-8")
        result, invalid_diff = self.run_cli(
            "optimize", "diff", str(experiment), "--file", str(schema_invalid),
            "--parent", "baseline", rules=rules)
        self.assertEqual(result.returncode, 2, invalid_diff)
        result, pristine = self.run_cli("optimize", "status", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, pristine)
        self.assertEqual(pristine["counters"]["candidates_dispatched"], 0)

        broken = self.root / "broken.yaml"
        broken_value = yaml.safe_load((experiment / "artifacts" / "active.yaml").read_text())
        broken_value["steps"][0]["prompt"] = "BROKEN_PROMPT {input}"
        broken.write_text(yaml.safe_dump(broken_value, sort_keys=False), encoding="utf-8")
        result, invalid = self.run_cli(
            "optimize", "candidate", str(experiment), "--file", str(broken),
            "--parent", "baseline", "--mechanism", "answer-prompt",
            "--hypothesis", "gate failure must rollback", rules=rules)
        self.assertEqual(result.returncode, 2, invalid)
        self.assertIsNone(invalid["state"])
        self.assertEqual(invalid["next_actions"], [])
        result, recovered = self.run_cli("optimize", "status", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, recovered)
        self.assertEqual(recovered["state"], "searching")
        self.assertEqual(recovered["counters"]["candidates_dispatched"], 1)
        self.assertEqual(recovered["counters"]["candidates_completed"], 1)
        self.assertEqual(
            (experiment / "artifacts" / "active.yaml").read_bytes(),
            (experiment / "artifacts" / "baseline.yaml").read_bytes(),
        )

        result, mechanisms = self.run_cli("optimize", "mechanisms", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, mechanisms)
        self.assertEqual(mechanisms["result"]["incumbent"]["id"], "baseline")
        self.assertIn("submit", [action["action"] for action in mechanisms["next_actions"]])

        improved = self.root / "improved.yaml"
        result, checkout = self.run_cli(
            "optimize", "checkout", str(experiment), "--out", str(improved), rules=rules)
        self.assertEqual(result.returncode, 0, checkout)
        self.assertEqual(checkout["result"]["parent_id"], "baseline")
        _, semantic_hash = semantic_workflow(improved.read_bytes())
        self.assertEqual(checkout["result"]["semantic_sha256"], semantic_hash)
        result, overwrite = self.run_cli(
            "optimize", "checkout", str(experiment), "--out", str(improved), rules=rules)
        self.assertEqual(result.returncode, 2, overwrite)
        self.assertIn("refusing to overwrite", overwrite["error"]["message"])
        improved_value = yaml.safe_load(improved.read_text(encoding="utf-8"))
        improved_value["steps"][0]["prompt"] = "IMPROVED_PROMPT {input}"
        improved.write_text(yaml.safe_dump(improved_value, sort_keys=False), encoding="utf-8")
        result, diff = self.run_cli(
            "optimize", "diff", str(experiment), "--file", str(improved),
            "--parent", "baseline", rules=rules)
        self.assertEqual(result.returncode, 0, diff)
        self.assertEqual(diff["result"]["mechanism"], "answer-prompt")
        result, candidate = self.run_cli(
            "optimize", "submit", str(experiment), "--file", str(improved),
            "--parent", "baseline", "--hypothesis", "equivalent quality exercises keep", rules=rules)
        self.assertEqual(result.returncode, 0, candidate)
        self.assertEqual(candidate["result"]["decision"], "keep")
        self.assertEqual(candidate["result"]["mechanism"], "answer-prompt")
        result, stale = self.run_cli(
            "optimize", "diff", str(experiment), "--file", str(improved),
            "--parent", "baseline", rules=rules)
        self.assertEqual(result.returncode, 2, stale)
        self.assertIn("current incumbent c002", stale["error"]["message"])
        result, _ = self.run_cli(
            "optimize", "stop", str(experiment), "--reason", "journey complete", rules=rules)
        self.assertEqual(result.returncode, 0)
        result, promoted = self.run_cli(
            "optimize", "promote", str(experiment), "--holdout-file", str(holdout), rules=rules)
        self.assertEqual(result.returncode, 0, promoted)
        self.assertEqual(promoted["result"]["outcome"], "promoted")
        result, receipt = self.run_cli("optimize", "receipt", str(experiment), rules=rules)
        self.assertEqual(result.returncode, 0, receipt)
        self.assertEqual(receipt["result"]["selected"]["id"], "c002")
        self.assertEqual(receipt["result"]["holdout"]["uses"], 1)


if __name__ == "__main__":
    unittest.main()
