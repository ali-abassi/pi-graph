from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "scripts" / "run_example_suite.py"


class PublicExampleTests(unittest.TestCase):
    def test_all_public_examples_validate_and_pin_live_calls_to_luna_medium(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "evidence"
            result = subprocess.run(
                [sys.executable, str(SUITE), "--validate-only", "--out", str(out)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["validated"], 12)
            self.assertEqual(report["model"], "openai-codex/gpt-5.6-luna")
            self.assertEqual(report["thinking"], "medium")
            self.assertEqual(report["liveRuns"], 0)


if __name__ == "__main__":
    unittest.main()
