"""Operator workflows, checkpoint integrity and fair comparisons for version 0.9."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_windows as fixtures
from nightweaver.benchmark import create_suite, paired_outcomes, read_suite, summarize
from nightweaver.benchmark_exports import compare_suites, regression_verdict
from nightweaver.cli import main
from nightweaver.diagnostics import database_check
from nightweaver.engine import Run, fork_run, git
from nightweaver.insights import gate_stats
from nightweaver.operations import attention, follow_events
from nightweaver.reports import metrics


@unittest.skipUnless(os.name == "nt", "Windows controller")
class OperatorRelease(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def cli(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else stderr.getvalue()

    def plan(self, **updates):
        data = {
            "repository": str(self.repo),
            "title": "Gallery",
            "objective": "Prepare gallery",
            "worker": {"kind": "command", "argv": fixtures.fixture("good")},
            "milestones": [{"title": "Prepare", "task": "Set ready"}],
            "gates": [{"name": "check", "argv": fixtures.fixture("gate")}],
            "budgets": {"max_seconds": 120},
        }
        data.update(updates)
        file = self.root / "plan.json"
        file.write_text(json.dumps(data))
        return file

    def test_publish_rechecks_manifest_and_gate_bytes(self):
        for target in ("checks.json", "gate.log"):
            rid = self.configured(fixtures.fixture("good"))
            original = Run.promote

            def edit_then_promote(run, state):
                path = run.evidence_dir(state)
                next(path.rglob(target)).write_text("changed")
                return original(run, state)

            with (
                self.subTest(target=target),
                patch.object(Run, "promote", edit_then_promote),
            ):
                self.assertEqual(Run(self.store, rid).run(), "blocked")
                self.assertFalse(
                    git(self.repo, "branch", "--list", "nightweaver/" + rid)
                )
                self.assertEqual(
                    self.store.get(rid)["state"]["blocked_from"], "checking"
                )
            self.assertEqual(Run(self.store, rid).run(retry=True), "done")
            self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_planner_cannot_silently_move_baseline(self):
        file = self.plan()

        def planning(*args, **kwargs):
            (self.repo / "result.txt").write_text("concurrent change")
            git(self.repo, "commit", "-am", "Concurrent work")
            return (
                0,
                None,
                json.dumps({"milestones": [{"title": "Step", "task": "Check"}]}),
            )

        with patch("nightweaver.cli.invoke", side_effect=planning):
            error = self.cli("goal", file, "Repair gallery", "--start", code=1)
        self.assertIn(self.head, error)
        self.assertEqual(self.store.runs(), [])

    def test_fork_rejects_changed_project_binding(self):
        self.store.bind_project("gallery", self.repo, self.repo / ".git")
        rid = self.cli("add", self.plan(project="gallery"))["run_id"]
        clone = self.root / "clone"
        git(self.repo, "clone", "--local", "--", str(self.repo), str(clone))
        self.store.bind_project("gallery", clone, clone / ".git", True)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            fork_run(self.store, rid, 0)
        self.assertEqual(len(self.store.runs()), 1)

    def test_partial_suite_and_invalid_database_are_visible(self):
        self.plan()
        suite = self.root / "suite.json"
        suite.write_text(
            json.dumps(
                {
                    "name": "Trial",
                    "tasks": [{"name": "t", "plan": "plan.json"}],
                    "repetitions": 2,
                    "contenders": [
                        {
                            "name": "fixture",
                            "worker": {
                                "kind": "command",
                                "argv": fixtures.fixture("good"),
                            },
                        }
                    ],
                }
            )
        )
        sid = create_suite(self.store, suite)["benchmark_id"]
        manifest = read_suite(self.store, sid)
        manifest["runs"].pop()
        (self.store.root / "benchmarks" / (sid + ".json")).write_text(
            json.dumps(manifest)
        )
        with self.assertRaisesRegex(ValueError, "1/2"):
            read_suite(self.store, sid)
        rid = manifest["runs"][0]["run_id"]
        self.store.transition(rid, "example", after=["000000000000"], phase="oops")
        audit = database_check(self.store.root / "runs.sqlite3")
        self.assertFalse(audit["ok"])
        self.assertTrue(any("missing prerequisite" in s for s in audit["problems"]))
        self.assertTrue(any("invalid run state" in s for s in audit["problems"]))

    def test_import_keeps_operator_template(self):
        file = self.plan()
        original = file.read_bytes()
        with (
            patch("nightweaver.intake.shutil.which", return_value="gh"),
            patch("nightweaver.intake.subprocess.run") as call,
        ):
            call.return_value.returncode = 0
            call.return_value.stdout = b'{"number":7,"title":"Fix","body":"repair","url":"https://example.invalid/7"}'
            self.assertIn(
                "distinct",
                self.cli(
                    "issue",
                    file,
                    "--repo",
                    "test/repo",
                    "--number",
                    7,
                    "--output",
                    file,
                    code=1,
                ),
            )
        self.assertEqual(file.read_bytes(), original)

    def test_wait_launch_pause_and_filters(self):
        self.store.bind_project("Gallery", self.repo, self.repo / ".git")
        rid = self.cli("add", self.plan(project="Gallery"))["run_id"]
        self.assertTrue(self.cli("wait", rid, "--timeout", 0, code=4)["timed_out"])
        with patch(
            "nightweaver.store.timestamp", return_value="2099-01-01T00:00:00.000+00:00"
        ):
            self.assertTrue(self.cli("pause", rid)["changed"])
        self.assertTrue(self.store.get(rid)["updated"].startswith("2099"))
        self.assertFalse(self.cli("pause", rid)["changed"])
        with patch("nightweaver.cli.background") as spawn:
            self.assertFalse(self.cli("start", rid, code=2)["launched"])
            self.assertEqual(self.cli("wait", rid, code=2)["phase"], "paused")
            self.assertEqual(self.cli("resume", rid, "--wait")["phase"], "done")
            self.assertFalse(self.cli("start", rid)["launched"])
            spawn.assert_not_called()
        self.assertEqual(self.cli("wait", rid)["phase"], "done")
        self.assertFalse(self.cli("pause", rid)["pause_requested"])
        self.assertEqual(
            len(self.cli("list", "--project", "gallery", "--phase", "done")), 1
        )
        self.assertEqual(self.cli("list", "--phase", "blocked"), [])
        self.assertEqual(
            self.cli("fleet", "--project", "Gallery")["projects"][0]["phases"],
            {"done": 1},
        )

    def test_dependency_graph_priority_and_session_handoff(self):
        a = self.configured(fixtures.fixture("good"), enqueue=True)
        b = self.configured(fixtures.fixture("good"), enqueue=True, after=[a])
        c = self.configured(fixtures.fixture("good"), enqueue=True, after=[a, b])
        self.store.transition(a, "blocked", phase="blocked")
        graph = self.cli("dependencies", c)
        self.assertEqual(set(graph["upstream"]), {a, b})
        self.assertEqual(len(graph["edges"]), 3)
        self.assertEqual(self.cli("dependencies", a)["downstream"], sorted([b, c]))
        rows = attention(self.store)
        self.assertEqual((rows[0]["run_id"], rows[0]["affected_runs"]), (a, 2))
        self.assertEqual(
            next(r for r in rows if r["run_id"] == c)["next_command"], ["handoff", a]
        )
        self.store.transition(
            b,
            "gate_finished",
            {"name": "before restart", "exit": 1, "attempt": 1, "tree": "tree"},
        )
        handoff = self.cli("handoff", b)
        self.assertEqual(handoff["last_check"]["name"], "before restart")
        self.assertEqual(handoff["next_worker"]["kind"], "command")

    def test_event_pages_incremental_trace_and_timeline(self):
        file = self.plan()
        rid = self.cli("add", file, "--start", "--wait")["run_id"]
        (first,) = self.cli("events", rid, "--kind", "worker_started", "--limit", 1)
        (last,) = self.cli(
            "events", rid, "--kind", "run_completed", "--after", first["seq"]
        )
        self.assertEqual(
            [
                e["seq"]
                for e in follow_events(
                    self.store, rid, first["seq"], 1, ["run_completed"]
                )
            ],
            [last["seq"]],
        )
        out = self.root / "slice.jsonl"
        trace = self.cli(
            "trace",
            rid,
            "--after",
            first["seq"],
            "--until",
            last["seq"],
            "--output",
            out,
        )
        records = [json.loads(line) for line in out.read_text().splitlines()][1:]
        self.assertTrue(all(first["seq"] < r["seq"] <= last["seq"] for r in records))
        self.assertEqual(trace["last_seq"], last["seq"])
        (entry,) = self.cli("timeline", rid)["milestones"]
        self.assertEqual(
            (entry["worker_attempts"], entry["gate_executions"], entry["rejections"]),
            (1, 1, 0),
        )
        self.assertTrue(entry["commit"] and entry["elapsed_seconds"] >= 0)

    def test_portable_evidence_coverage_and_ci_summary(self):
        file = self.plan(
            gates=[
                {
                    "name": "gallery",
                    "kind": "browser",
                    "stability_runs": 2,
                    "browser": {
                        "page": "result.txt",
                        "offline": True,
                        "steps": [{"action": "visible", "selector": "body"}],
                    },
                }
            ]
        )
        rid = self.cli("add", file, "--start", "--wait")["run_id"]
        coverage = self.cli("browser-coverage", rid)
        self.assertEqual(
            {
                (v["viewport"], v["samples"], v["completed_steps"])
                for v in coverage["viewports"]
            },
            {("desktop", 2, 2), ("mobile", 2, 2)},
        )
        output = self.root / "manifest.json"
        self.assertEqual(
            self.cli("evidence-manifest", rid, "--output", output)["status"], "verified"
        )
        rows = json.loads(output.read_text())["files"]
        self.assertEqual(sum(r["role"] == "log" for r in rows), 2)
        self.assertTrue(all(r["actual_sha256"] == r["expected_sha256"] for r in rows))
        self.assertNotIn(str(self.root), output.read_text())
        md = self.root / "summary.md"
        self.cli("ci-summary", rid, "--output", md)
        self.assertIn("4 / 4", md.read_text(encoding="utf-8"))
        log = Path(
            self.store.events(rid, limit=1, kinds=["gate_finished"])[0]["details"][
                "log"
            ]
        )
        log.write_text("edited")
        result = self.cli("evidence-manifest", rid, "--output", output, code=3)
        self.assertEqual(result["status"], "failed")

    def test_plan_diff_respects_normalization_and_gate_order(self):
        file = self.plan()
        after = self.root / "after.json"
        raw = json.loads(file.read_text())
        raw["gates"][0]["stability_runs"] = 1
        after.write_text(json.dumps(raw))
        self.assertTrue(self.cli("plan-diff", file, after)["identical"])
        raw["gates"][0]["stability_runs"] = 2
        raw["budgets"]["max_seconds"] = 130
        after.write_text(json.dumps(raw))
        fields = {c["field"] for c in self.cli("plan-diff", file, after)["changes"]}
        self.assertEqual(fields, {"budgets", "milestones[1].gates.check"})

    def test_selected_operational_benchmark_retry(self):
        self.plan()
        suite = self.root / "suite.json"
        suite.write_text(
            json.dumps(
                {
                    "name": "Retries",
                    "repetitions": 2,
                    "tasks": [{"name": "task", "plan": "plan.json"}],
                    "contenders": [
                        {
                            "name": "worker",
                            "worker": {
                                "kind": "command",
                                "argv": fixtures.fixture("good"),
                            },
                        }
                    ],
                }
            )
        )
        sid = create_suite(self.store, suite)["benchmark_id"]
        rows = read_suite(self.store, sid)["runs"]
        for row in rows:
            detail = {"during": "checking"} if row["repeat"] == 1 else {}
            self.store.transition(
                row["run_id"], "blocked", detail, phase="blocked", blocked_from="ready"
            )
        results = self.cli("benchmark", "retry-blocked", sid)
        self.assertEqual({r["phase"] for r in results}, {"done", "skipped"})
        rejected = next(r["run_id"] for r in rows if r["repeat"] == 2)
        self.assertEqual(metrics(self.store, rejected)["provider_calls"], 0)

    def test_distinct_failed_samples_reach_the_worker(self):
        count = self.root / "counter"
        code = "from pathlib import Path;import sys;p=Path(sys.argv[1]);n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));print('Failure reason',n);sys.exit(1)"
        rid = self.configured(
            fixtures.fixture("good"),
            attempts=1,
            gates=[
                {
                    "name": "value",
                    "stability_runs": 2,
                    "argv": [fixtures.fixture("good")[0], "-c", code, str(count)],
                }
            ],
        )
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        issue = self.store.get(rid)["state"]["last_error"]
        self.assertIn("Failure reason 1", issue)
        self.assertIn("Failure reason 2", issue)
        (stats,) = gate_stats(
            self.store.events(rid, limit=None)
            + [
                {
                    "kind": "gate_finished",
                    "details": {
                        **self.store.events(rid, limit=1, kinds=["gate_finished"])[0][
                            "details"
                        ],
                        "artifact_error": "missing",
                    },
                }
            ]
        )
        self.assertEqual(
            (
                stats["functional_failures"],
                stats["unscored"],
                stats["scored"],
                stats["scored_pass_rate"],
            ),
            (2, 1, 2, 0),
        )


class BenchmarkRelease(unittest.TestCase):
    def test_contracts_measurement_coverage_deltas_and_verdict(self):
        a = dict(
            run_id="a",
            task="task",
            contender="model",
            repeat=1,
            baseline="base",
            contract_sha256="spec",
            route_sha256="route",
            mode="nightweaver",
            phase="done",
            outcome="accepted",
            accepted=True,
            elapsed_seconds=None,
            provider_calls=2,
            observed_tokens=20,
            usage_unknown_calls=0,
            usage_pending_calls=0,
            unassisted_acceptance=True,
        )
        b = {**a, "run_id": "b", "provider_calls": 1, "observed_tokens": 10}
        c = {**b, "contender": "other", "contract_sha256": "different"}
        self.assertEqual(paired_outcomes([a, c])[0]["inconclusive_pairs"], 1)
        missing_contracts = [{**r, "contract_sha256": None} for r in (a, c)]
        self.assertEqual(paired_outcomes(missing_contracts)[0]["scored_pairs"], 0)
        self.assertIsNone(summarize([a])[0]["seconds_per_acceptance"])
        self.assertIsNone(summarize([a])[0]["seconds_all_attempts"])
        with patch(
            "nightweaver.benchmark_exports.suite_report",
            side_effect=[{"runs": [a]}, {"runs": [b]}],
        ):
            comparison = compare_suites(None, "baseline", "release")
        self.assertEqual(
            (
                comparison["median_provider_call_delta"],
                comparison["median_observed_token_delta"],
            ),
            (-1, -10),
        )
        self.assertEqual(regression_verdict(comparison, 1, 0)["verdict"], "pass")
        self.assertEqual(
            regression_verdict(comparison, 2, 0)["verdict"], "inconclusive"
        )
        with patch(
            "nightweaver.benchmark_exports.suite_report",
            side_effect=[{"runs": [a]}, {"runs": [{**b, "baseline": "other"}]}],
        ):
            changed = compare_suites(None, "baseline", "release")
        self.assertEqual(changed["pairs"][0]["changed_fields"], ["baseline"])
        self.assertEqual(regression_verdict(changed, 1, 0)["verdict"], "inconclusive")
        with patch(
            "nightweaver.benchmark_exports.suite_report",
            side_effect=[
                {"runs": [a]},
                {
                    "runs": [
                        {
                            **b,
                            "accepted": False,
                            "phase": "blocked",
                            "outcome": "rejected",
                        }
                    ]
                },
            ],
        ):
            regression = compare_suites(None, "baseline", "release")
        self.assertEqual(regression_verdict(regression, 1, 0)["verdict"], "regression")
