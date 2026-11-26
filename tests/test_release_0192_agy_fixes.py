"""Nightweaver 0.19.2, review fixes, round 2.

A second skeptic reproduced two more findings against the antigravity read-only layers, each with an offline harness (no model call):

1. A silent or refused agy judge blocked the run where 0.19.1 degraded to an unscored tie. invoke() now raises `no_answer` and
   `read_only_refused` for every read-only role, and the engine retries them as transient and then blocks. That is right for a role with no way
   past an unreadable answer (the reviewer, the pursuit judge, the wave planner), but the exploration judge, the direction planner and the
   rescue diagnoser already answer an unreadable reply themselves (ask once more, then an unscored tie or a skipped stage, "so one bad judge
   call cannot wedge the run"). They now take those two kinds as an unreadable answer again.
2. The loaded-rules guard asked agy about /permissions in the controller's folder, not in the folder the call runs in, so the project scope of
   that folder (which the repository's own files can change) was never part of the check.

Round 3 (the last four classes) reproduced three minor findings from the adversarial review of 0.19.2:

3. A denied READ counted as a refusal: a user's own deny rule on read_file(**/.env) ended the call as `read_only_refused`, and the match was
   any "deny rule" anywhere in an error message, so an error that only echoed a file name with those words counted too.
4. The wish brief writer lost its second attempt: an empty or refused agy answer is now a ProviderUnavailable out of invoke(), and
   write_brief gave up after one call, where e65b09b asked again.
5. `doctor --live` asked agy /permissions twice per antigravity route.

The agy streams are built from the captures in tests/agy_streams (blanked, retargeted, raw) as the other 0.19.2 modules do."""

import argparse
import json
import os
import shutil
import sys
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
from test_release_0192_agy_layers import REPO, Layered

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

    def test_doctor_asks_once_in_the_probes_folder_and_forgets_every_folder_of_that_agy_first(self):
        asked = []
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        repository = self.folder("repository")
        executable = shutil.which(sys.executable)
        providers.AGY_READ_ONLY[(executable, "a worktree the last run used")] = providers.monotonic()
        providers.AGY_READ_ONLY[providers.read_only_key(executable, repository)] = providers.monotonic()
        providers.AGY_READ_ONLY[("other agy", "kept")] = providers.monotonic()

        def fake(argv, cwd, log, timeout, **options):
            put(log, raw(3))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=self.stand_in(asked)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}, repository, True)
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        self.assertEqual(len(asked), 1, "one route, one question: the probe's own check reuses the answer the report just got")
        self.assertTrue(Path(asked[0]).name.startswith("probe-"), "asked in the folder the probe runs in, which is the folder that is checked")
        self.assertNotIn((executable, "a worktree the last run used"), providers.AGY_READ_ONLY)
        self.assertNotIn(providers.read_only_key(executable, repository), providers.AGY_READ_ONLY,
                         "an earlier call's memory of the repository did not stand in for doctor's own question")
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
READS = ("read_file", "view_file", "list_dir", "grep_search", "find_by_name", "read_url_content")


class DeniedReads(Layered):
    """Finding 3: only a tool that would change something is a refusal. A user's own rule on a read (read_file(**/.env)) is a denied step."""

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
                self.assertTrue(providers.AGY_DENIED.search(message), f"exp-{number:02d}: {message}")


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


class DoctorAsksOncePerRoute(Layered):
    """Finding 2 of the round: doctor --live asked agy /permissions twice for one antigravity route, once for the report and once for the
    probe's own check. It asks once, in the folder the probe runs in, and never from an earlier call's memory."""

    def agy(self, asked, answer=None):
        def run(executable, env, *args, timeout=30, cwd=None):
            if "/permissions" in args:
                asked.append(cwd)
                return 0, answer or LOADED
            return 0, "gemini-4-neon-xhigh"
        return run

    def doctor(self, asked, *, live=True, answer=None, config=None):
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir(exist_ok=True)
        repository = self.root / "repository"
        repository.mkdir(exist_ok=True)
        calls = []

        def fake(argv, cwd, log, timeout, **options):
            calls.append(cwd)
            put(log, raw(3))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=self.agy(asked, answer)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable, **(config or {})},
                                           repository, live)
        return report, calls, repository

    def test_one_route_is_asked_once_and_in_the_probes_folder(self):
        asked = []
        report, calls, _ = self.doctor(asked)
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        self.assertEqual(len(asked), 1, "the report and the probe's own check share one answer")
        self.assertEqual(len(calls), 1)
        self.assertTrue(Path(asked[0]).name.startswith("probe-"))
        self.assertTrue(same_folder(calls[0], asked[0]), "the probe ran where the question was asked")

    def test_every_doctor_run_asks_afresh_even_right_after_an_earlier_one(self):
        asked = []
        self.doctor(asked)
        self.assertEqual(len(asked), 1)
        self.doctor(asked)
        self.assertEqual(len(asked), 2, "the second doctor run does not take the first one's answer from the five-minute memory")
        self.doctor(asked)
        self.assertEqual(len(asked), 3)

    def test_a_call_that_just_cleared_the_repository_does_not_stand_in_for_doctors_question(self):
        asked = []
        executable = shutil.which(sys.executable)
        providers.AGY_READ_ONLY[providers.read_only_key(executable, self.root / "repository")] = providers.monotonic()
        providers.AGY_READ_ONLY[(executable, "a worktree")] = providers.monotonic()
        self.doctor(asked)
        self.assertEqual(len(asked), 1, "asked once even though the memory said both folders were clear a moment ago")
        self.assertNotIn((executable, "a worktree"), providers.AGY_READ_ONLY)

    def test_an_agy_that_loads_no_rules_in_the_probes_folder_is_still_blocked_before_a_model_call(self):
        asked = []
        report, calls, _ = self.doctor(asked, answer=BARE)
        self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("blocked", "configuration", "missing"))
        self.assertEqual(calls, [])

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
        self.assertFalse(Path(asked[0]).exists())

    def test_a_store_that_cannot_hold_a_probe_folder_is_launch_failed_not_a_crash(self):
        store = SimpleNamespace(root=self.root / "no such store")
        with patch("nightweaver.providers.agy_run", side_effect=self.agy([])), patch("nightweaver.providers.execute") as model:
            report = readiness.check_route(store, {"kind": "antigravity", "executable": sys.executable}, self.root, True)
        self.assertEqual((report["status"], report["reason"]), ("blocked", "launch_failed"))
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
