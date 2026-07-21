#!/usr/bin/env python3
"""Validate or live-run the graduated public example catalog.

Live mode copies every example into an isolated evidence directory, so model
cache, runs, agent effects, and logs never dirty the checkout. The resulting
report points to every run's log.md and ledger.json and identifies token,
latency, retry, cache, and parallelism signals worth optimizing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
WORKFLOWS = EXAMPLES / "workflows"
CATALOG = EXAMPLES / "catalog.json"
PIW = ROOT / "bin" / "piw"


def fail(message: str) -> None:
    raise RuntimeError(message)


def load_catalog() -> dict[str, Any]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if catalog.get("version") != 1 or not catalog.get("cases"):
        fail("examples/catalog.json must be a non-empty v1 catalog")
    return catalog


def assert_live_model(spec: dict[str, Any], model: str, thinking: str, case_id: str) -> None:
    """Every paid node in the public live suite must honor the cost route."""
    default_model = spec.get("model")
    default_thinking = spec.get("thinking", "medium")
    for step in spec.get("steps") or []:
        if not step.get("prompt"):
            continue
        actual_model = step.get("model", default_model)
        actual_thinking = step.get("thinking", default_thinking)
        if (actual_model, actual_thinking) != (model, thinking):
            fail(
                f"{case_id}:{step.get('id')} resolves to {actual_model}/{actual_thinking}; "
                f"live examples must use {model}/{thinking}"
            )
        judge = step.get("judge")
        if judge and (judge.get("model", default_model), judge.get("thinking", default_thinking)) != (model, thinking):
            fail(f"{case_id}:{step.get('id')} judge is not pinned to {model}/{thinking}")
    qa = spec.get("qa")
    if qa and (qa.get("model", default_model), qa.get("thinking", default_thinking)) != (model, thinking):
        fail(f"{case_id}:qa is not pinned to {model}/{thinking}")


def command(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=3_600, check=False)


def copy_case(source: Path, target: Path) -> None:
    """Copy authored fixtures only; local run evidence is never suite input."""
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("runs", "cache", ".artifacts", "__pycache__"),
    )


def validate_case(case: dict[str, Any], workspace: Path, env: dict[str, str], model: str, thinking: str) -> dict[str, Any]:
    case_id = case["id"]
    source = WORKFLOWS / case_id
    steps = source / "steps.yaml"
    input_file = source / "input.txt"
    if not steps.is_file() or not input_file.is_file():
        fail(f"{case_id}: expected steps.yaml and input.txt")
    spec = yaml.safe_load(steps.read_text(encoding="utf-8")) or {}
    assert_live_model(spec, model, thinking, case_id)

    target = workspace / case_id
    copy_case(source, target)
    result = command([str(PIW), "validate", str(target / "steps.yaml"), "--json"], env, target)
    try:
        verdict = json.loads(result.stdout)
    except ValueError:
        fail(f"{case_id}: validation returned non-JSON output: {result.stdout or result.stderr}")
    if result.returncode or not verdict.get("holds"):
        fail(f"{case_id}: validation failed: {result.stdout or result.stderr}")
    return {
        "id": case_id,
        "complexity": case["complexity"],
        "features": case["features"],
        "repeat": int(case.get("repeat", 1)),
        "workspace": target,
        "steps": target / "steps.yaml",
        "input": target / "input.txt",
        "validation": verdict,
    }


def live_run(case: dict[str, Any], env: dict[str, str], repetition: int) -> dict[str, Any]:
    started = time.monotonic()
    result = command(
        [str(PIW), "run", str(case["steps"]), "--input-file", str(case["input"]), "--json"],
        env,
        case["workspace"],
    )
    wall = round(time.monotonic() - started, 2)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        fail(f"{case['id']} run {repetition}: non-JSON output: {result.stdout or result.stderr}")
    run_dir = Path(payload.get("run_dir") or "")
    if result.returncode or not payload.get("ok") or not run_dir.is_dir():
        fail(f"{case['id']} run {repetition}: failed: {payload} {result.stderr}")
    ledger_path = run_dir / "ledger.json"
    log_path = run_dir / "log.md"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    qa_path = run_dir / "qa.md"
    qa = qa_path.read_text(encoding="utf-8", errors="replace") if qa_path.is_file() else None
    return {
        "case": case["id"],
        "complexity": case["complexity"],
        "features": case["features"],
        "repetition": repetition,
        "ok": True,
        "wallSeconds": wall,
        "computeSeconds": round(sum(float(entry.get("seconds") or 0) for entry in ledger), 2),
        "tokens": sum(int(entry.get("total") or 0) for entry in ledger),
        "cost": round(sum(float(entry.get("cost") or 0) for entry in ledger), 6),
        "cachedSteps": sum(1 for entry in ledger if entry.get("cached")),
        "retryAttempts": sum(max(0, int(entry.get("attempts") or 1) - 1) for entry in ledger),
        "qa": qa,
        "runDir": str(run_dir),
        "log": str(log_path),
        "ledger": str(ledger_path),
        "steps": ledger,
    }


def optimization_findings(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "seconds": 0.0, "runs": 0})
    for run in runs:
        for entry in run["steps"]:
            key = f"{run['case']}:{entry['id']}"
            totals[key]["tokens"] += int(entry.get("total") or 0)
            totals[key]["cost"] += float(entry.get("cost") or 0)
            totals[key]["seconds"] += float(entry.get("seconds") or 0)
            totals[key]["runs"] += 1
    hotspots = [
        {"step": key, **{name: round(value, 6) for name, value in values.items()}}
        for key, values in sorted(
            totals.items(),
            key=lambda item: (item[1]["tokens"], item[1]["seconds"], item[1]["cost"]),
            reverse=True,
        )[:8]
    ]

    notes: list[str] = []
    if hotspots:
        top = hotspots[0]
        notes.append(
            f"Token hotspot: {top['step']} used {int(top['tokens'])} tokens across {int(top['runs'])} run(s); "
            "inspect its resolved prompt before changing models or quality gates."
        )
    retries = [(run["case"], run["retryAttempts"]) for run in runs if run["retryAttempts"]]
    if retries:
        notes.append("Retry evidence: " + ", ".join(f"{case} +{count}" for case, count in retries) + ".")
    cached = [(run["case"], run["cachedSteps"]) for run in runs if run["cachedSteps"]]
    if cached:
        notes.append("Cache evidence: " + ", ".join(f"{case} {count} hit(s)" for case, count in cached) + ".")
    parallel = [run for run in runs if run["computeSeconds"] > run["wallSeconds"] * 1.15]
    if parallel:
        notes.append(
            "Parallelism evidence: "
            + ", ".join(
                f"{run['case']} {run['computeSeconds']:.1f}s compute/{run['wallSeconds']:.1f}s wall" for run in parallel
            )
            + "."
        )
    if not notes:
        notes.append("No retry, cache, or parallelism signal was large enough to recommend a change.")
    return hotspots, notes


def write_report(out_dir: Path, validated: list[dict[str, Any]], runs: list[dict[str, Any]], model: str, thinking: str) -> None:
    hotspots, notes = optimization_findings(runs) if runs else ([], [])
    payload = {
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": model,
        "thinking": thinking,
        "validated": len(validated),
        "liveRuns": len(runs),
        "passed": all(run["ok"] for run in runs),
        "tokens": sum(run["tokens"] for run in runs),
        "cost": round(sum(run["cost"] for run in runs), 6),
        "wallSeconds": round(sum(run["wallSeconds"] for run in runs), 2),
        "runs": runs,
        "hotspots": hotspots,
        "optimizationNotes": notes,
    }
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Pi Graph example certification",
        "",
        f"{len(validated)} workflows validated · {len(runs)} live runs · model `{model}` · thinking `{thinking}`",
        f"{sum(run['tokens'] for run in runs)} tokens · ${sum(run['cost'] for run in runs):.4f} · {sum(run['wallSeconds'] for run in runs):.1f}s wall",
        "",
        "| example | complexity | run | result | tokens | cost | wall | cache | retries | QA | evidence |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for run in runs:
        qa = "yes" if run["qa"] else "-"
        lines.append(
            f"| {run['case']} | {run['complexity']} | {run['repetition']} | PASS | {run['tokens']} | "
            f"${run['cost']:.4f} | {run['wallSeconds']:.1f}s | {run['cachedSteps']} | "
            f"{run['retryAttempts']} | {qa} | [log]({run['log']}) · [ledger]({run['ledger']}) |"
        )
    if not runs:
        lines.append("| validation only | - | - | PASS | - | - | - | - | - | - | - |")
    lines += ["", "## Optimization signals", ""]
    lines += [f"- {note}" for note in notes] if notes else ["- Live runs were not requested."]
    lines += ["", "Generated evidence is local and intentionally gitignored.", ""]
    report = "\n".join(lines)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"evidence: {out_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or live-run all public workflow examples")
    parser.add_argument("--validate-only", action="store_true", help="check contracts without model calls")
    parser.add_argument("--case", action="append", dest="cases", help="run only this catalog id (repeatable)")
    parser.add_argument("--out", type=Path, help="evidence directory")
    args = parser.parse_args()

    catalog = load_catalog()
    selected = [case for case in catalog["cases"] if not args.cases or case["id"] in args.cases]
    missing = set(args.cases or []) - {case["id"] for case in selected}
    if missing:
        fail(f"unknown example(s): {', '.join(sorted(missing))}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    temporary = tempfile.TemporaryDirectory(prefix="piw-example-validation-") if args.validate_only and not args.out else None
    try:
        default_out = Path(temporary.name) / "suite" if temporary else EXAMPLES / ".artifacts" / f"suite-{stamp}"
        out_dir = (args.out or default_out).resolve()
        workspace = out_dir / "workflows"
        workspace.mkdir(parents=True, exist_ok=False)
        env = {
            **os.environ,
            "PI_GRAPH_ROOTS": str(workspace),
            "PI_GRAPH_STATE_DIR": str(out_dir / "state"),
            "LOOPS_PORT": "1",
        }

        validated = [
            validate_case(case, workspace, env, catalog["liveModel"], catalog["liveThinking"])
            for case in selected
        ]
        runs: list[dict[str, Any]] = []
        if not args.validate_only:
            for case in validated:
                for repetition in range(1, case["repeat"] + 1):
                    print(f"running {case['id']} ({repetition}/{case['repeat']})...", flush=True)
                    run = live_run(case, env, repetition)
                    runs.append(run)
                    print(
                        f"  PASS · {run['tokens']} tok · ${run['cost']:.4f} · "
                        f"{run['wallSeconds']:.1f}s · log {run['log']}",
                        flush=True,
                    )
        write_report(out_dir, validated, runs, catalog["liveModel"], catalog["liveThinking"])
    finally:
        if temporary:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
