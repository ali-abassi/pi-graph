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
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_batch import load_items, run_item  # noqa: E402


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float]:
    if total < 1:
        return {"low": 0.0, "high": 0.0}
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return {"low": round(max(0.0, centre - margin), 6),
            "high": round(min(1.0, centre + margin), 6)}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower))


def summarize_model(model: str, rows: list[dict]) -> dict:
    total = len(rows)
    passed = sum(bool(row["passed"]) for row in rows)
    qa_rows = [row for row in rows if row.get("qa")]
    scores = [float(score) for row in rows for score in row.get("judge_scores") or []]
    costs = [float(row.get("cost") or 0) for row in rows]
    wall = [float(row.get("wall_s") or 0) for row in rows]
    tokens = [int(row.get("tokens") or 0) for row in rows]
    warnings = []
    if total < 5:
        warnings.append({"code": "small_corpus", "message":
                         f"only {total} paired item(s); use at least 5 before changing model policy"})
    return {
        "model": model, "total": total, "passed": passed,
        "pass_rate": round(passed / total, 6) if total else 0,
        "pass_interval_95": wilson_interval(passed, total),
        "qa": {"scored": len(qa_rows),
               "passed": sum(row.get("qa") == "pass" for row in qa_rows)},
        "judge": {"scores": len(scores), "mean": round(statistics.mean(scores), 6) if scores else None},
        # Preserve the v1 numeric totals; detailed distributions are additive.
        "tokens": sum(tokens), "cost": round(sum(costs), 6), "wall_seconds": round(sum(wall), 3),
        "token_stats": {"mean": round(statistics.mean(tokens), 3) if tokens else 0,
                        "median": round(statistics.median(tokens), 3) if tokens else 0},
        "cost_stats": {"mean": round(statistics.mean(costs), 6) if costs else 0,
                       "median": round(statistics.median(costs), 6) if costs else 0,
                       "p95": round(percentile(costs, 0.95), 6)},
        "wall_stats": {"mean": round(statistics.mean(wall), 3) if wall else 0,
                       "median": round(statistics.median(wall), 3) if wall else 0,
                       "p95": round(percentile(wall, 0.95), 3)},
        "warnings": warnings,
    }


def paired_comparison(baseline_model: str, candidate_model: str, results: list[dict]) -> dict:
    baseline = {str(row["id"]): row for row in results if row["model"] == baseline_model}
    candidate = {str(row["id"]): row for row in results if row["model"] == candidate_model}
    ids = sorted(set(baseline) & set(candidate))
    improvements = [identifier for identifier in ids
                    if not baseline[identifier]["passed"] and candidate[identifier]["passed"]]
    regressions = [identifier for identifier in ids
                   if baseline[identifier]["passed"] and not candidate[identifier]["passed"]]

    def deltas(key: str) -> list[float]:
        return [float(candidate[identifier].get(key) or 0) - float(baseline[identifier].get(key) or 0)
                for identifier in ids]

    cost_delta, token_delta, wall_delta = deltas("cost"), deltas("tokens"), deltas("wall_s")
    return {
        "baseline": baseline_model, "candidate": candidate_model,
        "paired_items": len(ids), "missing_baseline": sorted(set(candidate) - set(baseline)),
        "missing_candidate": sorted(set(baseline) - set(candidate)),
        "quality": {"improved": improvements, "regressed": regressions,
                    "unchanged": len(ids) - len(improvements) - len(regressions),
                    "non_regressing": not regressions},
        "delta": {
            "cost_mean": round(statistics.mean(cost_delta), 6) if cost_delta else 0,
            "cost_median": round(statistics.median(cost_delta), 6) if cost_delta else 0,
            "tokens_mean": round(statistics.mean(token_delta), 3) if token_delta else 0,
            "wall_seconds_mean": round(statistics.mean(wall_delta), 3) if wall_delta else 0,
        },
    }


