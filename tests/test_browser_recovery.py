"""Regression tests for browser runtime failure, complete observations, and OneShot admission."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_windows as fixtures
from nightweaver.benchmark import scored
from nightweaver.checks import read_browser_evidence
from nightweaver.cli import main
from nightweaver.engine import Run
from nightweaver.reports import metrics
from nightweaver.spec import gate_spec


class BrowserReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.config = gate_spec({"name": "gallery", "kind": "browser", "browser": {
            "page": "index.html", "steps": [{"action": "checkpoint", "value": "gallery"}]
        }})["browser"]
        self.views = []
        for v in self.config["viewports"]:
            name = v["name"]
            self.views.append({"viewport": name, "passed": True, "completed_steps": 1, "errors": []})
            for filename in (f"{name}.png", f"{name}.trace.zip", f"checkpoint.{name}.gallery.png"):
                (self.out / filename).write_bytes(b"fixture")

    def read(self, views, *, code=0, passed=True):
        (self.out / "browser.json").write_text(json.dumps({"passed": passed, "viewports": views}))
        receipt = {"exit": code, "artifacts": []}
        read_browser_evidence(self.config, self.out, receipt, 65536)
        return receipt

    def test_records_each_viewports_actual_coverage(self):
        receipt = self.read(self.views)
        self.assertEqual([(v["viewport"], v["completed_steps"], v["planned_steps"])
                          for v in receipt["browser_coverage"]], [("desktop", 1, 1), ("mobile", 1, 1)])

    def test_incomplete_or_contradictory_receipts_cannot_pass(self):
        desktop, mobile = self.views
        for label, views in {
            "missing phone": [desktop], "duplicate desktop": [desktop, desktop],
            "unknown viewport": [desktop, {**mobile, "viewport": "other"}],
            "incomplete journey": [desktop, {**mobile, "completed_steps": 0}],
            "boolean step count": [desktop, {**mobile, "completed_steps": True}],
            "uncaught error": [desktop, {**mobile, "errors": ["page error"]}],
            "failed viewport": [desktop, {**mobile, "passed": False}],
        }.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.read(views)
        with self.assertRaisesRegex(ValueError, "exit"):
            self.read(self.views, code=1)

    def test_missing_checkpoint_is_unusable_only_after_the_step_completes(self):
        (self.out / "checkpoint.mobile.gallery.png").unlink()
        with self.assertRaisesRegex(ValueError, "checkpoint.mobile.gallery.png"):
            self.read(self.views)
        self.views[1].update(passed=False, completed_steps=0, errors=["selector was hidden"])
        failures = self.read(self.views, passed=False, code=1)["browser_failures"]
        self.assertEqual(failures[0]["viewport"], "mobile")

    def test_empty_trace_cannot_support_a_passing_viewport(self):
        (self.out / "desktop.trace.zip").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "desktop.trace.zip"):
            self.read(self.views)


@unittest.skipUnless(os.name == "nt", "Windows-native browser runtime")
class BrowserRecoveryTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def test_missing_runtime_resumes_acceptance_without_worker_retry(self):
        gate = {"name": "gallery", "kind": "browser", "browser": {"page": "result.txt", "offline": True},
                "stability_runs": 2}
        rid = self.configured(fixtures.fixture("good"), gates=[gate], budgets={"max_calls": 1})
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.root / "empty-cache")}):
            self.assertEqual(Run(self.store, rid).run(), "blocked")
        m = metrics(self.store, rid)
        self.assertEqual((m["outcome"], m["failed_attempts"], m["provider_calls"]), ("execution_blocked", 0, 1))
        self.assertFalse(scored(m))
        receipt, = [e["details"] for e in self.store.events(rid) if e["kind"] == "gate_finished"]
        self.assertEqual(receipt["browser_issue"], "browser_environment")
        self.assertIn("Chromium", receipt["artifact_error"])
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_missing_candidate_page_reaches_repair_with_named_path(self):
        gate = {"name": "gallery", "kind": "browser", "browser": {"page": "missing-gallery.html"}}
        rid = self.configured(fixtures.fixture("good"), gates=[gate], attempts=1)
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        rejection, = [e["details"] for e in self.store.events(rid) if e["kind"] == "attempt_rejected"]
        self.assertEqual(rejection["phase"], "check")
        self.assertIn("missing-gallery.html", rejection["issue"])
        self.assertEqual(metrics(self.store, rid)["outcome"], "rejected")

    def test_missing_visual_dependency_is_a_runtime_failure(self):
        from nightweaver.browser import BrowserRuntimeUnavailable, run_browser
        config = {"page": "result.txt", "offline": True, "steps": [
            {"action": "visual_match", "selector": "body", "value": {"baseline": "ref.png", "sha256": "0" * 64}}
        ]}
        with patch.dict(sys.modules, {"PIL": None}), self.assertRaisesRegex(BrowserRuntimeUnavailable, "Pillow"):
            run_browser(config, self.repo, self.root / "browser")

    def test_immediate_oneshot_checks_final_browser_before_planner(self):
        plan = self.root / "template.json"
        plan.write_text(json.dumps({"repository": str(self.repo), "title": "Gallery", "worker": {"kind": "claude"},
                                    "final_gates": [{"name": "gallery", "kind": "browser",
                                                     "browser": {"page": "missing-gallery.html"}}]}))
        argv = ["--home", str(self.store.root), "goal", str(plan), "Repair gallery"]
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(self.root / "empty-cache")}), \
             patch("nightweaver.cli.invoke", return_value=(0, None, '{"milestones":[{"title":"Fix","task":"Repair"}]}')) as invoke:
            self.assertNotEqual(main([*argv, "--start"]), 0)
            invoke.assert_not_called()
            self.assertEqual(self.store.runs(), [])
            self.assertEqual(main(argv), 0)
            invoke.assert_called_once()
