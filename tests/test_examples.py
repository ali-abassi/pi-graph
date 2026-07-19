from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "scripts" / "run_example_suite.py"
sys.path.insert(0, str(ROOT / "scripts"))

from run_example_suite import copy_case  # noqa: E402


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
            self.assertEqual(report["validated"], 15)
            self.assertEqual(report["model"], "openai-codex/gpt-5.6-luna")
            self.assertEqual(report["thinking"], "medium")
            self.assertEqual(report["liveRuns"], 0)

    def test_suite_copy_excludes_local_run_and_cache_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "steps.yaml").write_text("version: 1\n", encoding="utf-8")
            for ignored in ("runs", "cache", ".artifacts", "__pycache__"):
                path = source / ignored
                path.mkdir()
                (path / "local-state").write_text("ignored", encoding="utf-8")
            target = root / "target"
            copy_case(source, target)
            self.assertTrue((target / "steps.yaml").is_file())
            self.assertFalse(any((target / name).exists() for name in ("runs", "cache", ".artifacts", "__pycache__")))


if __name__ == "__main__":
    unittest.main()
