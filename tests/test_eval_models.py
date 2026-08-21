from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from eval_models import paired_comparison, recommendation, summarize_model, wilson_interval  # noqa: E402


def row(model: str, identifier: str, passed: bool, *, cost: float = 0.1,
        tokens: int = 10, wall: float = 1.0) -> dict:
    return {"model": model, "id": identifier, "passed": passed, "qa": None,
            "judge_scores": [], "cost": cost, "tokens": tokens, "wall_s": wall}


class EvalEvidenceTests(unittest.TestCase):
    def test_wilson_interval_is_bounded_and_not_fake_certainty(self) -> None:
        self.assertEqual(wilson_interval(0, 0), {"low": 0.0, "high": 0.0})
        perfect_small = wilson_interval(2, 2)
        self.assertLess(perfect_small["low"], 1)
        self.assertEqual(perfect_small["high"], 1)

    def test_summary_warns_on_small_corpus_and_reports_percentiles(self) -> None:
        summary = summarize_model("a", [row("a", "1", True, cost=0.1),
                                        row("a", "2", False, cost=0.3)])
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertEqual(summary["cost"], 0.4)
        self.assertEqual(summary["cost_stats"]["median"], 0.2)
        self.assertEqual(summary["warnings"][0]["code"], "small_corpus")

    def test_paired_comparison_names_exact_regressions_and_improvements(self) -> None:
        results = [
            row("base", "a", True), row("base", "b", False), row("base", "c", True),
            row("candidate", "a", False), row("candidate", "b", True), row("candidate", "c", True),
        ]
        paired = paired_comparison("base", "candidate", results)
        self.assertEqual(paired["quality"]["regressed"], ["a"])
        self.assertEqual(paired["quality"]["improved"], ["b"])
        self.assertFalse(paired["quality"]["non_regressing"])

    def test_recommendation_refuses_small_corpus_and_regressing_leader(self) -> None:
        small = [summarize_model("base", [row("base", "a", True)]),
                 summarize_model("candidate", [row("candidate", "a", True)])]
        self.assertEqual(recommendation(small, [paired_comparison(
            "base", "candidate", [row("base", "a", True), row("candidate", "a", True)])
        ])["decision"], "inconclusive")

        baseline_rows = [row("base", str(index), True) for index in range(5)]
        candidate_rows = [row("candidate", str(index), index != 0) for index in range(5)]
        summaries = [summarize_model("base", baseline_rows), summarize_model("candidate", candidate_rows)]
        compared = [paired_comparison("base", "candidate", baseline_rows + candidate_rows)]
        chosen = recommendation(summaries, compared)
        self.assertEqual(chosen["model"], "base")

    def test_recommendation_requires_a_real_comparison_and_a_passing_model(self) -> None:
        single = summarize_model("only", [row("only", str(index), True) for index in range(5)])
        self.assertEqual(recommendation([single], [])["decision"], "inconclusive")
        failed = []
        for model in ("base", "candidate"):
            failed.extend(row(model, str(index), False) for index in range(5))
        summaries = [summarize_model(model, [item for item in failed if item["model"] == model])
                     for model in ("base", "candidate")]
        compared = [paired_comparison("base", "candidate", failed)]
        self.assertEqual(recommendation(summaries, compared)["decision"], "inconclusive")
        duplicate = [summarize_model("same", [row("same", str(index), True) for index in range(5)])] * 2
        self.assertEqual(recommendation(duplicate, [{"baseline": "same"}])["decision"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
