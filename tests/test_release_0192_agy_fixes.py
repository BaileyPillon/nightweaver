"""Nightweaver 0.19.2, review fixes, round 2.

A second skeptic reproduced two more findings against the antigravity read-only layers, each with an offline harness (no model call):

1. A silent or refused agy judge blocked the run where 0.19.1 degraded to an unscored tie. invoke() now raises `no_answer` and
   `read_only_refused` for every read-only role, and the engine retries them as transient and then blocks. That is right for a role with no way
   past an unreadable answer (the reviewer, the pursuit judge, the wave planner), but the exploration judge, the direction planner and the
   rescue diagnoser already answer an unreadable reply themselves (ask once more, then an unscored tie or a skipped stage, "so one bad judge
   call cannot wedge the run"). They now take those two kinds as an unreadable answer again.
2. The loaded-rules guard asked agy about /permissions in the controller's folder, not in the folder the call runs in, so the project scope of
   that folder (which the repository's own files can change) was never part of the check.

Round 3 (DeniedReads, DenyRuleText, BriefWriterAsksAgain) reproduced two minor findings from the adversarial review of 0.19.2:

3. A denied READ counted as a refusal: a user's own deny rule on read_file(**/.env) ended the call as `read_only_refused`, and the match was
   any "deny rule" anywhere in an error message, so an error that only echoed a file name with those words counted too.
4. The wish brief writer lost its second attempt: an empty or refused agy answer is now a ProviderUnavailable out of invoke(), and
   write_brief gave up after one call, where e65b09b asked again.

Round 4 checked round 3 adversarially and changed four things:

5. Round 3 made `doctor --live` ask agy /permissions once, in the empty probe folder. That lost a relative executable ("tools/agy.exe") and
   stopped asking in the repository, so project settings there that hide the deny rules read as ready. It asks twice again, on purpose
   (DoctorAsksTwiceOnPurpose).
6. A denied step was a refusal only when its tool was a recognised writer, so every unrecognised name (update_file, mkdir, Run_Command, mcp_*,
   browser_input, send_message, schedule, ...) that agy denied was taken for a harmless read and the answer kept. Now only agy's pure read
   tools (providers.AGY_READS) are harmless; every other denied tool outside agy's own folder is refused (DeniedReads).
7. The refusal pattern backtracked quadratically ("Permission denied for" repeated, 250 KB, 16 s). The match is linear now (LinearRefusalMatch).
8. write_brief's final error dropped agy's own message (the refused actions, the plan file, the log) after two unreadable answers.

The agy streams are built from the captures in tests/agy_streams (blanked, retargeted, raw) as the other 0.19.2 modules do."""

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import providers, readiness, wish
from nightweaver.cli import plan_goal
from nightweaver.engine import Run
from nightweaver.providers import SCHEMAS, UNREADABLE, ProviderUnavailable
from nightweaver.store import Store
import test_windows as fixtures
from test_release_0191_gemini import Hermetic
from test_release_0192_agy import BARE, LOADED, at_home, blanked, put, raw, retargeted, rules_output, steps, tooled
from test_release_0192_agy_layers import DELEGATES, INIT_TOOLS, REPO, WRITERS, Layered

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


# What agy 1.5.17 writes when a deny rule ends a tool step (tests/agy_streams/exp-06, 08, 10), and the same for a read a user's own rule denied.
REFUSAL = 'permission check failed for write_file "x": Permission denied for write_file(x). Matches user-configured deny rule.'
READ_DENIED = 'permission check failed for read_file "C:/work/repo/.env": Permission denied for read_file(C:/work/repo/.env). Matches user-configured deny rule.'
# agy 1.5.17's pure read tools (the init event's list, plus read_file, which the streams show): the only denied tools that are not a refusal.
READS = ("read_file", "view_file", "list_dir", "grep_search", "find_by_name", "read_url_content", "search_web", "read_resource", "list_resources",
         "read_browser_page")
# Names that are not in that list, whether agy announces them or not: a denied one is an attempt to act.
ACTING = ("update_file", "move_file", "mkdir", "Run_Command", "mcp_github_create_issue", "mcp__server__tool", "browser_input",
          "execute_browser_javascript", "send_message", "schedule", "open_browser_url", "browser_click_element", "manage_task", "ask_permission",
          "capture_browser_screenshot", "some_tool_nobody_has_seen")


