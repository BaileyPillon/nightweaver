"""0.19.1: a check that times out for good is unscored, never a failure. A judge's ledger gives its checks the same doubled-timeout retry as a
verification, and one that still times out blocks the judgment; a design direction or a rescue strategy whose checks time out is recorded as
unscored and left out of the contest, and a contest in which every candidate is unscored blocks instead of falling back as if all had failed."""

from math import inf
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_release_019_explore as explore_tests
import test_release_019_pursuit as pursuit_tests
import test_release_019_rescue as rescue_tests
import test_windows as fixtures
from test_release_0191_runtime import timeout_receipt

from nightweaver import engine, explore, rescue
from nightweaver.checks import observed_pass
from nightweaver.engine import Run
from nightweaver.reports import metrics

REAL = engine.run_gate
LIMIT = 10  # seconds the plan gives its check; the retry doubles it. No test waits: the runs that "time out" are faked.
GATES = [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": LIMIT}]
ADVICE = ("'read fixture value'", f"timeout of {LIMIT} s", f"again at {2 * LIMIT} s", '"timeout"', "in the plan", "as a new run", "--retry")


def ends_at_limit(where, seen, until=inf):
    """A run_gate that ends a check at its time limit when it runs in a checkout whose folder name starts with `where`, unless it is given
    `until` seconds or more. `seen` collects (folder name, timeout) for those runs; every other check really runs."""
    def fake(check, path, folder, snapshot, **options):
        if Path(path).name.startswith(where) and check["timeout"] < until:
            seen.append((Path(path).name, check["timeout"]))
            return timeout_receipt(check, folder, snapshot)
        return REAL(check, path, folder, snapshot, **options)
    return fake


@unittest.skipUnless(os.name == "nt", "Windows controller")
class LedgerTimeouts(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    script, route, prompts, pursue, events = (getattr(pursuit_tests.PursuitTests, name) for name in ("script", "route", "prompts", "pursue", "events"))

    def met(self, **extra):
        return self.pursue([pursuit_tests.judged(95, met=True)], gates=GATES, extra_pursue={"confirm": False}, **extra)

    def test_a_ledger_check_that_times_out_once_is_run_again_with_double_the_time(self):
        run, seen = self.met(), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("judge-", seen, until=2 * LIMIT)):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen, [("judge-01", LIMIT)], "the second run has the time it needed")
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["name"], retried["old_timeout"], retried["new_timeout"], retried["wave"], retried["ledger_index"]),
                         ("read fixture value", LIMIT, 2 * LIMIT, 1, 1))
        (finished,) = self.events(run, "judge_gate_finished")
        self.assertEqual((observed_pass(finished), finished["timeout_retried"]), (True, {"limits": [LIMIT, 2 * LIMIT]}))
        self.assertNotEqual(retried["log"], finished["log"], "each run of the check keeps its own evidence")
        (judged,) = self.events(run, "goal_judged")
        self.assertEqual(judged["ledger"], [{"name": "read fixture value", "passed": True}])

    def test_a_ledger_check_that_times_out_twice_is_unscored_and_blocks_the_judgment(self):
        run, seen = self.met(), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("judge-", seen)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(seen, [("judge-01", LIMIT), ("judge-01", 2 * LIMIT)])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["wave"]), ("blocked", "judging", 1))
        for text in ADVICE:
            self.assertIn(text, state["last_error"])
        (blocked,) = self.events(run, "blocked")
        self.assertEqual((blocked["during"], blocked["issue"]), ("judging", state["last_error"]))
        (finished,) = self.events(run, "judge_gate_finished")
        self.assertIsNone(observed_pass(finished), "the check scored nothing")
        self.assertTrue(finished["timeout_retried"]["timed_out"])
        self.assertEqual(len(self.events(run, "gate_timeout_retried")), 1)
        # Nothing was judged and nothing was planned: no model call, no judgment, no "Check failed" need for the planner.
        for kind in ("judge_ledger_ready", "judge_started", "goal_judged", "wave_planned", "planner_started"):
            self.assertEqual(self.events(run, kind), [], kind)
        self.assertFalse((self.root / "judge.prompts").exists() or (self.root / "planner.prompts").exists())
        self.assertEqual(state.get("pursuit_failures") or [], [])
        self.assertEqual(metrics(self.store, run)["outcome"], "execution_blocked")

    def test_a_blocked_judgment_is_judged_when_the_run_resumes(self):
        run = self.met()
        with patch("nightweaver.engine.run_gate", ends_at_limit("judge-", [])):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(Run(self.store, run).run(retry=True), "done", "the machine was only busy: the checks run again and the build is judged")
        (judged,) = self.events(run, "goal_judged")
        self.assertEqual(judged["ledger"], [{"name": "read fixture value", "passed": True}])
        self.assertEqual(self.events(run, "wave_planned"), [])

    def test_a_plan_without_retries_makes_the_first_timeout_unscored_with_the_same_advice(self):
        run, seen = self.met(retry={"gate_timeout_retries": 0}), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("judge-", seen)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(seen, [("judge-01", LIMIT)])
        state = self.store.get(run)["state"]
        self.assertEqual(state["blocked_from"], "judging")
        for text in ("'read fixture value'", f"timeout of {LIMIT} s", "allows no retry", "as a new run", "--retry"):
            self.assertIn(text, state["last_error"])
        self.assertEqual((self.events(run, "gate_timeout_retried"), self.events(run, "goal_judged")), ([], []))

    def test_a_ledger_check_that_fails_is_still_a_failure_not_unscored(self):
        gates = [{"name": "never", "argv": pursuit_tests.helper("never_judge", 2), "timeout": LIMIT}]
        run = self.pursue([pursuit_tests.judged(95, met=True)], [pursuit_tests.plan("Second")], gates=gates, extra_pursue={"confirm": False})
        self.assertEqual(Run(self.store, run).run(), "done")
        first, _ = self.events(run, "goal_judged")
        self.assertEqual([(r["name"], r["passed"]) for r in first["ledger"]], [("never", False)])
        self.assertIn("Check failed: never", self.prompts("planner")[0])


