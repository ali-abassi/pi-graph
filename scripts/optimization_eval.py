#!/usr/bin/env python3
"""Default evaluator/parser pair for `piw optimize` experiments.

One frozen file serves both contract roles so a scaffolded experiment needs no
hand-written evaluation code:

  evaluator argv: python3 optimization-eval.py summarize --batch-json {batch_json}
                  -> prints raw metrics JSON on stdout
  parser argv:    python3 optimization-eval.py parse
                  -> reads that stdout on stdin, re-emits canonical metrics

The metrics it derives from the canonical child batch receipt:

  primary     = passed / total items (pass rate)
  coverage    = every corpus item scored, exactly once
  usage       = the batch's recorded tokens/cost/wall clock, never less
  uncertainty = [primary, primary] (interval method requires a custom evaluator)

This evaluator assumes `evaluation.hard_gates` is empty; the runner requires the
metrics gate set to equal the declared set, so declare a custom evaluator before
adding hard gates.
"""
from __future__ import annotations

import argparse
import json
import sys

SCHEMA = "pi-graph.optimization-metrics.v1"
REQUIRED = ("schema", "primary", "coverage", "gates", "usage", "uncertainty")


def summarize(batch_json_path: str) -> dict:
    try:
        with open(batch_json_path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"optimization-eval: cannot read batch receipt: {error}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(summary, dict):
        print("optimization-eval: batch receipt is not an object", file=sys.stderr)
        raise SystemExit(2)
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    not_run = int(summary.get("not_run") or 0)
    if total < 1 or summary.get("status") != "completed" or not_run != 0 \
            or int(summary.get("contract_complete") or 0) != total \
            or int(summary.get("all_steps_passed") or 0) != total:
        print("optimization-eval: canonical batch did not complete every item; "
              "refusing to score a partial run", file=sys.stderr)
        raise SystemExit(2)
    primary = passed / total
    return {
        "schema": SCHEMA,
        "primary": primary,
        "coverage": {"expected": total, "scored": total},
        "gates": {},
        "usage": {
            "tokens": max(0, int(summary.get("tokens") or 0)),
            "cost_usd": max(0.0, float(summary.get("cost") or 0.0)),
            "wall_seconds": max(0.0, float(summary.get("wall_s") or 0.0)),
        },
        "uncertainty": {"low": primary, "high": primary},
    }


def parse(stream: str) -> dict:
    try:
        metrics = json.loads(stream)
    except ValueError as error:
        print(f"optimization-eval: evaluator output is not JSON: {error}", file=sys.stderr)
        raise SystemExit(2)
    missing = [key for key in REQUIRED if key not in metrics]
    if missing:
        print(f"optimization-eval: evaluator output missing {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser(description="default optimization evaluator/parser")
    sub = ap.add_subparsers(dest="mode", required=True)
    summarize_parser = sub.add_parser("summarize", help="derive metrics from a batch.json receipt")
    summarize_parser.add_argument("--batch-json", required=True)
    sub.add_parser("parse", help="validate and re-emit metrics received on stdin")
    args = ap.parse_args()
    if args.mode == "summarize":
        metrics = summarize(args.batch_json)
    else:
        metrics = parse(sys.stdin.read())
    print(json.dumps(metrics, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