class DeniedReads(Layered):
    """Findings 3 and 6: a denied step is a refusal unless its tool is one of agy's pure read tools. A user's own rule on a read
    (read_file(**/.env)) is a denied step; anything else agy denied, known or not, is a read-only role trying to act."""

    def test_the_allow_list_is_the_pure_reads_of_the_tool_list_agy_announces_and_nothing_else(self):
        self.assertEqual(sorted(providers.AGY_READS), sorted(READS))
        self.assertEqual(set(READS) - set(INIT_TOOLS), {"read_file"}, "read_file is in the streams (exp-06, 08, 09, 10), not in the init list")
        self.assertFalse(set(READS) & (WRITERS | DELEGATES), "no writer or subagent launcher is a read")
        self.assertFalse([name for name in READS if providers.agy_writes(name)])
        for name in READS:
            self.assertTrue(providers.agy_reads(name), name)
            self.assertTrue(providers.agy_reads(name.upper()), name.upper())
        for name in set(INIT_TOOLS) - set(READS) | set(ACTING) | {"", "read_file2", "read_file ", "xread_file", "mcp_read_file"}:
            self.assertFalse(providers.agy_reads(name), name)

    def test_every_denied_tool_outside_the_allow_list_is_refused(self):
        for name in sorted(set(INIT_TOOLS) - set(READS) - DELEGATES | set(ACTING)):
            with self.subTest(name=name):
                found = self.stream(2, "reviewer", tooled(2, (name, {"Path": REPO + "\\calc.py"}, REFUSAL)))
                self.assertEqual((len(found["denied"]), len(found["refused"])), (1, 1), f"{name}: denied, and not a read")
                self.assertEqual(found["wrote"], [])

    def test_a_denied_unrecognised_tool_discards_the_answer(self):
        for name in ACTING:
            for mode in ("reviewer", "judge"):
                with self.subTest(name=name, mode=mode):
                    error = self.refused_call(tooled(2, (name, {"Path": REPO + "\\calc.py"}, REFUSAL)), mode)
                    self.assertEqual(error.kind, "read_only_refused")
                    self.assertIn(name, str(error))

    def test_a_denied_tool_that_names_no_path_is_refused_too(self):
        for name in ACTING:
            with self.subTest(name=name):
                self.assertEqual(len(self.stream(2, "reviewer", tooled(2, (name, {}, REFUSAL)))["refused"]), 1)

    def test_names_are_compared_without_regard_to_case(self):
        for name in READS:
            with self.subTest(read=name):
                for spelled in (name.upper(), name.title(), name.swapcase()):
                    found = self.stream(2, "reviewer", tooled(2, (spelled, {"Path": REPO + "\\.env"}, READ_DENIED)))
                    self.assertEqual((len(found["denied"]), found["refused"]), (1, []), spelled)
        brain = str(self.brain)
        for name in ("Run_Command", "WRITE_FILE", "Edit_File", "Call_MCP_Tool", "Notebook_Execution", "Delete_Knowledge", "Delete_File", "APPLY_PATCH"):
            with self.subTest(writer=name):
                self.assertTrue(providers.agy_writes(name), name)
                self.assertEqual(len(self.stream(2, "reviewer", tooled(2, (name, {"Path": REPO + "\\calc.py"}, REFUSAL)))["refused"]), 1)
                self.assertEqual(len(self.stream(2, "reviewer", tooled(2, (name, {"Path": REPO + "\\calc.py"})))["wrote"]), 1, "completed too")
        self.assertEqual(len(self.stream(2, "reviewer", tooled(2, ("Run_Command", {"Path": brain + "\\x.md"}, REFUSAL)))["refused"]), 1,
                         "a runner is never exempt for a path, in any case")

    def test_a_denied_unrecognised_tool_inside_agys_own_folder_is_still_not_counted(self):
        found = self.stream(2, "reviewer", tooled(2, ("mkdir", {"Path": str(self.brain / "plan")}, REFUSAL)))
        self.assertEqual((len(found["denied"]), found["refused"]), (1, []))

    def test_a_denied_read_next_to_a_denied_unrecognised_tool_counts_only_the_tool(self):
        found = self.stream(2, "reviewer", tooled(2, ("read_file", {"Path": REPO + "\\.env"}, READ_DENIED),
                                                  ("update_file", {"Path": REPO + "\\calc.py"}, REFUSAL), ("search_web", {}, READ_DENIED)))
        self.assertEqual([entry.split()[0] for entry in found["denied"]], ["read_file", "update_file", "search_web"])
        self.assertEqual([entry.split()[0] for entry in found["refused"]], ["update_file"])

    def test_a_denied_read_is_denied_and_never_refused(self):
        for name in READS:
            with self.subTest(name=name):
                found = self.stream(2, "reviewer", tooled(2, (name, {"Path": REPO + "\\.env"}, READ_DENIED)))
                self.assertEqual(found["refused"], [], f"{name}: a denied read is not an attempt to change anything")
                self.assertEqual([entry.split()[0] for entry in found["denied"]], [name], "it still shows in the messages")
                self.assertEqual(found["wrote"], [])

    def test_a_denied_read_does_not_discard_a_good_answer(self):
        for name in READS:
            for mode in ("reviewer", "judge"):
                with self.subTest(name=name, mode=mode):
                    (code, _, answer), _ = self.call(tooled(2, (name, {"Path": REPO + "\\.env"}, READ_DENIED)), mode=mode)
                    self.assertEqual(code, 0)
                    self.assertIn("verdict", answer)

    def test_a_denied_read_appears_in_the_message_of_a_silent_call_and_never_raises_on_its_own(self):
        found = self.stream(2, "reviewer", tooled(2, ("read_file", {"Path": REPO + "\\.env"}, READ_DENIED)))
        message = providers.agy_silence(found, "reviewer", "call.jsonl")
        self.assertIn("refused 1 tool action", message)
        self.assertIn("read_file", message)

    def test_every_tool_that_changes_something_is_still_a_refusal_when_denied(self):
        for name, params in (("edit_file", {"Path": REPO + "\\calc.py"}), ("write_file", {"Path": REPO + "\\probe.txt"}),
                             ("run_command", {"CommandLine": "Set-Content probe.txt x"}), ("write_to_file", {"TargetFile": REPO + "\\a.txt"}),
                             ("apply_patch", {"Path": REPO + "\\calc.py"}), ("delete_file", {"Path": REPO + "\\calc.py"}),
                             ("call_mcp_tool", {}), ("", {"Path": REPO + "\\calc.py"})):
            with self.subTest(name=name):
                found = self.stream(2, "reviewer", tooled(2, (name, params, REFUSAL)))
                self.assertEqual(len(found["refused"]), 1)
                self.assertEqual(len(found["denied"]), 1)
                error = self.refused_call(tooled(2, (name, params, REFUSAL)), "reviewer")
                self.assertEqual(error.kind, "read_only_refused")

    def test_a_refusal_inside_agys_own_folder_is_still_not_counted(self):
        found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": str(self.brain / "plan" / "implementation_plan.md")}, REFUSAL)))
        self.assertEqual((len(found["denied"]), found["refused"]), (1, []))

    def test_a_denied_read_next_to_a_refused_write_counts_only_the_write(self):
        found = self.stream(2, "reviewer", tooled(2, ("read_file", {"Path": REPO + "\\.env"}, READ_DENIED),
                                                  ("edit_file", {"Path": REPO + "\\calc.py"}, REFUSAL)))
        self.assertEqual([entry.split()[0] for entry in found["denied"]], ["read_file", "edit_file"])
        self.assertEqual([entry.split()[0] for entry in found["refused"]], ["edit_file"])

    def test_the_real_captures_still_read_as_before(self):
        self.assertEqual([entry.split()[0] for entry in self.stream(6, "reviewer")["refused"]], ["edit_file", "write_file"])
        self.assertEqual([entry.split()[0] for entry in self.stream(8, "reviewer")["refused"]], ["run_command"])
        self.assertEqual([entry.split()[0] for entry in self.stream(10, "reviewer")["refused"]], ["write_file", "write_file"])
        for number in (6, 8, 9, 10):
            for step in steps(number, "tool", "ERROR"):
                message = step["tool_info"]["error"]["message"]
                self.assertTrue(providers.agy_denied(message), f"exp-{number:02d}: {message}")
        for number in range(1, 11):  # nothing else in the ten captures changed: no step of a role that was not refused is refused now
            found = self.stream(number, "reviewer")
            self.assertEqual(found["refused"], [e for e in found["denied"] if not e.startswith("read_file")], f"exp-{number:02d}")


