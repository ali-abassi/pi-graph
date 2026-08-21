#!/usr/bin/env python3
"""Compare models on the SAME deterministic workflow over the SAME frozen
inputs: pass rate, QA verdict, judge scores, retries, tokens, cost, wall time.

Usage:
  python3 eval_models.py steps.yaml --inputs corpus.jsonl --input-file idea.md \
      --models openai-codex/gpt-5.6-luna,openai-codex/gpt-5.6-sol [--parallel 2]

Only the top-level default `model:` is swapped per candidate — per-step model
pins (judges, QA) stay fixed, so the evaluator is held constant while the
generator varies. Cache is intentionally NOT shared across models (each model
gets its own cache namespace via its own eval dir) and inputs are frozen, so
the comparison is paired. Emits eval-report.md + eval.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_batch import load_items, run_item  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Model eval for a deterministic workflow")
    ap.add_argument("steps_file", type=Path)
    ap.add_argument("--inputs", type=Path, required=True)
    # --input-file is kept as a deprecated alias: it names the FILENAME each
    # item's content is staged under, never a path read from disk.
    ap.add_argument("--input-name", "--input-file", dest="input_name", default="input.txt")
    ap.add_argument("--models", required=True, help="comma-separated model ids")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--json", action="store_true", help="emit one machine-readable receipt")
    args, extra = ap.parse_known_args()
    if "--input-file" in sys.argv[1:]:
        print("warning: --input-file is deprecated and names the staged FILENAME, "
              "not a path; use --input-name", file=sys.stderr)
    extra = [*extra, "--no-cache"]  # paired comparison: no cross-run reuse

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    items = load_items(args.inputs)
    if args.limit:
        items = items[:args.limit]
    if not items or not models:
        raise SystemExit("need at least one input and one model")
    eval_dir = (args.out or args.steps_file.parent /
                f"eval-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}").resolve()
    eval_dir.mkdir(parents=True, exist_ok=True)
    if not args.json:
        print(f"eval: {len(models)} model(s) x {len(items)} input(s) · dir={eval_dir}", flush=True)

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {}
        for model in models:
            mdir = eval_dir / model.replace("/", "_")
            for item in items:
                fut = pool.submit(run_item, args.steps_file.resolve(), item, mdir,
                                  args.input_name, extra, model)
                futs[fut] = (model, item["id"])
        for fut in cf.as_completed(futs):
            model, iid = futs[fut]
            r = fut.result()
            r["model"] = model
            results.append(r)
            if not args.json:
                print(f"  {model} · {iid}: {'PASS' if r['passed'] else 'FAIL'} · "
                      f"${r['cost']:.4f} · {r['wall_s']}s", flush=True)

    (eval_dir / "eval.json").write_text(json.dumps(results, indent=1))
    lines = [f"# Model eval — {args.steps_file.name} · {len(items)} input(s)", "",
             "| model | pass | QA pass | avg judge | avg retries proxy | avg tokens | avg cost | avg wall |",
             "|---|---|---|---|---|---|---|---|"]
    for model in models:
        rs = [r for r in results if r["model"] == model]
        scores = [s for r in rs for s in r["judge_scores"]]
        lines.append("| {m} | {p}/{n} | {q}/{n} | {j} | {rt:.1f} | {t:.0f} | ${c:.4f} | {w:.0f}s |".format(
            m=model.split("/")[-1], n=len(rs),
            p=sum(r["passed"] for r in rs),
            q=sum(1 for r in rs if r["qa"] == "pass") if any(r["qa"] for r in rs) else "-",
            j=f"{statistics.mean(scores):.1f}" if scores else "-",
            rt=statistics.mean(len(r["judge_scores"]) for r in rs) if rs else 0,
            t=statistics.mean(r["tokens"] for r in rs),
            c=statistics.mean(r["cost"] for r in rs),
            w=statistics.mean(r["wall_s"] for r in rs)))
    report = "\n".join(lines) + "\n"
    report_path = eval_dir / "eval-report.md"
    report_path.write_text(report)
    if args.json:
        summaries = []
        for model in models:
            rows = [row for row in results if row["model"] == model]
            summaries.append({
                "model": model, "total": len(rows), "passed": sum(row["passed"] for row in rows),
                "tokens": sum(row["tokens"] for row in rows),
                "cost": sum(row["cost"] for row in rows),
                "wall_seconds": sum(row["wall_s"] for row in rows),
            })
        # ok mirrors the exit code: an eval that completed but had failing items
        # is a successful measurement with a non-zero exit, matching `run` and
        # `compare` conventions, so `piw eval ... && next` cannot proceed on red.
        ok = all(row["passed"] for row in results)
        print(json.dumps({"schema": "pi-graph.eval.v1", "ok": ok,
                          "eval_dir": str(eval_dir), "results_path": str(eval_dir / "eval.json"),
                          "report": str(report_path), "models": summaries}, separators=(",", ":")))
    else:
        print("\n" + report)
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
