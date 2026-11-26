"""Nightweaver 0.19.2, review fixes, round 2.

A second skeptic reproduced two more findings against the antigravity read-only layers, each with an offline harness (no model call):

1. A silent or refused agy judge blocked the run where 0.19.1 degraded to an unscored tie. invoke() now raises `no_answer` and
   `read_only_refused` for every read-only role, and the engine retries them as transient and then blocks. That is right for a role with no way
   past an unreadable answer (the reviewer, the pursuit judge, the wave planner), but the exploration judge, the direction planner and the
   rescue diagnoser already answer an unreadable reply themselves (ask once more, then an unscored tie or a skipped stage, "so one bad judge
   call cannot wedge the run"). They now take those two kinds as an unreadable answer again.
2. The loaded-rules guard asked agy about /permissions in the controller's folder, not in the folder the call runs in, so the project scope of
   that folder (which the repository's own files can change) was never part of the check.

The agy streams are built from the captures in tests/agy_streams (blanked, retargeted, raw) as the other 0.19.2 modules do."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import providers, readiness
from nightweaver.engine import Run
from nightweaver.providers import SCHEMAS, UNREADABLE, ProviderUnavailable
import test_windows as fixtures
from test_release_0191_gemini import Hermetic
from test_release_0192_agy import BARE, LOADED, at_home, blanked, put, raw, retargeted

TESTS = Path(__file__).parent
BUILD = str(TESTS / "explore_fixture.py")
RESCUE_BUILD = str(TESTS / "rescue_fixture.py")
SCRIPT = str(TESTS / "pursuit_fixture.py")
GOAL = {"verdict": "MET", "score": 90, "reason": "Fine", "gaps": [], "criteria": [{"criterion": "Looks right", "met": True, "note": "Observed"}]}
DIRECTIONS = {"directions": [{"name": n, "brief": f"A bold {n} look with its own palette and type."} for n in ("Brutalist", "Soft Pastel")]}


def judged(score):
    return {"verdict": "NOT_MET", "score": score, "reason": f"Reason {score}", "gaps": [{"title": f"Gap at {score}", "detail": f"Detail {score}"}],
            "criteria": [{"criterion": "Looks right", "met": False, "note": "Observed"}]}


def same_folder(one, other):
    return os.path.normcase(os.path.abspath(one)) == os.path.normcase(os.path.abspath(other))


class GuardFolder(Hermetic):
    """The loaded-rules check is made in the folder the call it protects runs in."""

    def stand_in(self, asked):
        """An agy that records the folder each /permissions question was asked in. It lists the rules everywhere except in a folder
        named `hijacked`, where a project setting is taken to override them."""
        def agy(executable, env, *args, timeout=30, cwd=None):
            if "/permissions" not in args:
                return 0, "gemini-4-neon-xhigh"
            asked.append(cwd)
            return 0, BARE if cwd is not None and Path(cwd).name == "hijacked" else LOADED
        return agy

    def call_in(self, folder, asked, executed=None, mode="reviewer"):
        def fake(argv, cwd, log, timeout, **options):
            if executed is not None:
                executed.append(cwd)
            put(log, at_home(raw(3), self.root / "user"))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=self.stand_in(asked)):
            return providers.invoke({"kind": "antigravity", "executable": sys.executable}, mode, folder, self.root / "call.jsonl", "Review.", 10,
                                    lambda: False)

    def folder(self, name):
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def test_the_permissions_question_is_asked_in_the_folder_the_call_runs_in(self):
        worktree, asked, executed = self.folder("run worktree"), [], []
        self.call_in(worktree, asked, executed)
        self.assertEqual(len(asked), 1)
        self.assertTrue(same_folder(asked[0], worktree), asked)
        self.assertTrue(same_folder(asked[0], executed[0]), "the same folder the model call runs in")

    def test_agy_run_runs_its_command_in_the_folder_it_is_given(self):
        folder = self.folder("somewhere else")
        code, output = providers.agy_run(sys.executable, dict(os.environ), "-c", "import os; print(os.getcwd())", cwd=folder)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.samefile(output.strip(), folder), output)
        code, output = providers.agy_run(sys.executable, dict(os.environ), "-c", "import os; print(os.getcwd())")
        self.assertTrue(os.path.samefile(output.strip(), os.getcwd()), "no folder given: this process's own, as before")
        self.assertEqual(providers.agy_run(sys.executable, dict(os.environ), "-c", "pass", cwd=self.root / "not there"), (None, ""),
                         "a folder that is gone is a check agy could not answer, not a crash")

    def test_a_folder_whose_own_settings_hide_the_rules_is_blocked_and_another_folder_is_not(self):
        asked, executed = [], []
        self.call_in(self.folder("clean"), asked, executed)
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call_in(self.folder("hijacked"), asked, executed)
        self.assertEqual(caught.exception.kind, "configuration")
        self.assertIn("did not load the read-only permission rules", str(caught.exception))
        self.assertEqual(len(asked), 2, "a clear check in one folder says nothing about another")
        self.assertEqual(len(executed), 1, "no model call was made in the hijacked folder")
        self.call_in(self.folder("clean"), asked, executed)
        self.assertEqual(len(asked), 2, "the clean folder's own clear check is still remembered")

    def test_a_clear_check_is_remembered_for_five_minutes_per_folder(self):
        asked = []
        first, second = self.folder("first"), self.folder("second")
        for folder in (first, first, second, second, first):
            self.call_in(folder, asked, mode="judge")
        self.assertEqual(len(asked), 2, "one question per agy per folder per five minutes")
        self.assertTrue(same_folder(asked[0], first) and same_folder(asked[1], second))
        self.assertEqual({key[0] for key in providers.AGY_READ_ONLY}, {sys.executable})

    def test_old_folders_are_forgotten_when_a_new_one_is_remembered(self):
        providers.AGY_READ_ONLY[(sys.executable, "long gone")] = providers.monotonic() - 301
        providers.AGY_READ_ONLY[("other agy", "kept")] = providers.monotonic()
        self.call_in(self.folder("new"), [])
        self.assertNotIn((sys.executable, "long gone"), providers.AGY_READ_ONLY)
        self.assertIn(("other agy", "kept"), providers.AGY_READ_ONLY)

    def test_doctor_asks_in_the_repository_and_forgets_every_folder_of_that_agy_before_its_probe(self):
        asked = []
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        repository = self.folder("repository")
        executable = shutil.which(sys.executable)
        providers.AGY_READ_ONLY[(executable, "a worktree the last run used")] = providers.monotonic()
        providers.AGY_READ_ONLY[("other agy", "kept")] = providers.monotonic()

        def fake(argv, cwd, log, timeout, **options):
            put(log, raw(3))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=self.stand_in(asked)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}, repository, True)
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        self.assertEqual(len(asked), 2, "once for the report, in the repository, and afresh for the probe call")
        self.assertTrue(same_folder(asked[0], repository))
        self.assertTrue(Path(asked[1]).name.startswith("probe-"), "the probe runs in a folder of its own, and is checked there")
        self.assertNotIn((executable, "a worktree the last run used"), providers.AGY_READ_ONLY)
        self.assertIn(("other agy", "kept"), providers.AGY_READ_ONLY)


class Scripted(unittest.TestCase):
    """A run whose agy routes are answered from captures: `answers(schema, number)` gives the stream of the number-th call with that schema."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    agy = {"kind": "antigravity", "executable": sys.executable, "model": "gemini-4-neon"}

    def script(self, name, items):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return path

    def command(self, name, items):
        return {"kind": "command", "argv": [sys.executable, SCRIPT, "script", str(self.script(name, items))]}

    def run_with(self, run, answers):
        from nightweaver import process
        real, seen = process.execute, []

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                return real(argv, cwd, log, timeout, **options)
            schema = next((a for a in argv if "agy-schema-" in a), "")
            name = Path(schema).stem.removeprefix("agy-schema-") if schema else "none"
            seen.append(name)
            put(log, answers(name, seen.count(name)))
            return 0

        with patch("nightweaver.providers.execute", side_effect=execute), \
                patch("nightweaver.providers.agy_run", lambda executable, env, *args, timeout=30, cwd=None:
                      (0, LOADED) if "/permissions" in args else (0, "gemini-4-neon")):
            outcome = Run(self.store, run).run()
        return outcome, seen

    def kinds(self, run):
        return [e["kind"] for e in self.store.events(run, limit=None)]

    def events(self, run, kind):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=[kind])]


