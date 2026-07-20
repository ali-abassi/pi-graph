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
    ap.add_argument("--input-file", required=True)
    ap.add_argument("--models", required=True, help="comma-separated model ids")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path)
    args, extra = ap.parse_known_args()
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
    print(f"eval: {len(models)} model(s) x {len(items)} input(s) · dir={eval_dir}", flush=True)

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {}
        for model in models:
            mdir = eval_dir / model.replace("/", "_")
            for item in items:
                fut = pool.submit(run_item, args.steps_file.resolve(), item, mdir,
                                  args.input_file, extra, model)
                futs[fut] = (model, item["id"])
        for fut in cf.as_completed(futs):
            model, iid = futs[fut]
            r = fut.result()
            r["model"] = model
            results.append(r)
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
    (eval_dir / "eval-report.md").write_text(report)
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
