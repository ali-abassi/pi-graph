#!/usr/bin/env python3
"""Generate a valid starter optimization contract from an existing workflow.

`piw optimize scaffold` calls build_contract() here. The generated contract is
validated against schemas/optimization-contract.schema.json before it is
written, so `piw optimize init` accepts it unchanged. Every value is derived
from the workflow and the two corpora; nothing is guessed silently — the agent
is expected to read and tune the result.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from optimization_bundle import _count_jsonl, sha256_bytes, sha256_file  # noqa: E402

EVAL_SOURCE_NAME = "optimization-eval.py"
MAX_MUTABLE = 100


class ScaffoldError(RuntimeError):
    pass


def _split_model(model: str) -> tuple[str, str]:
    provider, sep, identifier = str(model).partition("/")
    if not sep or not provider.strip() or not identifier.strip():
        raise ScaffoldError(
            f"workflow model '{model}' is not provider/id — set a pinned model "
            "(first two columns of `pi --list-models` joined with '/') before scaffolding")
    return provider.strip(), identifier.strip()


def _mutable_mechanisms(spec: dict[str, Any]) -> list[dict[str, str]]:
    mutable: list[dict[str, str]] = [
        {"mechanism": "default-model", "pointer": "/model"},
        {"mechanism": "default-thinking", "pointer": "/thinking"},
    ]
    if isinstance(spec.get("qa"), dict) and spec["qa"].get("prompt"):
        mutable.append({"mechanism": "qa-prompt", "pointer": "/qa/prompt"})
    model_steps = 0
    for index, step in enumerate(spec.get("steps") or []):
        if not isinstance(step, dict) or not step.get("prompt"):
            continue
        model_steps += 1
        step_id = str(step.get("id") or f"step-{index}")
        mutable.append({"mechanism": f"{step_id}-prompt", "pointer": f"/steps/{index}/prompt"})
        mutable.append({"mechanism": f"{step_id}-model", "pointer": f"/steps/{index}/model"})
        mutable.append({"mechanism": f"{step_id}-thinking", "pointer": f"/steps/{index}/thinking"})
    if not model_steps:
        raise ScaffoldError(
            "workflow has no prompt steps, so there is nothing to optimize — "
            "optimization mutates model prompts/models/thinking on llm, tool, or agent nodes")
    if len(mutable) > MAX_MUTABLE:
        raise ScaffoldError(
            f"workflow yields {len(mutable)} mutable mechanisms; the contract schema allows {MAX_MUTABLE}. "
            "Split the workflow or hand-prune boundaries.mutable.")
    return mutable


def _corpus_meta(path: Path, label: str) -> tuple[bytes, str, int]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ScaffoldError(f"cannot read {label} corpus {path}: {error}") from error
    return raw, sha256_bytes(raw), _count_jsonl(raw, label)


def _relative_under(path: Path, base: Path, label: str) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        raise ScaffoldError(
            f"the {label} corpus must live inside the contract directory ({base}); "
            f"move it next to steps.yaml or pass --contract-out") from None


def _install_eval_source(contract_dir: Path) -> Path:
    source = Path(__file__).resolve().parent / "optimization_eval.py"
    target = contract_dir / EVAL_SOURCE_NAME
    raw = source.read_bytes()
    if target.exists():
        if target.read_bytes() == raw:
            return target
        raise ScaffoldError(
            f"{target} already exists with different content; refusing to overwrite — "
            "point --contract-out at a fresh directory or remove it")
    shutil.copyfile(source, target)
    return target


def build_contract(workflow_path: Path, development: Path, holdout: Path,
                   contract_out: Path) -> dict[str, Any]:
    try:
        spec = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ScaffoldError(f"cannot read workflow {workflow_path}: {error}") from error
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise ScaffoldError(f"{workflow_path} has no steps list")

    provider, model_id = _split_model(spec.get("model") or "")
    thinking = str(spec.get("thinking") or "low")

    contract_dir = contract_out.resolve().parent
    contract_dir.mkdir(parents=True, exist_ok=True)
    eval_source = _install_eval_source(contract_dir)
    eval_sha = sha256_file(eval_source)

    dev_raw, dev_sha, dev_count = _corpus_meta(development, "development")
    del dev_raw
    _, holdout_sha, holdout_count = _corpus_meta(holdout, "holdout")
    dev_rel = _relative_under(development, contract_dir, "development")

    input_description = ""
    if isinstance(spec.get("input"), dict):
        input_description = str(spec["input"].get("description") or "")

    contract: dict[str, Any] = {
        "schema": "pi-graph.optimization-contract.v1",
        "evaluation_version": "pass-rate-v1",
        "route": "candidate_loop",
        "objective": {
            "description": (
                f"Improve workflow '{spec.get('workflow') or workflow_path.stem}' without breaking its gates: "
                + (f"one unit is {input_description}" if input_description
                   else "one unit is one workflow run over one development item")),
            "unit": "one workflow run over one development corpus item",
            "primary_metric": "pass_rate",
            "direction": "maximize",
            "minimum_gain": 0.05,
            "definition_of_done": (
                "A candidate is kept only when its uncertainty-adjusted pass rate beats the incumbent "
                "by minimum_gain on the frozen development corpus; promotion additionally requires the "
                "private holdout score to stay within maximum_drop_from_selected_dev."),
            "non_goals": [],
        },
        "boundaries": {
            "mutable": _mutable_mechanisms(spec),
            "protected_files": [{"path": EVAL_SOURCE_NAME, "sha256": eval_sha}],
            "authorized_effects": [],
            "rollback": ("Rejected candidates are restored byte-for-byte from candidates/<id>/parent.yaml; "
                         "promotion failure restores artifacts/baseline.yaml."),
            "network": "denied",
        },
        "development": {"path": dev_rel, "sha256": dev_sha, "format": "jsonl", "count": dev_count},
        # Digest-only by design: init never reads holdout bytes, and no locator
        # enters candidate-readable state. Keep the private file outside the repo.
        "holdout": {"sha256": holdout_sha, "format": "jsonl", "count": holdout_count},
        "evaluation": {
            "evaluator": {
                "argv": ["python3", EVAL_SOURCE_NAME, "summarize", "--batch-json", "{batch_json}"],
                "sources": [{"path": EVAL_SOURCE_NAME, "sha256": eval_sha}],
                "timeout_seconds": 120,
            },
            "parser": {
                "argv": ["python3", EVAL_SOURCE_NAME, "parse"],
                "sources": [{"path": EVAL_SOURCE_NAME, "sha256": eval_sha}],
                "timeout_seconds": 60,
            },
            "hard_gates": [],
            "repeats": 1,
            "seeds": [0],
            "uncertainty": {"method": "none", "minimum_repeats": 1},
            "tie_breakers": ["lower_cost", "lower_tokens"],
        },
        "execution": {
            "provider": provider,
            "model": model_id,
            "thinking": thinking,
            "tools": [],
            "environment": {},
            "dependency_files": [],
            "batch": {"parallel": 2, "require_all": True, "item_timeout_seconds": 900},
            "cache": False,
        },
        "budgets": {
            "max_candidates": 6,
            "max_wall_seconds": 14400,
            "max_tokens": 2_000_000,
            "max_cost_usd": 10.0,
            "max_failures": 2,
            "max_consecutive_non_keeps": 3,
            "per_candidate": {"max_wall_seconds": 1800, "max_tokens": 400_000,
                              "max_cost_usd": 2.0, "max_failures": 1},
            "promotion": {"max_wall_seconds": 1800, "max_tokens": 400_000, "max_cost_usd": 2.0},
        },
        "promotion": {
            "minimum_holdout_score": 0.0,
            "maximum_drop_from_selected_dev": 0.05,
            "require_all_gates": True,
        },
    }

    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:
        raise ScaffoldError(f"jsonschema unavailable: {error}") from error
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "optimization-contract.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(contract),
                    key=lambda error: [str(part) for part in error.absolute_path])
    if errors:  # Defensive: a scaffold bug must never produce a red contract.
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise ScaffoldError(f"scaffold produced an invalid contract at {location}: {first.message}")

    contract_out.write_text(json.dumps(contract, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return contract
