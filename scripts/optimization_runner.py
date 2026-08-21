#!/usr/bin/env python3
"""Canonical batch-backed evaluator and lifecycle transitions for Pi Graph optimization."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import graph as workflow_graph
from optimization_bundle import (
    OptimizationBundle, OptimizationError, _count_jsonl, _load_json, _safe_bytes, atomic_write,
    canonical, effective_environment, semantic_workflow, sha256_bytes, sha256_file,
    stop_reason, validate_candidate, validate_schema,
)

MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
PLACEHOLDERS = {"artifact", "batch_json", "raw_output", "metrics_output", "phase", "seed"}


class ArmBudgetExceeded(OptimizationError):
    def __init__(self, message: str, metrics: dict[str, Any]):
        super().__init__(message)
        self.metrics = metrics


def _evidence(path: Path, base: Path) -> dict[str, str]:
    return {"path": str(path.relative_to(base)), "sha256": sha256_file(path)}


def _bounded_text(data: str) -> bytes:
    raw = data.encode("utf-8", errors="replace")
    if len(raw) > MAX_PROCESS_OUTPUT:
        raise OptimizationError(f"process output exceeds {MAX_PROCESS_OUTPUT} bytes")
    return raw


def _expand(argv: list[str], values: dict[str, str]) -> list[str]:
    result = []
    for token in argv:
        expanded = token
        for name, value in values.items():
            expanded = expanded.replace("{" + name + "}", value)
        if "{" in expanded or "}" in expanded:
            raise OptimizationError(f"unknown or malformed command placeholder: {token}")
        result.append(sys.executable if expanded == "python3" else expanded)
    return result


def _program_cwd(bundle: OptimizationBundle, role: str) -> Path:
    sources = [Path(item["path"]) for item in bundle.manifest["sources"] if item["role"] == role]
    return sources[0].parent if sources else Path(bundle.manifest["baseline"]["source_path"]).parent


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
         stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    if timeout <= 0:
        raise OptimizationError(f"command has no remaining time budget: {argv[0]}")
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        if stdin is not None:
            input_raw = _bounded_text(stdin)
            input_file.write(input_raw); input_file.seek(0)
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env,
                stdin=input_file if stdin is not None else subprocess.DEVNULL,
                stdout=stdout_file, stderr=stderr_file)
        except OSError as error:
            raise OptimizationError(f"command could not start: {argv[0]}: {error}") from error
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            output_size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
            if output_size > MAX_PROCESS_OUTPUT:
                process.kill(); process.wait()
                raise OptimizationError(f"command output exceeds {MAX_PROCESS_OUTPUT} bytes: {argv[0]}")
            if time.monotonic() >= deadline:
                process.kill(); process.wait()
                raise OptimizationError(f"command timed out after {timeout}s: {argv[0]}")
            time.sleep(0.01)
        if os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size > MAX_PROCESS_OUTPUT:
            raise OptimizationError(f"command output exceeds {MAX_PROCESS_OUTPUT} bytes: {argv[0]}")
        stdout_file.seek(0); stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _env(contract: dict[str, Any]) -> dict[str, str]:
    return effective_environment(contract)


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise OptimizationError("per-arm wall-time budget exhausted")
    return value


def _run_gates(bundle: OptimizationBundle, artifact: Path, arm_dir: Path,
               candidate_id: str, deadline: float) -> tuple[dict[str, bool], list[dict[str, str]]]:
    contract = bundle.contract()
    gates: dict[str, bool] = {}
    evidence: list[dict[str, str]] = []
    for gate in contract["evaluation"]["hard_gates"]:
        receipt = arm_dir / f"gate-{gate['id']}.json"
        argv = _expand(gate["argv"], {"artifact": str(artifact), "batch_json": "", "raw_output": "",
                                             "metrics_output": "", "phase": "gate", "seed": "0"})
        started = time.monotonic()
        result = _run(argv, cwd=Path(bundle.manifest["baseline"]["source_path"]).parent,
                      env=_env(contract), timeout=min(gate["timeout_seconds"], _remaining(deadline)))
        value = {"id": gate["id"], "argv_sha256": sha256_bytes(canonical(gate["argv"])),
                 "returncode": result.returncode, "passed": result.returncode == 0,
                 "wall_seconds": round(time.monotonic() - started, 6),
                 "stdout_sha256": sha256_bytes(_bounded_text(result.stdout)),
                 "stderr_sha256": sha256_bytes(_bounded_text(result.stderr))}
        atomic_write(receipt, canonical(value))
        gates[gate["id"]] = value["passed"]
        evidence.append(_evidence(receipt, bundle.path))
        bundle.ledger.append("gate_evaluated", {"candidate_id": candidate_id, "gate_id": gate["id"],
                                                "passed": value["passed"],
                                                "receipt": _evidence(receipt, bundle.path)})
    return gates, evidence


def _batch_complete(summary: dict[str, Any]) -> bool:
    return (summary.get("status") == "completed" and summary.get("passed") == summary.get("total")
            and summary.get("not_run") == 0 and summary.get("contract_complete") == summary.get("total")
            and summary.get("all_steps_passed") == summary.get("total"))


def _run_batch(bundle: OptimizationBundle, artifact: Path, corpus: Path, out: Path,
               *, holdout: bool, timeout: float, max_tokens: int,
               max_cost_usd: float) -> tuple[dict[str, Any], list[dict[str, str]]]:
    existing_summary = out / "batch.json"
    if existing_summary.is_file():
        summary = _load_json(existing_summary)
        if not isinstance(summary, dict) or not _batch_complete(summary):
            raise OptimizationError("existing canonical batch summary is incomplete")
        evidence = [_evidence(existing_summary, bundle.path)]
        for name in ("batch.stdout", "batch.stderr"):
            path = out.parent / name
            if path.is_file():
                evidence.append(_evidence(path, bundle.path))
        return summary, evidence
    contract = bundle.contract()
    batch = contract["execution"]["batch"]
    command = [sys.executable, str(bundle.product_root / "scripts/run_batch.py"), str(artifact),
               "--inputs", str(corpus)]
    if (out / "batch-manifest.json").exists() and not (out / "batch.json").exists():
        command.extend(["--resume", str(out)])
    else:
        command.extend(["--out", str(out)])
    command.extend(["--parallel", str(batch["parallel"]), "--item-timeout", str(batch["item_timeout_seconds"]),
                    "--workflow-dir", str(Path(bundle.manifest["baseline"]["source_path"]).parent), "--json"])
    if batch["require_all"]:
        command.append("--require-all")
    budget = contract["budgets"]["promotion" if holdout else "per_candidate"]
    if max_tokens > 0:
        command.extend(["--max-tokens", str(max_tokens)])
    if max_cost_usd > 0:
        command.extend(["--max-cost", str(max_cost_usd)])
    if not holdout and budget.get("max_failures", 0) > 0:
        command.extend(["--stop-after-failures", str(budget["max_failures"])])
    result = _run(command, cwd=bundle.product_root, env=_env(contract), timeout=timeout)
    atomic_write(out.parent / "batch.stdout", _bounded_text(result.stdout))
    atomic_write(out.parent / "batch.stderr", _bounded_text(result.stderr))
    summary_path = out / "batch.json"
    if not summary_path.is_file():
        detail = " ".join((result.stderr or result.stdout).strip().split())[-1000:] or "no process detail"
        raise OptimizationError(f"batch produced no summary (exit {result.returncode}): {detail}")
    summary = _load_json(summary_path)
    if not isinstance(summary, dict):
        raise OptimizationError("batch summary is malformed")
    if result.returncode != 0 or not _batch_complete(summary):
        raise OptimizationError("canonical batch did not complete every declared contract")
    return summary, [_evidence(summary_path, bundle.path),
                     _evidence(out.parent / "batch.stdout", bundle.path),
                     _evidence(out.parent / "batch.stderr", bundle.path)]


def _evaluate_once(bundle: OptimizationBundle, artifact: Path, corpus: Path, arm_dir: Path,
                   *, phase: str, seed: int, holdout: bool, deadline: float,
                   max_tokens: int, max_cost_usd: float) -> tuple[dict[str, Any], list[dict[str, str]]]:
    contract = bundle.contract()
    arm_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = arm_dir / "batch"
    budget = contract["budgets"]["promotion" if holdout else "per_candidate"]
    batch, evidence = _run_batch(bundle, artifact, corpus, batch_dir, holdout=holdout,
                                 timeout=min(budget["max_wall_seconds"], _remaining(deadline)),
                                 max_tokens=max_tokens, max_cost_usd=max_cost_usd)
    batch_json = batch_dir / "batch.json"
    raw_output = arm_dir / "evaluator.raw"
    metrics_output = arm_dir / "metrics.json"
    values = {"artifact": str(artifact), "batch_json": str(batch_json), "raw_output": str(raw_output),
              "metrics_output": str(metrics_output), "phase": phase, "seed": str(seed)}
    evaluator = contract["evaluation"]["evaluator"]
    result = _run(_expand(evaluator["argv"], values), cwd=_program_cwd(bundle, "evaluator"),
                  env=_env(contract), timeout=min(evaluator["timeout_seconds"], _remaining(deadline)))
    atomic_write(raw_output, _bounded_text(result.stdout))
    atomic_write(arm_dir / "evaluator.stderr", _bounded_text(result.stderr))
    if result.returncode != 0:
        raise OptimizationError(f"evaluator failed with exit {result.returncode}")
    parser = contract["evaluation"]["parser"]
    parsed = _run(_expand(parser["argv"], values), cwd=_program_cwd(bundle, "parser"),
                  env=_env(contract), timeout=min(parser["timeout_seconds"], _remaining(deadline)), stdin=result.stdout)
    atomic_write(arm_dir / "parser.stderr", _bounded_text(parsed.stderr))
    if parsed.returncode != 0:
        raise OptimizationError(f"metrics parser failed with exit {parsed.returncode}")
    if not metrics_output.is_file():
        atomic_write(metrics_output, _bounded_text(parsed.stdout))
    metrics = _load_json(metrics_output)
    validate_schema(bundle.product_root, "optimization-metrics.schema.json", metrics)
    expected = bundle.manifest["holdout" if holdout else "development"]["count"]
    if metrics["coverage"] != {"expected": expected, "scored": expected}:
        raise OptimizationError("metrics coverage does not match frozen corpus")
    if set(metrics["gates"]) != {gate["id"] for gate in contract["evaluation"]["hard_gates"]}:
        raise OptimizationError("metrics gate set differs from frozen hard gates")
    if metrics["usage"]["tokens"] < int(batch.get("tokens", 0)) or metrics["usage"]["cost_usd"] < float(batch.get("cost", 0)):
        raise OptimizationError("metrics under-report canonical child usage")
    evidence.extend([_evidence(raw_output, bundle.path), _evidence(metrics_output, bundle.path),
                     _evidence(arm_dir / "evaluator.stderr", bundle.path),
                     _evidence(arm_dir / "parser.stderr", bundle.path)])
    return metrics, evidence


def evaluate_arm(bundle: OptimizationBundle, artifact: Path, corpus: Path, candidate_id: str,
                 *, phase: str, holdout: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, str]], bool]:
    bundle.verify_integrity(include_active=False)
    operation = bundle.path / "evaluations" / candidate_id / phase
    operation.mkdir(parents=True, exist_ok=True)
    contract = bundle.contract()
    budget = dict(contract["budgets"]["promotion" if holdout else "per_candidate"])
    if not holdout and candidate_id != "baseline":
        used = bundle.state()["budget"]["usage"]
        budget["max_tokens"] = min(budget["max_tokens"], contract["budgets"]["max_tokens"] - used["tokens"])
        budget["max_cost_usd"] = min(budget["max_cost_usd"], contract["budgets"]["max_cost_usd"] - used["cost_usd"])
        budget["max_wall_seconds"] = min(budget["max_wall_seconds"],
                                         contract["budgets"]["max_wall_seconds"] - _elapsed(bundle.manifest["created_at"]))
    if budget["max_tokens"] <= 0 or budget["max_cost_usd"] <= 0 or budget["max_wall_seconds"] <= 0:
        raise OptimizationError("no remaining arm budget")
    deadline = time.monotonic() + budget["max_wall_seconds"]
    gates, evidence = _run_gates(bundle, artifact, operation, candidate_id, deadline)
    if not all(gates.values()):
        return None, evidence, False
    repetitions = contract["evaluation"]["repeats"]
    seeds = contract["evaluation"]["seeds"]
    all_metrics: list[dict[str, Any]] = []
    used_tokens = 0
    used_cost = 0.0
    for repeat in range(repetitions):
        seed = seeds[repeat % len(seeds)]
        remaining_tokens = budget["max_tokens"] - used_tokens
        remaining_cost = budget["max_cost_usd"] - used_cost
        if remaining_tokens <= 0 or remaining_cost <= 0:
            raise OptimizationError("per-arm token or cost budget exhausted before all repeats")
        metrics, receipts = _evaluate_once(bundle, artifact, corpus, operation / f"repeat-{repeat + 1}",
                                            phase=phase, seed=seed, holdout=holdout, deadline=deadline,
                                            max_tokens=remaining_tokens, max_cost_usd=remaining_cost)
        used_tokens += metrics["usage"]["tokens"]
        used_cost += metrics["usage"]["cost_usd"]
        all_metrics.append(metrics); evidence.extend(receipts)
        if used_tokens > budget["max_tokens"] or used_cost > budget["max_cost_usd"]:
            raise ArmBudgetExceeded("per-arm token or cost budget exceeded", _aggregate(all_metrics))
    aggregate = _aggregate(all_metrics)
    aggregate["gates"] = gates
    validate_schema(bundle.product_root, "optimization-metrics.schema.json", aggregate)
    atomic_write(operation / "aggregate-metrics.json", canonical(aggregate))
    evidence.append(_evidence(operation / "aggregate-metrics.json", bundle.path))
    return aggregate, evidence, all(gates.values())


def _empty_metrics(bundle: OptimizationBundle, holdout: bool, gates: dict[str, bool]) -> dict[str, Any]:
    count = bundle.manifest["holdout" if holdout else "development"]["count"]
    return {"schema": "pi-graph.optimization-metrics.v1", "primary": 0,
            "coverage": {"expected": count, "scored": 0}, "gates": gates,
            "usage": {"tokens": 0, "cost_usd": 0, "wall_seconds": 0},
            "uncertainty": {"low": 0, "high": 0}}


def _aggregate(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["primary"]) for item in metrics]
    usage = {key: sum(item["usage"][key] for item in metrics) for key in ("tokens", "cost_usd", "wall_seconds")}
    usage["tokens"] = int(usage["tokens"])
    return {"schema": "pi-graph.optimization-metrics.v1", "primary": sum(values) / len(values),
            "coverage": metrics[0]["coverage"], "gates": metrics[0]["gates"], "usage": usage,
            "uncertainty": {"low": min(item["uncertainty"]["low"] for item in metrics),
                            "high": max(item["uncertainty"]["high"] for item in metrics)}}


def run_baseline(bundle: OptimizationBundle) -> dict[str, Any]:
    with bundle.lock():
        state = bundle.state()
        if state["status"] == "baseline_verified":
            return {"decision": "baseline_already_verified", "state": state}
        if state["status"] != "created":
            raise OptimizationError(f"baseline is illegal from state {state['status']}")
        bundle.verify_integrity()
        baseline_snapshot = bundle.path / "artifacts" / "baseline.yaml"
        artifact = bundle.path / "artifacts" / "active.yaml"
        if sha256_file(artifact) != sha256_file(baseline_snapshot):
            raise OptimizationError("active artifact differs before baseline")
        workflow_graph.parse_steps_text(_safe_bytes(artifact).decode(), artifact)
        try:
            metrics, evidence, gates = evaluate_arm(bundle, artifact, bundle.path / "development.jsonl",
                                                    "baseline", phase="development")
            if metrics is None or not gates or metrics["coverage"]["scored"] != metrics["coverage"]["expected"]:
                raise OptimizationError("baseline failed frozen gates or coverage without a score")
            bundle.verify_integrity()
        except Exception as error:
            bundle.restore_frozen_sources()
            bundle.ledger.append("failed", {"code": "baseline_failed", "reason": str(error)[:4000]})
            bundle.write_state()
            raise
        bundle.ledger.append("baseline_evaluated", {"candidate_id": None, "dataset": "development",
            "metrics": metrics, "gates_passed": gates, "usage": metrics["usage"], "evidence": evidence})
        baseline_hash = sha256_file(baseline_snapshot)
        bundle.ledger.append("baseline_verified", {"artifact_sha256": baseline_hash,
            "semantic_sha256": bundle.manifest["baseline"]["semantic_sha256"],
            "snapshot_sha256": baseline_hash, "metrics": metrics})
        return {"decision": "baseline_verified", "metrics": metrics, "state": bundle.write_state()}


def _gain(candidate: dict[str, Any], incumbent: dict[str, Any], direction: str) -> float:
    if direction == "maximize":
        return float(candidate["uncertainty"]["low"]) - float(incumbent["uncertainty"]["high"])
    return float(incumbent["uncertainty"]["low"]) - float(candidate["uncertainty"]["high"])


def run_candidate(bundle: OptimizationBundle, candidate_file: Path, parent_id: str,
                  mechanism: str, hypothesis: str) -> tuple[dict[str, Any], int]:
    with bundle.lock():
        bundle.verify_integrity()
        state = bundle.state()
        if state["status"] not in {"baseline_verified", "searching"} or state["incumbent"] is None:
            raise OptimizationError(f"candidate is illegal from state {state['status']}")
        if state["incumbent"]["id"] != parent_id:
            raise OptimizationError(f"candidate parent must be current incumbent {state['incumbent']['id']}")
        reason = stop_reason(state, bundle.manifest["budgets"], elapsed_wall_seconds=_elapsed(bundle.manifest["created_at"]))
        if reason:
            _terminal_for_stop(bundle, state, reason)
            raise OptimizationError(f"experiment stopped before dispatch: {reason}")
        candidate_id = f"c{state['candidate_cursor'] + 1:03d}"
        submitted = bundle.path / "candidates" / candidate_id / "submitted.yaml"
        try:
            candidate_raw = _safe_bytes(candidate_file)
            atomic_write(submitted, candidate_raw)
            details = validate_candidate(bundle, submitted, state["incumbent"]["artifact_sha256"],
                                         mechanism=mechanism, candidate_raw=candidate_raw)
            candidate_value, _semantic_hash = semantic_workflow(candidate_raw)
            validate_schema(bundle.product_root, "workflow.schema.json", candidate_value)
            workflow_graph.parse_steps_text(candidate_raw.decode(), submitted)
        except Exception:
            shutil.rmtree(submitted.parent, ignore_errors=True)
            raise
        mechanism = details["mechanism"]
        bundle.ledger.append("candidate_declared", {"candidate_id": candidate_id, "parent_id": parent_id,
                                                    "hypothesis": hypothesis, "mechanism": mechanism})
        parent_snapshot = bundle.snapshot_active(candidate_id)
        parent_hash = sha256_file(parent_snapshot)
        bundle.ledger.append("snapshot_created", {"candidate_id": candidate_id, "artifact_sha256": parent_hash,
                                                   "snapshot": _evidence(parent_snapshot, bundle.path)})
        try:
            details = validate_candidate(bundle, submitted, parent_hash, mechanism=mechanism,
                                         candidate_raw=candidate_raw)
            artifact_hash, semantic_hash = bundle.apply_candidate(
                submitted, candidate_id, candidate_raw=candidate_raw,
                expected_parent=parent_hash, validated=details)
            bundle.ledger.append("candidate_mutated", {"candidate_id": candidate_id,
                "artifact_sha256": artifact_hash, "semantic_sha256": semantic_hash,
                "diff_sha256": details["diff_sha256"]})
            metrics, evidence, gates = evaluate_arm(bundle, bundle.path / "artifacts" / "active.yaml",
                                                    bundle.path / "development.jsonl", candidate_id,
                                                    phase="development")
            if metrics is None:
                bundle.restore_frozen_sources()
                restored = bundle.restore(candidate_id, parent_hash)
                decision = "gate_failed"
                gain = 0.0
                bundle.ledger.append("candidate_decided", {"candidate_id": candidate_id, "decision": decision,
                    "reason": "hard gate failed before scoring", "trusted_score": False, "metrics": None})
                bundle.ledger.append("rollback_verified", {"candidate_id": candidate_id,
                    "restored_sha256": restored, "snapshot_sha256": parent_hash})
                exit_code = 1
            else:
                bundle.verify_integrity(include_active=False)
                bundle.ledger.append("arm_evaluated", {"candidate_id": candidate_id, "arm": "candidate",
                    "dataset": "development", "metrics": metrics, "evidence": evidence})
                incumbent_metrics = _incumbent_metrics(bundle, state["incumbent"]["id"])
                gain = _gain(metrics, incumbent_metrics, bundle.manifest["evaluation"]["metric"]["direction"])
                keep = gain >= bundle.manifest["evaluation"]["metric"]["minimum_gain"]
                decision = "keep" if keep else "discard"
                explanation = f"trusted uncertainty-adjusted gain {gain:.12g}"
                bundle.ledger.append("candidate_decided", {"candidate_id": candidate_id, "decision": decision,
                                                           "reason": explanation, "trusted_score": True, "metrics": metrics})
                if keep:
                    snapshot = bundle.path / "candidates" / candidate_id / "candidate.yaml"
                    bundle.ledger.append("incumbent_committed", {"candidate_id": candidate_id, "parent_id": parent_id,
                        "artifact_sha256": artifact_hash, "snapshot_sha256": sha256_file(snapshot),
                        "semantic_sha256": semantic_hash, "score": metrics["primary"]})
                    exit_code = 0
                else:
                    restored = bundle.restore(candidate_id, parent_hash)
                    bundle.ledger.append("rollback_verified", {"candidate_id": candidate_id,
                        "restored_sha256": restored, "snapshot_sha256": parent_hash})
                    exit_code = 0
        except ArmBudgetExceeded as error:
            bundle.restore_frozen_sources()
            restored = bundle.restore(candidate_id, parent_hash)
            metrics = error.metrics
            decision = "invalid_eval"
            gain = 0.0
            bundle.ledger.append("candidate_decided", {"candidate_id": candidate_id, "decision": decision,
                "reason": str(error), "trusted_score": False, "metrics": metrics})
            bundle.ledger.append("rollback_verified", {"candidate_id": candidate_id,
                "restored_sha256": restored, "snapshot_sha256": parent_hash})
            bundle.ledger.append("terminal", {"status": "budget_exhausted", "reason": "per-arm budget exceeded",
                "incumbent_id": state["incumbent"]["id"],
                "artifact_sha256": state["incumbent"]["artifact_sha256"]})
            exit_code = 1
        except Exception as error:
            bundle.restore_frozen_sources()
            restored = bundle.restore(candidate_id, parent_hash)
            bundle.ledger.append("candidate_decided", {"candidate_id": candidate_id, "decision": "invalid_eval",
                                                       "reason": str(error)[:4000], "trusted_score": False, "metrics": None})
            bundle.ledger.append("rollback_verified", {"candidate_id": candidate_id,
                "restored_sha256": restored, "snapshot_sha256": parent_hash})
            # The stop check below runs only on the return path; an exception
            # must still close the search when this failure exhausted the
            # plateau or budget budget, or status would keep claiming
            # "searching" after the experiment is actually over.
            state = bundle.write_state()
            reason = stop_reason(state, bundle.manifest["budgets"], elapsed_wall_seconds=_elapsed(bundle.manifest["created_at"]))
            if reason and state["status"] not in {"plateau", "budget_exhausted", "target_achieved"}:
                _terminal_for_stop(bundle, state, reason)
                bundle.write_state()
            raise
        state = bundle.write_state()
        reason = stop_reason(state, bundle.manifest["budgets"], elapsed_wall_seconds=_elapsed(bundle.manifest["created_at"]))
        if reason and state["status"] not in {"plateau", "budget_exhausted", "target_achieved"}:
            _terminal_for_stop(bundle, state, reason)
            state = bundle.write_state()
        return {"candidate_id": candidate_id, "mechanism": mechanism,
                "artifact_sha256": details["artifact_sha256"],
                "semantic_sha256": details["semantic_sha256"], "diff_sha256": details["diff_sha256"],
                "decision": decision, "gain": gain, "metrics": metrics, "state": state}, exit_code


def _incumbent_metrics(bundle: OptimizationBundle, incumbent_id: str) -> dict[str, Any]:
    path = bundle.path / "evaluations" / incumbent_id / "development" / "aggregate-metrics.json"
    if not path.is_file():
        raise OptimizationError(f"incumbent metrics missing: {incumbent_id}")
    value = _load_json(path)
    validate_schema(bundle.product_root, "optimization-metrics.schema.json", value)
    return value


def _elapsed(created_at: str) -> float:
    return max(0.0, time.time() - datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())


def _terminal_for_stop(bundle: OptimizationBundle, state: dict[str, Any], reason: str) -> None:
    incumbent = state["incumbent"] or state["baseline"]
    status = "plateau" if reason == "plateau" else "budget_exhausted"
    bundle.ledger.append("terminal", {"status": status, "reason": reason,
                                      "incumbent_id": incumbent["id"],
                                      "artifact_sha256": incumbent["artifact_sha256"]})


def stop_experiment(bundle: OptimizationBundle, reason: str) -> dict[str, Any]:
    with bundle.lock():
        state = bundle.state()
        if state.get("pending_operation"):
            raise OptimizationError("experiment has an interrupted operation; run optimize resume first")
        if state["status"] == "stopped_by_user":
            if state["terminal_reason"] == reason:
                return state
            raise OptimizationError("experiment already stopped with a different reason")
        if state["status"] not in {"baseline_verified", "searching", "plateau", "budget_exhausted"}:
            raise OptimizationError(f"stop is illegal from state {state['status']}")
        incumbent = state["incumbent"] or state["baseline"]
        bundle.ledger.append("terminal", {"status": "stopped_by_user", "reason": reason,
                                          "incumbent_id": incumbent["id"],
                                          "artifact_sha256": incumbent["artifact_sha256"]})
        return bundle.write_state()


def promote(bundle: OptimizationBundle, holdout_file: Path | None) -> tuple[dict[str, Any], int]:
    with bundle.lock():
        state = bundle.state()
        if state.get("pending_operation"):
            raise OptimizationError("experiment has an interrupted operation; run optimize resume first")
        bundle.verify_integrity()
        if state["status"] not in {"stopped_by_user", "plateau", "budget_exhausted", "target_achieved"}:
            raise OptimizationError(f"promotion is illegal from state {state['status']}")
        if state["holdout_uses"] != 0:
            raise OptimizationError("private holdout was already reserved; redispatch is forbidden")
        incumbent = state["incumbent"]
        if incumbent is None or incumbent["id"] == "baseline":
            baseline = state["baseline"]
            bundle.ledger.append("terminal", {"status": "retained_incumbent", "reason": "no development winner",
                "incumbent_id": "baseline", "artifact_sha256": baseline["artifact_sha256"]})
            return _write_receipt(bundle, "retained_baseline", None, None), 1
        if holdout_file is None:
            raise OptimizationError("--holdout-file is required for a non-baseline incumbent")
        bundle.ledger.append("holdout_access_granted", {"holdout_sha256": bundle.manifest["holdout"]["sha256"],
                                                       "candidate_id": incumbent["id"], "use": 1})
        holdout_raw = _safe_bytes(holdout_file.expanduser().absolute())
        if sha256_bytes(holdout_raw) != bundle.manifest["holdout"]["sha256"]:
            return _promotion_failure(bundle, incumbent, "holdout fingerprint mismatch"), 1
        if _count_jsonl(holdout_raw, "holdout corpus") != bundle.manifest["holdout"]["count"]:
            return _promotion_failure(bundle, incumbent, "holdout count mismatch"), 1
        frozen_holdout = bundle.path / "private" / "holdout.jsonl"
        atomic_write(frozen_holdout, holdout_raw)
        try:
            metrics, evidence, gates = evaluate_arm(bundle, bundle.path / "artifacts" / "active.yaml",
                                                    frozen_holdout, incumbent["id"], phase="holdout", holdout=True)
            bundle.verify_integrity(include_active=False)
        except Exception as error:
            return _promotion_failure(bundle, incumbent, f"holdout evaluation failed: {error}"), 1
        if metrics is None:
            return _promotion_failure(bundle, incumbent, "holdout gate failed before scoring"), 1
        bundle.ledger.append("holdout_evaluated", {"candidate_id": incumbent["id"], "dataset": "holdout",
            "metrics": metrics, "gates_passed": gates, "usage": metrics["usage"], "evidence": evidence})
        rule = bundle.contract()["promotion"]
        passed = gates and metrics["primary"] >= rule["minimum_holdout_score"] and (
            incumbent["score"] - metrics["primary"] <= rule["maximum_drop_from_selected_dev"])
        if passed:
            bundle.ledger.append("promotion", {"candidate_id": incumbent["id"],
                "artifact_sha256": incumbent["artifact_sha256"],
                "holdout_sha256": bundle.manifest["holdout"]["sha256"], "metrics": metrics,
                "gates_passed": True})
            return _write_receipt(bundle, "promoted", metrics, evidence), 0
        return _promotion_failure(bundle, incumbent, "holdout promotion rule failed"), 1


def _promotion_failure(bundle: OptimizationBundle, incumbent: dict[str, Any], reason: str) -> dict[str, Any]:
    bundle.restore_frozen_sources()
    baseline = bundle.path / "artifacts" / "baseline.yaml"
    active = bundle.path / "artifacts" / "active.yaml"
    atomic_write(active, _safe_bytes(baseline), bundle.manifest["baseline"]["mode"])
    restored = sha256_file(active)
    bundle.ledger.append("promotion_reverted", {"candidate_id": incumbent["id"], "reason": reason,
        "restored_incumbent_id": "baseline", "restored_sha256": restored})
    return _write_receipt(bundle, "not_promoted", None, None)


def _write_receipt(bundle: OptimizationBundle, outcome: str, holdout_metrics: dict[str, Any] | None,
                   holdout_evidence: list[dict[str, str]] | None) -> dict[str, Any]:
    state = bundle.write_state()
    records = bundle.ledger.replay()
    baseline_metrics = _incumbent_metrics(bundle, "baseline") if state["baseline"] else None
    incumbent = state["incumbent"] or state["baseline"]
    receipt: dict[str, Any] = {
        "schema": "pi-graph.optimization-receipt.v1", "experiment_id": bundle.experiment_id,
        "outcome": outcome, "assurance_scope": "local deterministic evidence and protected-source hash checks; no OS sandbox",
        "contract": {"sha256": bundle.manifest["contract"]["sha256"]},
        "runtime": bundle.manifest["runtime"], "baseline": {"artifact": bundle.manifest["baseline"], "metrics": baseline_metrics},
        "development": bundle.manifest["development"],
        "search": {"candidates": state["budget"]["candidates_completed"], "terminal": state["terminal_reason"],
                   "ledger_seq": records[-1]["seq"], "ledger_event_sha256": records[-1]["event_sha256"]},
        "selected": incumbent, "holdout": {"uses": state["holdout_uses"], "metrics": holdout_metrics,
                                             "sha256": bundle.manifest["holdout"]["sha256"]},
        "promotion": {"outcome": outcome, "external_effect_performed": False},
        "budgets": {"limits": bundle.manifest["budgets"], "used": state["budget"]},
        "effects": {"authorized": bundle.manifest["boundaries"]["authorized_effects"],
                    "controller_invoked_commit_push_deploy": False,
                    "sandbox_enforced": False,
                    "scope": "candidate commands retain normal user authority; use an external sandbox for strict effect isolation"},
        "evidence": {"ledger_prefix": {"seq": records[-1]["seq"],
                                             "event_sha256": records[-1]["event_sha256"]},
                     "holdout": holdout_evidence or []},
        "rollback": {"rule": bundle.manifest["boundaries"]["rollback"],
                     "active_sha256": sha256_file(bundle.path / "artifacts" / "active.yaml")},
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical(receipt))
    validate_schema(bundle.product_root, "optimization-receipt.schema.json", receipt)
    path = bundle.path / "receipts" / "final.json"
    atomic_write(path, canonical(receipt))
    receipt_evidence = _evidence(path, bundle.path)
    bundle.ledger.append("receipt_written", receipt_evidence)
    bundle.write_state()
    return receipt


def read_receipt(bundle: OptimizationBundle) -> tuple[dict[str, Any], int]:
    path = bundle.path / "receipts" / "final.json"
    if not path.is_file():
        raise OptimizationError("terminal receipt does not exist")
    receipt = _load_json(path)
    validate_schema(bundle.product_root, "optimization-receipt.schema.json", receipt)
    claimed = receipt["receipt_sha256"]
    check = dict(receipt); check["receipt_sha256"] = "0" * 64
    if sha256_bytes(canonical(check)) != claimed:
        raise OptimizationError("receipt hash mismatch")
    records = bundle.ledger.replay()
    prefix = receipt["evidence"].get("ledger_prefix", {})
    seq = prefix.get("seq")
    if not isinstance(seq, int) or seq < 1 or seq > len(records):
        raise OptimizationError("receipt ledger prefix is invalid")
    if records[seq - 1]["event_sha256"] != prefix.get("event_sha256"):
        raise OptimizationError("receipt ledger prefix hash mismatch")
    receipt_events = [record for record in records if record["type"] == "receipt_written"]
    if not receipt_events or receipt_events[-1]["payload"].get("sha256") != sha256_file(path):
        raise OptimizationError("ledger does not commit this receipt")
    return receipt, 0 if receipt["outcome"] == "promoted" else 1


def resume(bundle: OptimizationBundle) -> dict[str, Any]:
    with bundle.lock():
        bundle.verify_integrity(include_active=False)
        state = bundle.state()
        records = bundle.ledger.replay()
        if state["status"] == "promotion_running":
            incumbent = state["incumbent"]
            if incumbent is not None:
                _promotion_failure(bundle, incumbent, "holdout operation was interrupted; redispatch forbidden")
            return bundle.write_state()
        if state["status"] in {"promoted", "promotion_reverted", "retained_incumbent"}:
            bundle.verify_integrity()
            receipt = bundle.path / "receipts" / "final.json"
            receipt_events = [record for record in records if record["type"] == "receipt_written"]
            if receipt_events:
                if (not receipt.is_file() or
                        receipt_events[-1]["payload"].get("sha256") != sha256_file(receipt)):
                    raise OptimizationError("committed terminal receipt fingerprint drift")
                read_receipt(bundle)
                return state
            if state["status"] == "promoted":
                evaluated = next(record["payload"] for record in reversed(records)
                                 if record["type"] == "holdout_evaluated")
                _write_receipt(bundle, "promoted", evaluated["metrics"], evaluated["evidence"])
            elif state["status"] == "promotion_reverted":
                _write_receipt(bundle, "not_promoted", None, None)
            else:
                _write_receipt(bundle, "retained_baseline", None, None)
            return bundle.write_state()
        pending = records[-1]["type"] if records else None
        if pending == "baseline_evaluated":
            payload = records[-1]["payload"]
            baseline = bundle.path / "artifacts" / "baseline.yaml"
            baseline_hash = sha256_file(baseline)
            bundle.ledger.append("baseline_verified", {
                "artifact_sha256": baseline_hash,
                "semantic_sha256": bundle.manifest["baseline"]["semantic_sha256"],
                "snapshot_sha256": baseline_hash, "metrics": payload["metrics"],
            })
            return bundle.write_state()
        if pending in {"candidate_declared", "snapshot_created", "candidate_mutated", "gate_evaluated",
                       "arm_evaluated", "candidate_decided"}:
            candidate = next(record["payload"]["candidate_id"] for record in reversed(records)
                             if record["type"] == "candidate_declared")
            snapshot_event = next((record for record in reversed(records)
                                   if record["type"] == "snapshot_created" and
                                   record["payload"].get("candidate_id") == candidate), None)
            parent_hash = (snapshot_event["payload"]["artifact_sha256"] if snapshot_event else
                           state["incumbent"]["artifact_sha256"])
            decision_event = (records[-1] if pending == "candidate_decided" else None)
            if decision_event and decision_event["payload"]["decision"] == "keep":
                mutated = next(record["payload"] for record in reversed(records)
                               if record["type"] == "candidate_mutated" and
                               record["payload"].get("candidate_id") == candidate)
                candidate_snapshot = bundle.path / "candidates" / candidate / "candidate.yaml"
                if (sha256_file(candidate_snapshot) != mutated["artifact_sha256"] or
                        sha256_file(bundle.path / "artifacts" / "active.yaml") != mutated["artifact_sha256"]):
                    raise OptimizationError("kept candidate changed before interrupted commit recovery")
                declared = next(record["payload"] for record in reversed(records)
                                if record["type"] == "candidate_declared" and
                                record["payload"].get("candidate_id") == candidate)
                bundle.ledger.append("incumbent_committed", {
                    "candidate_id": candidate, "parent_id": declared["parent_id"],
                    "artifact_sha256": mutated["artifact_sha256"],
                    "semantic_sha256": mutated["semantic_sha256"],
                    "snapshot_sha256": sha256_file(candidate_snapshot),
                    "score": decision_event["payload"]["metrics"]["primary"],
                })
            else:
                snapshot = bundle.path / "candidates" / candidate / "parent.yaml"
                restored = (bundle.restore(candidate, parent_hash) if snapshot.exists() else
                            sha256_file(bundle.path / "artifacts" / "active.yaml"))
                if restored != parent_hash:
                    raise OptimizationError("active workflow changed before interrupted candidate recovery")
                if pending != "candidate_decided":
                    bundle.ledger.append("candidate_decided", {
                        "candidate_id": candidate, "decision": "blocked_by_infra",
                        "reason": "interrupted operation recovered without redispatch",
                        "trusted_score": False, "metrics": None})
                bundle.ledger.append("rollback_verified", {"candidate_id": candidate,
                    "restored_sha256": restored, "snapshot_sha256": parent_hash})
        elif state["status"] not in {"failed", "interrupted", "resuming"}:
            return state
        bundle.ledger.append("resumed", {"previous_status": "interrupted",
            "last_committed_seq": bundle.ledger.replay()[-1]["seq"], "resume_count": state["resume_count"] + 1})
        return bundle.write_state()
