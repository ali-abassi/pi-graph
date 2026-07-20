from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "serve_workflow.py"


class WorkflowUiTests(unittest.TestCase):
    def test_studio_runs_the_canonical_engine_and_returns_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workflow = Path(raw)
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "ui-proof",
                "input": {"required": True, "description": "Name to preserve"},
                "steps": [{
                    "id": "copy",
                    "cmd": 'cat "$INPUT"',
                    "gate": 'test -s "$OUT"',
                }],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PI_WORKFLOWS_ROOTS": raw, "LOOPS_PORT": "1"}
            process = subprocess.Popen(
                [sys.executable, str(SERVER), str(steps), "--port", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                match = re.search(r"(http://127\.0\.0\.1:\d+)$", line)
                self.assertIsNotNone(match, line)
                base = match.group(1)
                with urllib.request.urlopen(base, timeout=5) as response:
                    page = response.read().decode()
                    self.assertIn("Pi Workflows Studio", page)
                    self.assertIn("ui-proof", page)
                    self.assertIn("Content-Security-Policy", response.headers)
                token = json.loads(re.search(
                    r'<script id="piw-boot" type="application/json">(.*?)</script>', page, re.S,
                ).group(1))["token"]

                request = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps({"content": "Ada"}).encode(),
                    headers={"Content-Type": "application/json", "X-Piw-Token": token},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    session = json.loads(response.read())["session"]

                payload = {}
                for _ in range(80):
                    with urllib.request.urlopen(f"{base}/api/status?session={session}&after=0", timeout=5) as response:
                        payload = json.loads(response.read())
                    if payload.get("done"):
                        break
                    time.sleep(0.05)
                self.assertTrue(payload.get("done"), payload)
                self.assertEqual(payload["exit"], 0, payload.get("error"))
                self.assertEqual(payload["output"], "Ada")
                self.assertTrue(payload["detail"]["run"]["ok"])
                self.assertIn("run_end", {event["t"] for event in payload["events"]})
                with urllib.request.urlopen(base, timeout=5) as response:
                    restored = response.read().decode()
                self.assertIn('"latest":{"detail"', restored)
                self.assertIn('"output":"Ada"', restored)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def test_studio_refuses_a_rebound_host_and_never_leaks_the_run_token(self) -> None:
        """Binding to 127.0.0.1 does not stop DNS rebinding.

        Once an attacker domain resolves to loopback their page is same-origin,
        so SOP and CSP no longer apply and `GET /` would hand out the token that
        authorizes `POST /api/run` — which spends money and runs shell steps.
        Only a Host check stops it.
        """
        with tempfile.TemporaryDirectory() as raw:
            workflow = Path(raw)
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "host-guard",
                "input": {"required": True, "description": "Name"},
                "steps": [{"id": "copy", "cmd": 'cat "$INPUT"', "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PI_WORKFLOWS_ROOTS": raw, "LOOPS_PORT": "1"}
            process = subprocess.Popen(
                [sys.executable, str(SERVER), str(steps), "--port", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                base = re.search(r"(http://127\.0\.0\.1:\d+)$", line).group(1)
                port = base.rsplit(":", 1)[1]

                for host in ("evil.example", f"evil.example:{port}", "attacker.test"):
                    request = urllib.request.Request(base, headers={"Host": host})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, 403, host)
                    self.assertNotIn("token", caught.exception.read().decode(), host)

                # A rebound origin must not reach the endpoint that spends money.
                run = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps({"content": "Ada"}).encode(),
                    headers={"Content-Type": "application/json",
                             "X-Piw-Token": "irrelevant", "Host": "evil.example"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(run, timeout=5)
                self.assertEqual(caught.exception.code, 403)

                # The legitimate loopback names still work and still boot.
                for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    request = urllib.request.Request(base, headers={"Host": host})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200, host)
                        self.assertIn("piw-boot", response.read().decode(), host)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
