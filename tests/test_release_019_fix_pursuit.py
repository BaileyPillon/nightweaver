"""Fixes for the review of goal pursuit: judge anchoring, MET confirmation, keep-best, crash-consistent reverts, scoped waves and stops."""

import inspect
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_release_019_pursuit as base
import test_windows as fixtures

from nightweaver import explore, pursuit, readiness, report_activity, rescue
from nightweaver.contracts import lint_plan
from nightweaver.engine import Run, call_served, fork_run, git
from nightweaver.providers import ProviderUnavailable, invoke
from nightweaver.reports import metrics
from nightweaver.spec import anchor_provider_paths, validate
from nightweaver.store import current_stop

helper, judged, plan = base.helper, base.judged, base.plan

TWO_SHOTS = r"""
import os, pathlib
for name in ("one", "two"):
    pathlib.Path(os.environ["NIGHTWEAVER_EVIDENCE"], name + ".png").write_bytes(b"\x89PNG " + name.encode())
"""
# A screenshot named after the newest milestone in the build, so the page of one build is told apart from another's.
PAGE_SHOT = r"""
import os, pathlib
lines = pathlib.Path("lineage.txt").read_text(encoding="utf-8").splitlines()
name = lines[-1].lower().replace(" ", "-")
pathlib.Path(os.environ["NIGHTWEAVER_EVIDENCE"], name + ".png").write_bytes(b"\x89PNG " + name.encode())
"""
# Passes while a milestone is built, fails on the judge's checkout once the build holds two milestones.
FAILS_ON_LONG_BUILD = r"""
import pathlib, sys
here = pathlib.Path.cwd()
sys.exit(1 if here.name.startswith("judge-") and len(here.joinpath("lineage.txt").read_text(encoding="utf-8").splitlines()) >= 2 else 0)
"""
LOCAL_GATE = r"""
import pathlib, sys
sys.exit(1 if len(pathlib.Path("lineage.txt").read_text(encoding="utf-8").splitlines()) >= 2 else 0)
"""
# A worker that edits only the folder its milestone names: web for a title with UI in it, api otherwise.
SCOPED_WORKER = r"""
import pathlib, re, sys
prompt = sys.stdin.read()
title = re.search(r"^Milestone \d+/\d+: (.*)$", prompt, re.M).group(1)
folder = "web" if "UI" in title else "api"
pathlib.Path(folder).mkdir(exist_ok=True)
pathlib.Path(folder, re.sub(r"\W+", "-", title).strip("-") + ".txt").write_text(title, encoding="utf-8")
"""