class SilentJudgesDoNotWedgeTheRun(Scripted):
    def exploration(self, comparison):
        planner = self.command("planner", [DIRECTIONS, {"milestones": [{"title": "Next", "task": "Do Next"}]}])
        builder = [sys.executable, BUILD, "build", str(self.root / "worker.prompts"), "-", "zq-builder"]
        run = self.configured(builder, pursue={"judge": self.agy, "planner": planner, "explore": {"directions": 2}, "confirm": False},
                              milestones=[{"title": "Prepare fixture", "task": "Set the value"}], retry={"initial_seconds": 1, "max_seconds": 1})
        return run, self.run_with(run, lambda name, number: retargeted(2, GOAL, SCHEMAS["judge"]) if name == "judge" else comparison)

    def check_exploration(self, run, outcome, seen, reason, said):
        kinds = self.kinds(run)
        self.assertEqual(outcome, "done", "one unreadable judge call must not block a run that records an unscored tie")
        self.assertEqual(seen, ["comparison"] * 4 + ["judge"], "each of the two orders is asked twice, then the goal judge runs")
        self.assertEqual(kinds.count("comparison_invalid"), 2)
        self.assertEqual(kinds.count("provider_retry_scheduled"), 0, "the explorer asks again itself; the engine does not retry the call")
        self.assertNotIn("provider_unavailable", kinds)
        unreadable = self.events(run, "answer_unreadable")
        self.assertEqual([(d["role"], d["reason"]) for d in unreadable], [("judge", reason)] * 4)
        self.assertIn(said, unreadable[0]["issue"], "the operator still reads why agy gave nothing")
        self.assertTrue(unreadable[0]["log"].endswith(".jsonl"))
        compared = self.events(run, "direction_compared")
        self.assertEqual([c["result"] for c in compared], ["tie", "tie"])
        self.assertTrue(all("returned no valid comparison" in c["issue"] for c in compared))
        self.assertIn("goal_met", kinds)

    def test_a_silent_agy_judge_in_the_exploration_tournament_scores_an_unscored_tie(self):
        run, (outcome, seen) = self.exploration(blanked(2))
        self.check_exploration(run, outcome, seen, "no_answer", "returned no answer")

    def test_an_agy_judge_that_answered_after_a_refused_action_scores_an_unscored_tie(self):
        run, (outcome, seen) = self.exploration(raw(10))
        self.check_exploration(run, outcome, seen, "read_only_refused", "refused under the permission rules")

    def test_a_silent_agy_planner_skips_the_exploration_and_the_run_builds_normally(self):
        builder = [sys.executable, BUILD, "build", str(self.root / "worker.prompts"), "-", "zq-builder"]
        run = self.configured(builder, pursue={"judge": self.agy, "planner": self.agy, "explore": {"directions": 2}, "confirm": False},
                              milestones=[{"title": "Prepare fixture", "task": "Set the value"}], retry={"initial_seconds": 1, "max_seconds": 1})
        outcome, seen = self.run_with(run, lambda name, number: retargeted(2, GOAL, SCHEMAS["judge"]) if name == "judge" else blanked(2))
        kinds = self.kinds(run)
        self.assertEqual(outcome, "done")
        self.assertEqual(seen, ["directions", "directions", "judge"])
        self.assertEqual(kinds.count("directions_invalid"), 1)
        self.assertEqual(kinds.count("exploration_skipped"), 1)
        self.assertEqual(kinds.count("provider_retry_scheduled"), 0)
        self.assertEqual([d["reason"] for d in self.events(run, "answer_unreadable")], ["no_answer"] * 2)

    def test_a_silent_agy_diagnoser_skips_the_rescue_instead_of_blocking_on_the_diagnosis(self):
        builder = [sys.executable, RESCUE_BUILD, "build", str(self.root / "worker.prompts"), "-", "zq-builder"]
        planner = self.command("planner", [{"milestones": [{"title": "Next improvement", "task": "Do Next improvement"}]}])
        run = self.configured(builder, pursue={"judge": self.agy, "planner": planner, "plateau": 1, "max_waves": 4, "rescue": {"strategies": 2},
                                               "confirm": False}, retry={"initial_seconds": 1, "max_seconds": 1})
        outcome, seen = self.run_with(run, lambda name, number: retargeted(2, judged((60, 55)[min(number, 2) - 1]), SCHEMAS["judge"])
                                      if name == "judge" else blanked(2))
        kinds = self.kinds(run)
        self.assertEqual(seen, ["judge", "judge", "diagnosis", "diagnosis"])
        self.assertEqual(kinds.count("rescue_diagnosis_invalid"), 1)
        (skipped,) = self.events(run, "rescue_skipped")
        self.assertIn("attempt 2 of 2", skipped["reason"])
        self.assertEqual(skipped["stage"], "diagnose")
        self.assertEqual(kinds.count("provider_retry_scheduled"), 0)
        self.assertNotIn("provider_unavailable", kinds)
        self.assertEqual([d["reason"] for d in self.events(run, "answer_unreadable")], ["no_answer"] * 2)
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau", "the pursuit ends as a plateau, as for any skipped rescue")
        self.assertEqual(outcome, "blocked")

    def test_only_a_caller_that_asks_again_itself_gets_the_unreadable_answer_back(self):
        self.assertEqual(UNREADABLE, ("no_answer", "read_only_refused"))
        run = self.configured([sys.executable, BUILD, "build", str(self.root / "worker.prompts"), "-", "zq-builder"],
                              pursue={"judge": self.agy, "planner": self.command("planner", [{"milestones": []}]), "confirm": False},
                              retry={"initial_seconds": 1, "max_seconds": 1})
        outcome, seen = self.run_with(run, lambda name, number: blanked(2))
        state = self.store.get(run)["state"]
        self.assertEqual(outcome, "blocked", "the pursuit judge has no way past a silent answer: it retries as a transient failure, then blocks")
        self.assertEqual(seen, ["judge"] * 3)
        self.assertEqual(self.kinds(run).count("provider_retry_scheduled"), 2)
        self.assertEqual(state["provider_issue"]["reason"], "no_answer")
        self.assertIn("returned no answer", state["last_error"])
        self.assertNotIn("answer_unreadable", self.kinds(run))

    def test_a_provider_that_is_unavailable_for_any_other_reason_still_raises_for_an_exploring_caller(self):
        run = self.configured([sys.executable, BUILD, "build", str(self.root / "worker.prompts"), "-", "zq-builder"],
                              pursue={"judge": self.agy, "planner": self.command("planner", [DIRECTIONS]), "explore": {"directions": 2}, "confirm": False})
        engine = Run(self.store, run)
        state = self.store.get(run)["state"]
        for kind in ("transient", "usage_limit", "authentication", "configuration", "read_only_violation"):
            with self.subTest(kind=kind), patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("x", kind)):
                with self.assertRaises(ProviderUnavailable):
                    engine.call_provider(self.agy, "judge", self.root, self.root / "log.jsonl", "p", unreadable_ok=True)
        for kind in UNREADABLE:
            with self.subTest(kind=kind), patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("x", kind)):
                with self.assertRaises(ProviderUnavailable):
                    engine.call_provider(self.agy, "judge", self.root, self.root / "log.jsonl", "p")
                self.assertEqual(engine.call_provider(self.agy, "judge", self.root, self.root / "log.jsonl", "p", unreadable_ok=True),
                                 (0, None, ""))
        self.assertEqual(state["phase"], self.store.get(run)["state"]["phase"], "no call changed the phase by itself")


if __name__ == "__main__":
    unittest.main()