class DenyRuleText(Layered):
    """Finding 3, second half: the match is agy's own refusal text, not any occurrence of the words "deny rule"."""

    def test_an_error_that_only_echoes_a_file_name_with_those_words_is_not_a_refusal(self):
        for name, params, message in (
                ("read_file", {"Path": REPO + "\\docs\\deny rule list.md"}, "open C:\\work\\repo\\docs\\deny rule list.md: The system cannot find the file specified."),
                ("write_file", {"Path": REPO + "\\docs\\deny rule list.md"}, "open C:\\work\\repo\\docs\\deny rule list.md: Access is denied."),
                ("run_command", {"CommandLine": "type deny rule.txt"}, "The deny rule.txt file is locked by another process"),
                ("edit_file", {"Path": REPO + "\\a.py"}, "no deny rule matched; the target text was not found in the file"),
                ("write_file", {"Path": REPO + "\\Matches user-configured deny rule.md"},
                 "open C:\\work\\repo\\Matches user-configured deny rule.md: The system cannot find the file specified."),
                ("write_file", {"Path": REPO + "\\docs\\deny rule list.md"},
                 "Permission denied for write_file(C:/work/repo/docs/deny rule list.md): the file is read-only on disk")):
            with self.subTest(name=name, message=message):
                found = self.stream(2, "reviewer", tooled(2, (name, params, message)))
                self.assertEqual((found["denied"], found["refused"], found["wrote"]), ([], [], []))
                (code, _, answer), _ = self.call(tooled(2, (name, params, message)), mode="reviewer")
                self.assertEqual(code, 0, "an ordinary tool error does not discard the answer")

    def test_agys_own_refusal_is_recognised_in_its_real_shapes(self):
        for message in (REFUSAL, READ_DENIED,
                        'permission check failed for unsandboxed "git diff": Permission denied for unsandboxed(git diff). Matches user-configured deny rule.',
                        "Permission denied for run_command(Set-Content a.txt 'x'\nGet-Content a.txt). Matches user-configured deny rule.",
                        "PERMISSION DENIED FOR write_file(x). MATCHES USER-CONFIGURED DENY RULE",
                        REFUSAL + "\n", REFUSAL + " (rule 1 of 5)"):
            with self.subTest(message=message):
                found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": REPO + "\\x"}, message)))
                self.assertEqual(len(found["refused"]), 1)

    def test_an_ordinary_error_in_a_writing_tool_is_neither_denied_nor_a_violation(self):
        found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": REPO + "\\x"}, "disk full")))
        self.assertEqual((found["denied"], found["refused"], found["wrote"]), ([], [], []))