def code_gate(name, code):
    return {"name": name, "argv": [sys.executable, "-c", code], "timeout": 20}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class FixPursuitTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    cli = base.PursuitTests.cli
    script = base.PursuitTests.script
    route = base.PursuitTests.route
    prompts = base.PursuitTests.prompts
    events = base.PursuitTests.events
    lineage = base.PursuitTests.lineage

    def pursue(self, judge, planner=None, extra_pursue=None, **extra):
        # The older behaviour is the baseline: a test that is about confirmation asks for it.
        return base.PursuitTests.pursue(self, judge, planner, {"confirm": False, **(extra_pursue or {})}, **extra)

    def worker_prompts(self):
        return [part for part in self.worker_log.read_text(encoding="utf-8").split("\n=====\n") if part.strip()]

    def ready(self):
        return {"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20}

    def interrupted_at_wave(self, run, wave):
        """Run until the judge is about to judge `wave`, then stop the runner as a crash would."""
        real = pursuit.judge

        def stop(run_, state):
            if state.get("wave", 1) == wave:
                raise SystemExit
            return real(run_, state)

        with patch("nightweaver.pursuit.judge", side_effect=stop), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        return self.store.get(run)["state"]

    # ---------------------------------------------------------------- owner issue A: the judge is never told the target

    def test_prompts_never_reveal_the_target_score(self):
        run = self.pursue([judged(60), judged(95, met=True)], [plan("Round two")], extra_pursue={"target_score": 93, "confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        pattern = re.compile(r"(?<![\w.\\/-])93(?!\w)")
        every = [*self.prompts("judge"), *self.prompts("planner"), *self.worker_prompts()]
        self.assertEqual(len(self.prompts("judge")), 3, "the confirmation prompt is checked too")
        for text in every:
            self.assertIsNone(pattern.search(text), text[:300])
            self.assertNotIn("target_score", text)
        for text in self.prompts("judge"):
            for scale in ("95-100 exceptional, nothing a demanding owner would change", "85-94 finished, only polish-level gaps",
                          "70-84 solid with visible gaps", "50-69 partial", "below 50 major gaps"):
                self.assertIn(scale, text)
            self.assertIn("Verdict MET only if every criterion is fully met", text)
            self.assertIn("at most 30", text)
            self.assertIn("at most 60", text)
            self.assertNotIn("would accept it as finished", text)
        self.assertEqual(self.store.get(run)["spec"]["pursue"]["target_score"], 93, "the target stays a threshold the controller applies")

    def test_rescue_and_exploration_prompts_have_no_target_to_reveal(self):
        for module in (rescue, explore):
            self.assertNotIn("target_score", inspect.getsource(module), module.__name__)

    # ---------------------------------------------------------------- owner issue B: a MET must be confirmed

    def test_met_is_confirmed_by_an_independent_judgment_with_reversed_screenshots(self):
        gates = [self.ready(), {**code_gate("two screenshots", TWO_SHOTS), "artifacts": []}]
        run = self.pursue([judged(90, met=True)], gates=gates, extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        first, second = self.prompts("judge")
        shots = [re.findall(r"^- (\S.*?\.png)", text, re.M) for text in (first, second)]
        self.assertEqual(len(shots[0]), 2)
        self.assertEqual(shots[1], shots[0][::-1], "the confirmation sees the same images in the opposite order")
        self.assertIn("None; this is the first.", second, "the confirmation never sees the first verdict")
        self.assertNotIn("Wave 1: 90/100", second)
        kinds = [e["kind"] for e in self.store.events(run, limit=None)]
        self.assertEqual((kinds.count("goal_confirming"), kinds.count("goal_judged"), kinds.count("goal_met"), kinds.count("met_unconfirmed")), (1, 1, 1, 0))
        self.assertLess(kinds.index("goal_confirming"), kinds.index("goal_judged"))
        judgment = self.events(run, "goal_judged")[0]
        self.assertEqual((judgment["verdict"], judgment["score"]), ("MET", 90))
        self.assertEqual((judgment["confirmation"]["confirmed"], judgment["confirmation"]["confirmation"]["score"]), (True, 90))
        self.assertEqual([e.get("stage") for e in self.events(run, "judge_started")], [None, "confirm"])
        self.assertEqual(self.events(run, "judge_gate_finished")[0]["wave"], 1)
        self.assertEqual(len(self.events(run, "judge_gate_finished")), 2, "the ledger ran once for both judgments")
        logs = sorted(p.name for p in (self.store.directory(run) / "evidence" / "judge" / "01").rglob("*.jsonl"))
        self.assertEqual([name.split("-")[0] for name in logs], ["confirm", "judge"])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["judge_stage"], state["provider_calls"]), ("done", None, 3))
        self.assertEqual(metrics(self.store, run)["pursuit"]["confirmations"], 1)
        self.assertIn("confirmed by an independent second judgment", self.cli("pursuit", run, raw=True))
        self.assertEqual(self.cli("pursuit", run, "--json")["confirmations"], {"asked": 1, "unconfirmed": 0, "skipped": 0})

    def test_unconfirmed_met_continues_as_not_met_with_the_gaps_of_both_judgments(self):
        first = {**judged(92, met=True), "gaps": [{"title": "Polish A", "detail": "Spacing is off"}]}
        second = judged(80, gaps=[{"title": "Gap B", "detail": "Mobile layout breaks"}])
        run = self.pursue([first, second, judged(90, met=True), judged(90, met=True)], [plan("Fix both")], extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        open_ = self.events(run, "goal_judged")[0]
        self.assertEqual((open_["verdict"], open_["score"]), ("NOT_MET", 80), "scored by the lower of the two judgments")
        self.assertEqual([g["title"] for g in open_["gaps"]], ["Polish A", "Gap B"])
        self.assertFalse(open_["confirmation"]["confirmed"])
        record = self.events(run, "met_unconfirmed")
        self.assertEqual(len(record), 1)
        self.assertEqual((record[0]["first"]["score"], record[0]["first"]["verdict"]), (92, "MET"))
        self.assertEqual((record[0]["confirmation"]["score"], record[0]["confirmation"]["verdict"]), (80, "NOT_MET"))
        planner = self.prompts("planner")[0]
        self.assertIn("Polish A", planner)
        self.assertIn("Gap B", planner)
        row = self.store.get(run)["state"]
        self.assertEqual(row["judgments"][0], {"wave": 1, "score": 80, "verdict": "NOT_MET", "commit": open_["commit"]})
        self.assertEqual(len(self.prompts("judge")), 4)
        self.assertEqual(len(self.events(run, "goal_met")), 1)
        measured = metrics(self.store, run)["pursuit"]
        self.assertEqual((measured["confirmations"], measured["met_unconfirmed"], measured["judgments"]), (2, 1, 2))
        self.assertIn("MET not confirmed", self.cli("pursuit", run, raw=True))
        self.assertIn("not confirmed", Path(self.cli("report", run)["report"]).read_text(encoding="utf-8"))

    def test_an_unconfirmed_judgment_counts_toward_a_plateau_and_keeps_the_lower_score(self):
        run = self.pursue([judged(80), judged(92, met=True), judged(78)], [plan("More")],
                          extra_pursue={"confirm": True, "plateau": 1, "max_waves": 5, "rescue": {"max_rescues": 0}})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        first, second = self.events(run, "goal_judged")
        self.assertEqual((second["verdict"], second["score"]), ("NOT_MET", 78), "the build is scored by the lower of its two judgments")
        self.assertEqual((state["best_score"], state["best_commit"], state["stale_judgments"]), (80, first["commit"], 1))
        self.assertEqual(len(self.events(run, "met_unconfirmed")), 1)
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau")
        self.assertEqual(self.events(run, "pursuit_reverted")[0]["to"], first["commit"])

    def test_confirm_margin_sets_how_far_below_the_target_a_confirmation_may_score(self):
        for margin, confirmation, outcome in ((3, 87, "done"), (3, 86, "blocked"), (0, 89, "blocked"), (0, 90, "done")):
            with self.subTest(margin=margin, confirmation=confirmation):
                for name in ("judge", "planner"):
                    self.root.joinpath(f"{name}.pos").unlink(missing_ok=True)
                run = self.pursue([judged(95, met=True), judged(confirmation, met=True)], [plan("More")],
                                  extra_pursue={"confirm": True, "target_score": 90, "confirm_margin": margin, "max_waves": 1})
                self.assertEqual(Run(self.store, run).run(), outcome)
                self.assertEqual(len(self.events(run, "met_unconfirmed")), 0 if outcome == "done" else 1)

    def test_confirmation_off_judges_once(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": False})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.prompts("judge")), 1)
        self.assertEqual(self.events(run, "goal_confirming"), [])
        self.assertNotIn("confirmation", self.events(run, "goal_judged")[0])

    def test_a_judgment_that_does_not_finish_the_run_is_not_confirmed(self):
        gates = [self.ready(), {"name": "finished build keeps two milestones", "argv": helper("never_judge", 2), "timeout": 20}]
        run = self.pursue([judged(95, met=True), judged(60)], [plan("More")], gates=gates, extra_pursue={"confirm": True, "max_waves": 1})
        Run(self.store, run).run()
        self.assertEqual([c["passed"] for c in self.events(run, "goal_judged")[0]["ledger"]], [True, False])
        self.assertEqual(self.events(run, "goal_confirming"), [], "a failed ledger check already stops MET, so nothing needs confirming")

    def test_crash_after_the_first_judgment_resumes_at_the_confirmation(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True})
        real, seen = Run.call_provider, []

        def die_on_confirmation(self_, config, role, *args, **kwargs):
            seen.append(role)
            if role == "judge" and seen.count("judge") == 2:
                raise SystemExit
            return real(self_, config, role, *args, **kwargs)

        with patch.object(Run, "call_provider", die_on_confirmation), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["judge_stage"]["stage"]), ("judging", "confirm"))
        self.assertEqual(len(self.prompts("judge")), 1)
        self.assertEqual(self.events(run, "goal_judged"), [])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.prompts("judge")), 2, "the resumed run asks for the confirmation, not a second first judgment")
        self.assertEqual(len(self.events(run, "judge_ledger_ready")), 1)
        self.assertEqual(len(self.events(run, "goal_confirming")), 1)
        self.assertIsNone(self.store.get(run)["state"]["judge_stage"])

    def test_an_unreadable_confirmation_blocks_and_a_retry_confirms_without_a_new_ledger(self):
        run = self.pursue([judged(90, met=True), "not json", "still not json", judged(90, met=True)], extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], state["judge_attempts"], state["judge_stage"]["stage"]), ("judging", 2, "confirm"))
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(len(self.prompts("judge")), 4)
        self.assertEqual(len(self.events(run, "judge_ledger_ready")), 1, "the confirmation judges the evidence the first judgment saw")
        self.assertEqual(len(self.events(run, "goal_met")), 1)

    def test_confirm_judge_routes_the_confirmation_to_its_own_provider(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": self.route("confirmer", [judged(88, met=True)])})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual((len(self.prompts("judge")), len(self.prompts("confirmer"))), (1, 1))
        self.assertEqual(self.events(run, "goal_judged")[0]["confirmation"]["confirmation"]["score"], 88)

    def test_confirmation_uses_the_judge_fallbacks_when_its_provider_fails(self):
        real = __import__("nightweaver.engine", fromlist=["invoke"]).invoke

        def flaky(config, mode, *args, **kwargs):
            if mode == "judge" and config["kind"] == "claude":
                raise ProviderUnavailable("signed out", "authentication")
            return real(config, mode, *args, **kwargs)

        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "claude"},
                                                                "judge_fallbacks": [self.route("backup", [judged(89, met=True)])]})
        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual((len(self.prompts("judge")), len(self.prompts("backup"))), (1, 1))
        self.assertEqual(len(self.events(run, "provider_fallback")), 1)
        self.assertEqual(self.events(run, "provider_fallback")[0]["from"]["provider"], "claude", "the route the confirmation left, not the judge's")
        self.assertEqual(self.store.get(run)["state"]["judge_route"], 0)

    def test_a_usage_limit_during_the_confirmation_parks_and_resumes_there(self):
        real = __import__("nightweaver.engine", fromlist=["invoke"]).invoke
        calls = []

        def limited(config, mode, *args, **kwargs):
            if mode == "judge":
                calls.append(mode)
                if len(calls) == 2:
                    raise ProviderUnavailable("You have hit your usage limit", "usage_limit")
            return real(config, mode, *args, **kwargs)

        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True})
        with patch("nightweaver.engine.invoke", side_effect=limited):
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["judge_stage"]["stage"]), ("judging", "confirm"))
        self.assertTrue(state["retry_at"])
        self.assertEqual(len(self.events(run, "provider_parked")), 1)
        self.store.transition(run, "retry_cleared", retry_at=None)
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.prompts("judge")), 2)

    # ---------------------------------------------------------------- 7: --add-waves plans from the stored gaps

    def test_add_waves_on_a_stopped_run_plans_instead_of_judging_the_same_build_again(self):
        run = self.pursue([judged(60), judged(65), judged(90, met=True)], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        before = self.store.get(run)["state"]
        self.assertEqual(len(self.prompts("judge")), 2)
        result = self.store.extend_pursuit(run, 5)
        state = self.store.get(run)["state"]
        self.assertEqual((result["resumed"], result["phase"], state["phase"]), (True, "planning", "planning"))
        self.assertEqual((state["stale_judgments"], state["judgments"]), (0, before["judgments"]), "no judgment was recorded, so none is stale")
        self.assertEqual(state["pursuit_gaps"], before["best_gaps"])
        self.assertEqual(Run(self.store, run).run(), "done")
        extended = self.store.events(run, limit=None, kinds=["pursuit_extended"])[0]["seq"]
        after = [e["kind"] for e in self.store.events(run, limit=None) if e["seq"] > extended]
        self.assertLess(after.index("planner_started"), after.index("judge_started"), "the planner runs before any new judgment")
        self.assertIn("Gap at 65", self.prompts("planner")[1])
        self.assertEqual(len(self.prompts("judge")), 3)

    def test_a_plateau_stop_can_be_extended_and_the_extra_wave_is_planned(self):
        run = self.pursue([judged(60), judged(60), judged(60), judged(95, met=True)], [plan("Round")],
                          extra_pursue={"max_waves": 1, "plateau": 2, "rescue": {"max_rescues": 0}})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "wave_limit")
        self.assertEqual(self.store.extend_pursuit(run, 3)["phase"], "planning")
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.events(run, "wave_planned")), 3, "the waves granted after the stop were planned and built")

    def test_judging_the_best_build_again_does_not_count_toward_a_plateau(self):
        run = self.pursue([judged(60), judged(62), judged(61), judged(60)], [plan("Round")],
                          extra_pursue={"max_waves": 10, "rescue": {"max_rescues": 0}})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.store.get(run)["state"]["stale_judgments"], 2)
        self.assertEqual(Run(self.store, run).run(retry=True), "blocked")
        self.assertEqual(self.store.get(run)["state"]["stale_judgments"], 2, "the retry re-judged the best build, which is no new evidence")

    # ---------------------------------------------------------------- 9: keep-best ranks by (checks pass, score)

    def test_a_green_build_beats_a_higher_scored_build_that_fails_its_own_check(self):
        gates = [self.ready(), {"name": "finished build keeps two milestones", "argv": helper("never_judge", 2), "timeout": 20}]
        run = self.pursue([judged(95), judged(89)], [plan("Next improvement")], gates=gates, extra_pursue={"max_waves": 1, "target_score": 90})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        red, green = self.events(run, "goal_judged")
        self.assertEqual([c["passed"] for c in red["ledger"]], [True, False])
        self.assertEqual([c["passed"] for c in green["ledger"]], [True, True])
        state = self.store.get(run)["state"]
        self.assertEqual((state["best_commit"], state["best_score"], state["best_clean"]), (green["commit"], 89, True))
        self.assertEqual(self.events(run, "pursuit_reverted"), [], "the green build is not discarded for the red one")
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), green["commit"])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Next improvement"])

    def test_a_stop_restores_the_green_build_rather_than_a_higher_scored_red_one(self):
        gates = [self.ready(), code_gate("finished build stays short", FAILS_ON_LONG_BUILD)]
        run = self.pursue([judged(80), judged(95)], [plan("Next improvement")], gates=gates, extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        green, red = self.events(run, "goal_judged")
        self.assertEqual(([c["passed"] for c in green["ledger"]], [c["passed"] for c in red["ledger"]]), ([True, True], [True, False]))
        reverted = self.events(run, "pursuit_reverted")[0]
        self.assertEqual((reverted["from"], reverted["to"]), (red["commit"], green["commit"]))
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), green["commit"])
        self.assertEqual(self.store.get(run)["state"]["best_score"], 80)

    # ---------------------------------------------------------------- 26, 33: planned waves keep the plan's scope

    def scoped(self, milestones, **extra):
        block = {"judge": self.route("judge", [judged(60), judged(90, met=True)]), "planner": self.route("planner", [plan("Fix API validation")]),
                 "confirm": False}
        return self.configured([sys.executable, "-c", SCOPED_WORKER], pursue=block, gates=[code_gate("always passes", "pass")],
                               milestones=milestones, **extra)

    def test_planned_waves_inherit_the_union_of_the_plans_paths_and_the_planner_is_told(self):
        run = self.scoped([{"title": "A: API", "task": "Build the API", "paths": ["api"]}, {"title": "B: UI", "task": "Build the UI", "paths": ["web"]}],
                          protected_paths=["secret.txt"])
        self.assertEqual(Run(self.store, run).run(), "done")
        spec = self.store.get(run)["spec"]
        self.assertEqual([(m["title"], m["paths"]) for m in spec["milestones"]],
                         [("A: API", ["api"]), ("B: UI", ["web"]), ("Fix API validation", ["api", "web"])])
        planner = self.prompts("planner")[0]
        self.assertIn("Allowed paths: api, web", planner)
        self.assertIn("Protected paths (no milestone may change them): secret.txt", planner)
        written = git(self.repo, "ls-tree", "-r", "--name-only", f"nightweaver/{run}").splitlines()
        self.assertEqual(sorted(name for name in written if "/" in name), ["api/A-API.txt", "api/Fix-API-validation.txt", "web/B-UI.txt"])

    def test_a_plan_with_an_unrestricted_milestone_leaves_planned_waves_unrestricted(self):
        run = self.scoped([{"title": "A: API", "task": "Build the API", "paths": ["api"]}, {"title": "B: UI", "task": "Build the UI"}])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([m["paths"] for m in self.store.get(run)["spec"]["milestones"]], [["api"], [], []])
        self.assertIn("Allowed paths: any file in the repository", self.prompts("planner")[0])
        self.assertIn("Protected paths (no milestone may change them): none declared", self.prompts("planner")[0])

    def test_inherited_paths_is_the_union_or_nothing(self):
        steps = lambda *paths: [{"paths": list(p)} for p in paths]
        self.assertEqual(pursuit.inherited_paths(steps(["a", "b"], ["b", "c"])), ["a", "b", "c"])
        self.assertEqual(pursuit.inherited_paths(steps(["a"], [])), [])
        self.assertEqual(pursuit.inherited_paths(steps([])), [])

    # ---------------------------------------------------------------- 28: a revert is crash-consistent

    def test_a_crash_after_the_branch_moves_finishes_the_discard_without_judging_again(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        with patch("nightweaver.pursuit.lost_commits", side_effect=SystemExit), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        state = self.store.get(run)["state"]
        first = self.events(run, "goal_judged")[0]
        self.assertEqual(len(self.prompts("judge")), 2)
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), first["commit"], "the branch already went back to the best build")
        self.assertEqual((state["phase"], state["judge_stage"]["stage"]), ("judging", "apply"))
        self.assertNotEqual(state["accepted"], first["commit"], "the state still names the discarded build until the decision is finished")
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.prompts("judge")), 3, "the discarded wave was not judged a second time")
        completed = self.events(run, "run_completed")[0]
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), completed["commit"])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Wave three work"])
        reverted = self.events(run, "pursuit_reverted")
        self.assertEqual(len(reverted), 1)
        self.assertEqual(reverted[0]["to"], first["commit"])
        self.assertEqual(len(self.store.get(run)["state"]["discarded_commits"]), 1)

    def test_a_branch_left_at_the_best_build_without_a_saved_decision_is_adopted_not_judged_as_the_discarded_build(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        state = self.interrupted_at_wave(run, 2)
        best, discarded = state["best_commit"], state["accepted"]
        self.assertNotEqual(best, discarded)
        git(self.repo, "update-ref", f"refs/heads/nightweaver/{run}", best, discarded)  # the old order: the branch moved, the decision was lost
        self.assertEqual(Run(self.store, run).run(), "done")
        adopted = self.events(run, "pursuit_reconciled")
        self.assertEqual([(e["from"], e["to"]) for e in adopted], [(discarded, best)])
        self.assertEqual(self.store.get(run)["state"]["discarded_commits"], [discarded])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), self.events(run, "run_completed")[0]["commit"])

    def test_a_judgment_never_finishes_a_build_the_result_branch_no_longer_holds(self):
        run = self.pursue([judged(70), judged(90, met=True)], [plan("Wave two work")])
        self.interrupted_at_wave(run, 2)
        git(self.repo, "update-ref", f"refs/heads/nightweaver/{run}", self.head)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("moved outside this run", self.store.get(run)["state"]["last_error"])
        self.assertEqual(self.events(run, "run_completed"), [])
        self.assertEqual(self.events(run, "goal_met"), [])

    # ---------------------------------------------------------------- 29, 48: the milestone limit

    def test_the_milestone_limit_is_not_an_extendable_stop(self):
        run = self.pursue([judged(60)], [plan("More")])
        with patch("nightweaver.pursuit.MAX_MILESTONES", 1):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], self.events(run, "pursuit_stopped")[0]["reason"]), ("planning", "milestone_limit"))
        self.assertIn("More waves cannot lift this limit", state["last_error"])
        judge_calls = len(self.prompts("judge"))
        with self.assertRaisesRegex(ValueError, "50-milestone limit"):
            self.store.extend_pursuit(run, 2)
        self.cli("pursuit", run, "--add-waves", 2, code=1)
        self.assertEqual(self.store.get(run)["spec"]["pursue"]["max_waves"], 3, "a refused extension changes nothing")
        text = self.cli("pursuit", run, raw=True)
        self.assertIn("Stopped: milestone_limit", text)
        self.assertNotIn("--add-waves", text)
        self.assertEqual(self.cli("pursuit", run, "--json")["stopped"]["reason"], "milestone_limit")
        with patch("nightweaver.pursuit.MAX_MILESTONES", 1):
            self.assertEqual(Run(self.store, run).run(retry=True), "blocked")
        self.assertEqual(len(self.prompts("judge")), judge_calls, "a retry stops again without another judgment")
        self.assertEqual(metrics(self.store, run)["outcome"], "pursuit_stopped")

    def test_a_milestone_limit_stop_leaves_the_best_build_on_the_branch(self):
        run = self.pursue([judged(80), judged(78)], [plan("Second")])
        with patch("nightweaver.pursuit.MAX_MILESTONES", 2):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        best, latest = self.events(run, "goal_judged")
        state = self.store.get(run)["state"]
        self.assertEqual((state["best_commit"], state["best_score"]), (best["commit"], 80))
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), best["commit"])
        self.assertEqual(state["accepted"], best["commit"])
        reverted = self.events(run, "pursuit_reverted")
        self.assertEqual([(e["from"], e["to"]) for e in reverted], [(latest["commit"], best["commit"])])
        self.assertEqual(git(self.repo, "rev-parse", reverted[0]["discarded_ref"]), latest["commit"])
        self.assertIn("Gap at 80", state["last_error"])
        self.assertNotIn("Gap at 78", state["last_error"])
        self.assertEqual(state["discarded_commits"], [latest["commit"]])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture"])

    # ---------------------------------------------------------------- 30: --add-waves while the judge runs

    def test_waves_added_during_a_judge_call_count(self):
        run = self.pursue([judged(60), judged(65), judged(90, met=True)], [plan("More")], extra_pursue={"max_waves": 1})
        real, extended = Run.call_provider, []

        def during(self_, config, role, *args, **kwargs):
            result = real(self_, config, role, *args, **kwargs)
            if role == "judge" and self_.store.get(self_.id)["state"].get("wave") == 2 and not extended:
                extended.append(self_.store.extend_pursuit(self_.id, 3))
            return result

        with patch.object(Run, "call_provider", during):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(extended[0], {"max_waves": 4, "resumed": False, "phase": "judging"})
        self.assertEqual(self.events(run, "pursuit_stopped"), [], "the wave limit the judge call started under no longer applies")
        self.assertEqual(self.store.get(run)["state"]["wave"], 3)

    # ---------------------------------------------------------------- 31: every judgment has its own evidence

    def test_screenshots_of_a_discarded_build_never_reach_a_later_judgment(self):
        gates = [self.ready(), {**code_gate("page screenshot", PAGE_SHOT), "artifacts": []}]
        run = self.pursue([judged(70), judged(50), judged(50)], [plan("Wave two work")], gates=gates, extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        pages = [sorted(Path(p).name for p in j["screenshots"]) for j in self.events(run, "goal_judged")]
        self.assertEqual(pages, [["prepare-fixture.png"], ["wave-two-work.png"]])
        self.assertEqual(self.store.get(run)["state"]["accepted"], self.events(run, "goal_judged")[0]["commit"])
        self.assertEqual(Run(self.store, run).run(retry=True), "blocked")
        again = self.events(run, "goal_judged")[2]
        self.assertEqual(sorted(Path(p).name for p in again["screenshots"]), ["prepare-fixture.png"], "only the build being judged is shown")
        self.assertEqual(again["commit"], self.events(run, "goal_judged")[0]["commit"])
        discarded, restored = (Path(j["screenshots"][0]) for j in self.events(run, "goal_judged")[1:])
        self.assertEqual(discarded.parent.parent, restored.parent.parent, "both builds were judged in wave 2")
        self.assertNotEqual(discarded.parent, restored.parent, "each with an evidence ledger of its own")
        prompt = self.prompts("judge")[2]
        self.assertNotIn("wave-two-work.png", prompt)
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")

    # ---------------------------------------------------------------- 32: the ledger holds the checks of the final build

    def test_the_ledger_leaves_out_a_check_local_to_an_earlier_milestone(self):
        milestones = [{"title": "First", "task": "Build it", "gates": [code_gate("only while the first milestone is built", LOCAL_GATE)]},
                      {"title": "Second", "task": "Change what that check asserts"}]
        run = self.pursue([judged(95, met=True)], milestones=milestones)
        self.assertEqual(Run(self.store, run).run(), "done")
        judgment = self.events(run, "goal_judged")
        self.assertEqual(len(judgment), 1)
        self.assertEqual([c["name"] for c in judgment[0]["ledger"]], ["read fixture value"])

    def test_the_ledger_keeps_retained_and_final_checks(self):
        milestones = [{"title": "First", "task": "Build it", "gates": [code_gate("retained for later milestones", "pass")], "retain_gates": "subsequent"},
                      {"title": "Second", "task": "Build more", "gates": [code_gate("local to the second", "pass")]}]
        run = self.pursue([judged(95, met=True)], milestones=milestones, final_gates=[code_gate("delivery", "pass")])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(sorted(c["name"] for c in self.events(run, "goal_judged")[0]["ledger"]),
                         ["delivery", "local to the second", "read fixture value", "retained for later milestones"])

    # ---------------------------------------------------------------- 34: long lists are cut

    def test_a_judgment_with_many_gaps_and_criteria_is_cut_not_refused(self):
        many = judged(60, gaps=[{"title": f"Gap {n}", "detail": "d"} for n in range(40)])
        many["criteria"] = [{"criterion": f"Criterion {n}", "met": False, "note": "n"} for n in range(70)]
        parsed = pursuit.parse_judgment(json.dumps(many))
        self.assertEqual((len(parsed["gaps"]), len(parsed["criteria"])), (30, 60))
        self.assertEqual((parsed["gaps"][0]["title"], parsed["gaps"][-1]["title"]), ("Gap 0", "Gap 29"))
        with self.assertRaises(ValueError):
            pursuit.parse_judgment(json.dumps({**many, "gaps": "none"}))
        run = self.pursue([many, judged(90, met=True)], extra_pursue={"max_waves": 1})
        Run(self.store, run).run()
        self.assertEqual(self.events(run, "judge_invalid"), [])
        self.assertEqual(len(self.events(run, "goal_judged")[0]["gaps"]), 30)

    # ---------------------------------------------------------------- 49: a revert's discarded milestones are not accepted

    def test_discarded_milestones_are_reported_as_discarded_everywhere(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        self.assertEqual(Run(self.store, run).run(), "done")
        first, second, third = self.events(run, "goal_judged")
        handoff = self.cli("handoff", run)
        self.assertEqual([c["title"] for c in handoff["completed"]], ["Prepare fixture", "Wave three work"])
        self.assertEqual([c["commit"] for c in handoff["discarded"]], [second["commit"]])
        for entry in handoff["completed"]:
            git(self.repo, "merge-base", "--is-ancestor", entry["commit"], handoff["accepted_commit"])
        brief = self.root / "handoff.md"
        self.cli("handoff", run, "--markdown", brief)
        self.assertIn("Discarded milestones", brief.read_text(encoding="utf-8"))
        measured = metrics(self.store, run)
        self.assertEqual((measured["accepted_milestones"], measured["total_milestones"]), (2, 2), "a discarded wave is neither accepted nor planned work")
        data = self.cli("pursuit", run, "--json")
        wave_two = data["waves"][1]["milestones"][0]
        self.assertEqual((wave_two["title"], wave_two["discarded"], wave_two["accepted"]), ("Wave two work", True, False))
        self.assertEqual(wave_two["commit"], second["commit"])
        self.assertEqual(data["waves"][2]["milestones"][0]["discarded"], False)
        self.assertTrue(data["waves"][2]["milestones"][0]["accepted"])
        self.assertIn("discarded " + second["commit"][:12], self.cli("pursuit", run, raw=True))
        self.assertEqual([m["step"] for m in self.store.accepted_milestones(run)], [1, 3])
        self.assertEqual([m["step"] for m in self.store.accepted_milestones(run, discarded=True)], [1, 2, 3])
        forked = fork_run(self.store, run, 1)
        fork = self.store.get(forked)
        self.assertEqual([m["title"] for m in fork["spec"]["milestones"]], ["Wave three work"], "the discarded wave is not carried into a fork")
        self.assertEqual(fork["state"]["base"], first["commit"])
        with self.assertRaisesRegex(ValueError, "No milestones remain"):
            fork_run(self.store, run, 2)
        with self.assertRaisesRegex(ValueError, "Choose an accepted milestone"):
            fork_run(self.store, run, 3)
        from nightweaver.diagnostics import replay_checks
        with self.assertRaisesRegex(ValueError, "accepted by this run"):
            replay_checks(self.store, run, 2)
        self.assertEqual(third["commit"], handoff["accepted_commit"])

    # ---------------------------------------------------------------- 51: a superseded stop is not reported

    def test_a_stop_that_was_extended_is_not_reported_when_the_run_later_blocks_for_another_reason(self):
        run = self.pursue([judged(60), judged(65), "bad", "bad"], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.cli("pursuit", run, "--json")["stopped"]["reason"], "wave_limit")
        self.assertEqual(metrics(self.store, run)["pursuit"]["stop_reason"], "wave_limit")
        self.cli("pursuit", run, "--add-waves", 1, "--json")
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], state["judge_attempts"]), ("judging", 2))
        data = self.cli("pursuit", run, "--json")
        self.assertIsNone(data["stopped"])
        text = self.cli("pursuit", run, raw=True)
        self.assertNotIn("Stopped:", text)
        self.assertNotIn("--add-waves", text)
        self.assertIn("Blocked: The judge returned no valid judgment", text)
        measured = metrics(self.store, run)
        self.assertEqual((measured["outcome"], measured["pursuit"]["stop_reason"]), ("execution_blocked", None))

    def test_current_stop_reads_the_events_in_order(self):
        stop = {"kind": "pursuit_stopped", "details": {"reason": "plateau"}}
        self.assertEqual(current_stop([stop]), {"reason": "plateau"})
        self.assertIsNone(current_stop([]))
        for kind in ("pursuit_extended", "blocked_run_retried", "blocked", "judge_invalid", "plan_invalid", "provider_unavailable", "budget_exhausted"):
            self.assertIsNone(current_stop([stop, {"kind": kind, "details": {}}]), kind)
        self.assertEqual(current_stop([{"kind": "blocked", "details": {}}, stop, {"kind": "goal_judged", "details": {}}]), {"reason": "plateau"})
        later = {"kind": "pursuit_stopped", "details": {"reason": "wave_limit"}}
        self.assertEqual(current_stop([stop, {"kind": "pursuit_extended", "details": {}}, later]), {"reason": "wave_limit"})


    # ================================================================ second review: what the first fixes left open

    # ---------------------------------------------------------------- the confirmation route is the route that fails

    def unavailable(self, kind, message, reason):
        """An `invoke` that fails every judge call routed to a provider of this kind and runs the others."""
        def flaky(config, mode, *args, **kwargs):
            if mode == "judge" and config["kind"] == kind:
                raise ProviderUnavailable(message, reason)
            return invoke(config, mode, *args, **kwargs)
        return flaky

    def test_a_failed_confirmation_is_reported_against_the_confirmation_route_not_the_judge(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "codex"}})
        with patch("nightweaver.engine.invoke", side_effect=self.unavailable("codex", "signed out", "authentication")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        (event,) = self.events(run, "provider_unavailable")
        self.assertEqual((event["provider"], event["role"], event["reason"]), ("codex", "judge", "authentication"))
        self.assertEqual((state["provider_issue"]["provider"], state["provider_issue"]["role"]), ("codex", "judge"))
        self.assertEqual((state["blocked_from"], state["judge_stage"]["stage"], state["last_error"]), ("judging", "confirm", "signed out"))
        self.assertEqual(len(self.prompts("judge")), 1, "the judge itself answered; only the confirmation failed")

    def test_a_usage_limit_on_the_confirmation_names_that_provider_when_waiting_is_off(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "claude"}}, retry={"max_parks": 0})
        with patch("nightweaver.engine.invoke", side_effect=self.unavailable("claude", "You have hit your usage limit", "usage_limit")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertTrue(state["last_error"].startswith("Claude usage limit reached and waiting is disabled"), state["last_error"])
        self.assertEqual((state["provider_issue"]["provider"], state["provider_issue"]["reason"]), ("claude", "usage_limit"))
        self.assertEqual(self.events(run, "provider_unavailable")[0]["provider"], "claude")

    def test_a_usage_limit_on_the_confirmation_parks_against_that_provider(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "claude"}})
        with patch("nightweaver.engine.invoke", side_effect=self.unavailable("claude", "You have hit your usage limit", "usage_limit")):
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
        (parked,) = self.events(run, "provider_parked")
        self.assertEqual((parked["provider"], parked["role"]), ("claude", "judge"))
        self.assertEqual(self.store.get(run)["state"]["judge_stage"]["stage"], "confirm")

    def test_a_confirmation_that_falls_back_reports_the_route_it_left(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "claude"},
                                                                "judge_fallbacks": [self.route("backup", [judged(89, met=True)])]})
        with patch("nightweaver.engine.invoke", side_effect=self.unavailable("claude", "signed out", "authentication")):
            self.assertEqual(Run(self.store, run).run(), "done")
        (fallback,) = self.events(run, "provider_fallback")
        self.assertEqual((fallback["from"]["provider"], fallback["from"]["role"], fallback["to"]["provider"]), ("claude", "judge", "command"))

    def test_the_judge_route_still_names_the_judge_when_the_first_judgment_fails(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "judge": {"kind": "codex"}, "confirm_judge": {"kind": "claude"}})
        with patch("nightweaver.engine.invoke", side_effect=self.unavailable("codex", "signed out", "authentication")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "provider_unavailable")[0]["provider"], "codex")
        self.assertEqual(self.store.get(run)["state"]["provider_issue"]["provider"], "codex")

    def test_the_served_model_of_a_confirmation_is_looked_up_in_its_own_profile(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True})
        spec = self.store.get(run)["spec"]
        spec["pursue"]["confirm_judge"] = {"kind": "claude", "profile_dir": r"C:\profiles\confirmer"}
        with self.store.connect() as db:
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
        runner, seen = Run(self.store, run), []
        state = {"phase": "judging", "accepted": "c" * 40, "wave": 1, "step": 1, "judge_route": 0, "judge_stage": {"stage": "confirm", "commit": "c" * 40}}
        pending = {"provider": "claude", "role": "judge", "model": None, "log": "none"}
        with patch("nightweaver.engine.served_models", side_effect=lambda config, log: seen.append(config) or {"models": [], "source": None}):
            with patch.object(pursuit, "saved", return_value={"commit": "c" * 40}):
                call_served(runner, pending, state)
            call_served(runner, pending, {**state, "judge_stage": None})
        self.assertEqual(seen[0].get("profile_dir"), r"C:\profiles\confirmer")
        self.assertIsNone(seen[1].get("profile_dir"), "outside a confirmation the judge's own route is the one that matches")

    # ---------------------------------------------------------------- doctor and preflight check the confirmation judge

    def test_doctor_checks_the_confirmation_judge_and_only_while_confirmation_is_on(self):
        run = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True, "confirm_judge": {"kind": "gemini"}})
        report = readiness.diagnose(self.store, self.store.get(run)["spec"])
        gemini = [p for p in report["providers"] if p.get("provider") == "gemini"]
        self.assertEqual([p["roles"] for p in gemini], [["pursuit confirmation judge"]])
        off = self.pursue([judged(90, met=True)], extra_pursue={"confirm": False, "confirm_judge": {"kind": "gemini"}})
        report = readiness.diagnose(self.store, self.store.get(off)["spec"])
        self.assertEqual([p for p in report["providers"] if p.get("provider") == "gemini"], [], "an unused route is not checked")
        plain = self.pursue([judged(90, met=True)], extra_pursue={"confirm": True})
        roles = [role for p in readiness.diagnose(self.store, self.store.get(plain)["spec"])["providers"] for role in p["roles"]]
        self.assertNotIn("pursuit confirmation judge", roles, "the confirmation uses the judge's route, which is checked as the judge")

    # ---------------------------------------------------------------- the confirmation call is part of the budget

    def test_plan_lint_counts_the_confirmation_call(self):
        run = self.pursue([judged(90, met=True)], budgets={"max_calls": 2}, extra_pursue={"confirm": True})
        lint = lint_plan(self.store.get(run)["spec"])
        self.assertEqual((lint["minimum_calls_for_acceptance"], lint["feasible"]), (3, False), "a worker, a judgment and its confirmation")
        self.assertEqual([(f["code"], f["minimum"], f["configured"]) for f in lint["findings"]], [("insufficient_calls", 3, 2)])
        off = lint_plan(self.store.get(self.pursue([judged(90, met=True)], budgets={"max_calls": 2}, extra_pursue={"confirm": False}))["spec"])
        self.assertEqual((off["minimum_calls_for_acceptance"], off["feasible"]), (2, True))
        enough = lint_plan(self.store.get(self.pursue([judged(90, met=True)], budgets={"max_calls": 3}, extra_pursue={"confirm": True}))["spec"])
        self.assertEqual((enough["minimum_calls_for_acceptance"], enough["feasible"]), (3, True))
        two = self.pursue([judged(90, met=True)], budgets={"max_calls": 9}, extra_pursue={"confirm": True},
                          reviewer={"kind": "command", "argv": fixtures.fixture("review")})
        self.assertEqual(lint_plan(self.store.get(two)["spec"])["minimum_calls_for_acceptance"], 4, "a reviewer call comes before the judgments")

    def test_a_budget_that_cannot_fund_the_confirmation_lets_the_judgment_stand_and_says_so(self):
        run = self.pursue([judged(90, met=True)], budgets={"max_calls": 2}, extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["provider_calls"], state["judge_stage"]), ("done", 2, None))
        self.assertEqual(len(self.prompts("judge")), 1)
        (skipped,) = self.events(run, "confirmation_skipped")
        self.assertIn("Only 0 provider calls are left in the budget and the confirmation needs at least 1", skipped["reason"])
        kinds = [e["kind"] for e in self.store.events(run, limit=None)]
        self.assertEqual((kinds.count("goal_confirming"), kinds.count("goal_met"), kinds.count("run_completed")), (0, 1, 1))
        self.assertLess(kinds.index("confirmation_skipped"), kinds.index("goal_met"))
        judgment = self.events(run, "goal_judged")[0]
        self.assertEqual((judgment["verdict"], judgment["score"], judgment["confirmation_skipped"]), ("MET", 90, skipped["reason"]))
        self.assertEqual(self.events(run, "goal_met")[0]["confirmation_skipped"], True)
        self.assertIn("without a confirmation", report_activity.LABELS["confirmation_skipped"](skipped))
        measured = metrics(self.store, run)
        self.assertEqual((measured["outcome"], measured["pursuit"]["confirmations"], measured["pursuit"]["confirmations_skipped"]), ("accepted", 0, 1))
        self.assertIn("MET stood without a confirmation: Only 0 provider calls", self.cli("pursuit", run, raw=True))
        self.assertEqual(self.cli("pursuit", run, "--json")["confirmations"], {"asked": 0, "unconfirmed": 0, "skipped": 1})
        self.assertIn("without a confirmation (budget)", Path(self.cli("report", run)["report"]).read_text(encoding="utf-8"))

    def test_a_budget_that_funds_the_confirmation_still_confirms(self):
        run = self.pursue([judged(90, met=True)], budgets={"max_calls": 3}, extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual((len(self.prompts("judge")), self.events(run, "confirmation_skipped")), (2, []))

    def test_a_confirmation_that_ran_out_of_budget_after_a_failed_attempt_falls_back_the_same_way(self):
        run = self.pursue([judged(90, met=True), "not json", judged(90, met=True)], budgets={"max_calls": 3}, extra_pursue={"confirm": True})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.prompts("judge")), 2, "the unreadable confirmation used the last call")
        self.assertEqual(len(self.events(run, "judge_invalid")), 1)
        self.assertEqual(len(self.events(run, "confirmation_skipped")), 1)
        self.assertEqual(self.events(run, "goal_judged")[0]["verdict"], "MET")

    def test_the_confirmation_needs_calls_time_and_tokens_left(self):
        def room(limits, state):
            return pursuit.budget_short(SimpleNamespace(spec={"budgets": limits}), state, 1, "the confirmation")
        self.assertIsNone(room({}, {"provider_calls": 500}))
        self.assertIsNone(room({"max_calls": 4}, {"provider_calls": 3}))
        self.assertIn("Only 0 provider calls are left in the budget and the confirmation needs at least 1", room({"max_calls": 4}, {"provider_calls": 4}))
        self.assertIn("run-time budget is spent", room({"max_seconds": 5}, {"started_at": "2000-01-01T00:00:00+00:00"}))
        self.assertIn("observed-token budget is spent", room({"max_observed_tokens": 100}, {"observed_tokens": 100}))

    def test_a_rescue_keeps_calls_for_the_judgment_and_confirmation_that_follow_it(self):
        def room(limits, state, confirm):
            spec = {"budgets": limits, "pursue": {"rescue": {"strategies": 2, "max_rescues": 1}, **({"confirm": True} if confirm else {})}}
            return rescue.short_of_budget(SimpleNamespace(spec=spec), state)
        self.assertIsNone(room({"max_calls": 20}, {"provider_calls": 12}, False), "seven for the rescue and one judgment")
        self.assertIn("a rescue needs at least 8", room({"max_calls": 20}, {"provider_calls": 13}, False))
        self.assertIsNone(room({"max_calls": 20}, {"provider_calls": 11}, True), "seven for the rescue, a judgment and its confirmation")
        self.assertIn("a rescue needs at least 9", room({"max_calls": 20}, {"provider_calls": 12}, True))

    # ---------------------------------------------------------------- a run that reverted before the state kept its discards

    def forget_discards(self, run):
        """Make a run look as one that reverted under the earlier version: its state never kept `discarded_commits`."""
        state = self.store.get(run)["state"]
        found = state.pop("discarded_commits")
        with self.store.connect() as db:
            db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run))
        return found

    def test_a_run_stored_without_its_discards_still_reports_the_discarded_wave_as_discarded(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        self.assertEqual(Run(self.store, run).run(), "done")
        found = self.forget_discards(run)
        self.assertEqual(len(found), 1)
        self.assertNotIn("discarded_commits", self.store.get(run)["state"])
        self.assertEqual(self.store.discarded_commits(run), found, "read back from the pursuit_reverted events")
        handoff = self.cli("handoff", run)
        self.assertEqual([c["title"] for c in handoff["completed"]], ["Prepare fixture", "Wave three work"])
        self.assertEqual([c["title"] for c in handoff["discarded"]], ["Wave two work"])
        measured = metrics(self.store, run)
        self.assertEqual((measured["accepted_milestones"], measured["total_milestones"], measured["pursuit"]["discarded_milestones"]), (2, 2, 1))
        data = self.cli("pursuit", run, "--json")
        self.assertEqual([(m["title"], m["accepted"], m["discarded"]) for w in data["waves"] for m in w["milestones"]],
                         [("Prepare fixture", True, False), ("Wave two work", False, True), ("Wave three work", True, False)])
        self.assertEqual([m["step"] for m in self.store.accepted_milestones(run)], [1, 3])
        fork = self.store.get(fork_run(self.store, run, 1))
        self.assertEqual([m["title"] for m in fork["spec"]["milestones"]], ["Wave three work"])
        self.assertEqual([(m["title"], m["discarded"], m["accepted"] is None) for m in self.cli("timeline", run)["milestones"]],
                         [("Prepare fixture", False, False), ("Wave two work", True, True), ("Wave three work", False, False)])

    def test_a_run_that_reverted_under_the_earlier_version_records_its_discards_when_it_resumes(self):
        run = self.pursue([judged(70), judged(50), judged(95, met=True)], [plan("Wave two work"), plan("Wave three work")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        found = self.forget_discards(run)
        self.assertEqual(self.cli("handoff", run)["discarded"][0]["title"], "Wave two work")
        self.assertEqual(self.store.extend_pursuit(run, 2)["phase"], "planning")
        self.assertEqual(Run(self.store, run).run(), "done")
        state = self.store.get(run)["state"]
        self.assertEqual(state["discarded_commits"], found)
        self.assertEqual(self.events(run, "pursuit_discards_recorded"), [{"commits": found}])
        handoff = self.cli("handoff", run)
        self.assertEqual([c["title"] for c in handoff["completed"]], ["Prepare fixture", "Wave three work"])
        self.assertEqual([c["title"] for c in handoff["discarded"]], ["Wave two work"])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Wave three work"])

    def test_a_run_without_reverts_has_nothing_to_record(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("More")])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.store.discarded_commits(run), [])
        self.assertEqual(self.events(run, "pursuit_discards_recorded"), [])
        self.assertNotIn("discarded_commits", self.store.get(run)["state"])

    # ---------------------------------------------------------------- a revert's milestones are not part of the plan's count

    def test_a_finished_run_that_discarded_a_wave_reads_as_complete(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        self.assertEqual(Run(self.store, run).run(), "done")
        measured = metrics(self.store, run)
        self.assertEqual((measured["accepted_milestones"], measured["total_milestones"]), (2, 2))
        self.assertEqual(measured["pursuit"]["discarded_milestones"], 1)
        html = Path(self.cli("report", run)["report"]).read_text(encoding="utf-8")
        self.assertIn("Done · 2/2 milestones accepted", html)
        self.assertIn("Wave two work (discarded)", html)
        self.assertIn("1 milestones discarded", html)
        table = self.root / "ci.md"
        self.cli("ci-summary", run, "--output", table)
        text = table.read_text(encoding="utf-8")
        self.assertIn("| Milestones accepted | 2 / 2 |", text)
        self.assertIn("| Milestones discarded by a revert | 1 |", text)
        data = self.cli("pursuit", run, "--json")
        two = data["waves"][1]["milestones"][0]
        self.assertEqual((two["accepted"], two["discarded"]), (False, True), "a consumer reading `accepted` no longer counts the discarded wave")
        self.assertTrue(two["commit"])
        discarded = next(m for m in self.cli("timeline", run)["milestones"] if m["title"] == "Wave two work")
        self.assertEqual((discarded["discarded"], discarded["accepted"], discarded["commit"]), (True, None, two["commit"]))
        self.assertTrue(next(m for m in self.cli("timeline", run)["milestones"] if m["title"] == "Wave three work")["accepted"])

    def test_the_sidebar_summary_tells_the_desktop_how_many_milestones_a_revert_discarded(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        self.assertEqual(Run(self.store, run).run(), "done")
        plain = self.configured(fixtures.fixture("implement"))
        rows = {row["id"]: row for row in self.store.run_summaries()}
        self.assertEqual((rows[run]["state"]["pursuit"], rows[run]["state"]["discarded"], rows[run]["total_milestones"]), (True, 1, 3))
        self.assertNotIn("pursuit", rows[plain]["state"])
        self.assertNotIn("discarded", rows[plain]["state"])
        source = (Path(__file__).resolve().parents[1] / "Nightweaver.Desktop" / "MainWindow.xaml.cs").read_text(encoding="utf-8")
        self.assertIn('TryGetProperty("discarded"', source)
        self.assertIn('Read("reason") == "milestone_limit"', source)
        self.assertNotIn('"Add waves with the pursuit command to continue.",', source, "the hint is conditional, not appended to every stop")

    # ---------------------------------------------------------------- --add-waves after a discard plans from the best build

    def test_the_planner_after_a_discard_and_an_extension_is_told_the_best_builds_judgment_and_that_a_wave_was_discarded(self):
        one = {**judged(70), "criteria": [{"criterion": "Layout", "met": False, "note": "Wave one layout is cramped"}]}
        two = {**judged(50), "criteria": [{"criterion": "Spacing", "met": False, "note": "Wave two spacing is worse"}]}
        run = self.pursue([one, two, judged(95, met=True)], [plan("Wave two work"), plan("Wave three work")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.store.extend_pursuit(run, 2)["phase"], "planning")
        self.assertIn("so its changes were discarded", self.store.get(run)["state"]["pursuit_note"])
        self.assertEqual(Run(self.store, run).run(), "done")
        prompt = self.prompts("planner")[1]
        self.assertIn("Independent judgment of the build to plan from: wave 1 scored 70/100, best 70/100", prompt)
        self.assertIn("Wave one layout is cramped", prompt)
        self.assertNotIn("Wave two spacing is worse", prompt, "the discarded build's unmet criteria are not the result's")
        self.assertNotIn("Latest independent judgment: wave 2", prompt)
        self.assertIn("Wave 2 scored 50/100, below the best score of 70/100, so its changes were discarded", prompt)
        self.assertIn("Gap at 70", prompt)
        first = self.prompts("planner")[0]
        self.assertIn("Latest independent judgment: wave 1 scored 70/100", first, "with no discard the latest judgment is the build's own")

    def test_a_stop_that_kept_the_judged_build_adds_no_discard_note(self):
        run = self.pursue([judged(60), judged(65), judged(90, met=True)], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.store.extend_pursuit(run, 2)
        self.assertEqual(self.store.get(run)["state"]["pursuit_note"], "")

    # ---------------------------------------------------------------- the text says where an extended run resumes

    def test_the_extension_message_and_the_activity_label_name_the_stage_the_run_resumes_at(self):
        run = self.pursue([judged(60), judged(65), "bad", "bad", judged(90, met=True)], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        text = self.cli("pursuit", run, "--add-waves", 1, raw=True)
        self.assertIn("The run is back at the planner; resume or dispatch it to continue.", text)
        self.assertNotIn("back at the judge", text)
        self.assertEqual(self.store.get(run)["state"]["phase"], "planning")
        extended = self.store.events(run, limit=None, kinds=["pursuit_extended"])[-1]
        self.assertEqual(extended["details"]["phase"], "planning")
        self.assertEqual(report_activity.LABELS["pursuit_extended"](extended["details"]), "Pursuit extended by 1 wave; planning resumed")
        self.assertEqual(Run(self.store, run).run(), "blocked")  # the judge cannot read its answer
        self.assertEqual(self.store.get(run)["state"]["blocked_from"], "judging")
        text = self.cli("pursuit", run, "--add-waves", 1, raw=True)
        self.assertIn("The run is back at the judge; resume or dispatch it to continue.", text)
        self.assertEqual(report_activity.LABELS["pursuit_extended"](self.store.events(run, limit=None, kinds=["pursuit_extended"])[-1]["details"]),
                         "Pursuit extended by 1 wave; judging resumed")
        self.assertEqual(report_activity.LABELS["pursuit_extended"]({"waves": 2, "max_waves": 4, "resumed": True}), "Pursuit extended by 2 waves; run resumed")
        self.assertEqual(report_activity.LABELS["pursuit_extended"]({"waves": 1, "max_waves": 4, "resumed": False, "phase": "judging"}), "Pursuit extended by 1 wave")

    # ---------------------------------------------------------------- a saved discard survives waves added during the crash window

    def test_a_crash_after_the_branch_moves_finishes_the_discard_even_when_waves_were_added_meanwhile(self):
        run = self.pursue([judged(80), judged(78), judged(95, met=True)], [plan("Wave two work"), plan("Wave three work")], extra_pursue={"max_waves": 1})
        with patch("nightweaver.pursuit.lost_commits", side_effect=SystemExit), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        best = self.events(run, "goal_judged")[0]["commit"]
        state = self.store.get(run)["state"]
        self.assertEqual((state["judge_stage"]["stage"], git(self.repo, "rev-parse", f"nightweaver/{run}")), ("apply", best))
        self.assertNotEqual(state["accepted"], best)
        self.assertEqual(self.store.extend_pursuit(run, 2), {"max_waves": 3, "resumed": False, "phase": "judging"})
        self.assertEqual(Run(self.store, run).run(), "done")
        final = self.store.get(run)["state"]
        self.assertEqual(self.events(run, "pursuit_stopped"), [], "the wave limit that justified the revert no longer applies")
        (reverted,) = self.events(run, "pursuit_reverted")
        self.assertEqual(reverted["to"], best)
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), self.events(run, "run_completed")[0]["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), final["accepted"])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Wave three work"])
        self.assertEqual(len(final["discarded_commits"]), 1)
        self.assertIn("Wave 2 scored 78/100, below the best score of 80/100, so its changes were discarded", self.prompts("planner")[1])
        self.assertEqual(len(self.prompts("judge")), 3, "the discarded wave was judged once")

    def test_a_saved_discard_keeps_its_stop_when_nothing_was_added(self):
        run = self.pursue([judged(80), judged(78)], [plan("Wave two work")], extra_pursue={"max_waves": 1})
        with patch("nightweaver.pursuit.lost_commits", side_effect=SystemExit), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "wave_limit")
        state = self.store.get(run)["state"]
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), state["accepted"])
        self.assertEqual(state["accepted"], state["best_commit"])


class FixPursuitSpecTests(unittest.TestCase):
    def raw(self, acceptance=("Beautiful",), **pursue):
        return {"repository": str(Path(__file__).resolve().parents[1]), "title": "T", "objective": "O", "worker": {"kind": "command", "argv": ["x"]},
                "gates": [{"name": "g", "argv": ["x"]}], "milestones": [{"title": "M", "task": "T"}], "acceptance": list(acceptance),
                "pursue": {"judge": {"kind": "codex"}, **pursue}}

    def test_confirmation_defaults_are_on_with_a_margin_of_three(self):
        pursue = validate(self.raw())["pursue"]
        self.assertEqual((pursue["confirm"], pursue["confirm_margin"]), (True, 3))
        self.assertNotIn("confirm_judge", pursue)

    def test_confirmation_settings_are_validated(self):
        cases = {"confirm text": ({"confirm": "yes"}, "confirm must be true or false"), "confirm number": ({"confirm": 1}, "confirm must be true or false"),
                 "margin high": ({"confirm_margin": 21}, "confirm_margin must be an integer between 0 and 20"),
                 "margin low": ({"confirm_margin": -1}, "confirm_margin"), "margin bool": ({"confirm_margin": True}, "confirm_margin"),
                 "margin text": ({"confirm_margin": "3"}, "confirm_margin"), "judge kind": ({"confirm_judge": {"kind": "ollama"}}, "Invalid confirm judge adapter"),
                 "judge type": ({"confirm_judge": "claude"}, "confirm judge must be an object"),
                 "judge effort": ({"confirm_judge": {"kind": "codex", "effort": "huge"}}, "Unsupported effort")}
        for label, (pursue, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                validate(self.raw(**pursue))

    def test_confirmation_settings_are_kept_and_validate_again(self):
        pursue = validate(self.raw(confirm=False, confirm_margin=0, confirm_judge={"kind": "claude", "effort": "high", "model": "m"}))["pursue"]
        self.assertEqual((pursue["confirm"], pursue["confirm_margin"], pursue["confirm_judge"]),
                         (False, 0, {"kind": "claude", "model": "m", "effort": "high"}))
        again = validate({**self.raw(), "pursue": pursue})["pursue"]
        self.assertEqual(again, pursue)
        self.assertEqual(validate(self.raw(confirm_margin=20))["pursue"]["confirm_margin"], 20)

    def test_a_confirmation_route_is_anchored_to_the_plan_folder_like_the_other_routes(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = {"milestones": [], "pursue": {"judge": {"kind": "claude"}, "confirm_judge": {"kind": "claude", "profile_dir": "profiles/two"}}}
            anchor_provider_paths(raw, folder)
            self.assertEqual(raw["pursue"]["confirm_judge"]["profile_dir"], str((folder / "profiles" / "two").resolve()))

    def test_criteria_are_capped_at_what_a_judgment_can_list(self):
        sixty = [f"Requirement {n}" for n in range(60)]
        self.assertEqual(len(validate(self.raw(criteria=sixty))["pursue"]["criteria"]), 60)
        with self.assertRaisesRegex(ValueError, "at most 60"):
            validate(self.raw(criteria=[*sixty, "One more"]))
        with self.assertRaisesRegex(ValueError, "at most 60"):
            validate(self.raw(acceptance=[*sixty, "One more"]))
        self.assertEqual(len(validate(self.raw(acceptance=[*sixty, "One more"], criteria=["Short"]))["acceptance"]), 61,
                         "the cap applies to what the judge lists, not to a plan's own acceptance list")


if __name__ == "__main__":
    unittest.main()
