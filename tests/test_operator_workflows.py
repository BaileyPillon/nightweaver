"""Windows regression tests for operator handoffs, browser evidence and matched comparisons."""

import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import test_windows as fixtures

from nightweaver.benchmark import create_suite
from nightweaver.benchmark_exports import compare_suites, export_junit
from nightweaver.browser import run_browser, validate_browser
from nightweaver.cli import main
from nightweaver.context import capture
from nightweaver.engine import Run, git, git_bytes, tree
from nightweaver.operations import (
    attention,
    audit_evidence,
    changes,
    follow_events,
    handoff,
)
from nightweaver.process import run_lock
from nightweaver.reports import metrics
from nightweaver.safety import redact


@unittest.skipUnless(os.name == "nt", "Windows runner")
class OperatorTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def test_handoff_changes_events_and_artifact_audit(self):
        run = self.configured(
            fixtures.fixture("good"),
            gates=[{"name": "receipt", "argv": fixtures.fixture("emit"), "artifacts": ["receipt.txt"]}],
        )
        self.assertEqual(attention(self.store)[0]["reason"], "awaiting_start")
        self.assertEqual(audit_evidence(self.store, run)["status"], "no_artifacts")
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(attention(self.store), [])
        summary = handoff(self.store, run)
        self.assertIsNone(summary["current"])
        self.assertEqual(summary["completed"][0]["verified_checks"], ["receipt"])
        (self.repo / "result.txt").write_text("private local edit")
        result = changes(self.store, run, 1, True)
        self.assertEqual(result["files"], ["result.txt"])
        self.assertIn("+ready", result["patch"])
        self.assertNotIn("private local edit", result["patch"])
        with self.assertRaises(ValueError):
            changes(self.store, run, 2)
        timeline = self.store.events(run, limit=None)
        self.assertEqual(list(follow_events(self.store, run, timeline[0]["seq"], 2)), timeline[1:])
        event = next(e for e in timeline if e["kind"] == "gate_finished")
        receipt = Path(event["details"]["artifacts"][0]["path"])
        self.assertEqual(audit_evidence(self.store, run)["status"], "verified")
        receipt.write_text("replaced")
        self.assertEqual(audit_evidence(self.store, run)["files"][0]["status"], "changed")
        receipt.unlink()
        self.assertEqual(audit_evidence(self.store, run)["files"][0]["status"], "unavailable")

    def test_attention_detects_dispatcher_and_blocked_dependency(self):
        dep = self.configured(fixtures.fixture("good"), enqueue=True)
        child = self.configured(fixtures.fixture("good"), enqueue=True, after=[dep])
        self.assertEqual({r["reason"] for r in attention(self.store)}, {"dispatcher_stopped"})
        with run_lock(self.store.root / "dispatcher.lock"):
            self.assertEqual(attention(self.store), [])
        self.store.transition(
            dep, "blocked", {"issue": "failed check"}, phase="blocked", last_error="failed check"
        )
        row = next(r for r in attention(self.store) if r["run_id"] == child)
        self.assertEqual(row["next_command"], ["handoff", dep])
        self.store.pause(child, True)
        self.assertEqual(next(r for r in attention(self.store) if r["run_id"] == child)["reason"], "paused")

    def test_context_digest_is_exact_git_blob_and_scope_checks_both_rename_paths(self):
        (self.repo / "notes.txt").write_bytes(b"one\r\ntwo\r\n\r\n")
        git(self.repo, "add", "notes.txt")
        git(self.repo, "commit", "-qm", "context")
        packed = capture({"repository": str(self.repo), "context_files": ["notes.txt"]}, "HEAD")["files"][0]
        blob = git_bytes(self.repo, "cat-file", "blob", "HEAD:notes.txt")
        self.assertEqual(packed["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(packed["content"].encode(), blob)
        git(self.repo, "mv", "notes.txt", "moved.txt")
        with self.assertRaisesRegex(ValueError, "protected"):
            tree(self.repo, git(self.repo, "rev-parse", "HEAD"), [], ["notes.txt"])

    def test_blocked_metrics_ignore_later_notes_and_count_breaker_as_rejected(self):
        run = self.configured(fixtures.fixture("bad"))
        self.store.transition(run, "run_started")
        self.store.transition(run, "repeated_failure_stopped", {"count": 3}, phase="blocked")
        first = metrics(self.store, run)
        self.store.feedback(run, "Investigate tomorrow")
        with self.store.connect() as db:
            db.execute("UPDATE events SET time='2099-01-01T00:00:00+00:00' WHERE kind='operator_feedback'")
        self.assertEqual(first["outcome"], "rejected")
        self.assertEqual(metrics(self.store, run)["elapsed_seconds"], first["elapsed_seconds"])
        safe = redact({"refresh_token": "private", "note": "https://example.invalid/?code=private&view=x"})
        self.assertNotIn("private", str(safe))

    def test_offline_browser_checkpoints_and_actionable_failure(self):
        if find_spec("playwright") is None:
            self.skipTest("Playwright optional")
        (self.repo / "page.html").write_text(
            '<button id="open" onclick="this.hidden=true">Open</button><button disabled>Later</button>'
        )
        steps = [
            {"action": "enabled", "selector": "#open"},
            {"action": "press", "selector": "#open", "value": "Tab"},
            {"action": "disabled", "selector": "button:last-child"},
            {"action": "checkpoint", "value": "before"},
            {"action": "click", "selector": "#open"},
            {"action": "hidden", "selector": "#open"},
            {"action": "checkpoint", "value": "after"},
        ]
        config = {
            "page": "page.html",
            "offline": True,
            "timeout_ms": 400,
            "steps": steps,
            "viewports": [{"name": "phone", "width": 390, "height": 844}],
        }
        output = self.root / "captures"
        result = run_browser(config, self.repo, output)
        self.assertTrue(result["passed"], result)
        for check in result["viewports"][0]["checkpoints"]:
            self.assertTrue((output / check["file"]).is_file())
        config["steps"] = [*steps, {"action": "focused", "selector": "#open"}]
        failure = run_browser(config, self.repo, self.root / "failed")["viewports"][0]
        self.assertEqual(failure["failed_at"], {"index": 8, "action": "focused", "selector": "#open"})
        self.assertFalse(failure["passed"])
        with self.assertRaises(ValueError):
            validate_browser({**config, "steps": [steps[3], steps[3]]})
        with self.assertRaises(ValueError):
            validate_browser({"url": "http://localhost:80", "offline": True})
        (self.repo / "page.html").write_text('<img src="https://example.invalid/missing.jpg">')
        offline = run_browser({**config, "steps": []}, self.repo, self.root / "offline")
        self.assertFalse(offline["passed"])
        self.assertIn("External request during offline playback", offline["viewports"][0]["errors"])

    def test_real_suite_junit_and_comparison_exclude_changed_and_legacy_contracts(self):
        plan = {
            "title": "fixture",
            "objective": "Update result",
            "repository": str(self.repo),
            "worker": {"kind": "command", "argv": fixtures.fixture("good")},
            "budgets": {"max_seconds": 40},
            "milestones": [{"title": "first", "task": "implement"}],
            "gates": [{"name": "check", "argv": fixtures.fixture("gate")}],
        }
        plan_file, suite_file = self.root / "plan.json", self.root / "suite.json"
        plan_file.write_text(json.dumps(plan))
        suite_file.write_text(
            json.dumps(
                {
                    "name": "local trial",
                    "repetitions": 1,
                    "tasks": [{"name": "task", "plan": "plan.json"}],
                    "contenders": [{"name": "Fixture", "worker": plan["worker"]}],
                }
            )
        )

        def make():
            created = create_suite(self.store, suite_file)
            manifest = Path(created["manifest"])
            return created["benchmark_id"], manifest, json.loads(manifest.read_text())["runs"][0]["run_id"]

        before, manifest, r1 = make()
        after, _, r2 = make()
        self.store.transition(r1, "blocked", {}, phase="blocked")
        self.assertEqual(Run(self.store, r2).run(), "done")
        self.assertEqual(compare_suites(self.store, before, after)["counts"], {"improvement": 1})
        junit = self.root / "result.xml"
        export_junit(self.store, before, junit)
        self.assertIsNotNone(ET.parse(junit).find(".//failure"))
        self.store.transition(r1, "provider_unavailable", {}, phase="blocked", last_error="denied\x03<")
        export_junit(self.store, before, junit)
        self.assertIsNotNone(ET.parse(junit).find(".//error"))
        self.assertEqual(compare_suites(self.store, before, after)["counts"], {"inconclusive": 1})
        plan["objective"] = "Different work"
        plan_file.write_text(json.dumps(plan))
        changed, _, _ = make()
        self.assertEqual(compare_suites(self.store, after, changed)["counts"], {"changed_inputs": 1})
        export_junit(self.store, changed, junit)
        self.assertIsNotNone(ET.parse(junit).find(".//skipped"))
        legacy = json.loads(manifest.read_text())
        del legacy["runs"][0]["contract_sha256"]
        manifest.write_text(json.dumps(legacy))
        self.assertEqual(compare_suites(self.store, before, after)["counts"], {"unknown_contract": 1})

    def test_cli_outputs(self):
        rid = self.configured(fixtures.fixture("good"))
        for args, code in [
            (["handoff", rid], 0),
            (["attention"], 0),
            (["changes", rid], 0),
            (["audit-evidence", rid], 3),
        ]:
            out = io.StringIO()
            with patch.dict(os.environ), redirect_stdout(out):
                self.assertEqual(main(["--home", str(self.store.root), *args]), code)
            self.assertTrue(json.loads(out.getvalue()))

    def test_large_journey_after_interruption_ignores_stale_captures(self):
        if find_spec("playwright") is None:
            self.skipTest("Playwright optional")
        (self.repo / "page.html").write_text("<button disabled>Waiting</button>")
        git(self.repo, "add", "page.html")
        git(self.repo, "commit", "-qm", "Add local page")
        browser = {
            "page": "page.html",
            "offline": True,
            "viewports": [{"name": "phone", "width": 390, "height": 844}],
            "steps": [
                {"action": "hidden", "selector": "#" + "a" * 35500},
                {"action": "disabled", "selector": "button"},
                {"action": "checkpoint", "value": "waiting"},
            ],
        }
        run = self.configured(
            fixtures.fixture("good"),
            gates=[{"name": "offline", "kind": "browser", "browser": browser}],
            attempts=1,
        )
        stale = self.store.directory(run) / "evidence/01/01/gate-01/browser/stale.png"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"previous invocation")
        self.assertEqual(Run(self.store, run).run(), "done")
        receipt = audit_evidence(self.store, run)
        self.assertEqual(receipt["status"], "verified")
        names = [f["name"] for f in receipt["files"]]
        self.assertIn("checkpoint.phone.waiting.png", names)
        self.assertNotIn("stale.png", names)
        self.assertEqual(stale.read_bytes(), b"previous invocation")

    @unittest.skipUnless(find_spec("playwright"), "Playwright optional")
    def test_failed_focus_step_reaches_next_worker_and_autonomously_repairs(self):
        browser = {
            "page": "index.html",
            "offline": True,
            "timeout_ms": 500,
            "viewports": [{"name": "mobile", "width": 390, "height": 844}],
            "steps": [{"action": "click", "selector": "#open"}, {"action": "focused", "selector": "#next"}],
        }
        run = self.configured(
            fixtures.fixture("focus_repair"),
            attempts=2,
            gates=[{"name": "Focus transfer", "kind": "browser", "browser": browser}],
        )
        self.assertEqual(Run(self.store, run).run(), "done")
        events = self.store.events(run, limit=None)
        gates = [e["details"] for e in events if e["kind"] == "gate_finished"]
        self.assertEqual([g["exit"] for g in gates], [1, 0])
        self.assertEqual(gates[0]["browser_failures"][0]["stage"]["selector"], "#next")
        self.assertEqual(metrics(self.store, run)["interventions"], 0)
