#!/usr/bin/env python3
"""Run one frozen workflow contract over a corpus of isolated inputs.

The batch controller owns item concurrency, resumability, progress, and the
aggregate receipt. ``run_steps.py`` remains the only workflow engine.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_steps.py"
PIW = ROOT / "scripts" / "piw.py"
BATCH_VERSION = 1
MAX_PARALLEL = 32


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def digest(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def load_items(src: Path) -> list[dict[str, Any]]:
    """Load and validate a stable ordered corpus without retaining it in reports."""
    src = src.expanduser().resolve()
    if not src.exists():
        raise ValueError(f"input corpus not found: {src}")
    raw: list[tuple[str, str]] = []
    if src.is_dir():
        files = [path for path in sorted(src.iterdir()) if path.suffix.lower() in {".txt", ".md"}]
        raw = [(path.stem, path.read_text(encoding="utf-8")) for path in files]
    elif src.suffix.lower() == ".jsonl":
        for line_number, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{src}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                raise ValueError(f"{src}:{line_number}: expected an object with string content")
            raw.append((str(value.get("id", len(raw) + 1)), value["content"]))
    elif src.is_file():
        raw = [(str(index), line) for index, line in enumerate(
            (line for line in src.read_text(encoding="utf-8").splitlines() if line.strip()), start=1
        )]
    else:
        raise ValueError(f"unsupported input corpus: {src}")

    if not raw:
        raise ValueError("no items found")
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, (identifier, content) in enumerate(raw, start=1):
        identifier = identifier.strip()
        if not identifier:
            raise ValueError(f"item {index}: id must not be empty")
        if len(identifier) > 200:
            raise ValueError(f"item {index}: id exceeds 200 characters")
        if identifier in seen:
            raise ValueError(f"duplicate item id: {identifier}")
        seen.add(identifier)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier).strip("._-")[:64] or "item"
        items.append({
            "index": index,
            "id": identifier,
            "key": f"{index:06d}-{slug}",
            "content": content,
            "sha256": digest(content),
        })
    return items


def corpus_digest(items: list[dict[str, Any]]) -> str:
    receipt = [{"index": item["index"], "id": item["id"], "sha256": item["sha256"]} for item in items]
    return digest(json.dumps(receipt, separators=(",", ":"), ensure_ascii=False))


def safe_input_path(value: str) -> Path:
    path = Path(value)
    if (
        path.is_absolute() or len(path.parts) != 1
        or path.name in {"", ".", "..", "result.json"}
    ):
        raise ValueError("--input-file must be one safe filename inside each item directory")
    return path


def validate_workflow(steps_file: Path) -> tuple[str, list[str], str]:
    steps_file = steps_file.expanduser().resolve()
    if not steps_file.is_file():
        raise ValueError(f"workflow not found: {steps_file}")
    result = subprocess.run(
        [sys.executable, str(PIW), "validate", str(steps_file), "--json"],
        text=True, capture_output=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr).strip()
        raise ValueError(f"workflow validation failed before batch execution: {detail}")
    spec = yaml.safe_load(steps_file.read_text(encoding="utf-8")) or {}
    step_ids = [str(step["id"]) for step in spec.get("steps", [])]
    if not step_ids:
        raise ValueError("workflow has no steps")
    cwd = (steps_file.parent / spec.get("cwd", ".")).resolve()
    return str(spec.get("workflow") or steps_file.stem), step_ids, str(cwd)


def manifest_for(steps_file: Path, workflow: str, step_ids: list[str], cwd: str,
                 inputs: Path, items: list[dict[str, Any]], input_file: Path,
                 require_all: bool) -> dict[str, Any]:
    text = steps_file.read_text(encoding="utf-8")
    return {
        "version": BATCH_VERSION,
        "workflow": workflow,
        "workflow_path": str(steps_file),
        "workflow_sha256": digest(text),
        "workflow_cwd": cwd,
        "steps": step_ids,
        "corpus_path": str(inputs),
        "corpus_sha256": corpus_digest(items),
        "total": len(items),
        "input_file": str(input_file),
        "require_all_steps": require_all,
        "created_at": now(),
    }


def assert_resume_matches(existing: dict[str, Any], current: dict[str, Any]) -> None:
    keys = (
        "version", "workflow_sha256", "workflow_cwd", "steps", "corpus_sha256",
        "total", "input_file", "require_all_steps",
    )
    changed = [key for key in keys if existing.get(key) != current.get(key)]
    if changed:
        raise ValueError(
            "resume refused because the frozen batch contract changed: " + ", ".join(changed)
        )


def read_events(path: Path, expected_steps: list[str]) -> dict[str, Any]:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    run_end: dict[str, Any] | None = None
    malformed = 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            identifier = event.get("id")
            if event.get("t") == "step_cached" and identifier in expected_steps:
                passed.add(identifier)
            elif event.get("t") == "step_end" and identifier in expected_steps:
                (passed if event.get("passed") is True else failed).add(identifier)
            elif event.get("t") == "step_skipped" and identifier in expected_steps:
                skipped.add(identifier)
            elif event.get("t") == "run_end":
                run_end = event
    terminal = passed | failed | skipped
    return {
        "passed_steps": [step for step in expected_steps if step in passed],
        "failed_steps": [step for step in expected_steps if step in failed],
        "skipped_steps": [step for step in expected_steps if step in skipped],
        "terminal_steps": [step for step in expected_steps if step in terminal],
        "contract_complete": terminal == set(expected_steps) and malformed == 0 and run_end is not None,
        "run_end_ok": bool(run_end and run_end.get("ok") is True),
        "malformed_events": malformed,
    }


def stop_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_item(steps_yaml: Path, item: dict[str, Any], batch_dir: Path, input_file: str,
             extra_args: list[str], model: str | None = None, *,
             workflow_cwd: Path | None = None, expected_steps: list[str] | None = None,
             require_all: bool = False, item_timeout: float = 3600,
             git_history: bool = False) -> dict[str, Any]:
    """Execute one item in a new attempt directory and return its receipt.

    ``model`` remains an intentionally narrow compatibility hook for
    ``eval_models.py``. Batch execution itself always uses the frozen graph.
    """
    expected_steps = expected_steps or [
        str(step["id"]) for step in (yaml.safe_load(steps_yaml.read_text(encoding="utf-8")) or {}).get("steps", [])
    ]
    if workflow_cwd is None:
        source_spec = yaml.safe_load(steps_yaml.read_text(encoding="utf-8")) or {}
        workflow_cwd = (steps_yaml.parent / source_spec.get("cwd", ".")).resolve()
    item_dir = batch_dir / "items" / item.get("key", re.sub(r"[^A-Za-z0-9._-]", "_", str(item["id"])))
    attempts_dir = item_dir / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempt = len([path for path in attempts_dir.iterdir() if path.is_dir()]) + 1
    run_dir = attempts_dir / f"{attempt:03d}"
    run_dir.mkdir(parents=True, exist_ok=False)
    relative_input = safe_input_path(input_file)
    input_path = item_dir / relative_input
    input_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(input_path, str(item["content"]))
    events_path = run_dir / "events.jsonl"

    frozen_yaml = steps_yaml
    if model:
        text = steps_yaml.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^model:.*$", f"model: {model}", text, count=1)
        frozen_yaml = run_dir / "steps.eval.yaml"
        frozen_yaml.write_text(text, encoding="utf-8")

    command = [
        sys.executable, str(RUNNER), str(frozen_yaml),
        "--run-dir", str(run_dir),
        "--input-file", str(input_path),
        "--events", str(events_path),
    ]
    command.extend(["--cwd", str(workflow_cwd)])
    if not git_history:
        command.append("--no-history")
    command.extend(extra_args)
    started = time.monotonic()
    timed_out = False
    process = subprocess.Popen(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=item_timeout)
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process_group(process)
        stdout, stderr = process.communicate()
        exit_code = 124
        stderr = f"item exceeded {item_timeout:g}s timeout\n{stderr}"
    wall = time.monotonic() - started
    atomic_write(run_dir / "runner.log", f"{stdout}\n--- stderr ---\n{stderr}")

    evidence = read_events(events_path, expected_steps)
    ledger_path = run_dir / "ledger.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else []
    except (json.JSONDecodeError, ValueError):
        ledger = []
    tokens = sum(int(entry.get("total", 0) or 0) for entry in ledger if isinstance(entry, dict))
    cost = sum(float(entry.get("cost", 0.0) or 0.0) for entry in ledger if isinstance(entry, dict))
    judge_scores = [float(score) for score in re.findall(r"judge score ([0-9.]+)", stdout)]
    qa = None
    qa_path = run_dir / "qa.md"
    if qa_path.is_file():
        match = re.search(r'"verdict"\s*:\s*"(pass|fail)"', qa_path.read_text(encoding="utf-8"))
        qa = match.group(1) if match else "unparseable"
    all_steps_passed = evidence["passed_steps"] == expected_steps
    passed = (
        exit_code == 0
        and evidence["run_end_ok"]
        and evidence["contract_complete"]
        and (all_steps_passed or not require_all)
    )
    error = None
    if not passed:
        if timed_out:
            error = f"item exceeded {item_timeout:g}s timeout"
        elif require_all and evidence["skipped_steps"]:
            error = "--require-all rejected skipped step(s): " + ", ".join(evidence["skipped_steps"])
        elif not evidence["contract_complete"]:
            missing = [step for step in expected_steps if step not in evidence["terminal_steps"]]
            error = "execution contract incomplete; missing terminal evidence: " + ", ".join(missing)
        else:
            error = " ".join(stderr.strip().split())[-1000:] or f"runner exited {exit_code}"
    result = {
        "index": item.get("index"),
        "key": item.get("key"),
        "id": str(item["id"]),
        "input_sha256": item.get("sha256", digest(str(item["content"]))),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "attempt": attempt,
        "exit": exit_code,
        "timed_out": timed_out,
        "contract_complete": evidence["contract_complete"],
        "all_steps_passed": all_steps_passed,
        "expected_steps": expected_steps,
        **{key: evidence[key] for key in (
            "passed_steps", "failed_steps", "skipped_steps", "terminal_steps", "malformed_events"
        )},
        "wall_s": round(wall, 3),
        "tokens": tokens,
        "cost": round(cost, 6),
        "qa": qa,
        "judge_scores": judge_scores,
        "run_dir": str(run_dir),
        "log": str(run_dir / "runner.log"),
        "error": error,
        "finished_at": now(),
    }
    write_json(item_dir / "result.json", result)
    return result


def read_result(batch_dir: Path, item: dict[str, Any]) -> dict[str, Any] | None:
    path = batch_dir / "items" / item["key"] / "result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def summary_for(results: list[dict[str, Any]], total: int, batch_dir: Path,
                status: str, started: float) -> dict[str, Any]:
    ordered = sorted(results, key=lambda result: int(result.get("index") or 0))
    passed = sum(result.get("passed") is True for result in ordered)
    failed = len(ordered) - passed
    completed = len(ordered)
    return {
        "ok": status == "completed" and completed == total and failed == 0,
        "status": status,
        "total": total,
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "not_run": total - completed,
        "contract_complete": sum(result.get("contract_complete") is True for result in ordered),
        "all_steps_passed": sum(result.get("all_steps_passed") is True for result in ordered),
        "tokens": sum(int(result.get("tokens", 0) or 0) for result in ordered),
        "cost": round(sum(float(result.get("cost", 0.0) or 0.0) for result in ordered), 6),
        "wall_s": round(time.monotonic() - started, 3),
        "batch_dir": str(batch_dir),
        "updated_at": now(),
        "results": ordered,
    }


def write_report(summary: dict[str, Any], batch_dir: Path, label: str) -> None:
    write_json(batch_dir / "batch.json", summary)
    lines = [
        f"# Batch report — {label}", "",
        f"Status: **{summary['status']}** · {summary['passed']}/{summary['total']} passed · "
        f"{summary['not_run']} not run · ${summary['cost']:.4f} · {summary['tokens']} tokens · "
        f"{summary['wall_s']:.1f}s wall", "",
        f"Execution contracts complete: {summary['contract_complete']}/{summary['total']} · "
        f"all declared steps passed: {summary['all_steps_passed']}/{summary['total']}", "",
        "| # | item | result | exact steps | attempt | tokens | cost | wall | evidence |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for result in summary["results"]:
        exact = f"{len(result['passed_steps'])}/{len(result['expected_steps'])}"
        try:
            evidence = str(Path(result["run_dir"]).relative_to(batch_dir)) if result.get("run_dir") else "-"
        except ValueError:
            evidence = str(result.get("run_dir") or "-")
        lines.append(
            f"| {result['index']} | {result['id']} | {'PASS' if result['passed'] else 'FAIL'} | "
            f"{exact} | {result['attempt']} | {result['tokens']} | ${result['cost']:.4f} | "
            f"{result['wall_s']:.2f}s | `{evidence}` |"
        )
    atomic_write(batch_dir / "batch-report.md", "\n".join(lines) + "\n")


def controller_state(batch_dir: Path, status: str, **extra: Any) -> None:
    value = {"pid": os.getpid(), "status": status, "batch_dir": str(batch_dir), "updated_at": now(), **extra}
    write_json(batch_dir / "controller.json", value)


def default_batch_dir(steps_file: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return (steps_file.parent / f"batch-{stamp}-{uuid.uuid4().hex[:6]}").resolve()


def launch_detached(args: argparse.Namespace) -> int:
    if args.resume and args.out:
        raise ValueError("--resume and --out are mutually exclusive")
    batch_dir = Path(args.resume).expanduser().resolve() if args.resume else (
        Path(args.out).expanduser().resolve() if args.out else default_batch_dir(args.steps_file.resolve())
    )
    if batch_dir.exists() and not args.resume:
        raise ValueError(f"batch directory already exists: {batch_dir}")
    batch_dir.mkdir(parents=True, exist_ok=True)
    child_args = [argument for argument in sys.argv[1:] if argument not in {"--detach", "--json"}]
    if not args.out and not args.resume:
        child_args.extend(["--out", str(batch_dir)])
    log = (batch_dir / "controller.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *child_args],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
    )
    log.close()
    receipt = {
        "ok": True,
        "status": "starting",
        "pid": process.pid,
        "batch_dir": str(batch_dir),
        "status_command": f"piw batch-status {batch_dir} --json",
        "log": str(batch_dir / "controller.log"),
    }
    write_json(batch_dir / "controller.json", {**receipt, "updated_at": now()})
    print(json.dumps(receipt, separators=(",", ":")) if args.json else (
        f"batch started · pid={process.pid} · dir={batch_dir}\n"
        f"status: piw batch-status {batch_dir}"
    ))
    return 0


def run_batch(args: argparse.Namespace, extra: list[str]) -> int:
    if not 1 <= args.parallel <= MAX_PARALLEL:
        raise ValueError(f"--parallel must be from 1 to {MAX_PARALLEL}")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be at least 1")
    if args.item_timeout <= 0:
        raise ValueError("--item-timeout must be greater than zero")
    if args.stop_after_failures is not None and args.stop_after_failures < 1:
        raise ValueError("--stop-after-failures must be at least 1")
    if args.progress_every is not None and args.progress_every < 1:
        raise ValueError("--progress-every must be at least 1")
    reserved = {"--run-dir", "--input", "--input-file", "--events", "--from", "--verify", "--cwd"}
    if any(argument.split("=", 1)[0] in reserved for argument in extra):
        raise ValueError("batch owns run directories, immutable inputs, events, and resume boundaries")

    steps_file = args.steps_file.expanduser().resolve()
    inputs = args.inputs.expanduser().resolve()
    input_file = safe_input_path(args.input_file)
    items = load_items(inputs)
    if args.limit:
        items = items[:args.limit]
    workflow, expected_steps, workflow_cwd = validate_workflow(steps_file)
    current = manifest_for(
        steps_file, workflow, expected_steps, workflow_cwd, inputs, items, input_file, args.require_all,
    )

    if args.resume and args.out:
        raise ValueError("--resume and --out are mutually exclusive")
    batch_dir = Path(args.resume).expanduser().resolve() if args.resume else (
        Path(args.out).expanduser().resolve() if args.out else default_batch_dir(steps_file)
    )
    manifest_path = batch_dir / "batch-manifest.json"
    if args.resume:
        if not manifest_path.is_file():
            raise ValueError(f"resume manifest not found: {manifest_path}")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert_resume_matches(existing, current)
        manifest = existing
    else:
        if batch_dir.exists():
            existing_names = {path.name for path in batch_dir.iterdir()}
            if existing_names - {"controller.json", "controller.log"}:
                raise ValueError(f"batch directory already exists: {batch_dir}")
        else:
            batch_dir.mkdir(parents=True)
        manifest = current
        frozen_dir = batch_dir / "workflow"
        frozen_dir.mkdir()
        atomic_write(frozen_dir / "steps.yaml", steps_file.read_text(encoding="utf-8"))
        manifest["frozen_workflow"] = "workflow/steps.yaml"
        write_json(manifest_path, manifest)

    frozen_workflow = batch_dir / str(manifest["frozen_workflow"])
    started = time.monotonic()
    controller_state(batch_dir, "running", total=len(items), workflow=workflow)
    existing_results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for item in items:
        result = read_result(batch_dir, item)
        if result and result.get("passed") is True:
            existing_results.append(result)
        else:
            pending.append(item)
    results = list(existing_results)
    failures = sum(result.get("passed") is not True for result in results)
    stopped = False
    progress_every = args.progress_every or max(1, min(100, len(items) // 20 or 1))

    def persist(status: str, running: int = 0) -> dict[str, Any]:
        summary = summary_for(results, len(items), batch_dir, status, started)
        progress = {key: value for key, value in summary.items() if key != "results"}
        progress["running"] = running if status == "running" else 0
        write_json(batch_dir / "progress.json", progress)
        return summary

    persist("running")
    if not args.json:
        resumed = f" · {len(existing_results)} already passed" if args.resume else ""
        print(
            f"batch: {len(items)} item(s) · {len(expected_steps)} step(s) each · "
            f"parallel={args.parallel}{resumed} · dir={batch_dir}", flush=True,
        )

    iterator = iter(pending)
    active: dict[cf.Future[dict[str, Any]], dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        def dispatch() -> None:
            while len(active) < args.parallel and not stopped:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                future = pool.submit(
                    run_item, frozen_workflow, item, batch_dir, str(input_file), extra,
                    workflow_cwd=Path(workflow_cwd), expected_steps=expected_steps,
                    require_all=args.require_all, item_timeout=args.item_timeout,
                    git_history=args.git_history,
                )
                active[future] = item

        dispatch()
        while active:
            completed, _ = cf.wait(active, return_when=cf.FIRST_COMPLETED)
            for future in completed:
                item = active.pop(future)
                try:
                    result = future.result()
                except Exception as error:  # controller failure; preserve an actionable receipt
                    result = {
                        "index": item["index"], "key": item["key"], "id": item["id"],
                        "input_sha256": item["sha256"], "status": "failed", "passed": False,
                        "attempt": 0, "exit": 70, "timed_out": False,
                        "contract_complete": False, "all_steps_passed": False,
                        "expected_steps": expected_steps, "passed_steps": [], "failed_steps": [],
                        "skipped_steps": [], "terminal_steps": [], "malformed_events": 0,
                        "wall_s": 0.0, "tokens": 0, "cost": 0.0, "run_dir": "", "log": "",
                        "error": str(error), "finished_at": now(),
                    }
                    write_json(batch_dir / "items" / item["key"] / "result.json", result)
                results.append(result)
                if not result["passed"]:
                    failures += 1
                complete_count = len(results)
                if not args.json and (not result["passed"] or complete_count % progress_every == 0 or complete_count == len(items)):
                    print(
                        f"  {complete_count}/{len(items)} · {result['id']}: "
                        f"{'PASS' if result['passed'] else 'FAIL'} · "
                        f"{len(result['passed_steps'])}/{len(expected_steps)} steps · "
                        f"${result['cost']:.4f} · {result['wall_s']:.2f}s", flush=True,
                    )
            persist("running", len(active))
            if args.stop_after_failures and failures >= args.stop_after_failures:
                stopped = True
            dispatch()

    status = "stopped" if stopped and len(results) < len(items) else "completed"
    summary = summary_for(results, len(items), batch_dir, status, started)
    write_report(summary, batch_dir, steps_file.name)
    write_json(batch_dir / "progress.json", {key: value for key, value in summary.items() if key != "results"})
    controller_state(batch_dir, status, ok=summary["ok"], total=len(items), completed=summary["completed"])
    if args.json:
        receipt = {key: value for key, value in summary.items() if key != "results"}
        receipt["results_path"] = str(batch_dir / "batch.json")
        receipt["report_path"] = str(batch_dir / "batch-report.md")
        print(json.dumps(receipt, separators=(",", ":")))
    else:
        print(
            f"{status}: {summary['passed']}/{summary['total']} passed · "
            f"{summary['not_run']} not run · ${summary['cost']:.4f} · "
            f"{summary['wall_s']:.1f}s · {batch_dir / 'batch-report.md'}",
            flush=True,
        )
    return 0 if summary["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one deterministic workflow over a corpus")
    parser.add_argument("steps_file", type=Path)
    parser.add_argument("--inputs", type=Path, required=True,
                        help="corpus: JSONL objects, a text/markdown directory, or non-empty lines")
    parser.add_argument("--input-file", default="input.txt",
                        help="safe relative filename for each immutable item (default: input.txt)")
    parser.add_argument("--parallel", type=int, default=4,
                        help=f"concurrent items, 1-{MAX_PARALLEL} (default: 4)")
    parser.add_argument("--out", type=Path, help="new batch directory")
    parser.add_argument("--resume", type=Path, help="resume a batch with the same graph and corpus")
    parser.add_argument("--limit", type=int, help="canary: run only the first N items")
    parser.add_argument("--require-all", action="store_true",
                        help="fail an item if any declared step is skipped")
    parser.add_argument("--stop-after-failures", type=int,
                        help="stop dispatching new items after N failures")
    parser.add_argument("--item-timeout", type=float, default=3600,
                        help="hard wall timeout per item in seconds (default: 3600)")
    parser.add_argument("--git-history", action="store_true",
                        help="commit every item step to Git (off by default for bulk efficiency)")
    parser.add_argument("--progress-every", type=int,
                        help="print one progress line every N completed items")
    parser.add_argument("--detach", action="store_true",
                        help="start in the background and return a status command")
    parser.add_argument("--json", action="store_true", help="print one machine-readable receipt")
    return parser


def main() -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args()
    try:
        if args.detach:
            return launch_detached(args)
        return run_batch(args, extra)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
