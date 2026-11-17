import ctypes
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from rookline.engine import Run, create_run, git
from rookline.process import runner_active
from rookline.providers import ProviderUnavailable
from rookline.spec import validate
from rookline.store import Store


FIXTURE = str(Path(__file__).with_name("fixture.py").resolve())


def fixture(mode, *args):
    return [sys.executable, FIXTURE, mode, *args]


@unittest.skipUnless(os.name == "nt", "Windows owns the controller and test processes")
class WindowsRunTest(unittest.TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory(prefix="Rookline test ")
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.repo = self.root / "source checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Rookline Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "result.txt").write_text("original", encoding="utf-8")
        (self.repo / ".gitignore").write_text("receipt.txt\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "fixture baseline")
        self.head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "untouched.txt").write_text("in-progress user file", encoding="utf-8")
        self.store = Store(self.root / "private data")

    def configured(self, worker, gate="gate", **extra):
        data = {
            "title": "Test milestone", "objective": "Produce a fixture whose independent check passes",
            "repository": str(self.repo), "worker": {"kind": "command", "argv": worker},
            "gates": [{"name": "read fixture value", "argv": fixture(gate), "timeout": 20}],
            "agent_timeout": 40, "attempts": 3,
            "milestones": [{"title": "Prepare fixture", "task": "Set the value"}],
        }
        data.update(extra)
        return create_run(self.store, validate(data))

    def test_failed_check_is_sent_back_and_next_milestone_inherits_result(self):
        run = self.configured(fixture("implement"), reviewer={"kind": "command", "argv": fixture("review")},
                              milestones=[
            {"title": "Prepare fixture", "task": "Set the value", "paths": ["result.txt"]},
            {"title": "Deliver update", "task": "Use the verified value", "paths": ["delivery.txt"],
             "worker": {"kind": "command", "argv": fixture("continue")}},
        ])
        self.assertEqual(Run(self.store, run).run(), "done")
        events = self.store.events(run)
        kinds = [event["kind"] for event in events]
        self.assertIn("attempt_rejected", kinds)
        self.assertEqual(kinds.count("milestone_accepted"), 2)
        self.assertEqual(kinds.count("review_passed"), 2)
        self.assertEqual(git(self.repo, "show", f"rookline/{run}:delivery.txt"), "second milestone")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)
        self.assertEqual((self.repo / "result.txt").read_text(encoding="utf-8"), "original")
        self.assertTrue((self.repo / "untouched.txt").exists())

    def test_gate_cannot_promote_changed_snapshot(self):
        run = self.configured(fixture("implement"), gate="drift", attempts=1)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("changed tracked", self.store.get(run)["state"]["last_error"])
        self.assertEqual(git(self.repo, "branch", "--list", "rookline/*"), "")

    def test_expired_profile_blocks_without_repeated_attempts(self):
        run = self.configured(fixture("implement"))
        with patch("rookline.engine.invoke", side_effect=ProviderUnavailable("Selected profile needs login")) as worker:
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(worker.call_count, 1)
        state = self.store.get(run)["state"]
        self.assertEqual(state["blocked_from"], "ready")
        self.assertEqual(state["attempt"], 1)

    def test_second_gate_must_generate_new_evidence(self):
        run = self.configured(fixture("implement"), attempts=1, gates=[
            {"name": "make receipt", "argv": fixture("emit"), "artifacts": ["receipt.txt"]},
            {"name": "repeat receipt", "argv": fixture("no_evidence"), "artifacts": ["receipt.txt"]},
        ])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("fresh evidence", self.store.get(run)["state"]["last_error"])

    def test_killed_runner_releases_its_process_tree_and_can_resume(self):
        marker = self.root / "child.pid"
        run = self.configured(fixture("interrupt", str(marker)))
        command = [sys.executable, "-m", "rookline", "--home", str(self.store.root), "run", run]
        engine = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and engine.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(marker.exists())
            lock = self.store.directory(run) / "runner.lock"
            self.assertTrue(runner_active(lock))
            child = int(marker.read_text(encoding="utf-8"))
            engine.terminate()
            engine.wait(timeout=8)
            self.assertFalse(runner_active(lock))
            kernel = ctypes.WinDLL("kernel32")
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel.OpenProcess(0x100000, False, child)  # SYNCHRONIZE
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                finally:
                    kernel.CloseHandle(handle)
            self.assertEqual(Run(self.store, run).run(), "done")
            self.assertIn("interrupted_worker_recovered", [event["kind"] for event in self.store.events(run)])
        finally:
            if engine.poll() is None:
                engine.terminate()
                engine.wait(timeout=8)


if __name__ == "__main__":
    unittest.main()