# Round 3's pattern, kept here to show the linear match says the same thing on short messages.
OLD_DENIED = re.compile(r"Permission denied for\b.*\bMatches user-configured deny rule\b", re.I | re.S)
MEGABYTE = 1 << 20


def repeated(piece, size=MEGABYTE):
    return piece * (size // len(piece) + 1)


class LinearRefusalMatch(Layered):
    """Finding 7: the refusal pattern was quadratic ("Permission denied for" repeated, no second half: 250 KB took 16 s). agy_denied is linear."""

    def timed(self, message):
        started = time.perf_counter()
        outcome = providers.agy_denied(message)
        return outcome, time.perf_counter() - started

    def test_a_megabyte_of_the_first_half_and_no_second_half_is_classified_in_well_under_a_second(self):
        outcome, took = self.timed(repeated("Permission denied for "))
        self.assertFalse(outcome)
        self.assertLess(took, 1.0, f"{took:.2f} s for a 1 MB message")

    def test_other_megabyte_shapes_are_linear_and_classified_correctly(self):
        for label, message, expected in (
                ("first half on every line", repeated("Permission denied for\n"), False),
                ("first half and a near miss of the second", repeated("Permission denied for ") + "Matches user-configured deny rul", False),
                ("second half only", repeated("Matches user-configured deny rule. "), False),
                ("second half before the first", repeated("Matches user-configured deny rule. ") + "Permission denied for x", False),
                ("the plural of the second half", repeated("Permission denied for ") + "Matches user-configured deny rules", False),
                ("a near miss of the second half everywhere", "Permission denied for " + repeated("Matches user-configured deny rul "), False),
                ("the first half followed by a word character", repeated("Permission denied forever "), False),
                ("the real refusal at the end", repeated("Permission denied for ") + "Matches user-configured deny rule.", True),
                ("a real refusal after a megabyte of other text", "Permission denied for write_file(" + "x" * MEGABYTE + "). Matches user-configured deny rule.", True)):
            with self.subTest(label=label):
                outcome, took = self.timed(message)
                self.assertEqual(outcome, expected)
                self.assertLess(took, 1.0, f"{label}: {took:.2f} s")

    def test_a_long_error_in_a_step_is_classified_without_stalling_the_stream_parser(self):
        for message, denied in ((repeated("Permission denied for "), 0), (repeated("Permission denied for ") + "Matches user-configured deny rule.", 1)):
            with self.subTest(denied=denied):
                started = time.perf_counter()
                found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": REPO + "\\x"}, message)))
                self.assertLess(time.perf_counter() - started, 3.0)
                self.assertEqual((len(found["denied"]), len(found["refused"])), (denied, denied), "the long line was parsed, not dropped")

    def test_the_real_refusals_in_the_captures_still_match_and_the_echo_of_a_file_name_still_does_not(self):
        real = [step["tool_info"]["error"]["message"] for number in (6, 8, 9, 10) for step in steps(number, "tool", "ERROR")]
        self.assertEqual(len(real), 5, "exp-06: edit_file and write_file, exp-08: run_command, exp-10: two write_file, exp-09: none")
        for message in real:
            self.assertTrue(providers.agy_denied(message), message)
        for message in ("open C:\\work\\repo\\docs\\deny rule list.md: The system cannot find the file specified.",
                        "Permission denied for write_file(x): the file is read-only on disk", "Matches user-configured deny rule.",
                        "Matches user-configured deny rule. Permission denied for x", "", " "):
            self.assertFalse(providers.agy_denied(message), message)

    def test_it_says_what_the_old_pattern_said_on_short_messages(self):
        pieces = ("Permission denied for", "permission DENIED for ", "Permission denied for", " Matches user-configured deny rule", "Matches user-configured deny rule.",
                  "matches user-configured deny rules", "forever", "x", " ", "\n", "(", "write_file(x).", "deny rule", "Permission denied", "_")
        chance = random.Random(4)
        shapes = set()
        for _ in range(4000):
            message = "".join(chance.choice(pieces) for _ in range(chance.randint(0, 7)))
            self.assertEqual(providers.agy_denied(message), bool(OLD_DENIED.search(message)), repr(message))
            shapes.add(providers.agy_denied(message))
        self.assertEqual(shapes, {True, False}, "the sample holds both answers")


class BriefWriterAsksAgain(Layered):
    """Finding 1 of the round: write_brief asks a second time after an answer agy gave nothing readable for, as it did at e65b09b, where an
    empty answer was a ValueError from read_brief. A failure of any other kind still ends it at once."""

    BRIEF = '{"title": "Fix sub", "objective": "Make sub subtract.", "acceptance": ["sub(5, 3) is 2", "add is unchanged", "tests pass", "README lists sub"], "visual": false}\n'

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(wish, "brief_prompt", return_value="Write the brief."))  # the repository's layout is not under test

    def write(self, *streams):
        """wish.write_brief over an agy that answers each call from the next of these streams (the last one repeats): the folder's brief or the
        ValueError it raised, and the prompt of every call."""
        prompts = []

        def fake(argv, cwd, log, timeout, **options):
            prompts.append(json.loads(options["prompt"])["message"]["content"])
            put(log, at_home(streams[min(len(prompts), len(streams)) - 1], self.root / "user"))
            return 0

        folder = self.root / "brief"
        folder.mkdir(exist_ok=True)
        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            try:
                outcome = wish.write_brief({"kind": "antigravity", "executable": sys.executable}, {"repository": str(self.root)}, "wish", folder)
            except ValueError as error:
                outcome = error
        return outcome, prompts

    def good(self):
        return retargeted(1, None, text=self.BRIEF)

    def test_an_empty_answer_is_asked_again_and_the_second_is_taken(self):
        (brief, log, attempt), prompts = self.write(blanked(1), self.good())
        self.assertEqual((brief["title"], attempt, len(prompts)), ("Fix sub", 2, 2))
        self.assertTrue(log.name.endswith("brief-writer-2.jsonl"))
        self.assertNotIn("previous answer was rejected", prompts[0])
        self.assertIn("Your previous answer was rejected: it was empty", prompts[1])
        self.assertIn("Return the corrected JSON object only.", prompts[1])

    def test_an_answer_given_after_a_refused_edit_is_asked_again_and_the_second_is_taken(self):
        refused = tooled(1, ("write_file", {"Path": REPO + "\\probe.txt"}, REFUSAL))
        (brief, _, attempt), prompts = self.write(refused, self.good())
        self.assertEqual((brief["title"], attempt, len(prompts)), ("Fix sub", 2, 2))
        self.assertIn("Your previous answer was rejected: it came after an attempt to change files or run commands", prompts[1])

    def test_two_unreadable_answers_end_in_the_writers_own_message_after_exactly_two_calls(self):
        for streams in ((blanked(1),), (tooled(1, ("write_file", {"Path": REPO + "\\probe.txt"}, REFUSAL)),),
                        (blanked(1), tooled(1, ("write_file", {"Path": REPO + "\\probe.txt"}, REFUSAL)))):
            with self.subTest(streams=len(streams)):
                error, prompts = self.write(*streams)
                self.assertIsInstance(error, ValueError)
                self.assertIn("returned no valid brief twice", str(error))
                self.assertIn("Nothing was registered", str(error))
                self.assertEqual(len(prompts), 2)

    def test_the_final_error_keeps_what_agy_said_on_the_last_call(self):
        refused = tooled(1, ("write_file", {"Path": REPO + "\\probe.txt"}, REFUSAL), ("update_file", {"Path": REPO + "\\calc.py"}, REFUSAL))
        for label, streams, said, absent in (
                ("silent twice", (blanked(1),), ("returned no answer", "no agent message with text", "brief call (status SUCCESS)"), ("refused 2",)),
                ("refused twice", (refused,), ("tried 2 actions that change files or run commands", "refused under the permission rules",
                                               "write_file", "update_file", "so it is not taken as a brief result"), ("returned no answer",)),
                ("silent, then refused", (blanked(1), refused), ("tried 2 actions", "update_file"), ("returned no answer",)),
                ("refused, then silent", (refused, blanked(1)), ("returned no answer",), ("tried 2 actions", "update_file")),
                ("silent after the plan file", (blanked(5),), ("implementation plan", "implementation_plan.md"), ())):
            with self.subTest(label=label):
                error, prompts = self.write(*streams)
                text = str(error)
                self.assertIsInstance(error, ValueError)
                self.assertEqual(len(prompts), 2)
                self.assertIn("returned no valid brief twice", text)
                self.assertIn("The last call said: The Antigravity", text)
                for part in said:
                    self.assertIn(part, text)
                for part in absent:
                    self.assertNotIn(part, text)
                self.assertIn("brief-writer-2.jsonl", text, "the log path of the last call is in agy's message")
                self.assertTrue(text.endswith(f"Nothing was registered. See {self.root / 'brief'}"), text[-120:])
                self.assertNotIn("..", text.replace("...", ""), "no doubled full stop where the two messages join")

    def test_the_last_message_of_two_unreadable_calls_is_the_one_kept(self):
        answers = [ProviderUnavailable("first said nothing", "no_answer"), ProviderUnavailable("second refused an edit", "read_only_refused")]
        with patch.object(wish, "invoke", side_effect=answers):
            with self.assertRaises(ValueError) as caught:
                wish.write_brief({"kind": "antigravity"}, {"repository": str(self.root)}, "wish", self.root)
        text = str(caught.exception)
        self.assertIn("(it came after an attempt to change files or run commands, which this read-only call refuses)", text)
        self.assertIn("The last call said: second refused an edit.", text)
        self.assertNotIn("first said nothing", text)

    def test_an_unreadable_call_then_an_invalid_brief_reports_the_invalid_brief_only(self):
        answers = [ProviderUnavailable("agy returned no answer", "no_answer"), (0, None, "not json at all")]
        with patch.object(wish, "invoke", side_effect=answers):
            with self.assertRaises(ValueError) as caught:
                wish.write_brief({"kind": "antigravity"}, {"repository": str(self.root)}, "wish", self.root)
        self.assertIn("returned no valid brief twice", str(caught.exception))
        self.assertNotIn("The last call said", str(caught.exception))
        self.assertNotIn("agy returned no answer", str(caught.exception))

    def test_an_invalid_brief_then_an_unreadable_call_keeps_the_unreadable_message(self):
        answers = [(0, None, "not json at all"), ProviderUnavailable("agy returned no answer!", "no_answer")]
        with patch.object(wish, "invoke", side_effect=answers):
            with self.assertRaises(ValueError) as caught:
                wish.write_brief({"kind": "antigravity"}, {"repository": str(self.root)}, "wish", self.root)
        self.assertIn("(it was empty). The last call said: agy returned no answer! Nothing was registered.", str(caught.exception))

    def test_an_unreadable_answer_then_another_failure_raises_at_once_on_the_second_call(self):
        violation = tooled(1, ("write_file", {"Path": REPO + "\\calc.py"}))
        error, prompts = self.write(blanked(1), violation)
        self.assertIsInstance(error, ValueError)
        self.assertIn("is unavailable", str(error))
        self.assertIn("read-only, but agy completed", str(error))
        self.assertEqual(len(prompts), 2)

    def test_every_other_provider_failure_raises_at_once(self):
        violation = tooled(1, ("write_file", {"Path": REPO + "\\calc.py"}))
        error, prompts = self.write(violation, self.good())
        self.assertIn("is unavailable", str(error))
        self.assertEqual(len(prompts), 1, "a read_only_violation is not asked again")
        kinds = ("transient", "usage_limit", "authentication", "configuration", "read_only_violation", "model_access", "missing_executable")
        for kind in kinds:
            with self.subTest(kind=kind):
                asked = []

                def fail(*args, **kwargs):
                    asked.append(kind)
                    raise ProviderUnavailable(f"agy said {kind}", kind)

                with patch.object(wish, "invoke", side_effect=fail):
                    with self.assertRaises(ValueError) as caught:
                        wish.write_brief({"kind": "antigravity"}, {"repository": str(self.root)}, "wish", self.root)
                self.assertIn(f"agy said {kind}", str(caught.exception))
                self.assertEqual(asked, [kind], f"{kind} must not be asked again")

    def test_the_two_unreadable_kinds_are_exactly_the_ones_asked_again(self):
        self.assertEqual(UNREADABLE, ("no_answer", "read_only_refused"))
        for kind in UNREADABLE:
            with self.subTest(kind=kind):
                answers = [ProviderUnavailable(f"agy said {kind}", kind), (0, None, self.BRIEF)]
                with patch.object(wish, "invoke", side_effect=answers):
                    brief, _, attempt = wish.write_brief({"kind": "antigravity"}, {"repository": str(self.root)}, "wish", self.root)
                self.assertEqual((brief["title"], attempt), ("Fix sub", 2))

    def test_the_initial_planner_call_never_had_a_second_attempt_and_still_has_none(self):
        """cli.plan_goal asked the planner once at e65b09b (an empty answer was a ValueError from extract_object and an unavailable provider
        raised), so no retry was lost there and none is added."""
        template = self.root / "plan.json"
        template.write_text(json.dumps({"repository": str(self.root), "title": "A project", "worker": {"kind": "deepseek"},
                                        "gates": [{"name": "Contract", "argv": [sys.executable, "check.py"]}], "acceptance": ["Deliver."]}),
                            encoding="utf-8")
        args = argparse.Namespace(template=str(template), objective="Deliver it", planner="deepseek", model="deepseek-v4-pro", profile_dir=None,
                                  start=False, enqueue=False)
        for outcome, raised in ((ProviderUnavailable("agy returned no answer", "no_answer"), ProviderUnavailable), ((0, None, ""), ValueError)):
            with self.subTest(outcome=str(outcome)), patch("nightweaver.cli.invoke", side_effect=[outcome]) as planner:
                with self.assertRaises(raised):
                    plan_goal(Store(self.root / "runtime"), args)
                self.assertEqual(planner.call_count, 1)


