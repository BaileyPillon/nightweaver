"""Repair loop: a repeated-failure detector that fires, no-progress short-circuit, stall refunds, tolerant verdicts and a better reviewer."""

import json
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_windows as fixtures

from nightweaver.cli import main
from nightweaver.engine import Run, common_dir
from nightweaver.process import ControllerExit
from nightweaver.providers import invoke as real_invoke
from nightweaver.pursuit import parse_judgment, parse_wave
from nightweaver.reports import metrics
from nightweaver.repair import family, fingerprint
from nightweaver.verdicts import conflict_notes, extract_object, extract_verdict, normalize

HELPER = str(Path(__file__).with_name("repair_fixture.py").resolve())
SCRIPT = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
FAST = {"initial_seconds": 1, "max_seconds": 1}
PASS = {"verdict": "PASS", "reason": "Verified the change and the evidence."}
LOADER = "The brief requires one loader but forbids the rows that loader reads."


def fx(mode, *args):
    return [sys.executable, HELPER, mode, *map(str, args)]


def judged(score, met=False, **extra):
    gaps = [] if met else [{"title": f"Gap at {score}", "detail": f"Detail {score}"}]
    return {"verdict": "MET" if met else "NOT_MET", "score": score, "reason": f"Reason {score}", "gaps": gaps,
            "criteria": [{"criterion": "Looks right", "met": met, "note": "Observed"}], **extra}