@unittest.skipUnless(os.name == "nt", "Windows controller")
class UnscoredDirections(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    script, sequence, table, builder, prompts, workers, explored, events, kinds = (
        getattr(explore_tests.ExploreTests, name) for name in ("script", "sequence", "table", "builder", "prompts", "workers", "explored", "events", "kinds"))

    def test_every_direction_unscored_blocks_the_exploration_instead_of_failing_it(self):
        run, seen = self.explored(gates=GATES), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("explore-", seen)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(sorted(set(seen)), [(f"explore-0{n}", t) for n in (1, 2, 3) for t in (LIMIT, 2 * LIMIT)])
        unscored = self.events(run, "direction_unscored")
        self.assertEqual([(d["index"], d["passed"], d["status"], d["failed_checks"]) for d in unscored], [(i, None, "unscored", []) for i in (1, 2, 3)])
        for text in ADVICE:
            self.assertIn(text, unscored[0]["reason"])
        for kind in ("direction_built", "direction_failed", "direction_compared", "exploration_failed", "direction_chosen"):
            self.assertEqual(self.events(run, kind), [], kind)
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"]), ("blocked", "exploring"))
        self.assertIn("All 3 directions ran out of time in their checks, so none could be scored or compared", state["last_error"])
        for text in ADVICE:
            self.assertIn(text, state["last_error"])
        self.assertEqual((state["exploration"]["stage"], state["exploration"]["builds"]), ("build", []), "--retry builds the directions again")
        self.assertEqual(len(self.workers()), 3)
        self.assertFalse(self.root.joinpath("table.prompts").exists(), "no judge call compared anything")
        self.assertEqual(metrics(self.store, run)["exploration"]["unscored"], 3)
        self.assertEqual(list((self.store.directory(run) / "worktrees").glob("explore-*")), [])

    def test_a_blocked_exploration_builds_its_directions_again_on_retry(self):
        run = self.explored(gates=GATES)
        with patch("nightweaver.engine.run_gate", ends_at_limit("explore-", [])):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual([b["index"] for b in self.events(run, "direction_built")], [1, 2, 3])
        self.assertEqual(len(self.events(run, "direction_unscored")), 3)
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["winner"], chosen["passed"], chosen["directions"]), (2, 3, 3))
        self.assertEqual(len(self.workers()), 7, "three builds, three rebuilds and the second milestone")

    def test_an_unscored_direction_is_left_out_of_the_tournament_and_counted_apart(self):
        run = self.explored(gates=GATES)
        with patch("nightweaver.engine.run_gate", ends_at_limit("explore-02", [])):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([b["index"] for b in self.events(run, "direction_built")], [1, 3])
        self.assertEqual([d["index"] for d in self.events(run, "direction_unscored")], [2])
        self.assertEqual(self.events(run, "direction_failed"), [])
        compared = self.events(run, "direction_compared")
        self.assertEqual([tuple(c["order"]) for c in compared], [(1, 3), (3, 1)], "the unscored direction met no one")
        self.assertNotIn("Soft Pastel", "".join(self.prompts("table")[:2]))
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["winner"], chosen["passed"], chosen["unscored"], chosen["directions"]), (3, 2, 1, 3))
        self.assertEqual([r["index"] for r in chosen["ranking"]], [3, 1])
        data = metrics(self.store, run)["exploration"]
        self.assertEqual((data["built"], data["failed"], data["unscored"]), (2, 0, 1))
        report = explore.report(self.store, run)
        self.assertEqual([(r["index"], r["passed"], r["unscored"]) for r in report["directions"]], [(1, True, False), (2, None, True), (3, True, False)])
        lines = "\n".join(explore.format_lines(report))
        self.assertIn("2. Soft Pastel", lines)
        self.assertIn("unscored: its checks ran out of time", lines)
        self.assertNotIn("not built yet", lines)

    def test_unscored_and_failed_directions_together_still_fall_back_to_the_milestone(self):
        run = self.explored(gates=GATES, explore={"workers": [self.builder("bad", failing="Brutalist,Neon Terminal", log="explorer")]})
        with patch("nightweaver.engine.run_gate", ends_at_limit("explore-02", [])):
            self.assertEqual(Run(self.store, run).run(), "done")
        (failed,) = self.events(run, "exploration_failed")
        self.assertEqual([(d["index"], d.get("status")) for d in failed["directions"]], [(1, None), (2, "unscored"), (3, None)])
        self.assertEqual((len(self.events(run, "direction_failed")), len(self.events(run, "direction_unscored"))), (2, 1))