class DoctorAsksTwiceOnPurpose(Layered):
    """doctor --live asks agy /permissions twice per antigravity route, on purpose: the report asks in the repository (what a run's worktrees
    inherit, and where a relative executable is found) after forgetting the memory of earlier calls, and the probe's own check asks again in
    the probe's folder. Round 3 asked once, in the probe's folder; that lost a relative executable ("tools/agy.exe", looked for in the empty
    folder) and never asked in the repository, so project settings that hide the deny rules there read as ready while a run would block."""

    def agy(self, asked, answer=None, hidden=None):
        """An agy that records the folder of each /permissions question. It lists no rules in a folder named `hidden` (a project setting that
        overrides them there), and `answer` everywhere else when given."""
        def run(executable, env, *args, timeout=30, cwd=None):
            if "/permissions" in args:
                asked.append(cwd)
                return 0, BARE if hidden and cwd is not None and Path(cwd).name == hidden else answer or LOADED
            return 0, "gemini-4-neon-xhigh"
        return run

    def doctor(self, asked, *, live=True, answer=None, hidden=None, config=None):
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir(exist_ok=True)
        repository = self.root / "repository"
        repository.mkdir(exist_ok=True)
        calls = []

        def fake(argv, cwd, log, timeout, **options):
            calls.append((argv[0], cwd))
            put(log, raw(3))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=self.agy(asked, answer, hidden)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable, **(config or {})},
                                           repository, live)
        return report, calls, repository

    def test_a_live_route_is_asked_twice_first_in_the_repository_then_in_the_probes_folder(self):
        asked = []
        report, calls, repository = self.doctor(asked)
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        self.assertEqual(len(asked), 2, "once for the report and once for the probe's own check, by design")
        self.assertTrue(same_folder(asked[0], repository), "the report asks where the run's worktrees come from")
        self.assertTrue(Path(asked[1]).name.startswith("probe-"), "the probe asks in the folder it runs in")
        self.assertEqual(len(calls), 1)
        self.assertTrue(same_folder(calls[0][1], asked[1]), "the probe ran where its own question was asked")

    def test_every_doctor_run_asks_afresh_even_right_after_an_earlier_one(self):
        asked = []
        for run in (1, 2, 3):
            self.doctor(asked)
            self.assertEqual(len(asked), 2 * run, "the five-minute memory of an earlier run or call never stands in for doctor's question")

    def test_a_call_that_just_cleared_the_repository_does_not_stand_in_for_doctors_question(self):
        asked = []
        executable = shutil.which(sys.executable)
        providers.AGY_READ_ONLY[providers.read_only_key(executable, self.root / "repository")] = providers.monotonic()
        providers.AGY_READ_ONLY[(executable, "a worktree")] = providers.monotonic()
        self.doctor(asked)
        self.assertEqual(len(asked), 2, "asked in the repository even though the memory said it was clear a moment ago")
        self.assertTrue(same_folder(asked[0], self.root / "repository"))
        self.assertNotIn((executable, "a worktree"), providers.AGY_READ_ONLY)

    def test_an_executable_given_relative_to_the_repository_is_found_there(self):
        """Round 3's regression: the report ran in the empty probe folder, where `tools/agy.exe` does not exist, and said missing_executable."""
        repository = self.root / "repository"
        (repository / "tools").mkdir(parents=True)
        agy = repository / "tools" / "agy.exe"
        agy.write_bytes(b"")
        agy.chmod(0o755)
        for live in (True, False):
            with self.subTest(live=live):
                asked = []
                report, calls, _ = self.doctor(asked, live=live, config={"executable": "tools/agy.exe"})
                self.assertEqual(report["status"], "ready" if live else "configured", report)
                self.assertTrue(same_folder(report["executable"], agy), report["executable"])
                self.assertTrue(same_folder(asked[0], repository))
                if live:
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(same_folder(calls[0][0], agy), "the probe runs the executable the report found")

    def test_a_relative_executable_that_is_not_in_the_repository_is_still_missing(self):
        report, calls, _ = self.doctor([], config={"executable": "tools/agy.exe"})
        self.assertEqual((report["status"], report["reason"]), ("blocked", "missing_executable"))
        self.assertEqual(calls, [])

    def test_rules_hidden_by_the_repository_and_loaded_in_a_clean_folder_are_not_ready(self):
        """Round 3's regression: only the probe's clean folder was asked, so a repository whose project settings hide the deny rules read as
        ready while a run (its worktrees inherit those settings) would be blocked before every reviewer call."""
        asked = []
        report, calls, repository = self.doctor(asked, hidden="repository")
        self.assertNotEqual(report["status"], "ready")
        self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("unverified", "read_only_unverified", "missing"))
        self.assertTrue(same_folder(asked[0], repository), "the repository was asked, and said the rules were not loaded")
        self.assertTrue(Path(asked[1]).name.startswith("probe-"), "the clean probe folder was asked too, and its answer does not hide that")
        self.assertEqual(len(calls), 1)

    def test_the_plain_report_says_missing_for_a_repository_that_hides_the_rules(self):
        asked = []
        report, calls, _ = self.doctor(asked, live=False, hidden="repository")
        self.assertEqual((report["status"], report["read_only_rules"]), ("configured", "missing"))
        self.assertIn("did not report Nightweaver's read-only permission rules as loaded", report["action"])
        self.assertEqual(calls, [])

    def test_an_agy_that_loads_no_rules_anywhere_is_blocked_before_a_model_call(self):
        asked = []
        report, calls, _ = self.doctor(asked, answer=BARE)
        self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("blocked", "configuration", "missing"))
        self.assertEqual(calls, [])
        self.assertEqual(len(asked), 2, "the probe's own check asked in its folder and stopped the call")

    def test_the_plain_report_asks_once_in_the_repository_and_makes_no_probe_folder(self):
        asked = []
        report, calls, repository = self.doctor(asked, live=False)
        self.assertEqual((report["status"], report["read_only_rules"]), ("configured", "enforced"))
        self.assertEqual(len(asked), 1)
        self.assertTrue(same_folder(asked[0], repository))
        self.assertEqual(calls, [])
        self.assertFalse(list((self.root / "store").glob("probe-*")))

    def test_the_probe_folder_is_removed_when_doctor_is_done(self):
        asked = []
        self.doctor(asked)
        self.assertFalse(list((self.root / "store").glob("probe-*")))
        self.assertFalse(Path(asked[1]).exists())

    def test_a_store_that_cannot_hold_a_probe_folder_is_launch_failed_not_a_crash(self):
        store = SimpleNamespace(root=self.root / "no such store")
        with patch("nightweaver.providers.agy_run", side_effect=self.agy([])), patch("nightweaver.providers.execute") as model:
            report = readiness.check_route(store, {"kind": "antigravity", "executable": sys.executable}, self.root, True)
        self.assertEqual((report["status"], report["reason"]), ("blocked", "launch_failed"))
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