def plan(*titles, **extra):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles], **extra}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class RepairTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def script(self, name, items):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return {"kind": "command", "argv": [sys.executable, SCRIPT, "script", str(path)]}

    def prompts(self, name):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def kinds(self, run):
        return [e["kind"] for e in self.store.events(run, limit=None)]

    def state(self, run):
        return self.store.get(run)["state"]

    def calls(self, run, role):
        return sum(c["role"] == role for c in self.events(run, "provider_call_finished"))

    def cli(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else stderr.getvalue()

    def pursue(self, judge, planner, **extra):
        self.worker_log = self.root / "worker.prompts"
        block = {"judge": self.script("judge", judge), "planner": self.script("planner", planner)}
        return self.configured([sys.executable, SCRIPT, "build", str(self.worker_log)], pursue=block, **extra)

    # The repeated-failure detector

    def test_changing_wording_does_not_hide_a_repeated_gate_failure(self):
        notes = self.root / "worker.prompts"
        run = self.configured(fx("write", notes, "not ready"), attempts=6, retry={**FAST, "repeated_failure_limit": 3},
                              gates=[{"name": "read fixture value", "argv": fx("vary", self.root / "count.txt"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual(len(rejected), 3)
        self.assertEqual(len({r["issue"] for r in rejected}), 3, "every attempt words the failure differently")
        self.assertEqual([r["same_failure_count"] for r in rejected], [1, 2, 3])
        self.assertEqual([r["failed_gates"] for r in rejected], [["read fixture value"]] * 3)
        stopped = self.events(run, "repeated_failure_stopped")
        self.assertEqual([(s["count"], s["cause"]) for s in stopped],
                         [(3, "Stopped: gate read fixture value failed 3 times on the same build")])
        error = self.state(run)["last_error"]
        self.assertTrue(error.startswith("Stopped: gate read fixture value failed 3 times on the same build\n\n"), error)
        self.assertIn("variant 3 of the failure", error)
        self.assertIn("Worker's last explanation:\nSUMMARY-MARK I changed the build", error)
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 3)

    def test_exhausted_attempts_name_the_cause(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "not ready"), attempts=2,
                              retry={**FAST, "repeated_failure_limit": 20},
                              gates=[{"name": "read fixture value", "argv": fx("vary", self.root / "count.txt"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        error = self.state(run)["last_error"]
        self.assertTrue(error.startswith("Stopped: 2 of 2 attempts used\n\n"), error)
        self.assertIn("Worker's last explanation:", error)
        self.assertEqual(self.events(run, "repeated_failure_stopped"), [])
        self.assertEqual(self.events(run, "blocked")[0]["cause"], "Stopped: 2 of 2 attempts used")

    def test_reviewer_paraphrases_on_the_same_build_count_as_one_failure(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), attempts=5, retry={**FAST, "repeated_failure_limit": 3})
        controller = Run(self.store, run)
        controller.gitdir = common_dir(controller.repository)
        path = controller.worktree(self.state(run))
        (path / "result.txt").write_text("ready", encoding="utf-8")
        self.store.transition(run, "fixture_review", {}, phase="reviewing", attempt=1, attempts_used=1)
        counts = []

        def reject(reason):
            controller.reject(reason, "review")
            counts.append(self.events(run, "attempt_rejected")[-1]["same_failure_count"])

        reject("Rename the helper.")
        reject("The helper name is unclear; consider renaming.")
        (path / "result.txt").write_text("ready, edited", encoding="utf-8")
        reject("Rename the helper.")  # a different build starts the count again
        reject("Still the helper name.")
        self.assertEqual(self.state(run)["phase"], "ready")
        reject("Yet another way to say the same thing.")
        self.assertEqual(counts, [1, 2, 1, 2, 3])
        self.assertEqual(self.state(run)["phase"], "blocked")
        self.assertEqual(self.events(run, "repeated_failure_stopped")[0]["count"], 3)
        self.assertTrue(self.state(run)["last_error"].startswith("Stopped: the reviewer asked for changes 3 times on the same build"))

    def test_failure_identity_is_what_failed_on_which_build(self):
        base = fingerprint("check", "tree1", ["b", "a"], reason="first wording")
        self.assertEqual(base, fingerprint("check", "tree1", ["a", "b"], reason="entirely different wording"))
        self.assertNotEqual(base, fingerprint("check", "tree2", ["a", "b"]))
        self.assertNotEqual(base, fingerprint("check", "tree1", ["a"]))
        self.assertNotEqual(base, fingerprint("review", "tree1", ["a", "b"]))
        self.assertEqual(fingerprint("review", "tree1", reason="one"), fingerprint("review", "tree1", reason="two"))
        self.assertNotEqual(fingerprint("worker", "tree1", exit_code=1), fingerprint("worker", "tree1", exit_code=124))
        self.assertNotEqual(fingerprint("snapshot", "unknown", reason="one"), fingerprint("snapshot", "unknown", reason="two"))

    # The no-progress short-circuit

    def test_a_worker_that_changes_nothing_after_review_is_stopped_without_checks_or_review(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Reason one: rename the helper."},
                                            {"verdict": "REVISE", "reason": "Reason two: the name is unclear."}])
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=reviewer, attempts=6,
                              retry={**FAST, "repeated_failure_limit": 2})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([r["phase"] for r in rejected], ["review", "no_progress", "no_progress"])
        self.assertEqual(len({r["tree"] for r in rejected}), 1)
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 3)
        self.assertEqual(len(self.prompts("reviewer")), 1, "the reviewer is not called again for an unchanged build")
        self.assertEqual(len(self.events(run, "gate_started")), 1, "the checks are not run again for an unchanged build")
        for row in rejected[1:]:
            self.assertTrue(row["issue"].startswith("The worker made no change after the previous rejection"), row["issue"])
            self.assertIn("Reason one: rename the helper.", row["issue"])
            self.assertEqual(row["issue"].count("made no change"), 1)
        self.assertEqual(self.events(run, "repeated_failure_stopped")[0]["cause"], "Stopped: no change after review feedback")
        self.assertTrue(self.state(run)["last_error"].startswith("Stopped: no change after review feedback\n\n"))
        self.assertIn("Worker's last explanation:", self.state(run)["last_error"])

    def test_an_unchanged_build_after_a_check_failure_runs_the_checks_again(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), attempts=3, retry=FAST,
                              reviewer=self.script("reviewer", [PASS]),
                              gates=[{"name": "flaky gate", "argv": fx("flaky", self.root / "count.txt"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([r["phase"] for r in self.events(run, "attempt_rejected")], ["check"])
        self.assertEqual(len(self.events(run, "gate_started")), 2)
        self.assertEqual(self.events(run, "attempt_rejected")[0]["failed_gates"], ["flaky gate"])

    # Idle stalls

    def stalled(self, run, plan):
        """The listed worker calls return these exit codes, as execute() returns the limits it forces (None runs the real worker). Records
        each call's session and attempts used."""
        seen = []

        def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, **kwargs):
            if mode == "worker":
                seen.append((session, self.state(run)["attempts_used"]))
                if len(seen) <= len(plan) and plan[len(seen) - 1] is not None:
                    return ControllerExit(plan[len(seen) - 1]), None, "no output for a long time"
            return real_invoke(config, mode, cwd, log, prompt, timeout, paused, session, **kwargs)

        return patch("nightweaver.engine.invoke", side_effect=invoke), seen

    def test_idle_stalls_refund_the_attempt_and_start_a_fresh_session(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), attempts=1, retry=FAST)
        self.store.transition(run, "fixture_session", {}, worker_session="old-session")
        patcher, seen = self.stalled(run, [125, 125])
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen, [("old-session", 1), (None, 1), (None, 1)])
        stalls = self.events(run, "worker_stalled")
        self.assertEqual([(s["stall"], s["stalls_allowed"], s["exit"]) for s in stalls], [(1, 2, 125), (2, 2, 125)])
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 3)

    def test_the_third_consecutive_stall_is_a_normal_rejection(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), attempts=2, retry=FAST)
        patcher, seen = self.stalled(run, [125, 125, 125])
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([attempts for _, attempts in seen], [1, 1, 1, 2])
        self.assertEqual(len(self.events(run, "worker_stalled")), 2)
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([(r["phase"], r["exit"]) for r in rejected], [("worker", 125)])
        self.assertIn("The worker was stopped by the idle watchdog after 900 s without output, its idle timeout (budgets.idle_timeout in the plan)", rejected[0]["issue"])

    def test_a_stall_streak_ends_with_any_other_outcome_and_wall_clock_limits_stay_rejections(self):
        notes = self.root / "worker.prompts"
        run = self.configured(fx("grow", notes), attempts=1, retry=FAST,
                              milestones=[{"title": "First", "task": "Do first"}, {"title": "Second", "task": "Do second"}])
        patcher, seen = self.stalled(run, [125, None, 125, 125])
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.events(run, "worker_stalled")), 3, "two streaks of one and two stalls, none rejected")
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        timed = self.configured(fx("write", notes, "ready"), attempts=2, retry=FAST)
        patcher, _ = self.stalled(timed, [124])
        with patcher:
            self.assertEqual(Run(self.store, timed).run(), "done")
        self.assertEqual(self.events(timed, "worker_stalled"), [])
        self.assertEqual([(r["phase"], r["exit"]) for r in self.events(timed, "attempt_rejected")], [("worker", 124)])

    # Tolerant parsing

    def test_extract_object_finds_the_object_with_the_key_and_fails_closed_on_ambiguity(self):
        verdict = json.dumps({"verdict": "pass", "reason": "Fine {with} braces and a } stray"})
        for label, answer in {
            "prose around": f"I looked at everything. {verdict} Thanks for asking!",
            "fenced": f"Here you go:\n```json\n{verdict}\n```\nAnything else?",
            "only the json": verdict,
            "brace in prose before": "Use { carefully with braces. " + verdict,
            "other object after": verdict + '\nFor reference {"note": 1}',
            "same verdict twice": '{"verdict": "PASS", "reason": "draft"} and now the real one: ' + verdict,
        }.items():
            with self.subTest(label):
                found = extract_verdict(answer, ("PASS", "REVISE"))
                self.assertEqual((found["verdict"], found["reason"]), ("PASS", "Fine {with} braces and a } stray"))
        for label, answer in {"no json": "I approve of this change.", "no key": '{"result": "ok"}', "empty": "  ",
                              "unknown verdict": '{"verdict": "APPROVED", "reason": "x"}', "duplicate keys": '{"verdict": "PASS", "verdict": "REVISE"}',
                              "nested only": '{"outer": {"verdict": "PASS"}}', "truncated": '{"verdict": "PASS", "reason": "cut off',
                              "earlier example": '{"verdict": "REVISE", "reason": "an example"} and now the real one: ' + verdict}.items():
            with self.subTest(label), self.assertRaises(ValueError):
                extract_verdict(answer, ("PASS", "REVISE"))
        self.assertEqual(extract_object('x {"a": 1} y {"a": 2, "b": 3} z {"c": 4}', "a"), {"a": 2, "b": 3},
                         "a field that decides nothing keeps the rule that the last object wins")
        with self.assertRaises(ValueError):
            extract_object('x {"a": 1} y {"a": 2, "b": 3} z {"c": 4}', "a", fail_closed=True)

    def test_verdicts_read_case_insensitively(self):
        for value in ("pass", "Pass", " PASS "):
            self.assertEqual(normalize(value, ("PASS", "REVISE")), "PASS")
        self.assertEqual(normalize("revise", ("PASS", "REVISE")), "REVISE")
        for value in ("not met", "not_met", "Not-Met", "NOT_MET"):
            self.assertEqual(normalize(value, ("MET", "NOT_MET")), "NOT_MET")
        self.assertEqual(normalize("met", ("MET", "NOT_MET")), "MET")
        for value in ("maybe", "", None, 3, ["PASS"]):
            self.assertIsNone(normalize(value, ("PASS", "REVISE")))
        self.assertEqual(conflict_notes(None), [])
        self.assertEqual(conflict_notes([" a "]), ["a"])
        for bad in ("text", [""], [1], ["x"] * 11, ["x" * 1001]):
            with self.assertRaises(ValueError):
                conflict_notes(bad)

    def test_judge_and_planner_answers_survive_prose_and_lowercase_verdicts(self):
        prose = "Assessment follows.\n```json\n" + json.dumps(judged(60, verdict="not met")) + "\n```\nI hope this helps."
        verdict = parse_judgment(prose)
        self.assertEqual((verdict["verdict"], verdict["score"]), ("NOT_MET", 60))
        self.assertEqual(parse_judgment("Final: " + json.dumps(judged(90, met=True, verdict="Met")) + " Done.")["verdict"], "MET")
        with self.assertRaisesRegex(ValueError, "score"):
            parse_judgment(json.dumps(judged("high")))
        planned, conflicts = parse_wave("Here is the plan:\n" + json.dumps(plan("One", conflicts=[LOADER])) + "\nTell me if it is wrong.", 3)
        self.assertEqual(([p["title"] for p in planned], conflicts), (["One"], [LOADER]))
        self.assertEqual(parse_wave(json.dumps(plan("One")), 3)[1], [])
        with self.assertRaises(ValueError):
            parse_wave(json.dumps(plan("One", conflicts="not a list")), 3)

    def test_reviewer_prose_and_a_lowercase_verdict_still_pass(self):
        reviewer = self.script("reviewer", ['Sure, here is my verdict: {"verdict": "pass", "reason": "Looks right."} Cheers.'])
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=reviewer)
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.events(run, "review_passed")[0]["reason"], "Looks right.")
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 1)

    def test_unparseable_reviewer_is_asked_once_more_without_touching_the_worker(self):
        reviewer = self.script("reviewer", ["Looks fine to me, no JSON here.", PASS])
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=reviewer)
        self.assertEqual(Run(self.store, run).run(), "done")
        again = self.events(run, "review_unparseable")
        self.assertEqual([(a["count"], a["excerpt"]) for a in again], [(1, "Looks fine to me, no JSON here.")])
        self.assertEqual((self.calls(run, "reviewer"), self.calls(run, "worker")), (2, 1))
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        self.assertEqual(self.state(run).get("review_parse_failures", 0), 0)

    def test_a_second_unparseable_answer_makes_the_reviewer_unavailable_instead_of_rejecting_the_worker(self):
        reviewer = self.script("reviewer", ["no json at all", "still no json at all"])
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=reviewer, attempts=1, retry=FAST)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual([e["count"] for e in self.events(run, "review_unparseable")], [1, 2, 1, 2, 1, 2])
        self.assertEqual((self.calls(run, "reviewer"), self.calls(run, "worker")), (6, 1))
        self.assertEqual(self.events(run, "attempt_rejected"), [], "an unreadable answer is neither feedback nor a rejection")
        error = self.state(run)["last_error"]
        self.assertTrue(error.startswith("Reviewer unavailable: its answer could not be read after two tries"), error)
        self.assertIn("still no json at all", error)
        self.assertEqual(self.state(run)["blocked_from"], "reviewing")
        self.assertEqual(self.state(run)["review_parse_failures"], 0)

    def test_an_unreadable_review_is_not_feedback_the_worker_can_be_blamed_for_ignoring(self):
        reviewer = self.script("reviewer", ["no json", "still no json", PASS])
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=reviewer, attempts=3, retry=FAST)
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        self.assertEqual(len(self.events(run, "gate_started")), 1, "the build is checked once; only the review is retried")
        self.assertEqual((self.calls(run, "reviewer"), self.calls(run, "worker")), (3, 1))

    # What the reviewer sees

    def test_reviewer_sees_the_diff_the_screenshots_and_its_earlier_findings(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper to something clearer."}, PASS])
        shots = []

        def invoke(config, mode, *args, **kwargs):
            if mode == "reviewer":
                shots.append(kwargs.get("images"))
            return real_invoke(config, mode, *args, **kwargs)

        run = self.configured(fx("grow", self.root / "worker.prompts"), reviewer=reviewer, attempts=3, retry=FAST,
                              gates=[{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20},
                                     {"name": "screenshot", "argv": [sys.executable, SCRIPT, "shot"], "timeout": 20}])
        with patch("nightweaver.engine.invoke", side_effect=invoke):
            self.assertEqual(Run(self.store, run).run(), "done")
        first, second = self.prompts("reviewer")
        evidence = self.store.directory(run) / "evidence" / "01"
        patch_file = evidence / "01" / "review-diff.patch"
        self.assertIn("diff --git", patch_file.read_text(encoding="utf-8"))
        self.assertIn("notes.txt", patch_file.read_text(encoding="utf-8"))
        for prompt, attempt in ((first, "01"), (second, "02")):
            self.assertIn(str(evidence / attempt / "review-diff.patch"), prompt)
            self.assertIn("diff --git a/notes.txt b/notes.txt", prompt)
            self.assertIn("+revision 1", prompt)
            self.assertIn("OPEN each image file", prompt)
            self.assertIn(str(evidence / attempt / "screen.png"), prompt)
        self.assertEqual([[Path(p).name for p in images] for images in shots], [["screen.png"], ["screen.png"]])
        self.assertEqual([Path(images[0]).parent.name for images in shots], ["01", "02"])
        self.assertNotIn("Your own earlier findings", first)
        self.assertIn("Your own earlier findings on this milestone\n- Attempt 1: Rename the helper to something clearer.", second)
        for sentence in ("Verify these earlier findings were addressed.", "Return REVISE only for defects that matter for the task, "
                         "the acceptance requirements or the goal; mention minor polish inside the reason of a PASS.",
                         "Do not raise new nitpicks about code that did not change."):
            self.assertIn(sentence, second)
            self.assertNotIn(sentence, first)

    def test_a_huge_diff_is_capped_and_says_so(self):
        run = self.configured(fx("big", self.root / "worker.prompts"), reviewer=self.script("reviewer", [PASS]))
        self.assertEqual(Run(self.store, run).run(), "done")
        patch_file = self.store.directory(run) / "evidence" / "01" / "01" / "review-diff.patch"
        data = patch_file.read_bytes()
        self.assertLess(len(data), 400 * 1024 + 200)
        self.assertGreater(len(data), 399 * 1024)
        self.assertIn(b"[Diff truncated at 400 KB;", data)
        prompt = self.prompts("reviewer")[0]
        self.assertIn("truncated to 400 KB", prompt)
        self.assertIn("[Excerpt truncated.]", prompt)
        self.assertLess(len(prompt), 40 * 1024)

    # The local model's advice

    def test_the_local_note_reaches_the_reviewer_and_the_next_workers(self):
        notes = "Check the empty-input path"
        run = self.configured([sys.executable, SCRIPT, "build", str(self.root / "worker.prompts")],
                              reviewer=self.script("reviewer", [PASS]), advisory_ollama={"model": "local"},
                              milestones=[{"title": "First", "task": "Do first"}, {"title": "Second", "task": "Do second"}])
        with patch("nightweaver.engine.advisory", return_value=notes) as advise:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(advise.call_count, 2)
        kinds = self.kinds(run)
        self.assertLess(kinds.index("local_advice"), kinds.index("review_started"), "the local model speaks before the reviewer")
        for prompt in self.prompts("reviewer"):
            self.assertIn(f"A local model suggested checking: {notes}. Verify it if relevant.", prompt)
        first, second = self.root.joinpath("worker.prompts").read_text(encoding="utf-8").split("\n=====\n")[:2]
        self.assertNotIn("Local reviewer note", first)
        self.assertIn(f"Local reviewer note from the previous milestone: {notes}", second)
        self.assertEqual(self.state(run)["advice_note"], notes)

    def test_the_local_note_is_repeated_to_the_worker_on_the_same_milestone(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Handle the empty input."}, PASS])
        run = self.configured(fx("grow", self.root / "worker.prompts"), reviewer=reviewer, attempts=3, retry=FAST,
                              advisory_ollama={"model": "local"})
        with patch("nightweaver.engine.advisory", return_value="Check the empty-input path"):
            self.assertEqual(Run(self.store, run).run(), "done")
        first, second = self.prompts("worker")[:2]
        self.assertNotIn("Local reviewer note", first)
        self.assertIn("Local reviewer note from this milestone's previous review: Check the empty-input path", second)

    def test_a_required_local_note_still_blocks_before_the_reviewer(self):
        run = self.configured(fx("write", self.root / "worker.prompts", "ready"), reviewer=self.script("reviewer", [PASS]),
                              advisory_ollama={"model": "local", "required": True})
        with patch("nightweaver.engine.advisory", side_effect=OSError("local server offline")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.state(run)["blocked_from"], "reviewing")
        self.assertEqual(self.calls(run, "reviewer"), 0)
        with patch("nightweaver.engine.advisory", return_value="Check it"):
            self.assertEqual(Run(self.store, run).run(retry=True), "done")

    # Independence

    def test_same_vendor_families_are_flagged_once_per_role_and_pair(self):
        self.assertEqual([family({"kind": k}) for k in ("codex", "claude", "deepseek", "gemini", "command")],
                         ["openai", "anthropic", "deepseek", "google", None])

        def invoke(config, mode, *args, **kwargs):
            if mode == "reviewer":
                return 0, None, json.dumps(PASS)
            if config["kind"] == "codex":
                config = {"kind": "command", "argv": fx("grow", self.root / "worker.prompts")}
            return real_invoke(config, mode, *args, **kwargs)

        steps = [{"title": "First", "task": "Do first", "worker": {"kind": "codex"}},
                 {"title": "Second", "task": "Do second", "worker": {"kind": "codex"}}]
        with patch("nightweaver.engine.invoke", side_effect=invoke):
            same = self.configured(fx("write", "unused", "x"), reviewer={"kind": "codex"}, milestones=steps)
            self.assertEqual(Run(self.store, same).run(), "done")
            different = self.configured(fx("write", "unused", "x"), reviewer={"kind": "claude"}, milestones=steps)
            self.assertEqual(Run(self.store, different).run(), "done")
        warned = self.events(same, "independence_degraded")
        self.assertEqual(len(warned), 1, "two reviews, one warning")
        self.assertEqual((warned[0]["role"], warned[0]["worker"]["family"], warned[0]["reviewer_or_judge"]["family"]),
                         ("reviewer", "openai", "openai"))
        self.assertEqual(warned[0]["worker"]["kind"], "codex")
        self.assertEqual(self.events(different, "independence_degraded"), [])

    # Pursuit: judge and planner

    def test_pursuit_judge_and_planner_tolerate_prose_and_report_what_the_planner_flags(self):
        bad = plan("Unused", conflicts="not a list")
        flagged = "Here is the plan:\n```json\n" + json.dumps(plan("Polish layout", conflicts=[LOADER])) + "\n```\nTell me if it is wrong."
        run = self.pursue(["Assessment:\n```json\n" + json.dumps(judged(60, verdict="not met")) + "\n```\nI hope this helps.",
                           "Final answer " + json.dumps(judged(90, met=True, verdict="met"))], [bad, flagged])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([(j["verdict"], j["score"]) for j in self.events(run, "goal_judged")], [("NOT_MET", 60), ("MET", 90)])
        self.assertEqual(len(self.events(run, "plan_invalid")), 1)
        planned = self.events(run, "wave_planned")[0]
        self.assertEqual((planned["titles"], planned["conflicts"]), (["Polish layout"], [LOADER]))
        self.assertEqual(self.state(run)["pursuit_conflicts"], [LOADER])
        first, second = self.root.joinpath("worker.prompts").read_text(encoding="utf-8").split("\n=====\n")[:2]
        self.assertNotIn("Planner notes", first)
        self.assertIn("Planner notes\nThe planner flagged", second)
        self.assertIn(f"- {LOADER}", second)
        self.assertIn('"conflicts"', self.prompts("planner")[0])
        # The judge and the worker are both command providers here, whose vendor is unknown, so nothing is claimed about them.
        self.assertEqual(self.events(run, "independence_degraded"), [])

    # The one-shot planner

    def template(self):
        path = self.root / "template.json"
        path.write_text(json.dumps({"repository": str(self.repo), "title": "Template",
                                    "worker": {"kind": "command", "argv": [sys.executable, SCRIPT, "build", str(self.root / "worker.prompts")]},
                                    "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}]}), encoding="utf-8")
        return path

    def test_goal_planner_conflicts_reach_the_task_and_the_worker_prompt(self):
        answer = ("Here you go:\n```json\n"
                  + json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}], "conflicts": [LOADER]}) + "\n```\nCheers.")
        with patch("nightweaver.cli.invoke", return_value=(0, None, answer)):
            result = self.cli("goal", self.template(), "Deliver", "--start", "--wait")
        self.assertEqual((result["phase"], result["planner_conflicts"]), ("done", [LOADER]))
        task = result["milestones"][0]["task"]
        self.assertTrue(task.startswith("Write result.txt\n\nPlanner notes\n"), task)
        self.assertIn(LOADER, self.store.get(result["run_id"])["spec"]["milestones"][0]["task"])
        self.assertIn(f"- {LOADER}", self.prompts("worker")[0])
        saved = self.root / "saved.json"
        saved.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}], "conflicts": "oops"}), encoding="utf-8")
        self.assertIn("conflicts must be a list", self.cli("goal", self.template(), "Deliver", "--plan-from", saved, code=1))
        saved.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}), encoding="utf-8")
        plain = self.cli("goal", self.template(), "Deliver", "--plan-from", saved)
        self.assertEqual(plain["milestones"], [{"title": "Deliver", "task": "Write result.txt"}])
        preview = self.cli("goal", self.template(), "Deliver", "--prompt-only")["prompt"]
        self.assertIn("list them as an additional \"conflicts\"", preview)


if __name__ == "__main__":
    unittest.main()
