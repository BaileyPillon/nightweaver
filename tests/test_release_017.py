"""Repository OneShots, functional worker handoffs and trustworthy retry accounting."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_release_09 as operator
import test_windows as fixtures

from nightweaver import discovery, suite_runner
from nightweaver.engine import Run, git
from nightweaver.providers import ProviderUnavailable, json_response
from nightweaver.repair import next_worker, policy, repair_history
from nightweaver.reports import metrics


class AdapterTests(unittest.TestCase):
    def test_provider_boundary(self):
        for data in (
            '{"verdict":"REVISE","verdict":"PASS"}',
            '{"reason":NaN}',
            '{"x":1e999}',
            '{"x":' + "[" * 2000 + "0" + "]" * 2000 + "}",
            '{"x":"' + "é" * 500_000 + '"}',
            " " * 1_000_001 + "{}",
        ):
            with self.subTest(prefix=data[:12]), self.assertRaises(ValueError):
                json_response(data)
        self.assertEqual(
            json_response("  " + chr(96) * 3 + 'json\n{"ok":true}\n' + chr(96) * 3),
            {"ok": True},
        )

    def test_policy_bounds_and_distinct_routes(self):
        a, b = {"kind": "codex"}, {"kind": "claude"}
        step = {"worker": a, "worker_fallbacks": [a, b, a, b]}
        self.assertEqual(next_worker(step, 0), 2)
        self.assertIsNone(next_worker(step, 2))
        for raw in (
            {"collect_failures": 1},
            {"switch_after": -1},
            {"history": 11},
            {"typo": 1},
        ):
            with self.assertRaises(ValueError):
                policy(raw)

    def test_unittest_requires_at_least_one_normal_test(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "test_local.py"
            for decorator in ("EMPTY", "@unittest.skip('absent')", ""):
                file.write_text(
                    "import unittest\n"
                    + (
                        ""
                        if decorator == "EMPTY"
                        else f"{decorator}\nclass Example(unittest.TestCase):\n def test_it(self): pass\n"
                    )
                )
                p = subprocess.run(
                    [sys.executable, "-B", suite_runner.__file__, "unittest", "."],
                    check=False,
                    cwd=root,
                    capture_output=True,
                )
                self.assertEqual(p.returncode, int(bool(decorator)), p.stderr)

    def test_trx_requires_executed_passes_in_each_target(self):
        def report(args, check=False):
            out = Path(args[args.index("--results-directory") + 1])
            out.mkdir(parents=True, exist_ok=True)
            for i, xml in enumerate(reports):
                (out / f"target{i}.trx").write_text(
                    f"<TestRun><ResultSummary><Counters {xml}/></ResultSummary></TestRun>"
                )
            return subprocess.CompletedProcess(args, 0)

        good = 'executed="2" passed="2"'
        for reports, code in (
            ([], 1),
            ([good], 0),
            ([good, 'executed="1" passed="0"'], 1),
            (['executed="2" passed="1" failed="1"'], 1),
        ):
            with patch("nightweaver.suite_runner.subprocess.run", side_effect=report):
                self.assertEqual(
                    suite_runner.dotnet_suite("dotnet", "test.csproj"), code
                )


@unittest.skipUnless(os.name == "nt", "Windows controller")
class OneShotTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = operator.OperatorRelease.cli
    configured = fixtures.WindowsRunTest.configured

    def commit(self, files):
        for name, data in files.items():
            file = self.repo / name
            file.parent.mkdir(exist_ok=True, parents=True)
            file.write_text(data)
            git(self.repo, "add", name)
        git(self.repo, "commit", "-m", "Add committed verifier inputs")

    def test_preview_discovers_committed_suites_and_protects_tests_only(self):
        self.commit(
            {
                "tests/test_a.py": "import unittest\nclass TestA(unittest.TestCase):\n def test_a(self): pass",
                "tests/sub/test_b.py": "import unittest",
                "tests/pkg/__init__.py": "",
                "tests/pkg/test_c.py": "import unittest",
                "suite.test.cjs": "require('node:test')",
                "src/test.csproj": "<Project><PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup></Project>",
                "src/tests.cs": "",
                "src/product/prod.csproj": "<Project/>",
                "src/product/prod.cs": "",
            }
        )
        (self.repo / "tests/test_a.py").write_text("import pytest")
        with patch(
            "nightweaver.cli.invoke", side_effect=AssertionError("unexpected call")
        ):
            plan = self.cli(
                "oneshot", self.repo, "Build gallery", "--preview", "--rescue", "claude"
            )
        self.assertEqual(
            {g["name"] for g in plan["gates"]},
            {
                "Python unittest tests",
                "Python unittest tests/sub",
                "Node tests",
                ".NET src/test.csproj",
            },
        )
        self.assertIn("src/tests.cs", plan["protected_paths"])
        self.assertNotIn("src/product/prod.cs", plan["protected_paths"])
        self.assertEqual(plan["expected_base"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.store.runs(), [])
        self.assertIn(
            "moved since preview",
            self.cli(
                "oneshot",
                self.repo,
                "Build",
                "--expected-base",
                self.head,
                "--enqueue",
                code=1,
            ),
        )

    def test_pytest_conventions_and_unsupported_repositories(self):
        self.assertIn(
            "No supported", self.cli("oneshot", self.repo, "Build", "--preview", code=1)
        )
        self.assertTrue(
            discovery.needs_pytest(
                "class TestThing:\n def test_example(self): pass", "test_t.py"
            )
        )
        self.commit({"tests/layout_test.py": "def test_layout(): pass"})
        self.assertEqual(
            self.cli("oneshot", self.repo, "Build", "--preview")["gates"][0]["name"],
            "Python pytest",
        )
        self.commit({"package.json": '{"scripts":{"test":"echo no test specified"}}'})
        self.assertIn(
            "placeholder", self.cli("oneshot", self.repo, "Build", "--preview", code=1)
        )

    def test_oneshot_planner_and_worker_share_selected_profile(self):
        self.commit(
            {
                "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n def test_it(self): pass"
            }
        )
        with patch(
            "nightweaver.cli.invoke",
            return_value=(0, None, '{"milestones":[{"title":"One","task":"Build"}]}'),
        ) as planner:
            result = self.cli(
                "oneshot",
                self.repo,
                "Build",
                "--worker",
                "claude",
                "--profile-dir",
                self.root,
                "--model",
                "sonnet",
                "--rescue",
                "codex",
                "--enqueue",
            )
        worker = self.store.get(result["run_id"])["spec"]["milestones"][0]["worker"]
        self.assertEqual(planner.call_args.args[0], worker)
        self.assertEqual(Path(worker["profile_dir"]), self.root.resolve())
        with patch(
            "nightweaver.cli.invoke", side_effect=AssertionError("unexpected call")
        ):
            direct = self.cli("oneshot", self.repo, "Fix it", "--direct", "--enqueue")
        self.assertEqual(
            self.store.get(direct["run_id"])["spec"]["milestones"][0]["task"], "Fix it"
        )

    def test_failed_checks_handoff_is_durable_and_next_milestone_starts(self):
        example = str(
            Path(__file__).resolve().parents[1] / "examples" / "compare_repair.py"
        )

        def route(name):
            return {
                "kind": "command",
                "argv": [sys.executable, example, "worker", name],
            }

        gates = [
            {"name": k, "argv": [sys.executable, example, "check", k]}
            for k in ("search", "restore")
        ]
        rid = self.configured(
            route("primary")["argv"],
            worker_fallbacks=[route("primary"), route("alternate")],
            attempts=2,
            gates=gates,
            repair={"collect_failures": True, "switch_after": 1},
            retry={"initial_seconds": 1, "max_seconds": 1, "repeated_failure_limit": 1},
            milestones=[
                {"title": "Fix", "task": "Repair both"},
                {"title": "Deliver", "task": "Document", "worker": route("delivery")},
            ],
        )
        self.assertEqual(Run(self.store, rid).run(defer_retries=True), "waiting")
        state = self.store.get(rid)["state"]
        self.assertEqual(state["worker_route"], 2)
        self.assertEqual(state["attempts_used"], 1)
        history = self.cli("repair-history", rid)
        row = history["rejections"][0]
        self.assertEqual(
            (row["step"], row["attempt"], row["changed_paths"], row["failed_gates"]),
            (1, 1, ["result.txt"], ["search", "restore"]),
        )
        self.assertEqual(history["handoffs"][0]["tree"], row["tree"])
        self.assertEqual(len(row["tree"]), 40)
        self.assertEqual(Run(self.store, rid).run(), "done")
        numbers = metrics(self.store, rid)
        self.assertEqual(
            (
                numbers["provider_calls"],
                numbers["accepted_milestones"],
                numbers["accepted_after_handoff"],
            ),
            (3, 2, 1),
        )
        self.assertTrue(self.cli("replay-checks", rid, "--through")["passed"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)

    def test_budget_and_last_attempt_do_not_create_phantom_work(self):
        rid = self.configured(
            fixtures.fixture("bad"),
            attempts=3,
            budgets={"max_calls": 1},
            retry={"initial_seconds": 1},
        )
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertEqual(metrics(self.store, rid)["worker_attempts"], 1)
        last = self.configured(
            fixtures.fixture("bad"),
            attempts=1,
            retry={"initial_seconds": 300, "max_seconds": 300},
        )
        self.assertEqual(Run(self.store, last).run(defer_retries=True), "blocked")
        self.assertIsNone(self.store.get(last)["state"]["retry_at"])

    def test_fresh_session_and_infrastructure_boundaries(self):
        sessions = []

        def worker(config, role, path, log, prompt, timeout, paused, session, **kwargs):
            sessions.append(session)
            (path / "result.txt").write_text("ready" if len(sessions) > 1 else "bad")
            return 0, "original-session", ""

        rid = self.configured(
            fixtures.fixture("good"),
            repair={"fresh_session_after": 1},
            retry={"initial_seconds": 1},
        )
        with patch("nightweaver.engine.invoke", side_effect=worker):
            self.assertEqual(Run(self.store, rid).run(), "done")
        self.assertEqual(sessions, [None, None])
        rid = self.configured(
            fixtures.fixture("good"),
            repair={"collect_failures": True, "switch_after": 1},
            worker_fallbacks=[{"kind": "command", "argv": fixtures.fixture("bad")}],
            gates=[
                {
                    "name": "missing",
                    "argv": [sys.executable, "-c", "pass"],
                    "junit": {"min_passed": 1},
                },
                {"name": "later", "argv": fixtures.fixture("gate")},
            ],
        )
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        events = self.store.events(rid, limit=None, kinds=["gate_started"])
        self.assertEqual([e["details"]["name"] for e in events], ["missing"])
        self.assertFalse(repair_history(self.store, rid)["handoffs"])

    def test_reviewer_failover_preserves_worker_session(self):
        sessions, reviews = [], []

        def invoke(config, role, path, log, prompt, timeout, paused, session, **kwargs):
            if role == "worker":
                sessions.append(session)
                (path / "result.txt").write_text("ready")
                return 0, "worker-thread", ""
            reviews.append(config["kind"])
            if len(reviews) == 1:
                raise ProviderUnavailable("review service unavailable")
            verdict = "REVISE" if len(reviews) == 2 else "PASS"
            return (
                0,
                None,
                json.dumps({"verdict": verdict, "reason": "Inspect the edit"}),
            )

        rid = self.configured(
            fixtures.fixture("good"),
            reviewer={"kind": "claude"},
            reviewer_fallbacks=[{"kind": "codex"}],
            retry={"initial_seconds": 1},
        )
        with patch("nightweaver.engine.invoke", side_effect=invoke):
            self.assertEqual(Run(self.store, rid).run(), "done")
        self.assertEqual(sessions, [None, "worker-thread"])
        self.assertEqual(reviews, ["claude", "codex", "codex"])

    def test_handoff_and_history_for_previous_runs(self):
        rid = self.configured(
            fixtures.fixture("good"),
            milestones=[
                {"title": "A", "task": "first"},
                {"title": "B", "task": "next"},
            ],
        )
        self.store.transition(rid, "worker_started", {"attempt": 1})
        self.store.transition(
            rid, "attempt_rejected", {"phase": "check", "issue": "broken"}
        )
        self.store.transition(rid, "gate_finished", {"step": 1, "name": "previous"})
        self.store.transition(
            rid, "milestone_accepted", {"step": 1, "commit": self.head}, step=1
        )
        self.assertIsNone(self.cli("handoff", rid)["last_check"])
        row = self.cli("repair-history", rid)["rejections"][0]
        self.assertEqual((row["step"], row["attempt"]), (1, 1))

    def test_interrupted_worker_keeps_previous_failure_in_prompt(self):
        rid = self.configured(fixtures.fixture("good"))
        run = Run(self.store, rid)
        self.store.transition(
            rid,
            "worker_started",
            {"attempt": 1},
            phase="working",
            attempt=1,
            attempts_used=1,
            log=str(self.root / "interrupted.log"),
            last_error="Restore failed on the mobile reel",
        )
        calls = []

        def worker(config, role, path, log, prompt, *args, **kwargs):
            calls.append(prompt)
            (path / "result.txt").write_text("ready")
            return 0, None, ""

        with patch("nightweaver.engine.invoke", side_effect=worker):
            self.assertEqual(run.run(), "done")
        self.assertIn(
            "Previous diagnostic: Restore failed on the mobile reel", calls[0]
        )
