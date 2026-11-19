"""Windows acceptance, control calibration and comparable repeated trials."""

import csv
import json
import os
import sys
import unittest
from unittest.mock import patch

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.benchmark import read_suite, reliability, summarize
from nightweaver.browser import run_browser, validate_browser
from nightweaver.checks import read_browser_evidence
from nightweaver.diagnostics import database_check
from nightweaver.engine import Run, git
from nightweaver.evaluation import calibration, call_frontier, shard_trials
from nightweaver.reports import metrics


@unittest.skipUnless(os.name == "nt", "Native Windows controller")
class ReleaseAcceptance(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def test_missing_executable_holds_verification_and_real_exit_127_rejects(self):
        gate = self.root / "absent.exe"
        run = self.configured(
            fixtures.fixture("good"), gates=[{"name": "required tool", "argv": [str(gate)]}]
        )
        self.assertEqual(Run(self.store, run).run(), "blocked")
        m = self.cli("metrics", run)
        self.assertEqual(
            (m["outcome"], m["worker_attempts"], m["failed_attempts"]), ("execution_blocked", 1, 0)
        )
        (receipt,) = self.store.events(run, limit=None, kinds=["gate_finished"])
        self.assertTrue(receipt["details"]["execution_error"])
        self.assertIsNone(receipt["details"]["exit"])
        rid = self.configured(
            fixtures.fixture("good"),
            attempts=1,
            gates=[{"name": "exit", "argv": [sys.executable, "-c", "raise SystemExit(127)"]}],
        )
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertEqual(metrics(self.store, rid)["last_rejection_stage"], "check")

    def test_mutating_gate_cannot_pass_before_restoring_gate(self):
        rid = self.configured(
            fixtures.fixture("good"),
            attempts=1,
            gates=[
                {"name": "mutating", "argv": fixtures.fixture("drift")},
                {"name": "restoring", "argv": fixtures.fixture("good")},
            ],
        )
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        receipts = self.store.events(rid, limit=None, kinds=["gate_finished"])
        self.assertEqual(len(receipts), 1)
        self.assertIn("source_error", receipts[0]["details"])
        self.assertFalse(git(self.repo, "branch", "--list", "nightweaver/" + rid))

    def test_replaced_pinned_repository_blocks_and_registered_run_survives_rebinding(self):
        self.store.bind_project("gallery", self.repo, self.repo / ".git")
        rid = self.cli("add", self.plan(project="gallery"))["run_id"]
        other = self.root / "replacement"
        git(self.repo, "clone", "--local", str(self.repo), str(other))
        self.store.bind_project("gallery", other, other / ".git", True)
        saved = self.root / "original"
        self.repo.rename(saved)
        git(saved, "clone", "--local", str(saved), str(self.repo))
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 0)
        self.assertIn("replaced", self.store.get(rid)["state"]["last_error"])
        self.repo.rename(self.root / "substitute")
        saved.rename(self.repo)
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")

    def test_corrupt_cursor_and_dependency_cycle_are_reported(self):
        a, b = [self.configured(fixtures.fixture("good")) for _ in range(2)]
        self.store.transition(a, "example", step=4, after=[b])
        self.store.transition(b, "example", after=[a])
        problems = database_check(self.store.root / "runs.sqlite3")["problems"]
        self.assertIn("cyclic prerequisite: 2", problems)
        self.assertIn("invalid milestone cursor: 1", problems)
        self.store.transition(a, "fixed", step=0, after=[])
        self.assertTrue(database_check(self.store.root / "runs.sqlite3")["ok"])

    def test_unknown_terminal_stop_never_accrues_seconds(self):
        rid = self.configured(fixtures.fixture("good"), budgets={"max_seconds": 80})
        self.store.transition(rid, "run_started", started_at="2024-01-01T00:00:00+00:00")
        self.store.transition(rid, "legacy_transition", phase="blocked")
        self.assertIsNone(self.cli("metrics", rid)["elapsed_seconds"])
        budget = self.cli("budget", rid)
        self.assertIsNone(budget["used"]["max_seconds"])
        self.assertIsNone(budget["remaining"]["max_seconds"])

    def test_pending_and_recovered_calls_have_no_measured_duration(self):
        rid = self.configured(fixtures.fixture("good"))
        pending = {
            "call": 1,
            "role": "worker",
            "provider": "command",
            "model": None,
            "step": 1,
            "attempt": 1,
            "started": "2024-01-01T00:00:00+00:00",
            "log": str(self.root / "missing.log"),
        }
        self.store.transition(rid, "provider_call_started", pending, pending_call=pending, provider_calls=1)
        self.assertEqual(self.cli("call-ledger", rid)["calls"][0]["status"], "pending")
        Run(self.store, rid).finish_call(pending, recovered=True)
        (row,) = self.cli("call-ledger", rid)["calls"]
        self.assertEqual(
            (row["seconds"], row["status"], row["step"], row["attempt"]), (None, "recovered", 1, 1)
        )
        self.assertNotIn("log", row)

    def test_browser_persistence_form_state_hotspots_and_diagnostics(self):
        for name in ("con", "LPT1".lower(), "phone "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_browser(
                    {"page": "app.html", "viewports": [{"name": name, "width": 320, "height": 540}]}
                )
        (
            self.repo / "app.html"
        ).write_text("""<link rel="icon" href="data:,"><input id="a"><input type="checkbox" id="b">
            <script>let a=document.querySelector('#a');a.value=localStorage.getItem('v')||'';
            a.oninput=()=>localStorage.setItem('v',a.value);</script>""")
        git(self.repo, "add", "app.html")
        git(self.repo, "commit", "-qm", "form")
        steps = [
            {"action": "unchecked", "selector": "#b"},
            {"action": "check", "selector": "#b"},
            {"action": "checked", "selector": "#b"},
            {"action": "fill", "selector": "#a", "value": "kept"},
            {"action": "reload"},
            {"action": "value", "selector": "#a", "value": "kept"},
        ]
        browser = {
            "page": "app.html",
            "steps": steps,
            "viewports": [{"name": "phone", "width": 390, "height": 844}],
        }
        rid = self.configured(
            fixtures.fixture("good"),
            attempts=1,
            protected_paths=["app.html"],
            gates=[{"name": "form", "kind": "browser", "browser": browser}],
        )
        self.assertEqual(Run(self.store, rid).run(), "done")
        rows = self.cli("browser-hotspots", rid)
        self.assertEqual((len(rows["actions"]), rows["missing_timing_records"]), (6, 0))
        self.assertTrue(all(r["timed_observations"] == 1 and r["median_ms"] >= 0 for r in rows["actions"]))
        (gate,) = self.cli("gate-stats", rid)
        self.assertEqual(gate["measured_time_share"], 1)
        self.assertGreater(gate["total_measured_ms"], 0)
        (call,) = self.cli("call-ledger", rid)["calls"]
        self.assertEqual((call["status"], call["step"], call["attempt"]), ("finished", 1, 1))
        prompt = next(self.store.directory(rid).rglob("worker.input.txt")).read_text(encoding="utf-8")
        self.assertIn("Protected paths: app.html", prompt)
        m = self.cli("metrics", rid)
        self.assertGreaterEqual(m["queue_seconds"], 0)
        self.assertGreater(m["seconds_to_first_acceptance"], 0)
        # A genuinely wrong post-reload value must fail at the named assertion.
        steps[-1]["value"] = "wrong"
        config = validate_browser({**browser, "timeout_ms": 500})
        out = self.root / "failed-browser"
        receipt = run_browser(config, self.repo.resolve(), out)
        self.assertFalse(receipt["passed"])
        receipt["viewports"][0]["errors"] += ["second diagnostic", "second diagnostic"]
        (out / "browser.json").write_text(json.dumps(receipt), encoding="utf-8")
        detail = {"exit": 1, "artifacts": []}
        read_browser_evidence(config, out, detail, 8 * 1024 * 1024)
        self.assertIn("second diagnostic", detail["browser_failures"][0]["errors"])
        self.assertEqual(detail["browser_failures"][0]["stage"]["index"], 6)
        self.store.transition(rid, "gate_finished", {**detail, "name": "form", "step": 1, "gate_index": 1})
        self.assertEqual(self.cli("browser-hotspots", rid)["actions"][0]["failed_observations"], 1)

    def test_calibrate_shard_sensitivity_frontier_and_trial_manifest(self):
        plan = self.plan(attempts=1, budgets={"max_seconds": 60, "max_calls": 2})
        suite = self.root / "bench.json"
        spec = {
            "name": "controls",
            "repetitions": 1,
            "tasks": [{"name": t, "plan": plan.name} for t in ("a", "b")],
            "contenders": [
                {"name": name, "worker": {"kind": "command", "argv": fixtures.fixture(name)}}
                for name in ("bad", "good")
            ],
        }
        suite.write_text(json.dumps(spec))
        preview = self.cli("benchmark", "preview", suite)
        self.assertEqual([c["max_calls"] for c in preview["contender_budgets"]], [4, 4])
        sid = self.cli("benchmark", "create", suite)["benchmark_id"]

        def bench(action, *args, code=0):
            return self.cli("benchmark", action, sid, *args, code=code)

        controls = ("--positive", "good", "--negative", "bad")
        self.assertEqual(bench("calibrate", *controls, code=4)["verdict"], "inconclusive")
        pieces = [bench("shard", "--shard", f"{i}/2")["runs"] for i in (1, 2)]
        self.assertEqual(len({r["run_id"] for group in pieces for r in group}), 4)
        for i, group in enumerate(pieces, 1):
            code = 3 if any(r["contender"] == "bad" for r in group) else 0
            results = bench("run", "--shard", f"{i}/2", code=code)
            self.assertEqual({r["run_id"] for r in results}, {r["run_id"] for r in group})
        self.assertEqual(bench("calibrate", *controls)["counts"], {"pass": 2})
        self.assertEqual(
            bench("calibrate", "--positive", "bad", "--negative", "good", code=3)["verdict"], "failed"
        )
        (group,) = bench("failures")
        self.assertEqual(
            (group["outcome"], group["rejection_stage"], group["trials"]), ("rejected", "check", 2)
        )
        frontier = bench("frontier")
        self.assertEqual(frontier["matched_trials_per_contender"], 2)
        bad, good = frontier["points"]
        self.assertEqual((bad["dominated_by"], good["on_frontier"]), (["good"], True))
        (pair,) = bench("sensitivity")
        self.assertEqual((pair["scored_tasks"], pair["macro_gap_pp"]), (2, -100))
        self.assertEqual(len(pair["leave_one_task_out"]), 2)
        export = self.root / "trials.csv"
        bench("csv", "--output", export)
        with export.open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertTrue(all(r["usage_pending_calls"] == "0" and r["interventions"] == "0" for r in rows))
        original = read_suite(self.store, sid)
        path = self.store.root / "benchmarks" / (sid + ".json")
        for field in ("id", "coordinate"):
            edited = json.loads(json.dumps(original))
            a, b = edited["runs"][:2]
            for k in ["run_id"] if field == "id" else ["task", "repeat", "contender"]:
                b[k] = a[k]
            path.write_text(json.dumps(edited))
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                read_suite(self.store, sid)


class Calculations(unittest.TestCase):
    def test_incomplete_inputs_cost_and_task_threshold(self):
        from nightweaver.benchmark_exports import compare_suites, regression_verdict

        row = {
            "task": "x",
            "repeat": 1,
            "baseline": "base",
            "contract_sha256": "same",
            "mode": "external",
            "route_sha256": "route",
            "contender": "control",
            "phase": "done",
            "outcome": "accepted",
            "accepted": True,
            "run_id": "a",
            "provider_calls": 1,
            "elapsed_seconds": 2,
            "observed_tokens": None,
            "usage_unknown_calls": 1,
            "usage_pending_calls": 0,
            "unassisted_acceptance": True,
        }
        self.assertIsNone(reliability([{**row, "baseline": None}], 1)[0]["pass_at_k"])
        self.assertIsNone(
            summarize([row, {**row, "phase": "working", "accepted": False}])[0]["seconds_all_attempts"]
        )
        with patch(
            "nightweaver.benchmark_exports.suite_report",
            side_effect=[
                {"runs": [row], "runtime": {"nightweaver": "0.9.0"}},
                {"runs": [row], "runtime": {"nightweaver": "0.10.0"}},
            ],
        ):
            report = compare_suites(None, "one", "two")
        self.assertEqual(report["runtime_changes"]["nightweaver"]["after"], "0.10.0")
        self.assertEqual(regression_verdict(report, 1, 0, 2)["verdict"], "inconclusive")
        self.assertEqual(regression_verdict(report, 1, 0, 1)["verdict"], "pass")
        bad = {
            **row,
            "phase": "blocked",
            "accepted": False,
            "outcome": "worker_timeout",
            "contender": "faulty",
        }
        self.assertEqual(calibration([row, bad], "control", "faulty")["verdict"], "inconclusive")
        self.assertIsNone(
            call_frontier([row, {**bad, "contract_sha256": "other"}])["points"][0]["on_frontier"]
        )
        with self.assertRaises(ValueError):
            shard_trials([row], "2/1")
