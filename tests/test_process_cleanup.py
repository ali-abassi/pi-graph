"""Real local subprocesses exercise timeout cleanup without model/API calls."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_steps


class ProcessCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.child_pid = self.root / "child.pid"
        self.addCleanup(self.kill_fixture_child)

    def kill_fixture_child(self) -> None:
        if not self.child_pid.exists():
            return
        try:
            os.kill(int(self.child_pid.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass

    def child_command(self, *, leader_exits: bool, ignore_term: bool = False,
                      close_output: bool = False) -> list[str]:
        child = (
            "import os,signal,time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, {int(signal.SIG_IGN if ignore_term else signal.SIG_DFL)}); "
            f"Path({str(self.child_pid)!r}).write_text(str(os.getpid())); "
            "print('child diagnostic', file=__import__('sys').stderr, flush=True); "
            + ("os.close(1); os.close(2); " if close_output else "") +
            "time.sleep(30)"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            f"time.sleep({0 if leader_exits else 30})"
        )
        return [sys.executable, "-c", parent]

    def assert_child_stopped(self) -> None:
        pid = int(self.child_pid.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
            ).stdout.strip()
            if not status or status.startswith("Z"):
                return
            time.sleep(0.02)
        self.fail(f"descendant {pid} survived timeout cleanup")

    def test_shell_timeout_cleans_group_after_leader_exits(self) -> None:
        command = shlex.join(self.child_command(leader_exits=True))
        with self.assertRaises(run_steps.ShellTimeout) as caught:
            run_steps.run_shell(command, self.root / "out", self.root, "test", self.root, timeout=1)
        self.assert_child_stopped()
        self.assertIn("child diagnostic", caught.exception.stderr)

    def test_shell_timeout_kills_term_resistant_descendant(self) -> None:
        command = shlex.join(self.child_command(leader_exits=True, ignore_term=True))
        with self.assertRaises(run_steps.ShellTimeout):
            run_steps.run_shell(command, self.root / "out", self.root, "test", self.root, timeout=1)
        self.assert_child_stopped()

    def test_model_timeout_cleans_tools_and_returns_retryable_failure(self) -> None:
        command = self.child_command(leader_exits=False)
        with patch.object(run_steps, "PI_BASE", command):
            result = run_steps.call_pi({"timeout": 1}, {"model": "fixture/model"}, "input", self.root)
        self.assertFalse(result[2])
        self.assertEqual(result[4], 124)
        self.assert_child_stopped()

    def test_timeout_kills_descendant_even_when_capture_pipes_close(self) -> None:
        command = shlex.join(self.child_command(
            leader_exits=False, ignore_term=True, close_output=True,
        ))
        with self.assertRaises(run_steps.ShellTimeout):
            run_steps.run_shell(command, self.root / "out", self.root, "test", self.root, timeout=1)
        self.assert_child_stopped()

    def test_shell_preserves_output_environment_and_exit_status(self) -> None:
        result = run_steps.run_shell(
            'printf "%s" "$STEP"; printf diagnostic >&2; exit 7',
            self.root / "out", self.root, "example", self.root,
        )
        self.assertEqual((result.stdout, result.stderr, result.returncode), ("example", "diagnostic", 7))
        self.assertEqual(run_steps.ACTIVE_GROUPS, set())

    def wait_for_child(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.child_pid.exists():
                return
            time.sleep(0.02)
        self.fail("fixture child did not start")

    def test_cancellation_reaches_model_tool_processes(self) -> None:
        script = (
            f"import sys,signal; sys.path.insert(0, {str(Path(run_steps.__file__).parent)!r}); "
            "import run_steps; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, run_steps._on_terminate); "
            f"run_steps.PI_BASE = {self.child_command(leader_exits=False, ignore_term=True)!r}; "
            f"run_steps.call_pi({{}}, {{'model':'fixture/model'}}, 'input', Path({str(self.root)!r}))"
        )
        with subprocess.Popen([sys.executable, "-c", script], start_new_session=True) as process:
            try:
                self.wait_for_child()
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 143)
                self.assert_child_stopped()
            finally:
                if process.poll() is None:
                    process.kill()
