import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from nightweaver.benchmark import create_suite, paired_outcomes, read_suite, suite_report
from nightweaver.browser import run_browser, validate_browser
from nightweaver.engine import Run, git
from nightweaver.server import managed_server
from nightweaver.reports import metrics
from nightweaver.providers import ProviderUnavailable
from nightweaver.process import PausedRun
from nightweaver.store import Store
import test_windows as fixtures
from test_windows import fixture


@unittest.skipUnless(os.name == "nt", "Native Windows server ownership")
class ServerTest(unittest.TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory(prefix="Nightweaver server ")
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.marker = self.root / "processes.json"
        (self.root / "index.html").write_text('<link rel="icon" href="data:,"><h1>Owned candidate</h1>')

    def config(self, mode="serve", **extra):
        return {"argv": fixture(mode, str(self.marker), "{port}"), "startup_timeout": 5, **extra}

    def assert_stopped(self):
        data = json.loads(self.marker.read_text())
        kernel = ctypes.WinDLL("kernel32")
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        for pid in (data["pid"], data["child"]):
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0, f"Leaked process {pid}")
                finally:
                    kernel.CloseHandle(handle)

    def test_candidate_is_served_and_descendants_stop_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with managed_server(self.config(), self.root, self.root / "evidence") as (url, check):
                        with urlopen(url, timeout=3) as response:
                            self.assertIn("Owned candidate", response.read().decode())
                        check()
                        if fail:
                            raise RuntimeError("journey failed")
                except RuntimeError as error:
                    self.assertEqual(str(error), "journey failed")
                self.assert_stopped()

    def test_launch_failure_and_readiness_timeout_have_actionable_evidence(self):
        with self.assertRaisesRegex(ValueError, "fixture server could not start"):
            with managed_server(self.config("serve_fail"), self.root, self.root / "failed"):
                self.fail("failed server cannot be ready")
        with self.assertRaisesRegex(ValueError, "readiness timed out"):
            with managed_server(self.config("serve_redirect", startup_timeout=2), self.root, self.root / "redirect"):
                self.fail("redirect cannot establish readiness")
        self.assert_stopped()

    def test_killed_browser_parent_releases_owned_server_tree(self):
        script = self.root / "parent.py"
        script.write_text('from nightweaver.server import managed_server\nfrom pathlib import Path\nimport time\n'
                          f'with managed_server({self.config()!r}, Path({str(self.root)!r}), Path({str(self.root / "hard-kill")!r})):\n'
                          '    time.sleep(120)\n')
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        child = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 15
            while not self.marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(self.marker.exists(), "Server did not start")
            child.terminate()
            child.wait(timeout=5)
            self.assert_stopped()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Install browser extra")
    def test_actual_browser_uses_owned_app_and_preserves_evidence(self):
        config = {"server": self.config(), "heading": "Owned candidate",
                  "viewports": [{"name": "desktop", "width": 1200, "height": 800}]}
        if os.environ.get("NIGHTWEAVER_TEST_BROWSER_CHANNEL"):
            config["channel"] = os.environ["NIGHTWEAVER_TEST_BROWSER_CHANNEL"]
        evidence = self.root / "browser-evidence"
        result = run_browser(config, self.root, evidence)
        self.assertTrue(result["passed"], result)
        self.assertTrue((evidence / "desktop.png").is_file())
        self.assertTrue((evidence / "server.log").is_file())
        self.assert_stopped()


