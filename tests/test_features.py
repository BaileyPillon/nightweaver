import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nightweaver.benchmark import create_suite, read_suite, suite_report
from nightweaver.cli import dispatch
from nightweaver.context import brief
from nightweaver.engine import Run, fork_run, git
from nightweaver.intake import issue_plan
from nightweaver.process import execute
from nightweaver.providers import adapter, metadata, ProviderUnavailable, usage, json_response
from nightweaver.reports import metrics, run_report
from nightweaver.safety import atomic_write, redact
from nightweaver.spec import validate
from nightweaver.store import Store
import test_windows as fixtures
from test_windows import fixture


@unittest.skipUnless(os.name == "nt", "Windows integration")
class FeatureTest(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def test_required_local_advice_can_resume_after_service_recovers(self):
        run = self.configured(fixture("good"), advisory_ollama={"model": "local", "think": True, "required": True})
        with patch("nightweaver.engine.advisory", side_effect=OSError("local server offline")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual(state["blocked_from"], "reviewing")
        self.assertIn("local server offline", state["last_error"])
        with patch("nightweaver.engine.advisory", return_value="Check empty input") as advise:
            self.assertEqual(Run(Store(self.store.root), run).run(retry=True), "done")
            self.assertIs(advise.call_args.args[0]["think"], True)

    def test_budget_survives_restart_and_cannot_be_reset_by_retry(self):
        run = self.configured(fixture("good"), budgets={"max_calls": 1},
                              reviewer={"kind": "command", "argv": fixture("review")})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("provider-call budget", self.store.get(run)["state"]["last_error"])
        self.assertEqual(Run(Store(self.store.root), run).run(retry=True), "blocked")
        self.assertEqual(metrics(self.store, run)["provider_calls"], 1)
        timed = self.configured(fixture("good"), budgets={"max_seconds": 1})
        self.store.transition(timed, "fixture_clock", {}, started_at="2020-01-01T00:00:00+00:00")
        self.assertEqual(Run(self.store, timed).run(), "blocked")
        self.assertEqual(metrics(self.store, timed)["provider_calls"], 0)
        token_limited = self.configured(fixture("good"), budgets={"max_observed_tokens": 10})
        self.store.transition(token_limited, "fixture_usage", {}, observed_tokens=10)
        self.assertEqual(Run(Store(self.store.root), token_limited).run(), "blocked")
        self.assertIn("observed-token budget", self.store.get(token_limited)["state"]["last_error"])
        self.assertEqual(metrics(self.store, token_limited)["provider_calls"], 0)

    def test_fallback_completes_without_spending_repair_attempt(self):
        run = self.configured([str(self.root / "missing.exe")], attempts=1,
                              worker_fallbacks=[{"kind": "command", "argv": fixture("good")}],
                              reviewer={"kind": "command", "argv": [str(self.root / "missing-reviewer.exe")]},
                              reviewer_fallbacks=[{"kind": "command", "argv": fixture("review")}])
        self.assertEqual(Run(self.store, run).run(), "done")
        measured = metrics(self.store, run)
        self.assertEqual(measured["fallbacks"], 2)
        self.assertEqual(measured["provider_calls"], 4)
        self.assertEqual(measured["usage_unknown_calls"], 4)
        self.assertIsNone(measured["observed_tokens"])

    def test_rate_limit_persists_deadline_and_dispatcher_skips_until_due(self):
        run = self.configured(fixture("good"), enqueue=True, retry={"initial_seconds": 60, "max_seconds": 60})
        with patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("Limited", "usage_limit")) as model:
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
        self.assertEqual(model.call_count, 1)
        self.assertIsNotNone(self.store.get(run)["state"]["retry_at"])
        self.assertEqual(Store(self.store.root).dispatch_runs(), [])
        self.store.transition(run, "fixture_clock", {}, retry_at="2020-01-01T00:00:00+00:00")
        self.assertEqual(dispatch(Store(self.store.root), False), [{"run_id": run, "phase": "done"}])

    def test_feedback_during_review_forces_another_worker_attempt(self):
        run = self.configured(fixture("good"), reviewer={"kind": "command", "argv": fixture("review")})
        from nightweaver.providers import invoke as real_invoke
        changed = False

        def reviewed(config, role, *args, **kwargs):
            nonlocal changed
            result = real_invoke(config, role, *args, **kwargs)
            if role == "reviewer" and not changed:
                changed = True
                self.store.feedback(run, "Recheck the keyboard acceptance requirement before promotion.")
            return result

        with patch("nightweaver.engine.invoke", side_effect=reviewed):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 2)
        self.assertFalse(metrics(self.store, run)["unassisted_acceptance"])

    def test_dispatcher_isolates_bad_runs_and_reports_blocked_dependencies(self):
        first = self.configured(fixture("good"), enqueue=True)
        bad = self.configured(fixture("good"), enqueue=True)
        dependent = self.configured(fixture("good"), enqueue=True, after=[bad])
        self.store.transition(bad, "fixture_corruption", {}, phase="invalid_phase")
        result = dispatch(self.store, False)
        self.assertEqual(result[0]["phase"], "done")
        self.assertEqual(self.store.get(bad)["state"]["phase"], "blocked")
        self.assertEqual(self.store.get(dependent)["state"]["phase"], "queued")
        from nightweaver.cli import with_live_status
        self.assertEqual(with_live_status(self.store, self.store.get(dependent))["blocked_prerequisites"], [bad])

    def test_unchanged_failure_stops_before_full_attempt_budget(self):
        run = self.configured(fixture("bad"), attempts=5, retry={"repeated_failure_limit": 2, "initial_seconds": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 2)
        self.assertIn("repeated_failure_stopped", [e["kind"] for e in self.store.events(run)])

    def test_context_feedback_checkpoint_and_report_preserve_source(self):
        (self.repo / "BRIEF.md").write_text("Preserve the quiet green interface.", encoding="utf-8")
        git(self.repo, "add", "BRIEF.md")
        git(self.repo, "commit", "-m", "brief")
        run = self.configured(fixture("good"), context_files=["BRIEF.md"], repository_map=True,
                              milestones=[{"title": "First", "task": "Prepare"}, {"title": "Second", "task": "Deliver",
                                          "worker": {"kind": "command", "argv": fixture("bad")}}])
        (self.repo / "BRIEF.md").write_text("Uncommitted change must not enter context.", encoding="utf-8")
        self.assertIn("quiet green", brief(self.store, run))
        self.assertNotIn("Uncommitted", brief(self.store, run))
        self.store.feedback(run, "Keep keyboard controls. api_key=private-fixture-value")
        self.assertNotIn("private-fixture-value", self.store.feedback_text(run))
        self.assertEqual(Run(self.store, run).run(), "blocked")
        original = self.store.get(run)
        forked = fork_run(self.store, run, 1, {"kind": "command", "argv": fixture("bad")})
        self.assertEqual(self.store.get(forked)["state"]["base"], self.store.accepted_milestones(run)[0]["commit"])
        self.assertEqual(self.store.get(run), original)
        self.assertEqual(len(self.store.get(forked)["spec"]["milestones"]), 1)
        for i in range(125):
            self.store.transition(run, "diagnostic", {"value": i})
        first = self.store.events(run, limit=50, after=0)
        second = self.store.events(run, limit=50, after=first[-1]["seq"])
        self.assertLess(first[-1]["seq"], second[0]["seq"])
        report = self.root / "report.html"
        self.store.transition(run, "markup", {"message": "<script>alert(1)</script>"})
        run_report(self.store, run, report)
        html = report.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("private-fixture-value", html)
        self.assertGreater(metrics(self.store, run)["event_count"], 125)

    def test_oversized_artifact_cannot_be_accepted(self):
        run = self.configured(fixture("good"), attempts=1, gates=[{
            "name": "bounded evidence", "argv": fixture("large_artifact"), "artifacts": ["receipt.txt"], "max_artifact_bytes": 64}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("exceeds 64", self.store.get(run)["state"]["last_error"])
        self.assertFalse(self.store.accepted_milestones(run))

    def test_idle_and_noisy_processes_are_bounded(self):
        self.assertEqual(execute(fixture("sleep"), self.root, self.root / "idle.log", 10, idle_timeout=1), 125)
        self.assertEqual(execute(fixture("flood"), self.root, self.root / "noise.log", 10, max_log_bytes=1024), 126)

    def test_comparison_freezes_baselines_and_keeps_failures(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"repository": str(self.repo), "title": "Benchmark task", "objective": "Write ready",
                                    "worker": {"kind": "command", "argv": fixture("good")}, "budgets": {"max_seconds": 60}, "attempts": 1,
                                    "gates": [{"name": "check", "argv": fixture("gate")}],
                                    "milestones": [{"title": "Implement", "task": "Write ready"}]}), encoding="utf-8")
        suite = self.root / "suite.json"
        suite.write_text(json.dumps({"name": "Fixture comparison", "repetitions": 1, "tasks": [{"name": "Readiness", "plan": "plan.json"}],
                                    "contenders": [{"name": "Working fixture", "worker": {"kind": "command", "argv": fixture("good")}},
                                                   {"name": "Failing fixture", "mode": "external", "worker": {"kind": "command", "argv": fixture("bad")}}]}), encoding="utf-8")
        created = create_suite(self.store, suite)
        manifest = read_suite(self.store, created["benchmark_id"])
        self.assertEqual({r["baseline"] for r in manifest["runs"]}, {self.head})
        for row in manifest["runs"]:
            Run(self.store, row["run_id"]).run()
        report = suite_report(self.store, created["benchmark_id"], self.root / "comparison.html")
        rows = {r["contender"]: r for r in report["summaries"]}
        self.assertEqual(rows["Working fixture"]["accepted"], 1)
        self.assertEqual(rows["Failing fixture"]["accepted"], 0)
        self.assertEqual(rows["Failing fixture"]["finished"], 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Install optional browser extra")
    def test_browser_failure_is_repaired_with_fresh_screenshots_and_traces(self):
        config = {"page": "index.html", "heading": "Atlas fixture", "steps": [
            {"action": "fill", "selector": "#search", "value": "Ready"},
            {"action": "click", "selector": "#apply"}, {"action": "text", "selector": "#result", "value": "Ready"}]}
        if os.environ.get("NIGHTWEAVER_TEST_BROWSER_CHANNEL"):
            config["channel"] = os.environ["NIGHTWEAVER_TEST_BROWSER_CHANNEL"]
        run = self.configured(fixture("browser_build"), gates=[{"kind": "browser", "name": "Browser journey", "browser": config, "timeout": 180}])
        self.assertEqual(Run(self.store, run).run(), "done", self.store.get(run)["state"].get("last_error"))
        gates = [e["details"] for e in self.store.events(run, limit=None) if e["kind"] == "gate_finished"]
        self.assertEqual([g["exit"] for g in gates], [1, 0])
        self.assertTrue(all(len(g["artifacts"]) == 5 for g in gates))
        self.assertNotEqual(gates[0]["artifacts"][0]["path"], gates[1]["artifacts"][0]["path"])
        html = self.root / "browser-report.html"
        run_report(self.store, run, html, include_images=True)
        self.assertIn("data:image/png;base64,", html.read_text(encoding="utf-8"))


class ContractsTest(unittest.TestCase):
    def test_initial_guidance_is_not_counted_as_mid_run_intervention(self):
        with TemporaryDirectory() as folder:
            store = Store(folder)
            run = store.create({"title": "Metrics", "milestones": []}, "baseline")
            with patch("nightweaver.store.timestamp", return_value="2026-01-01T00:00:00+00:00"):
                store.feedback(run, "Initial brief")
                store.transition(run, "run_started", {})
            self.assertEqual(metrics(store, run)["interventions"], 0)
            store.feedback(run, "Mid-run correction")
            self.assertEqual(metrics(store, run)["interventions"], 1)

    def test_gemini_contract_and_malformed_streams(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"kind": "gemini", "model": "chosen-model", "profile_dir": str(root)}
            argv, env = adapter(config, "reviewer", "prior")
            self.assertEqual(env["GEMINI_CLI_HOME"], str(root))
            self.assertEqual(argv[argv.index("--approval-mode") + 1], "plan")
            self.assertEqual(argv[argv.index("--resume") + 1], "prior")
            log = root / "events.jsonl"
            log.write_text('null\n[]\ninvalid\n{"type":"init","session_id":"new"}\n'
                           '{"type":"message","role":"assistant","content":"RE","delta":true}\n'
                           '{"type":"message","role":"assistant","content":"ADY","delta":true}\n'
                           '{"type":"result","stats":{"total_tokens":12}}\n', encoding="utf-8")
            self.assertEqual(metadata(log, "gemini"), ("new", "READY"))
            self.assertEqual(usage(log, "gemini")["observed_tokens"], 12)
            with self.assertRaisesRegex(ValueError, "JSON object"):
                json_response("[]")
            self.assertEqual(json_response('```json\n{"verdict":"PASS"}\n```')["verdict"], "PASS")

    def test_validation_rejects_typos_shapes_and_windows_paths(self):
        with TemporaryDirectory() as folder:
            raw = {"repository": folder, "title": "Test", "objective": "Check contracts", "worker": {"kind": "codex"},
                   "gates": [{"name": "check", "argv": [sys.executable, "check.py"]}], "milestones": [{"title": "One", "task": "Do"}]}
            for extra in ({"attempt": 2}, {"schema_version": 999}, {"budgets": {"max_calls": True}},
                          {"protected_paths": "tests"}, {"context_files": ["../secret"]}, {"context_files": ["NUL.txt"]},
                          {"worker": {"kind": "codex", "modle": "x"}}, {"milestones": [None]}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    validate({**raw, **extra})

    def test_atomic_publication_preserves_old_file_on_replace_failure(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("original")
            with patch("nightweaver.safety.os.replace", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    atomic_write(path, "replacement")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertNotIn("sk-abcdefghijklmnop", redact("Bearer sk-abcdefghijklmnop"))

    def test_issue_import_is_read_only_and_keeps_operator_checks(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "template.json"
            raw = {"repository": str(root), "worker": {"kind": "codex"}, "gates": [{"name": "trusted check", "argv": [sys.executable, "check.py"]}]}
            template.write_text(json.dumps(raw))
            issue = {"number": 12, "title": "Repair search", "body": "Preserve keyboard input", "url": "https://github.com/example/project/issues/12"}
            result = subprocess.CompletedProcess([], 0, json.dumps(issue).encode(), b"")
            with patch("nightweaver.intake.shutil.which", return_value="gh.exe"), patch("nightweaver.intake.subprocess.run", return_value=result) as process:
                response = issue_plan(Store(root / "runtime"), template, "example/project", 12, root / "issue.json")
            self.assertEqual(process.call_args.args[0][1:3], ["issue", "view"])
            saved = json.loads((root / "issue.json").read_text())
            self.assertEqual(saved["gates"], raw["gates"])
            self.assertFalse(response["started"])


if __name__ == "__main__":
    unittest.main()