def recommendation(summaries: list[dict], comparisons: list[dict]) -> dict:
    if not summaries:
        return {"decision": "inconclusive", "model": None, "reason": "no model results"}
    minimum = min(summary["total"] for summary in summaries)
    if minimum < 5:
        return {"decision": "inconclusive", "model": None,
                "reason": "paired corpus is too small to change model policy",
                "needs_more_items": 5 - minimum}
    if len({summary["model"] for summary in summaries}) < 2 or not comparisons:
        return {"decision": "inconclusive", "model": None,
                "reason": "at least two models are required for a comparison"}
    best_passed = max(summary["passed"] for summary in summaries)
    if best_passed == 0:
        return {"decision": "inconclusive", "model": None,
                "reason": "no model produced a passing result"}
    quality_leaders = [summary for summary in summaries if summary["passed"] == best_passed]
    safe_models = {comparisons[0]["baseline"]} if comparisons else {summaries[0]["model"]}
    safe_models.update(item["candidate"] for item in comparisons if item["quality"]["non_regressing"])
    eligible = [summary for summary in quality_leaders if summary["model"] in safe_models]
    if not eligible:
        return {"decision": "inconclusive", "model": None,
                "reason": "quality leaders contain paired regressions"}
    selected = min(eligible, key=lambda item: (item["cost"], item["tokens"], item["wall_seconds"]))
    return {"decision": "observed_non_regressing_winner", "model": selected["model"],
            "reason": "highest observed pass count, no paired regressions, then lower cost/tokens/latency",
            "not_statistical_proof": True}


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
    if len(models) != len(set(models)):
        raise ValueError("model ids must be unique")
    items = load_items(args.inputs)
    if args.limit:
        items = items[:args.limit]
    if not items or not models:
        raise ValueError("need at least one input and one model")
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
    summaries = [summarize_model(model, [row for row in results if row["model"] == model])
                 for model in models]
    comparisons = [paired_comparison(models[0], model, results) for model in models[1:]]
    decision = recommendation(summaries, comparisons)
    lines = [f"# Model eval — {args.steps_file.name} · {len(items)} input(s)", "",
             "| model | pass | 95% pass interval | QA pass | avg judge | avg tokens | median / p95 cost | median / p95 wall |",
             "|---|---|---|---|---|---|---|---|"]
    for model in models:
        rs = [r for r in results if r["model"] == model]
        scores = [s for r in rs for s in r["judge_scores"]]
        summary = next(item for item in summaries if item["model"] == model)
        lines.append("| {m} | {p}/{n} | {lo:.2f}–{hi:.2f} | {q}/{qn} | {j} | {t:.0f} | ${cm:.4f} / ${cp:.4f} | {wm:.1f}s / {wp:.1f}s |".format(
            m=model.split("/")[-1], n=len(rs),
            p=sum(r["passed"] for r in rs),
            lo=summary["pass_interval_95"]["low"], hi=summary["pass_interval_95"]["high"],
            q=summary["qa"]["passed"], qn=summary["qa"]["scored"],
            j=f"{statistics.mean(scores):.1f}" if scores else "-",
            t=statistics.mean(r["tokens"] for r in rs),
            cm=summary["cost_stats"]["median"], cp=summary["cost_stats"]["p95"],
            wm=summary["wall_stats"]["median"], wp=summary["wall_stats"]["p95"]))
    lines += ["", f"Recommendation: **{decision['decision']}**" +
              (f" · `{decision['model']}`" if decision.get("model") else ""),
              f"Reason: {decision['reason']}"]
    report = "\n".join(lines) + "\n"
    report_path = eval_dir / "eval-report.md"
    report_path.write_text(report)
    if args.json:
        # ok mirrors the exit code: an eval that completed but had failing items
        # is a successful measurement with a non-zero exit, matching `run` and
        # `compare` conventions, so `piw eval ... && next` cannot proceed on red.
        ok = all(row["passed"] for row in results)
        print(json.dumps({"schema": "pi-graph.eval.v1", "ok": ok,
                          "eval_dir": str(eval_dir), "results_path": str(eval_dir / "eval.json"),
                          "report": str(report_path), "models": summaries,
                          "paired_comparisons": comparisons, "recommendation": decision},
                         separators=(",", ":")))
    else:
        print("\n" + report)
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        if "--json" in sys.argv[1:]:
            print(json.dumps({"schema": "pi-graph.eval.v1", "ok": False,
                              "error": {"code": "E_EVAL", "message": str(error)[:4000]}},
                             separators=(",", ":")))
        else:
            print(f"eval failed: {error}", file=sys.stderr)
        exit_code = 2
    sys.exit(exit_code)
