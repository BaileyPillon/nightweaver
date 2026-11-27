"""Nightweaver 0.19.4: the goal planner has its own time limit (planner_timeout, default agent_timeout, never a fixed 240 s), and a call the
controller ended (exit 124, 125 or 126) is said in words, naming the setting, by every role that turns an exit code into a message.

Everything here uses stand-in providers: a patched `invoke`, a `command` provider that sleeps or exits, or scripted pursuit fixtures. No model
is called."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch

import test_release_019_wish as wish_tests
import test_windows as fixtures

from nightweaver import engine, process, wish as wishing
from nightweaver.cli import main, planner_limit, planner_seconds
from nightweaver.engine import Run
from nightweaver.process import CONTROLLER_EXITS, DEFAULT_MAX_LOG, controller_exit_text
from nightweaver.providers import invoke as real_invoke
from nightweaver.spec import validate

ROOT = Path(__file__).resolve().parent.parent
PURSUIT = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
EXPLORE = str(Path(__file__).with_name("explore_fixture.py").resolve())
RESCUE = str(Path(__file__).with_name("rescue_fixture.py").resolve())
PLAN = json.dumps({"milestones": [{"title": "Do it", "task": "Do the whole thing"}]})
KEPT = "a run keeps the plan it was created with, so raise it in the plan and add the plan as a new run"
SLEEPER = {"kind": "command", "argv": [sys.executable, "-c", "import time; time.sleep(120)"]}
FAST = {"initial_seconds": 1, "max_seconds": 1}


def judged(score, met=False):
    return {"verdict": "MET" if met else "NOT_MET", "score": score, "reason": f"Reason {score}",
            "gaps": [] if met else [{"title": f"Gap at {score}", "detail": f"Detail {score}"}],
            "criteria": [{"criterion": "Looks right", "met": met, "note": "Observed"}]}


def plan(*titles):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles]}


class ControllerExitWords(unittest.TestCase):
    """The one helper every role message uses (process.controller_exit_text)."""

    def test_the_three_controller_exits_are_the_ones_execute_returns(self):
        self.assertEqual(CONTROLLER_EXITS, (124, 125, 126))
        self.assertEqual(DEFAULT_MAX_LOG, 16 * 1024 * 1024)
        from inspect import signature
        self.assertEqual(signature(process.execute).parameters["max_log_bytes"].default, DEFAULT_MAX_LOG)

    def test_124_is_the_time_limit_and_names_the_setting_and_the_seconds(self):
        words = controller_exit_text(124, "The planner", (240, "planner_timeout; raise it in the template or with --planner-timeout"))
        self.assertEqual(words, "The planner timed out after 240 s, its time limit (planner_timeout; raise it in the template or with --planner-timeout)")

    def test_125_is_the_idle_watchdog_and_names_the_setting_and_the_silence(self):
        words = controller_exit_text(125, "The judge", (1800, "agent_timeout"), (900, "budgets.idle_timeout in the plan"))
        self.assertEqual(words, "The judge was stopped by the idle watchdog after 900 s without output, its idle timeout (budgets.idle_timeout in the plan)")
        self.assertIn("idle watchdog", controller_exit_text(125, "The planner", (1, "x")), "no idle value is known for a call without a watchdog")
        self.assertNotIn(" after ", controller_exit_text(125, "The planner", (1, "x")).split(", its idle")[0].replace("stopped by", ""))

    def test_126_is_the_output_limit_and_gives_the_size_in_megabytes(self):
        words = controller_exit_text(126, "The reviewer", (1, "x"), None, (32 * 1024 * 1024, "budgets.max_log_bytes in the plan"))
        self.assertEqual(words, "The reviewer was stopped at its output limit: its log grew past 32 MB (budgets.max_log_bytes in the plan)")
        self.assertIn("past 16 MB", controller_exit_text(126, "The planner", (1, "x")))
        self.assertIn("past 0.5 MB", controller_exit_text(126, "The planner", (1, "x"), None, (512 * 1024, "x")))

    def test_every_other_exit_code_has_no_words_so_the_caller_keeps_its_own_text(self):
        for code in (0, 1, 2, 3, 7, 41, 55, 123, 127, 255, -1, None):
            with self.subTest(code=code):
                self.assertIsNone(controller_exit_text(code, "The planner", (240, "x"), (900, "y"), (1, "z")))


@unittest.skipUnless(os.name == "nt", "Windows controller")
class PlannerTimeoutInThePlan(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp

    def raw(self, **extra):
        return {"title": "A plan", "objective": "Produce a fixture", "repository": str(self.repo), "worker": {"kind": "command", "argv": ["x"]},
                "gates": [{"name": "g", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "One", "task": "Do one"}], **extra}

    def test_a_plan_without_it_has_no_such_key_and_a_plan_with_it_keeps_it(self):
        self.assertNotIn("planner_timeout", validate(self.raw()))
        for seconds in (1, 240, 5400, 14400):
            self.assertEqual(validate(self.raw(planner_timeout=seconds))["planner_timeout"], seconds)

    def test_it_has_the_bounds_of_agent_timeout(self):
        for bad in (0, -1, 14401, 99999, "300", 30.5, True, False, None, [300]):
            with self.subTest(planner_timeout=bad), self.assertRaisesRegex(ValueError, "Planner timeout must be between 1 and 14400 seconds"):
                validate(self.raw(planner_timeout=bad))
        for bad in (0, 14401):
            with self.subTest(agent_timeout=bad), self.assertRaisesRegex(ValueError, "Agent timeout must be between 1 and 14400 seconds"):
                validate(self.raw(agent_timeout=bad))
        self.assertEqual(validate(self.raw(agent_timeout=14400, planner_timeout=14400))["agent_timeout"], 14400)

    def test_it_is_independent_of_agent_timeout_in_either_direction(self):
        for planner, agent in ((7200, 1800), (60, 5400)):
            spec = validate(self.raw(planner_timeout=planner, agent_timeout=agent))
            self.assertEqual((spec["planner_timeout"], spec["agent_timeout"]), (planner, agent))

    def test_the_flag_accepts_whole_seconds_from_1_to_14400_only(self):
        self.assertEqual([planner_seconds(v) for v in ("1", "240", "14400")], [1, 240, 14400])
        for bad in ("0", "14401", "-3", "abc", "", "1.5", "1e3"):
            with self.subTest(value=bad), self.assertRaises(argparse.ArgumentTypeError):
                planner_seconds(bad)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class GoalPlannerTimeout(unittest.TestCase):
    """cli.plan_goal called the initial planner with a fixed 240 s and ignored agent_timeout. It now takes, in this order: --planner-timeout,
    the template's planner_timeout, the template's agent_timeout (1800 unless it sets one)."""
    setUp = fixtures.WindowsRunTest.setUp

    def cli(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def template(self, name="template.json", **extra):
        data = {"repository": str(self.repo), "title": "Planner timeout", "worker": {"kind": "command", "argv": fixtures.fixture("implement")},
                "gates": [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20}], **extra}
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def spy(self, answer=PLAN, code=0):
        calls = []

        def fake(config, mode, cwd, log, prompt, timeout, paused, *rest, **options):
            calls.append({"mode": mode, "timeout": timeout, "prompt": prompt, "log": log, "config": config})
            return code, None, answer
        return calls, patch("nightweaver.cli.invoke", side_effect=fake)

    def timeout_of(self, *flags, **template):
        calls, patcher = self.spy()
        with patcher:
            self.cli("goal", self.template(**template), "Build the thing", *flags)
        self.assertEqual([c["mode"] for c in calls], ["planner"])
        return calls[0]["timeout"]

    def test_the_precedence_is_flag_then_planner_timeout_then_agent_timeout_then_1800(self):
        cases = [((), {}, 1800), ((), {"agent_timeout": 5400}, 5400), ((), {"agent_timeout": 5400, "planner_timeout": 7200}, 7200),
                 ((), {"agent_timeout": 5400, "planner_timeout": 300}, 300), (("--planner-timeout", "600"), {"agent_timeout": 5400}, 600),
                 (("--planner-timeout", "600"), {"agent_timeout": 5400, "planner_timeout": 7200}, 600),
                 (("--planner-timeout", "14400"), {"planner_timeout": 60}, 14400), (("--planner-timeout", "1"), {}, 1)]
        for flags, template, expected in cases:
            with self.subTest(flags=flags, template=template):
                self.assertEqual(self.timeout_of(*flags, **template), expected)

    def test_the_planner_never_falls_back_to_240_seconds(self):
        for template in ({}, {"agent_timeout": 40}, {"agent_timeout": 240}, {"agent_timeout": 14400}, {"planner_timeout": 90}):
            with self.subTest(template=template):
                expected = template.get("planner_timeout", template.get("agent_timeout", 1800))
                self.assertEqual(self.timeout_of(**template), expected)
        source = (ROOT / "nightweaver" / "cli.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r'invoke\(config, "planner", [^\n]*\b240\b')

    def test_a_template_agent_timeout_below_240_is_followed_down_too(self):
        self.assertEqual(self.timeout_of(agent_timeout=40), 40, "the planner follows agent_timeout, up or down, unless planner_timeout says otherwise")

    def test_planner_limit_names_where_the_seconds_came_from(self):
        flagged = argparse.Namespace(planner_timeout=600)
        unflagged = argparse.Namespace(planner_timeout=None)
        seconds, where = planner_limit(flagged, {"planner_timeout": 7200, "agent_timeout": 5400})
        self.assertEqual(seconds, 600)
        self.assertIn("--planner-timeout", where)
        seconds, where = planner_limit(unflagged, {"planner_timeout": 7200, "agent_timeout": 5400})
        self.assertEqual(seconds, 7200)
        self.assertTrue(where.startswith("planner_timeout in the template") and "--planner-timeout" in where, where)
        seconds, where = planner_limit(argparse.Namespace(), {"agent_timeout": 5400})
        self.assertEqual(seconds, 5400)
        self.assertTrue(where.startswith("agent_timeout in the template") and "no planner_timeout is set" in where and "--planner-timeout" in where, where)

    def test_the_run_limits_line_shows_planner_timeout_only_when_one_is_set(self):
        def prompt(*flags, **template):
            out, _ = self.cli("goal", self.template(**template), "Build the thing", "--prompt-only", *flags)
            return json.loads(out)["prompt"]

        plain = prompt()
        limits = re.search(r"^Run limits: (.*)$", plain, re.M).group(1)
        self.assertEqual(json.loads(limits)["agent_timeout_seconds"], 1800)
        self.assertNotIn("planner_timeout", plain, "a prompt of a template that sets none is exactly what 0.19.3 sent")
        self.assertTrue(limits.endswith('"agent_timeout_seconds": 1800}'), limits)
        for shown, flags, template in ((3000, (), {"planner_timeout": 3000}), (2500, ("--planner-timeout", "2500"), {}),
                                       (2500, ("--planner-timeout", "2500"), {"planner_timeout": 3000})):
            with self.subTest(shown=shown):
                text = prompt(*flags, **template)
                self.assertEqual(json.loads(re.search(r"^Run limits: (.*)$", text, re.M).group(1))["planner_timeout_seconds"], shown)
                self.assertEqual(text.replace(f', "planner_timeout_seconds": {shown}', ""), plain, "nothing else in the prompt changed")

    def test_a_flag_outside_1_to_14400_is_refused_before_anything_runs(self):
        for bad in ("0", "14401", "-5", "ten", "1.5"):
            with self.subTest(flag=bad):
                calls, patcher = self.spy()
                err = StringIO()
                with patcher, redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
                    main(["--home", str(self.store.root), "goal", str(self.template()), "Build it", "--planner-timeout", bad])
                self.assertEqual((stopped.exception.code, calls), (2, []))
                self.assertIn("--planner-timeout", err.getvalue())
                self.assertIn("from 1 to 14400", err.getvalue())

    def test_the_flag_is_part_of_the_plan_the_goal_prints_and_registers(self):
        calls, patcher = self.spy()
        with patcher:
            out, _ = self.cli("goal", self.template(planner_timeout=900), "Build it", "--planner-timeout", "1200")
        self.assertEqual(json.loads(out)["planner_timeout"], 1200)
        saved = self.root / "saved.json"
        saved.write_text(PLAN, encoding="utf-8")
        with patch("nightweaver.cli.invoke", side_effect=AssertionError("a saved plan makes no model call")):
            out, _ = self.cli("goal", self.template(), "Build it", "--plan-from", saved, "--planner-timeout", "1500", "--enqueue")
        spec = self.store.get(json.loads(out)["run_id"])["spec"]
        self.assertEqual((spec["planner_timeout"], spec["agent_timeout"]), (1500, 1800))
        out, _ = self.cli("goal", self.template(), "Build it", "--plan-from", saved, "--enqueue")
        self.assertNotIn("planner_timeout", self.store.get(json.loads(out)["run_id"])["spec"], "no flag and no template field: the run carries none")

    def test_a_stand_in_that_sleeps_past_the_limit_is_told_it_timed_out_naming_the_setting(self):
        template = self.template()
        with patch("nightweaver.cli.planner_route", return_value=SLEEPER):
            began = time.monotonic()
            _, err = self.cli("goal", template, "Build it", "--planner-timeout", "2", code=1)
            self.assertLess(time.monotonic() - began, 60)
            self.assertIn("The planner timed out after 2 s, its time limit (--planner-timeout; give it a larger value, or set planner_timeout in the template).", err)
            self.assertRegex(err, r"See .+\.jsonl")
            self.assertNotIn("Planner exited", err)
            _, err = self.cli("goal", self.template("with-planner-timeout.json", planner_timeout=2, agent_timeout=5400), "Build it", code=1)
            self.assertIn("The planner timed out after 2 s, its time limit (planner_timeout in the template; raise it there or with --planner-timeout).", err)
            _, err = self.cli("goal", self.template("with-agent-timeout.json", agent_timeout=2), "Build it", code=1)
            self.assertIn("The planner timed out after 2 s, its time limit (agent_timeout in the template, which the planner follows because "
                          "no planner_timeout is set; set planner_timeout in the template or pass --planner-timeout).", err)
        self.assertEqual(self.store.runs(), [], "a planner that timed out registers nothing")

    def test_a_stand_in_that_finishes_inside_a_small_limit_is_not_cut_off(self):
        quick = {"kind": "command", "argv": [sys.executable, "-c", f"import sys; sys.stdin.read(); print({PLAN!r})"]}
        with patch("nightweaver.cli.planner_route", return_value=quick):
            out, _ = self.cli("goal", self.template(), "Build it", "--planner-timeout", "30")
        self.assertEqual(json.loads(out)["milestones"], [{"title": "Do it", "task": "Do the whole thing"}])

    def test_the_other_two_controller_exits_say_what_they_are(self):
        for code, words in ((125, "The planner was stopped by the idle watchdog, its idle timeout"),
                            (126, "The planner was stopped at its output limit: its log grew past 16 MB (a fixed limit of this call that no setting changes)")):
            with self.subTest(code=code):
                calls, patcher = self.spy(code=code)
                with patcher:
                    _, err = self.cli("goal", self.template(), "Build it", code=1)
                self.assertIn(words, err)
                self.assertRegex(err, r"\. See .+\.jsonl")
                self.assertNotIn("Planner exited", err)

    def test_any_other_exit_code_keeps_the_old_text_exactly(self):
        for code in (1, 2, 3, 7, 127, 255):
            with self.subTest(code=code):
                calls, patcher = self.spy(code=code)
                with patcher:
                    _, err = self.cli("goal", self.template(), "Build it", code=1)
                self.assertRegex(err, rf"Planner exited {code}\. See .+\.jsonl")
                self.assertNotRegex(err, r"timed out|watchdog|output limit")
        calls, patcher = self.spy()
        with patcher:
            self.cli("goal", self.template(), "Build it")
        self.assertEqual(calls[0]["mode"], "planner", "a clean exit is still a clean exit")

    def test_the_planner_gets_the_same_timeout_for_every_provider_kind(self):
        for kind in ("claude", "codex", "deepseek", "gemini", "antigravity"):
            with self.subTest(kind=kind):
                calls, patcher = self.spy()
                with patcher:
                    self.cli("goal", self.template(planner_timeout=777), "Build it", "--planner", kind)
                self.assertEqual((calls[0]["config"]["kind"], calls[0]["timeout"]), (kind, 777))


@unittest.skipUnless(os.name == "nt", "Windows controller")
class OneShotPlannerTimeout(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp

    def discovered(self):
        return {"repository": str(self.repo), "title": "Discovered", "objective": "placeholder", "worker": {"kind": "codex"},
                "gates": [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20}],
                "milestones": [{"title": "Build", "task": "Build it"}], "budgets": {"max_calls": 15, "max_seconds": 3600}}

    def cli(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ), \
                patch("nightweaver.discovery.discover", side_effect=lambda *a, **k: self.discovered()), \
                patch("nightweaver.discovery.verify_tools"):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_the_flag_is_in_the_previewed_plan(self):
        out, _ = self.cli("oneshot", self.repo, "Build it", "--preview", "--planner-timeout", "3000")
        self.assertEqual(json.loads(out)["planner_timeout"], 3000)
        out, _ = self.cli("oneshot", self.repo, "Build it", "--preview")
        self.assertNotIn("planner_timeout", json.loads(out))

    def test_the_planner_call_gets_the_flag_else_agent_timeout(self):
        for flags, expected in (((), 1800), (("--planner-timeout", "4000"), 4000)):
            with self.subTest(flags=flags):
                seen = []

                def fake(config, mode, cwd, log, prompt, timeout, paused, *rest, **options):
                    seen.append((mode, timeout))
                    return 0, None, PLAN
                with patch("nightweaver.cli.invoke", side_effect=fake):
                    out, _ = self.cli("oneshot", self.repo, "Build it", "--enqueue", *flags)
                self.assertEqual(seen, [("planner", expected)])
                spec = self.store.get(json.loads(out)["run_id"])["spec"]
                self.assertEqual(spec.get("planner_timeout"), flags and 4000 or None)

    def test_direct_registers_the_flag_without_a_planner_call(self):
        with patch("nightweaver.cli.invoke", side_effect=AssertionError("--direct skips the planner")):
            out, _ = self.cli("oneshot", self.repo, "Build it", "--direct", "--enqueue", "--planner-timeout", "900")
        self.assertEqual(self.store.get(json.loads(out)["run_id"])["spec"]["planner_timeout"], 900)

    def test_the_flag_is_checked_like_the_goal_flag(self):
        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            main(["--home", str(self.store.root), "oneshot", str(self.repo), "Build it", "--planner-timeout", "99999"])
        self.assertIn("from 1 to 14400", err.getvalue())


@unittest.skipUnless(os.name == "nt", "Windows controller")
class WishPlannerTimeout(unittest.TestCase):
    setUp = wish_tests.WishTests.setUp
    cli = wish_tests.WishTests.cli
    writer = wish_tests.WishTests.writer
    saved = wish_tests.WishTests.saved
    wishes = wish_tests.WishTests.wishes

    def test_the_flag_is_saved_in_the_template_shown_in_the_summary_and_repeated_in_the_relaunch_line(self):
        self.writer([wish_tests.fenced()])
        out, _ = self.cli("wish", self.repo, "Calm the page", "--planner-timeout", "4321")
        _, template, folder = self.saved()
        self.assertEqual(template["planner_timeout"], 4321)
        self.assertIn("3 attempts per milestone, 30 min per call, 4321 s per planner call", out)
        again = next(line for line in out.splitlines() if " wish " in line)
        self.assertIn("--planner-timeout 4321", again)
        goal_line = next(line for line in out.splitlines() if " goal " in line)
        self.assertNotIn("--planner-timeout", goal_line, "the saved template already carries it")
        plan = self.root / "planned.json"
        plan.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}), encoding="utf-8")
        launched, _ = self.cli("goal", folder / "template.json", folder.joinpath("objective.txt").read_text(encoding="utf-8"), "--plan-from", plan, "--enqueue")
        self.assertEqual(self.store.get(json.loads(launched)["run_id"])["spec"]["planner_timeout"], 4321)

    def test_without_the_flag_the_template_the_summary_and_the_relaunch_line_are_what_they_were(self):
        self.writer([wish_tests.fenced()])
        out, _ = self.cli("wish", self.repo, "Calm the page")
        _, template, _ = self.saved()
        self.assertNotIn("planner_timeout", template)
        self.assertNotIn("per planner call", out)
        self.assertNotIn("--planner-timeout", out)
        self.assertIn("30 min per call", out)

    def test_start_hands_the_flag_to_the_goal_planner_call(self):
        for flags, expected in (((), 1800), (("--planner-timeout", "4321"), 4321)):
            with self.subTest(flags=flags):
                for folder in self.wishes():
                    for item in folder.iterdir():
                        item.unlink()
                    folder.rmdir()
                self.writer([wish_tests.fenced()])
                planner = self.root / "planner.json"
                planner.write_text(json.dumps([{"milestones": [{"title": "Restyle", "task": "Apply the teal palette"}]}]), encoding="utf-8")
                route = {"kind": "command", "argv": [sys.executable, wish_tests.HELPER, "script", str(planner)]}
                seen = []

                def spy(config, mode, cwd, log, prompt, timeout, paused, *rest, **options):
                    seen.append((mode, timeout))
                    return real_invoke(config, mode, cwd, log, prompt, timeout, paused, *rest, **options)
                with patch("nightweaver.cli.planner_route", return_value=route), patch("nightweaver.cli.invoke", side_effect=spy), \
                        patch("nightweaver.cli.background", return_value=4242):
                    out, _ = self.cli("wish", self.repo, "Calm the page", "--start", *flags)
                self.assertEqual(seen, [("planner", expected)])
                spec = self.store.get(re.search(r"^Run id: (\S+)$", out, re.M).group(1))["spec"]
                self.assertEqual(spec.get("planner_timeout"), flags and 4321 or None)

    def test_the_flag_is_bounded_like_the_others(self):
        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            main(["--home", str(self.store.root), "wish", str(self.repo), "Calm the page", "--planner-timeout", "0"])
        self.assertIn("from 1 to 14400", err.getvalue())

    def test_the_brief_writer_keeps_its_own_900_seconds_and_says_so(self):
        """The brief writer is a pre-run call of the wish command with a fixed limit; it was not one of the roles that needed the setting."""
        self.assertEqual(wishing.BRIEF_TIMEOUT, 900)
        seen = []

        def timed_out(config, mode, cwd, log, prompt, timeout, paused, *rest, **options):
            seen.append(timeout)
            return 124, None, ""
        with patch.object(wishing, "brief_prompt", return_value="Write the brief."), patch.object(wishing, "invoke", side_effect=timed_out):
            with self.assertRaises(ValueError) as caught:
                wishing.write_brief({"kind": "claude"}, {"repository": str(self.root)}, "wish", self.root)
        self.assertEqual(seen, [900])
        self.assertIn("The brief writer timed out after 900 s, its time limit (a fixed 900 s limit of the wish command; "
                      "--planner-timeout does not change it). See ", str(caught.exception))

    def test_the_brief_writer_says_the_other_controller_exits_and_keeps_the_text_of_any_other(self):
        for code, words in ((125, "The brief writer was stopped by the idle watchdog"), (126, "The brief writer was stopped at its output limit: its log grew past 16 MB"),
                            (7, "The brief writer exited 7. See "), (1, "The brief writer exited 1. See ")):
            with self.subTest(code=code):
                with patch.object(wishing, "brief_prompt", return_value="Write the brief."), \
                        patch.object(wishing, "invoke", return_value=(code, None, "")):
                    with self.assertRaises(ValueError) as caught:
                        wishing.write_brief({"kind": "claude"}, {"repository": str(self.root)}, "wish", self.root)
                self.assertIn(words, str(caught.exception))


@unittest.skipUnless(os.name == "nt", "Windows controller")
class InRunRoles(unittest.TestCase):
    """Every provider call inside a run goes through Run.call_provider, which holds it to the plan's agent_timeout, or to planner_timeout for a
    planner. No in-run role had a shorter cap of its own, so nothing needed raising; the plan gained planner_timeout and the messages gained words."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def script(self, name, items):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return path

    def route(self, name, items):
        return {"kind": "command", "argv": [sys.executable, PURSUIT, "script", str(self.script(name, items))]}

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def forcing(self, forced):
        """A patch of the engine's invoke that records (mode, timeout, health) of every call and answers the modes in `forced` with that exit
        code and nothing else; every other call goes through the real invoke to its stand-in."""
        seen = []

        def fake(config, mode, cwd, log, prompt, timeout, paused, *rest, **options):
            seen.append((mode, timeout, dict(options.get("health") or {})))
            if mode in forced:
                return forced[mode], None, ""
            return real_invoke(config, mode, cwd, log, prompt, timeout, paused, *rest, **options)
        return seen, patch("nightweaver.engine.invoke", side_effect=fake)

    def pursuit(self, judge, planner=None, extra_pursue=None, **extra):
        block = {"judge": judge, "planner": planner or self.route("planner", [plan("Next improvement")]), **(extra_pursue or {})}
        return self.configured([sys.executable, PURSUIT, "build", str(self.root / "worker.prompts")], pursue=block, **extra)

    # ------------------------------------------------------------------------------------------------ the table

    def limits(self, **spec):
        return Run(self.store, self.configured(fixtures.fixture("implement"), **spec))

    def test_call_limits_give_every_role_agent_timeout_and_a_planner_its_own_when_the_plan_sets_one(self):
        plain = self.limits()
        for role in ("worker", "reviewer", "judge", "planner", "diagnosis"):
            self.assertEqual(plain.call_limits(role)[:2], (40, "agent_timeout"), role)
        own = self.limits(planner_timeout=900)
        self.assertEqual(own.call_limits("planner")[:2], (900, "planner_timeout"))
        for role in ("worker", "reviewer", "judge"):
            self.assertEqual(own.call_limits(role)[:2], (40, "agent_timeout"), f"{role} never takes the planner's limit")
        long = self.limits(agent_timeout=100, planner_timeout=14400)
        self.assertEqual((long.call_limits("planner")[0], long.call_limits("worker")[0]), (14400, 100))
        short = self.limits(agent_timeout=14400, planner_timeout=30)
        self.assertEqual((short.call_limits("planner")[0], short.call_limits("judge")[0]), (30, 14400))

    def test_call_limits_give_the_idle_watchdog_to_the_four_roles_of_a_plan_written_by_019_and_budgets_win(self):
        run = self.limits()
        for role in ("worker", "reviewer", "judge", "planner"):
            self.assertEqual(run.call_limits(role)[2], 900, role)
        self.assertEqual(run.call_limits("diagnosis")[2], None)
        self.assertEqual(run.call_limits("worker")[3], 16 * 1024 * 1024)
        tuned = self.limits(budgets={"idle_timeout": 45, "max_log_bytes": 5 * 1024 * 1024})
        for role in ("worker", "planner", "diagnosis"):
            self.assertEqual((tuned.call_limits(role)[2], tuned.call_limits(role)[3]), (45, 5 * 1024 * 1024), role)

    def test_a_pursuit_calls_each_role_with_the_documented_timeout(self):
        for planner_timeout, expected in ((None, 40), (70, 70)):
            with self.subTest(planner_timeout=planner_timeout):
                extra = {} if planner_timeout is None else {"planner_timeout": planner_timeout}
                run = self.pursuit(self.route(f"judge-{expected}", [judged(60), judged(90, met=True)]),
                                   reviewer={"kind": "command", "argv": fixtures.fixture("review")}, **extra)
                seen, patcher = self.forcing({})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "done")
                by_mode = {}
                for mode, timeout, health in seen:
                    by_mode.setdefault(mode, set()).add(timeout)
                    self.assertEqual((health["idle_timeout"], health["max_log_bytes"]), (900, 16 * 1024 * 1024), mode)
                self.assertEqual(by_mode, {"worker": {40}, "reviewer": {40}, "judge": {40}, "replanner": {expected}})
                self.assertNotIn(240, {t for _, t, _ in seen})

    def test_only_the_goal_planner_wish_and_doctor_call_invoke_outside_the_engines_one_call_site(self):
        """The in-run table above holds for every role because they all use Run.call_provider. A new call to invoke() elsewhere would be a role
        with a cap of its own, so this lists the four modules that have one: the goal planner (cli), the brief writer (wish), the doctor probe
        (readiness, 45 s, left alone on purpose) and call_provider (engine)."""
        found = {}
        for path in sorted((ROOT / "nightweaver").glob("*.py")):
            if path.name == "providers.py":
                continue
            calls = re.findall(r"(?<![\w.])invoke\(", re.sub(r"def invoke\(", "", path.read_text(encoding="utf-8")))
            if calls:
                found[path.name] = len(calls)
        self.assertEqual(found, {"cli.py": 1, "engine.py": 1, "readiness.py": 1, "wish.py": 1})
        self.assertEqual(len(re.findall(r"call_provider\(", (ROOT / "nightweaver" / "pursuit.py").read_text(encoding="utf-8"))), 2)
        probe = (ROOT / "nightweaver" / "readiness.py").read_text(encoding="utf-8")
        self.assertRegex(probe, r'"Reply with the single word READY\.",\s+45, lambda')

    def test_a_planner_that_outruns_planner_timeout_inside_a_run_is_cut_off_and_named(self):
        run = self.pursuit(self.route("judge-real", [judged(60)]), planner=SLEEPER, planner_timeout=1, attempts=1)
        began = time.monotonic()
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertLess(time.monotonic() - began, 90)
        invalid = self.events(run, "plan_invalid")
        self.assertEqual([i["exit"] for i in invalid], [124, 124])
        self.assertIn("The plan returned no valid plan (attempt 1 of 2): The planner timed out after 1 s, its time limit "
                      f"(planner_timeout in the plan; {KEPT})", invalid[0]["issue"])
        self.assertIn("attempt 2 of 2", self.store.get(run)["state"]["last_error"])

    # ------------------------------------------------------------------------------------------------ messages

    def test_the_pursuit_judge_and_planner_say_what_the_controller_did_and_name_the_plan_setting(self):
        for code, words in ((124, f"timed out after 40 s, its time limit (agent_timeout in the plan; {KEPT})"),
                            (125, f"was stopped by the idle watchdog after 900 s without output, its idle timeout (budgets.idle_timeout in the plan; {KEPT})"),
                            (126, f"was stopped at its output limit: its log grew past 16 MB (budgets.max_log_bytes in the plan; {KEPT})")):
            with self.subTest(code=code):
                run = self.pursuit(self.route(f"judge-{code}", [judged(60)]))
                seen, patcher = self.forcing({"judge": code})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "blocked")
                first = self.events(run, "judge_invalid")[0]
                self.assertEqual(first["exit"], code)
                self.assertEqual(first["issue"], f"The judge returned no valid judgment (attempt 1 of 2): The judge {words}")
                self.assertEqual(self.store.get(run)["state"]["blocked_from"], "judging")

    def test_the_wave_planner_follows_planner_timeout_in_its_message_and_agent_timeout_without_one(self):
        for extra, shown in (({"planner_timeout": 70}, f"timed out after 70 s, its time limit (planner_timeout in the plan; {KEPT})"),
                             ({}, "timed out after 40 s, its time limit (agent_timeout in the plan, which a planner follows because the plan "
                                  f"sets no planner_timeout; {KEPT})")):
            with self.subTest(extra=extra):
                run = self.pursuit(self.route("judge-wave", [judged(60)]), **extra)
                seen, patcher = self.forcing({"replanner": 124})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "blocked")
                self.assertEqual(self.events(run, "plan_invalid")[0]["issue"],
                                 f"The plan returned no valid plan (attempt 1 of 2): The planner {shown}")
                self.assertEqual({t for m, t, _ in seen if m == "replanner"}, {70 if extra else 40})

    def test_other_exit_codes_in_the_pursuit_keep_the_text_they_had(self):
        for role, mode, kind in (("judge", "judge", "judge_invalid"), ("planner", "replanner", "plan_invalid")):
            for code in (1, 2, 7, 127):
                with self.subTest(role=role, code=code):
                    run = self.pursuit(self.route(f"judge-{role}{code}", [judged(60)]))
                    seen, patcher = self.forcing({mode: code})
                    with patcher:
                        Run(self.store, run).run()
                    issue = self.events(run, kind)[0]["issue"]
                    self.assertTrue(issue.endswith(f"The {role} exited {code}: "), issue)
                    self.assertNotRegex(issue, r"timed out|watchdog|output limit")

    def test_the_worker_names_the_controller_and_keeps_its_old_head_for_any_other_exit(self):
        for code, head in ((124, "The worker timed out after 40 s, its time limit (agent_timeout in the plan)\n"),
                           (126, "The worker was stopped at its output limit: its log grew past 16 MB (budgets.max_log_bytes in the plan)\n"),
                           (5, "Worker exit 5\n"), (1, "Worker exit 1\n")):
            with self.subTest(code=code):
                run = self.configured(fixtures.fixture("implement"), attempts=1, retry=FAST)
                seen, patcher = self.forcing({"worker": code})
                with patcher:
                    Run(self.store, run).run()
                rejected = self.events(run, "attempt_rejected")
                self.assertEqual([(r["phase"], r["exit"]) for r in rejected], [("worker", code)])
                self.assertTrue(rejected[0]["issue"].startswith(head), rejected[0]["issue"])

    def test_the_idle_worker_is_named_when_the_stall_budget_is_spent(self):
        run = self.configured(fixtures.fixture("implement"), attempts=1, retry=FAST)
        seen, patcher = self.forcing({"worker": 125})
        with patcher:
            Run(self.store, run).run()
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([(r["phase"], r["exit"]) for r in rejected], [("worker", 125)])
        self.assertTrue(rejected[0]["issue"].startswith(
            "The worker was stopped by the idle watchdog after 900 s without output, its idle timeout (budgets.idle_timeout in the plan)\n"),
            rejected[0]["issue"])

    def test_the_reviewer_names_the_controller_and_keeps_its_old_words_for_any_other_exit(self):
        for code, words in ((124, "the reviewer timed out after 40 s, its time limit (agent_timeout in the plan; "),
                            (125, "the reviewer was stopped by the idle watchdog after 900 s without output, its idle timeout ("),
                            (126, "the reviewer was stopped at its output limit: its log grew past 16 MB ("),
                            (3, "the reviewer exited with code 3. The worker's attempt was not charged")):
            with self.subTest(code=code):
                run = self.configured(fixtures.fixture("good"), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, attempts=1, retry=FAST)
                seen, patcher = self.forcing({"reviewer": code})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "blocked")
                self.assertIn("Reviewer unavailable: " + words, self.store.get(run)["state"]["last_error"])
                self.assertEqual({e["exit"] for e in self.events(run, "reviewer_failed")}, {code})

    def test_call_failure_words_the_controller_and_keeps_the_old_sentence_for_other_exits(self):
        run = Run(self.store, self.configured(fixtures.fixture("implement"), planner_timeout=75))
        log = self.root / "role.jsonl"
        log.write_text("the last words of the call", encoding="utf-8")
        for role in ("planner", "judge", "diagnosis"):
            for code in (124, 125, 126):
                text = run.call_failure(code, role, "some answer", log)
                self.assertTrue(text.startswith(f"The {role} "), text)
                self.assertNotIn("the last words", text, "a cut-off call has no answer to quote")
                self.assertNotIn("exited", text)
            self.assertEqual(run.call_failure(9, role, "some answer", log), f"The {role} exited 9: some answer")
            self.assertEqual(run.call_failure(9, role, "", log), f"The {role} exited 9: the last words of the call")
        self.assertEqual(run.call_failure(124, "planner", "", log, subject="The explorer"), run.exit_words(124, "The explorer", "planner"))
        self.assertIn("planner_timeout in the plan", run.exit_words(124, "The planner", "planner"))
        self.assertIn("agent_timeout in the plan;", run.exit_words(124, "The judge", "judge"))
        self.assertIsNone(run.exit_words(1, "The planner", "planner"))
        self.assertEqual(run.call_failure(0, "judge", "", log), "The judge exited 0: the last words of the call", "the caller decides what a zero exit means")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ExploreAndRescueRoles(unittest.TestCase):
    """The design-direction planner and judge (explore.py) and the plateau diagnosis (rescue.py) use the same words."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    script = InRunRoles.script
    route = InRunRoles.route
    events = InRunRoles.events
    forcing = InRunRoles.forcing

    def explored(self, tag="x", **extra):
        table = self.script(f"table-{tag}", {"order": ["Soft Pastel", "Neon Terminal", "Brutalist"]})
        names = ["Brutalist", "Soft Pastel", "Neon Terminal"]
        directions = {"directions": [{"name": n, "brief": f"A bold {n} look with its own palette and type."} for n in names]}
        block = {"judge": {"kind": "command", "argv": [sys.executable, EXPLORE, "judge", str(table)]},
                 "planner": self.route(f"explore-planner-{tag}", [directions, plan("Next improvement")]), "explore": {}, "confirm": False}
        worker = [sys.executable, EXPLORE, "build", str(self.root / f"worker-{tag}.prompts"), "-", "zq-builder"]
        return self.configured(worker, pursue=block, milestones=[{"title": "Prepare fixture", "task": "Set the value"}], **extra)

    def test_the_direction_planner_is_held_to_planner_timeout_and_its_failure_is_named(self):
        for extra, limit, field in (({"planner_timeout": 55}, 55, "planner_timeout in the plan"), ({}, 40, "agent_timeout in the plan, which a planner follows")):
            with self.subTest(extra=extra):
                run = self.explored(f"planner{limit}", **extra)
                seen, patcher = self.forcing({"directions": 124})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "done", "two failed direction calls skip the exploration, as any other failure of it did")
                self.assertEqual({t for m, t, _ in seen if m == "directions"}, {limit})
                invalid = self.events(run, "directions_invalid")[0]
                self.assertEqual(invalid["exit"], 124)
                self.assertTrue(invalid["issue"].startswith("The planner returned no valid directions (attempt 1 of 2): "
                                                           f"The planner timed out after {limit} s, its time limit ({field}"), invalid["issue"])
                self.assertEqual(len(self.events(run, "exploration_skipped")), 1)

    def test_the_direction_judge_is_named_too_and_a_plain_failure_keeps_its_text(self):
        for code, words in ((125, "The judge was stopped by the idle watchdog after 900 s without output, its idle timeout (budgets.idle_timeout in the plan"),
                            (4, "The judge exited 4: ")):
            with self.subTest(code=code):
                run = self.explored(f"judge{code}")
                seen, patcher = self.forcing({"comparison": code})
                with patcher:
                    Run(self.store, run).run()
                issues = [i.get("issue", "") for i in self.events(run, "comparison_invalid")]
                self.assertTrue(any(f"(attempt 1 of 2): {words}" in issue for issue in issues), issues)

    def test_the_explore_worker_is_named(self):
        run = self.explored()
        seen, patcher = self.forcing({"worker": 126})
        with patcher:
            Run(self.store, run).run()
        built = self.events(run, "direction_failed")
        self.assertEqual(len(built), 3, "all three directions ended with the worker's controller exit")
        self.assertEqual(self.events(run, "direction_built"), [])
        self.assertTrue(all(b["reason"].startswith("The worker was stopped at its output limit: its log grew past 16 MB (budgets.max_log_bytes in the plan)\n")
                            for b in built), [b["reason"] for b in built])

    def test_the_rescue_diagnosis_is_named_and_a_plain_failure_keeps_its_text(self):
        for code, words in ((124, f"The judge timed out after 40 s, its time limit (agent_timeout in the plan; {KEPT})"), (6, "The judge exited 6: ")):
            with self.subTest(code=code):
                table = self.script(f"rescue-table-{code}", {"goal": [judged(60), judged(55), judged(90, met=True)], "order": ["Current best build"]})
                judge = {"kind": "command", "argv": [sys.executable, RESCUE, "judge", str(table)]}
                block = {"judge": judge, "planner": self.route(f"rescue-planner-{code}", [plan("Next improvement")]), "plateau": 1, "max_waves": 4,
                         "rescue": {"strategies": 2}, "confirm": False}
                run = self.configured([sys.executable, RESCUE, "build", str(self.root / f"worker-{code}.prompts"), "-", "zq-builder"], pursue=block)
                seen, patcher = self.forcing({"diagnosis": code})
                with patcher:
                    self.assertEqual(Run(self.store, run).run(), "blocked")
                invalid = self.events(run, "rescue_diagnosis_invalid")
                self.assertTrue(invalid[0]["issue"].startswith(f"The judge returned no valid diagnosis (attempt 1 of 2): {words}"), invalid[0]["issue"])
                self.assertEqual({t for m, t, _ in seen if m == "diagnosis"}, {40})


class Release(unittest.TestCase):
    def test_the_version_is_0_19_4_everywhere(self):
        import nightweaver
        self.assertEqual(nightweaver.__version__, "0.19.4")
        self.assertIn('version = "0.19.4"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## What's new in 0.19.4", readme)
        self.assertLess(readme.index("## What's new in 0.19.4"), readme.index("## What's new in 0.19.3"))
        self.assertIn("docs/v0.19.4-notes.md", readme)
        leftovers = [str(path.relative_to(ROOT)) for path in (ROOT / "nightweaver").glob("*.py") if '0.19.3"' in path.read_text(encoding="utf-8")]
        self.assertEqual(leftovers, [])

    def test_the_readme_counts_the_suite_by_its_test_methods_and_lists_this_module(self):
        counts = {path.name: len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"), re.M)) for path in Path(__file__).parent.glob("test_*.py")}
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"The suite has {sum(counts.values()):,} tests.", readme)
        mine = counts["test_release_0194_planner_timeout.py"]
        self.assertIn(f"Version 0.19.4 added {mine} in `test_release_0194_planner_timeout`", readme)
        self.assertIn("Version 0.19.3 added 153 in `test_release_0193_agy_reads`", readme)

    def test_the_readme_documents_both_settings_next_to_the_other_limits(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        new = readme[readme.index("## What's new in 0.19.4"):readme.index("## What's new in 0.19.3")]
        for phrase in ("planner_timeout", "--planner-timeout", "agent_timeout", "240", "1800", "14400", "timed out after", "idle watchdog", "output limit",
                       "docs/v0.19.4-notes.md"):
            self.assertIn(phrase, new)
        for phrase in ("`planner_timeout`", "`--planner-timeout SECONDS`", "`goal`, `oneshot` and `wish`"):
            self.assertIn(phrase, readme)
        self.assertGreater(readme.index("- **Planner timeout.**"), readme.index("### Run unattended"))
        self.assertLess(readme.index("- **Planner timeout.**"), readme.index("- **Silent workers.**"))

    def test_the_notes_say_the_bug_the_cause_the_setting_and_the_messages_in_plain_english(self):
        notes = (ROOT / "docs" / "v0.19.4-notes.md").read_text(encoding="utf-8")
        for phrase in ("Planner exited 124", "240", "agent_timeout", "planner_timeout", "--planner-timeout", "1800", "timed out after", "idle watchdog",
                       "output limit", "research repository", "cli.py", "process.py", "brief writer", "doctor"):
            self.assertIn(phrase, notes)
        self.assertLess(len(notes.split()), 900, "short, as asked")
        self.assertFalse(re.search(r"Administrator|C:\\Users|@gmail|Nightweaver-trials|nw0194", notes))

    def test_nothing_private_is_written_into_the_public_files_of_this_release(self):
        private = "CR" + "OW"
        for path in (ROOT / "README.md", ROOT / "docs" / "v0.19.4-notes.md", Path(__file__), *(ROOT / "nightweaver").glob("*.py")):
            self.assertFalse(re.search(rf"\b{private}\b", path.read_text(encoding="utf-8"), re.I), path.name)


if __name__ == "__main__":
    unittest.main()
