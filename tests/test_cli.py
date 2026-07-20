from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_steps.py"
CLI = ROOT / "scripts" / "piw.py"


class ProductCliTests(unittest.TestCase):
    def test_agent_guides_expose_the_complete_operating_loop(self) -> None:
        required = [
            "piw actions", "piw validate", "piw run", "piw detail", "--step",
            "piw set", "--judge-prompt-file", "piw compare", "piw eval",
            "piw batch", "--max-cost", "--output-step",
        ]
        for guide in (ROOT / "SKILL.md", ROOT / "AGENTS.md"):
            text = guide.read_text(encoding="utf-8")
            for command in required:
                self.assertIn(command, text, f"{guide.name} omits {command}")
            self.assertIn("one-step task", text)
            self.assertIn("Done means", text)

    def test_every_piw_command_shown_in_the_readme_actually_exists(self) -> None:
        """The README shipped `--step verdict` against a graph whose real id was
        `parallel-review-verdict`, so its headline command exited 1. Parse the
        commands out of the docs and check them against the CLI's own list."""
        listing = subprocess.run(
            [sys.executable, str(CLI), "--help"], capture_output=True, text=True, timeout=60,
        ).stdout
        known = set(re.findall(r"^\s{4}([a-z][a-z-]+)\s{2,}", listing, re.M))
        self.assertIn("run", known, listing)

        for doc in (ROOT / "README.md", ROOT / "SKILL.md"):
            text = doc.read_text(encoding="utf-8")
            invoked: set[str] = set()
            for line in text.splitlines():
                # Only read real invocations: a line that starts with `piw`, or
                # an inline `piw x` in backticks. Prose like "not ./bin/piw from
                # the clone" is not a command.
                body = line.split("#", 1)[0]
                start = re.match(r"\s*piw ([a-z][a-z-]+)", body)
                if start:
                    invoked.add(start.group(1))
                invoked.update(re.findall(r"`piw ([a-z][a-z-]+)", body))
            self.assertTrue(invoked, f"{doc.name} shows no piw commands at all")
            for command in sorted(invoked):
                self.assertIn(
                    command, known,
                    f"{doc.name} shows `piw {command}`, which is not a piw command",
                )

    def test_the_readme_step_reference_matches_a_real_scaffolded_step(self) -> None:
        """The README pairs `piw create --action parallel-review` with a
        `--step` id. Action expansion prefixes step ids, so the two drift apart
        silently unless something checks them together."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        referenced = re.search(r"piw detail \S+ RUN_ID --step (\S+)", readme)
        self.assertIsNotNone(referenced, "README no longer shows a `piw detail --step` example")
        action = re.search(r"piw create \S+ --action (\S+)", readme)
        self.assertIsNotNone(action, "README no longer shows a `piw create --action` example")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = {**os.environ, "PI_WORKFLOWS_ROOTS": str(root), "HOME": str(root)}
            subprocess.run(
                [sys.executable, str(CLI), "create", "demo", "--action", action.group(1)],
                cwd=root, capture_output=True, text=True, env=environment, timeout=120, check=True,
            )
            spec = yaml.safe_load((root / "demo" / "steps.yaml").read_text(encoding="utf-8"))
            ids = [step["id"] for step in spec["steps"]]
            wanted = referenced.group(1)
            self.assertTrue(
                any(wanted == sid or wanted in sid for sid in ids),
                f"README references --step {wanted}, but the scaffold produces {ids}",
            )

    def test_agent_can_inspect_one_node_compare_runs_and_configure_node_qa(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "inspection-loop",
                "input": {"required": True, "description": "fixture"},
                "steps": [
                    {"id": "first", "cmd": 'cat "$INPUT"'},
                    {"id": "second", "needs": ["first"], "cmd": 'cat "$RUN/first.md"'},
                ],
            }, sort_keys=False), encoding="utf-8")
            for value in ("baseline", "candidate"):
                result = subprocess.run(
                    [sys.executable, str(RUNNER), str(steps), "--input", value],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            runs = sorted((root / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            baseline, candidate = runs
            candidate_ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            target = next(item for item in candidate_ledger if item["id"] == "second")
            target.update({"model": "test/cheap", "cost": 0.01, "total": 50, "seconds": 2.0})
            (candidate / "ledger.json").write_text(json.dumps(candidate_ledger), encoding="utf-8")

            inspected = subprocess.run(
                [sys.executable, str(CLI), "detail", str(steps), candidate.name,
                 "--step", "second", "--json"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            inspection = json.loads(inspected.stdout)
            self.assertEqual([item["id"] for item in inspection["steps"]], ["second"])
            self.assertEqual(inspection["steps"][0]["output"], "candidate")

            compared = subprocess.run(
                [sys.executable, str(CLI), "compare", str(steps), baseline.name, candidate.name,
                 "--step", "second", "--json"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(compared.returncode, 0, compared.stderr)
            comparison = json.loads(compared.stdout)
            self.assertEqual(comparison["delta"]["cost"], 0.01)
            self.assertEqual(comparison["steps"][0]["candidate"]["model"], "test/cheap")
            self.assertEqual(comparison["quality_regressions"], [])

            model_steps = root / "model" / "steps.yaml"
            model_steps.parent.mkdir()
            model_steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "node-qa", "model": "test/generator",
                "steps": [{"id": "draft", "prompt": "Draft from {input}"}],
            }, sort_keys=False), encoding="utf-8")
            judge_prompt = root / "judge.txt"
            judge_prompt.write_text(
                'Score the candidate. Return JSON: {"score": 0, "feedback": "..."}\n{out}\n',
                encoding="utf-8",
            )
            configured = subprocess.run(
                [sys.executable, str(CLI), "set", str(model_steps), "draft",
                 "--judge-model", "test/reviewer", "--judge-thinking", "low",
                 "--judge-score", "8", "--judge-max-iters", "2",
                 "--judge-prompt-file", str(judge_prompt)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            judge = yaml.safe_load(model_steps.read_text(encoding="utf-8"))["steps"][0]["judge"]
            self.assertEqual(judge["model"], "test/reviewer")
            self.assertEqual(judge["score"], 8.0)
            self.assertEqual(judge["max_iters"], 2)
            self.assertIn("{out}", judge["prompt"])

            cleared = subprocess.run(
                [sys.executable, str(CLI), "set", str(model_steps), "draft", "--clear-judge"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(cleared.returncode, 0, cleared.stderr)
            self.assertNotIn("judge", yaml.safe_load(model_steps.read_text(encoding="utf-8"))["steps"][0])

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
            [item["capability"] for item in metadata["graphCapabilities"]],
            ["fan-out", "join", "route", "gate", "retry", "judge-loop", "final-qa", "cache", "artifact-capture", "evidence"],
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

    def test_validate_enforces_the_published_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            steps = Path(raw) / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "schema-boundary",
                "steps": [{"id": "build", "cmd": "true", "model": "not-allowed"}],
                "mystery": True,
            }, sort_keys=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(CLI), "validate", str(steps), "--json"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            schema_clause = next(item for item in payload["clauses"] if item["clause"] == "matches the versioned JSON Schema")
            self.assertFalse(schema_clause["pass"])
            messages = " ".join(item["message"] for item in schema_clause["open"])
            self.assertIn("mystery", messages)
            self.assertIn("model", messages)

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

    def test_action_catalog_creates_and_expands_plain_valid_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {**os.environ, "PI_WORKFLOWS_ROOTS": raw}
            listed = subprocess.run(
                [sys.executable, str(CLI), "actions", "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            actions = {item["id"]: item for item in json.loads(listed.stdout)}
            self.assertGreaterEqual(len(actions), 12)
            self.assertEqual(actions["parallel-review"]["nodes"], 3)
            for action in actions.values():
                self.assertIn("effect", action)
                self.assertIn("retry_safe", action)
                self.assertIn("idempotency", action)
                self.assertIn("cost", action)

            created_paths = {}
            for action_id in actions:
                target_path = root / action_id
                created = subprocess.run(
                    [sys.executable, str(CLI), "create", action_id, "--dir", str(target_path),
                     "--action", action_id, "--json"],
                    capture_output=True, text=True, env=env, timeout=30, check=False,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                validated_action = subprocess.run(
                    [sys.executable, str(CLI), "validate", str(target_path / "steps.yaml"), "--json"],
                    capture_output=True, text=True, env=env, timeout=30, check=False,
                )
                self.assertEqual(
                    validated_action.returncode, 0,
                    f"{action_id}: {validated_action.stdout}{validated_action.stderr}",
                )
                created_paths[action_id] = target_path

            target = created_paths["parallel-review"]
            spec = yaml.safe_load((target / "steps.yaml").read_text(encoding="utf-8"))
            self.assertIn("parallel-review", spec["qa"]["prompt"])
            self.assertIn("Output contract:", spec["qa"]["prompt"])
            self.assertIn("Do not require", spec["qa"]["prompt"])
            self.assertEqual(
                [step["id"] for step in spec["steps"]],
                ["parallel-review-correctness", "parallel-review-failure-modes", "parallel-review-verdict"],
            )
            self.assertNotIn("{{", (target / "steps.yaml").read_text(encoding="utf-8"))

            added = subprocess.run(
                [sys.executable, str(CLI), "add", str(target / "steps.yaml"),
                 "extract-action-items", "--id", "extract", "--needs", "parallel-review-verdict", "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["added"], ["extract"])
            expanded = yaml.safe_load((target / "steps.yaml").read_text(encoding="utf-8"))
            extract = expanded["steps"][-1]
            self.assertEqual(extract["needs"], ["parallel-review-verdict"])
            self.assertIn("{step.parallel-review-verdict}", extract["prompt"])

            validated = subprocess.run(
                [sys.executable, str(CLI), "validate", str(target / "steps.yaml"), "--json"],
                capture_output=True, text=True, env=env, timeout=30, check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["holds"])

    def test_retry_policy_classifies_eligibility_and_records_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "retry-policy",
                "steps": [{
                    "id": "transient",
                    "cmd": "n=$(cat \"$RUN/count\" 2>/dev/null || printf 0); n=$((n+1)); "
                           "printf '%s' \"$n\" > \"$RUN/count\"; "
                           "if [ \"$n\" -lt 2 ]; then exit 7; fi; printf recovered",
                    "retries": 3,
                    "retry_on": ["command_exit"],
                    "retry_delay_seconds": 0.01,
                    "retry_backoff": "exponential",
                    "retry_max_delay_seconds": 0.02,
                    "retry_jitter": 0.1,
                }],
            }, sort_keys=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(RUNNER), str(steps)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_dir = next((root / "runs").iterdir())
            ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger[0]["attempts"], 2)
            self.assertIn("(command_exit)", (run_dir / "log.md").read_text(encoding="utf-8"))

            blocked = root / "blocked.yaml"
            blocked.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "retry-blocked",
                "steps": [{"id": "terminal", "cmd": "exit 9", "retries": 3,
                           "retry_on": ["gate_failed"]}],
            }, sort_keys=False), encoding="utf-8")
            stopped = subprocess.run(
                [sys.executable, str(RUNNER), str(blocked)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertNotEqual(stopped.returncode, 0)
            blocked_run = next((root / "runs").glob("retry-blocked-*"))
            blocked_ledger = json.loads((blocked_run / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(blocked_ledger[0]["attempts"], 1)
            self.assertIn("no retry for command_exit", (blocked_run / "log.md").read_text(encoding="utf-8"))

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

    def test_run_resolves_relative_input_file_before_changing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflow = root / "workflow"
            workflow.mkdir()
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "relative-input",
                "input": {"required": True, "description": "value"},
                "steps": [{"id": "copy", "cmd": 'cat "$INPUT"', "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            input_file = root / "input.txt"
            input_file.write_text("Ada", encoding="utf-8")
            env = {
                **os.environ,
                "PI_WORKFLOWS_ROOTS": str(root),
                "LOOPS_PORT": "1",
                "PI_WORKFLOWS_STATE_DIR": str(root / "state"),
            }
            result = subprocess.run(
                [sys.executable, str(CLI), "run", "workflow/steps.yaml", "--input-file", "input.txt", "--json"],
                capture_output=True, text=True, cwd=root, env=env, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])

    def test_run_fails_fast_when_runner_exits_before_emitting_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "missing-required-input",
                "input": {"required": True, "description": "required value"},
                "steps": [{"id": "never", "cmd": "true"}],
            }, sort_keys=False), encoding="utf-8")
            env = {
                **os.environ,
                "PI_WORKFLOWS_ROOTS": raw,
                "LOOPS_PORT": "1",
                "PI_WORKFLOWS_STATE_DIR": str(root / "state"),
            }
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(CLI), "run", str(steps), "--json", "--timeout", "10"],
                capture_output=True, text=True, env=env, timeout=5, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["failed_ids"], ["<runner-exited>"])
            self.assertLess(time.monotonic() - started, 4)

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
            git_config = (ledger_path.parent / ".git" / "config").read_text(encoding="utf-8")
            self.assertIn("auto = 0", git_config)
            self.assertIn("auto = false", git_config)
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