@unittest.skipUnless(os.name == "nt", "Windows benchmark execution")
class ComparisonTest(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Install browser extra")
    def test_owned_app_failure_is_repaired_inside_the_run_loop(self):
        marker = self.root / "owned-app-processes.json"
        config = {"server": {"argv": fixture("serve", str(marker), "{port}")}, "heading": "Atlas fixture"}
        if os.environ.get("NIGHTWEAVER_TEST_BROWSER_CHANNEL"):
            config["channel"] = os.environ["NIGHTWEAVER_TEST_BROWSER_CHANNEL"]
        run = self.configured(fixture("browser_build"), gates=[{
            "kind": "browser", "name": "Owned app layout", "browser": config, "timeout": 180}])
        self.assertEqual(Run(self.store, run).run(), "done", self.store.get(run)["state"].get("last_error"))
        gates = [event["details"] for event in self.store.events(run, limit=None) if event["kind"] == "gate_finished"]
        self.assertEqual([gate["exit"] for gate in gates], [1, 0])
        self.assertTrue(all(len(gate["artifacts"]) == 6 for gate in gates))
        self.assertTrue(all(any(item["name"] == "server.log" for item in gate["artifacts"]) for gate in gates))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)

    def make_suite(self):
        plan = {"repository": str(self.repo), "title": "Full task", "objective": "Prepare and deliver",
                "worker": {"kind": "codex"}, "budgets": {"max_seconds": 120},
                "gates": [{"name": "value", "argv": fixture("gate")}],
                "milestones": [{"title": "Prepare", "task": "Write ready to result.txt", "paths": ["result.txt"]},
                               {"title": "Deliver", "task": "Add delivery.txt with final handoff", "paths": ["delivery.txt"]}],
                "advisory_ollama": {"model": "unused"}}
        (self.root / "plan.json").write_text(json.dumps(plan))
        suite = {"name": "Full task comparison", "repetitions": 1,
                 "tasks": [{"name": "Full delivery", "plan": "plan.json"}],
                 "contenders": [{"name": "Codex standalone", "mode": "standalone", "worker": {"kind": "codex"}},
                                {"name": "External", "mode": "external", "worker": {"kind": "command", "argv": fixture("bad")}}]}
        path = self.root / "suite.json"
        path.write_text(json.dumps(suite))
        return create_suite(self.store, path)["benchmark_id"]

    def test_standalone_preserves_full_brief_and_never_receives_repair_call(self):
        suite = read_suite(self.store, self.make_suite())
        item = next(row for row in suite["runs"] if row["mode"] == "standalone")
        plan = self.store.get(item["run_id"])["spec"]
        self.assertEqual(len(plan["milestones"]), 1)
        self.assertEqual(plan["milestones"][0]["paths"], ["result.txt", "delivery.txt"])
        self.assertIn("Add delivery.txt with final handoff", plan["milestones"][0]["task"])
        self.assertEqual(plan["budgets"]["max_calls"], 1)
        self.assertNotIn("advisory_ollama", plan)
        def fail(_config, _role, cwd, _log, prompt, *_args, **_kwargs):
            self.assertIn("Write ready to result.txt", prompt)
            self.assertIn("Add delivery.txt with final handoff", prompt)
            (cwd / "result.txt").write_text("not accepted")
            return 0, None, "Finished"
        with patch("nightweaver.engine.invoke", side_effect=fail) as invoke:
            self.assertEqual(Run(self.store, item["run_id"]).run(), "blocked")
            self.assertEqual(Run(self.store, item["run_id"]).run(retry=True), "blocked")
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)

    def test_reports_distinguish_unfinished_from_failed_and_match_pairs(self):
        suite_id = self.make_suite()
        manifest = read_suite(self.store, suite_id)
        report = suite_report(self.store, suite_id)
        self.assertTrue(all(row["finished_acceptance_rate"] is None and row["unfinished"] == 1 for row in report["summaries"]))
        self.assertEqual(report["paired_outcomes"][0]["unfinished_pairs"], 1)
        external = next(row for row in manifest["runs"] if row["mode"] == "external")
        Run(self.store, external["run_id"]).run()
        report = suite_report(self.store, suite_id, self.root / "comparison.html")
        external_summary = next(row for row in report["summaries"] if row["contender"] == "External")
        self.assertEqual(external_summary["blocked"], 1)
        self.assertEqual(external_summary["unfinished"], 0)
        self.assertEqual(external_summary["finished_acceptance_rate"], 0)
        self.assertEqual(len(report["per_task"]), 1)
        rows = [{"task": "a", "repeat": 1, "baseline": "same", "contract_sha256": "checks", "phase": "done", "accepted": True, "contender": "a"},
                {"task": "a", "repeat": 1, "baseline": "same", "contract_sha256": "checks", "phase": "blocked", "accepted": False, "contender": "b"}]
        self.assertEqual(paired_outcomes(rows)[0]["left_only_accepted"], 1)
        rows[1]["outcome"] = "provider_blocked"
        self.assertEqual(paired_outcomes(rows)[0]["inconclusive_pairs"], 1)
        self.assertEqual(paired_outcomes(rows)[0]["left_only_accepted"], 0)
        rows[1]["baseline"] = "different"
        self.assertEqual(paired_outcomes(rows)[0]["unfinished_pairs"], 2)

    def test_account_blockers_are_not_presented_as_model_rejections(self):
        suite_id = self.make_suite()
        row = next(row for row in read_suite(self.store, suite_id)["runs"] if row["mode"] == "standalone")
        with patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("Authentication expired", "authentication")):
            self.assertEqual(Run(self.store, row["run_id"]).run(), "blocked")
        result = metrics(self.store, row["run_id"])
        self.assertEqual(result["outcome"], "provider_blocked")
        self.assertIn("Authentication expired", result["block_reason"])

    def test_interrupted_single_invocation_is_inconclusive_and_not_relaunched(self):
        suite_id = self.make_suite()
        row = next(row for row in read_suite(self.store, suite_id)["runs"] if row["mode"] == "standalone")
        with patch("nightweaver.engine.invoke", side_effect=PausedRun) as invoke:
            self.assertEqual(Run(self.store, row["run_id"]).run(), "paused")
            self.assertEqual(Run(self.store, row["run_id"]).run(), "blocked")
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(metrics(self.store, row["run_id"])["outcome"], "interrupted")


class ServerValidationTest(unittest.TestCase):
    def test_server_paths_launchers_and_wait_modes_are_validated(self):
        for config in ({"server": {"argv": ["npm.cmd", "start"]}},
                       {"server": {"argv": ["node", "app.js"], "path": "//example.com"}},
                       {"server": {"argv": ["node", "app.js"], "ready_path": "/\\example.com"}},
                       {"page": "index.html", "server": {"argv": ["node", "app.js"]}},
                       {"page": "index.html", "wait_until": "never"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_browser(config)