@unittest.skipUnless(os.name == "nt", "Windows controller")
class UnscoredStrategies(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    script, sequence, table, builder, prompts, asked, stalled, events, kinds, workers = (
        getattr(rescue_tests.RescueTests, name) for name in ("script", "sequence", "table", "builder", "prompts", "asked", "stalled", "events", "kinds", "workers"))

    def compare_calls(self):
        return self.asked("table", rescue_tests.COMPARE)

    def test_every_strategy_unscored_blocks_the_rescue_unspent_instead_of_ending_the_pursuit(self):
        run, seen = self.stalled(gates=GATES), []
        with patch("nightweaver.engine.run_gate", ends_at_limit(("rescue-01", "rescue-02"), seen)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(sorted(set(seen)), [(f"rescue-0{n}", t) for n in (1, 2) for t in (LIMIT, 2 * LIMIT)])
        unscored = self.events(run, "rescue_candidate_unscored")
        self.assertEqual([(d["index"], d["passed"], d["status"], d["failed_checks"]) for d in unscored], [(1, None, "unscored", []), (2, None, "unscored", [])])
        for text in ADVICE:
            self.assertIn(text, unscored[0]["reason"])
        for kind in ("rescue_candidate_built", "rescue_candidate_failed", "rescue_compared", "rescue_failed", "rescue_chosen", "pursuit_stopped"):
            self.assertEqual(self.events(run, kind), [], kind)
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state.get("rescues_used", 0)), ("blocked", "rescuing", 0))
        self.assertIn("All 2 strategies ran out of time in their checks, so none could be scored or compared", state["last_error"])
        for text in ADVICE:
            self.assertIn(text, state["last_error"])
        self.assertEqual((state["rescue"]["stage"], state["rescue"]["builds"]), ("build", []))
        self.assertEqual(self.compare_calls(), [], "no judge call compared anything")
        self.assertEqual(list((self.store.directory(run) / "worktrees").glob("rescue-*")), [])

    def test_a_blocked_rescue_builds_its_strategies_again_on_retry(self):
        run = self.stalled(gates=GATES)
        with patch("nightweaver.engine.run_gate", ends_at_limit(("rescue-01", "rescue-02"), [])):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual([b["index"] for b in self.events(run, "rescue_candidate_built")], [1, 2])
        self.assertEqual(len(self.events(run, "rescue_candidate_unscored")), 2)
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual((chosen["winner"], chosen["passed"], chosen["strategies"]), (1, 2, 2))

    def test_an_unscored_strategy_is_left_out_of_the_comparison_and_counted_apart(self):
        names = rescue_tests.NAMES
        run = self.stalled(gates=GATES, judge=self.table(order=[names[1], rescue_tests.INCUMBENT, names[0]]))
        with patch("nightweaver.engine.run_gate", ends_at_limit("rescue-01", [])):
            self.assertEqual(Run(self.store, run).run(), "done")
        (unscored,) = self.events(run, "rescue_candidate_unscored")
        self.assertEqual((unscored["index"], unscored["name"], unscored["passed"]), (1, names[0], None))
        self.assertEqual([b["index"] for b in self.events(run, "rescue_candidate_built")], [2])
        compared = self.events(run, "rescue_compared")
        self.assertEqual([tuple(c["order"]) for c in compared], [(0, 2), (2, 0)], "the unscored strategy met no one")
        for prompt in self.compare_calls():
            self.assertNotIn(names[0], prompt)
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual((chosen["winner"], chosen["name"], chosen["passed"], chosen["unscored"], chosen["strategies"]), (2, names[1], 1, 1, 2))
        (data,) = rescue.report(self.store, run)
        self.assertEqual([(r["index"], r["passed"], r["unscored"]) for r in data["strategies"]], [(1, None, True), (2, True, False)])
        lines = "\n".join(rescue.format_lines(data))
        self.assertIn("1 unscored: their checks ran out of time", lines)
        self.assertIn("unscored: its checks ran out of time", lines)
        self.assertNotIn("not built yet", lines)
        summary = metrics(self.store, run)["rescue"]
        self.assertEqual((summary["unscored_strategies"], summary["failed"]), (1, 0))

    def test_a_best_build_check_that_times_out_twice_blocks_the_rescue_in_rescuing(self):
        run, seen = self.stalled(gates=GATES), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("rescue-00", seen)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(seen, [("rescue-00", LIMIT), ("rescue-00", 2 * LIMIT)])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state.get("rescues_used", 0)), ("blocked", "rescuing", 0))
        for text in ADVICE:
            self.assertIn(text, state["last_error"])
        self.assertEqual(self.events(run, "rescue_incumbent_checked"), [], "the best build was not checked, so nothing was compared")
        self.assertEqual((len(self.events(run, "rescue_candidate_built")), self.compare_calls()), (2, []), "the strategies were built and are kept")
        self.assertIsNone(observed_pass(self.events(run, "judge_gate_finished")[-1]), "the check that ran out of time scored nothing")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(len(self.events(run, "rescue_candidate_built")), 2, "the strategies were not built again")
        self.assertEqual(len(self.events(run, "rescue_incumbent_checked")), 1)

    def test_a_best_build_check_that_times_out_once_is_run_again(self):
        run, seen = self.stalled(gates=GATES), []
        with patch("nightweaver.engine.run_gate", ends_at_limit("rescue-00", seen, until=2 * LIMIT)):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen, [("rescue-00", LIMIT)])
        (retried,) = [e for e in self.events(run, "gate_timeout_retried") if e.get("ledger_index")]
        self.assertEqual((retried["old_timeout"], retried["new_timeout"]), (LIMIT, 2 * LIMIT))
        self.assertEqual(self.events(run, "rescue_incumbent_checked")[0]["failed"], [])


if __name__ == "__main__":
    unittest.main()
