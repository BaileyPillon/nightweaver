"""Review fixes for the 0.19 repair loop: droppings, ambiguous verdicts, git timeouts, the idle default, reviewer failures, feedback, advice and families."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_windows as fixtures

from nightweaver import engine
from nightweaver.engine import Run, common_dir, git, tree
from nightweaver.providers import ProviderUnavailable, invoke as real_invoke
from nightweaver.pursuit import parse_wave
from nightweaver.repair import family
from nightweaver.verdicts import extract_object, extract_verdict

SCRIPT = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
REPAIR = str(Path(__file__).with_name("repair_fixture.py").resolve())
FAST = {"initial_seconds": 1, "max_seconds": 1}
PASS = {"verdict": "PASS", "reason": "Verified the change and the evidence."}
VERDICTS = ("PASS", "REVISE")
HELPER = '''
import sys
from pathlib import Path

mode = sys.argv[1]
if mode == "worker":
    prompt = sys.stdin.read()
    counter = Path(sys.argv[2])
    with counter.with_suffix(".prompts").open("a", encoding="utf-8") as record:
        record.write(prompt + chr(10) + "=====" + chr(10))
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    Path("result.txt").write_text(sys.argv[3], encoding="utf-8")
    for name in sys.argv[4:]:
        target = Path(name.format(n=count))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"attempt {count}", encoding="utf-8")
    print(f"SUMMARY worker attempt {count}")
elif mode == "exists":
    if not Path(sys.argv[2]).is_file():
        print("missing " + sys.argv[2])
        raise SystemExit(2)
elif mode == "grandchild":
    import subprocess, time
    subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3]])
    time.sleep(120)
elif mode == "pulse":
    import time
    while True:
        with open(sys.argv[2], "ab") as out:
            out.write(b"x")
        time.sleep(.1)
'''


@unittest.skipUnless(os.name == "nt", "Windows controller")
class FixRepairBase(unittest.TestCase):
    configured = fixtures.WindowsRunTest.configured

    def setUp(self):
        fixtures.WindowsRunTest.setUp(self)
        self.addCleanup(setattr, engine, "GIT_TIMEOUT", engine.GIT_TIMEOUT)
        self.helper = self.root / "helper.py"
        self.helper.write_text(HELPER, encoding="utf-8")

    def fx(self, *args):
        return [sys.executable, str(self.helper), *map(str, args)]

    def script(self, name, items):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return {"kind": "command", "argv": [sys.executable, SCRIPT, "script", str(path)]}

    def prompts(self, name):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def state(self, run):
        return self.store.get(run)["state"]

    def calls(self, run, role):
        return sum(c["role"] == role for c in self.events(run, "provider_call_finished"))

    def worker(self, name="worker", result="ready", *names):
        return self.fx("worker", self.root / f"{name}.count", result, *names)

    def rewrite_spec(self, run, **changes):
        with self.store.connect() as db:
            spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (run,)).fetchone()["spec"])
            for key, value in changes.items():
                if value is None:
                    spec.pop(key, None)
                else:
                    spec[key] = value
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))

    def reviewer_outcomes(self, outcomes):
        """Reviewer calls return these (exit code, answer) pairs in order, the last one repeating. A ProviderUnavailable is raised."""
        prompts = []

        def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, **kwargs):
            if mode == "reviewer":
                prompts.append(prompt)
                item = outcomes[min(len(prompts), len(outcomes)) - 1]
                if isinstance(item, Exception):
                    raise item
                return item[0], None, item[1]
            return real_invoke(config, mode, cwd, log, prompt, timeout, paused, session, **kwargs)

        return patch("nightweaver.engine.invoke", side_effect=invoke), prompts

    def head_names(self, run):
        return sorted(git(self.repo, "ls-tree", "-r", "--name-only", f"nightweaver/{run}").splitlines())


class ToolDroppingsReachNeitherGatesNorCommits(FixRepairBase):
    def worktree(self, base):
        path = self.root / f"wt-{len(list(self.root.glob('wt-*')))}"
        git(self.repo, "worktree", "add", "--detach", str(path), base)
        return path

    def write(self, path, name, text="x"):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_a_gate_cannot_pass_on_a_dropping_the_accepted_commit_does_not_contain(self):
        run = self.configured(self.worker("worker", "ready", "tests/golden.dmp"), attempts=2, retry={**FAST, "repeated_failure_limit": 20},
                              gates=[{"name": "fixture present", "argv": self.fx("exists", "tests/golden.dmp"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "blocked", "the gate saw no golden.dmp, because the commit would not have it")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([(r["phase"], r["failed_gates"]) for r in rejected], [("check", ["fixture present"])] * 2)
        removed = self.events(run, "tool_droppings_removed")
        self.assertTrue(removed and all(r["paths"] == ["tests/golden.dmp"] for r in removed), removed)
        self.assertFalse((self.store.directory(run) / "worktrees" / "01" / "tests" / "golden.dmp").exists())
        self.assertEqual(git(self.repo, "branch", "--list", "nightweaver/*"), "")
        # Nothing is a silent dead end: the rejection and the next worker's prompt name the deleted file and the way to keep it.
        self.assertTrue(all("tool droppings were deleted" in r["issue"] and "tests/golden.dmp" in r["issue"] for r in rejected), rejected)
        self.assertIn("tool droppings were deleted", self.state(run)["last_error"])
        second = self.prompts("worker")[1]
        self.assertIn("tests/golden.dmp", second.partition("Last observed failure")[2])
        self.assertIn("Name a path in the milestone paths to keep it", second)

    def test_a_dropping_the_milestone_names_is_a_deliverable_and_stays(self):
        run = self.configured(self.worker("worker", "ready", "tests/golden.dmp"), attempts=1,
                              milestones=[{"title": "Fixture", "task": "Write it", "paths": ["result.txt", "tests/golden.dmp"]}],
                              gates=[{"name": "fixture present", "argv": self.fx("exists", "tests/golden.dmp"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.head_names(run), [".gitignore", "result.txt", "tests/golden.dmp"])
        self.assertEqual(self.events(run, "tool_droppings_removed"), [])

    def test_a_dropping_inside_a_folder_the_milestone_names_is_a_deliverable_and_stays(self):
        run = self.configured(self.worker("worker", "ready", "tests/golden.dmp", "tests/fixtures/sample.dmp", "debug.log"), attempts=1,
                              milestones=[{"title": "Fixture", "task": "Write it", "paths": ["result.txt", "tests", "debug.log"]}],
                              gates=[{"name": "fixture present", "argv": self.fx("exists", "tests/golden.dmp"), "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.head_names(run), [".gitignore", "debug.log", "result.txt", "tests/fixtures/sample.dmp", "tests/golden.dmp"])
        self.assertEqual(self.events(run, "tool_droppings_removed"), [])
        self.assertEqual(self.events(run, "attempt_rejected"), [])

    def test_droppings_outside_the_named_folders_are_still_deleted(self):
        base = self.head
        path = self.worktree(base)
        for name in ("tests/golden.dmp", "elsewhere/crash.dmp", ".playwright-mcp/x.png", "tests/.playwright-mcp/y.png", "debug.log"):
            self.write(path, name)
        removed = []
        snapshot = tree(path, base, ["tests", "result.txt"], [], removed)
        self.assertEqual(sorted(removed), [".playwright-mcp/x.png", "debug.log", "elsewhere/crash.dmp"])
        self.assertEqual(git(path, "ls-tree", "-r", "--name-only", snapshot).splitlines(),
                         [".gitignore", "result.txt", "tests/.playwright-mcp/y.png", "tests/golden.dmp"])

    def test_a_tracked_dropping_the_milestone_moves_is_a_deliverable_not_a_dropping(self):
        for name, body in {"fixtures/crash.dmp": "MDMP one", "fixtures/debug.log": "expected log sample", "old/chrome_debug.log": "before",
                           "keep/dump.dmp": "same bytes", "gone.dmp": "deleted outright",
                           "samples/crash-sample.bin": "".join(f"line {n} of a recorded crash\n" for n in range(12))}.items():
            self.write(self.repo, name, body)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Tracked fixtures")
        base = git(self.repo, "rev-parse", "HEAD")
        path = self.worktree(base)
        moves = {"fixtures/crash.dmp": ("tests/data/crash.dmp", "MDMP one"), "fixtures/debug.log": ("tests/data/debug.log", "expected log sample"),
                 "old/chrome_debug.log": ("tests/data/chrome_debug.log", "edited while moving"),
                 "keep/dump.dmp": ("tests/data/renamed-dump.dmp", "same bytes"),
                 "samples/crash-sample.bin": ("tests/data/crash-sample.dmp",
                                              "".join(f"line {n} of a recorded crash\n" for n in range(11)) + "line 11, edited\n")}
        for old, (new, body) in moves.items():
            (path / old).unlink()
            self.write(path, new, body)
        (path / "gone.dmp").unlink()
        self.write(path, "result.txt", "ready")
        self.write(path, "trace.dmp", "a real dropping")
        self.write(path, ".playwright-mcp/page.yml", "another")
        removed = []
        snapshot = tree(path, base, [], [], removed)
        self.assertEqual(sorted(removed), [".playwright-mcp/page.yml", "trace.dmp"], "only the new files nothing moved from are droppings")
        committed = git(path, "ls-tree", "-r", "--name-only", snapshot).splitlines()
        for new, _ in moves.values():
            self.assertIn(new, committed)
            self.assertTrue((path / new).is_file(), new)
        self.assertEqual((path / "tests" / "data" / "chrome_debug.log").read_text(encoding="utf-8"), "edited while moving")
        self.assertNotIn("gone.dmp", committed)

    def test_droppings_are_deleted_with_their_empty_folders_and_tracked_files_are_never_touched(self):
        self.write(self.repo, "debug.log", "v1")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Tracked dropping")
        base = git(self.repo, "rev-parse", "HEAD")
        path = self.worktree(base)
        self.write(path, "result.txt", "ready")
        self.write(path, "debug.log", "v2")
        for name in ("sub/debug.log", "chrome_debug.log", "a/.playwright-mcp/deep/x.png", "crash.dmp", "keep/real.txt"):
            self.write(path, name)
        removed = []
        snapshot = tree(path, base, [], [], removed)
        self.assertEqual(sorted(removed), ["a/.playwright-mcp/deep/x.png", "chrome_debug.log", "crash.dmp", "sub/debug.log"])
        for name in removed:
            self.assertFalse((path / name).exists(), name)
        for folder in ("sub", "a"):
            self.assertFalse((path / folder).exists(), "a folder only droppings were in goes with them")
        self.assertEqual((path / "debug.log").read_text(encoding="utf-8"), "v2", "a tracked file keeps its edit")
        self.assertTrue((path / "keep" / "real.txt").is_file())
        self.assertEqual(git(path, "ls-tree", "-r", "--name-only", snapshot).splitlines(),
                         [".gitignore", "debug.log", "keep/real.txt", "result.txt", "untouched.txt"])
        self.assertEqual(tree(path, base, [], []), snapshot, "a second snapshot is identical")

    def test_a_dropping_that_cannot_be_deleted_is_an_error_not_a_silent_pass(self):
        path = self.worktree(self.head)
        self.write(path, "debug.log")
        with patch.object(Path, "unlink", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaisesRegex(ValueError, "cannot be removed: debug.log"):
                engine.remove_droppings(path, ["debug.log"])

    def test_the_failure_fingerprint_and_the_changed_list_ignore_varying_droppings(self):
        names = ["debug.log", ".playwright-mcp/console-{n}.log", "trace-{n}.dmp"]
        run = self.configured(self.worker("worker", "defective", *names), attempts=6, retry={**FAST, "repeated_failure_limit": 3},
                              gates=[{"name": "read fixture value", "argv": [sys.executable, REPAIR, "vary", str(self.root / "vary.txt")],
                                      "timeout": 20}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([r["same_failure_count"] for r in rejected], [1, 2, 3], "the same defect on the same build, whatever the tools left")
        self.assertEqual(len({r["tree"] for r in rejected}), 1)
        self.assertEqual([r["changed_paths"] for r in rejected], [["result.txt"]] * 3)
        self.assertEqual(self.events(run, "repeated_failure_stopped")[0]["count"], 3)

    def test_the_no_progress_check_ignores_varying_droppings(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}])
        run = self.configured(self.worker("worker", "ready", "debug.log", ".playwright-mcp/console-{n}.log"), reviewer=reviewer,
                              attempts=6, retry={**FAST, "repeated_failure_limit": 2})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([r["phase"] for r in rejected], ["review", "no_progress", "no_progress"])
        self.assertEqual(len(self.prompts("reviewer")), 1, "the reviewer is not asked again about the same build")
        self.assertEqual(len(self.events(run, "gate_started")), 1)

    def test_reject_fingerprints_the_same_filtered_build_as_the_snapshot(self):
        run = self.configured(self.worker(), attempts=5, retry={**FAST, "repeated_failure_limit": 3})
        controller = Run(self.store, run)
        controller.gitdir = common_dir(controller.repository)
        path = controller.worktree(self.state(run))
        self.write(path, "result.txt", "ready")
        self.store.transition(run, "fixture_review", {}, phase="reviewing", attempt=1, attempts_used=1)
        for number in range(3):
            self.write(path, "debug.log", f"variant {number}")
            self.write(path, f".playwright-mcp/console-{number}.log")
            controller.reject("Rename the helper.", "review")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([r["same_failure_count"] for r in rejected], [1, 2, 3])
        self.assertEqual(len({r["tree"] for r in rejected}), 1)
        self.assertEqual([r["changed_paths"] for r in rejected], [["result.txt"]] * 3)

    def test_stage_is_the_one_definition_of_the_build(self):
        path = self.worktree(self.head)
        self.write(path, "result.txt", "ready")
        self.write(path, "debug.log", "one")
        changes, dropped = engine.stage(path, self.head)
        self.assertEqual((changes, dropped), ([("A", "debug.log"), ("M", "result.txt")], ["debug.log"]))
        before = git(path, "write-tree")
        self.write(path, "debug.log", "two")
        self.write(path, "trace.dmp", "three")
        engine.stage(path, self.head)
        self.assertEqual(git(path, "write-tree"), before)
        self.assertEqual(engine.stage(path, self.head, keep=["Debug.log"])[1], ["trace.dmp"], "a named path is kept, whatever its case")


class VerdictsFailClosed(FixRepairBase):
    def test_a_quoted_example_next_to_the_real_verdict_is_unreadable_not_the_last_one(self):
        revise = json.dumps({"verdict": "REVISE", "reason": "The rows the brief forbids are loaded."})
        passing = json.dumps({"verdict": "PASS", "reason": "ok"})
        for label, answer in {
            "revise then a hypothetical pass": f"{revise} If I were satisfied I would answer {passing}.",
            "pass then a revise": f"{passing}\nOn reflection: {revise}",
            "fenced example and answer": f"Example:\n```json\n{passing}\n```\nMy answer: {revise}",
            "truncated final after a draft": f'Draft: {passing}  Final: {{"verdict":"REVISE","reason":"missing tests for',
            "final with a duplicate field": f'{passing} Final: {{"verdict":"REVISE","verdict":"PASS","reason":"x"}}',
            "final with a cut verdict key": passing + ' Final {"reason": "r", "verdict": "REV',
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                extract_verdict(answer, VERDICTS)

    def test_a_malformed_verdict_is_not_outvoted_by_a_readable_one_wherever_it_stands(self):
        passing = json.dumps(PASS)
        then = " If I were satisfied I would answer " + passing + "."
        for label, answer in {
            "unescaped quotes in the reason": '{"verdict":"REVISE","reason":"The "forbidden" rows are loaded."}' + then,
            "a trailing comma": '{"verdict":"REVISE","reason":"Rows are lost.",}' + then,
            "a literal newline in the reason": '{"verdict":"REVISE","reason":"Rows are lost.\nSee the loader."}' + then,
            "a duplicate key": '{"verdict":"REVISE","verdict":"REVISE","reason":"Rows are lost."}' + then,
            "a truncated body": '{"verdict":"REVISE","reason":"missing tests for the loader' + then,
            "single quotes": "{'verdict': 'REVISE', 'reason': 'Rows are lost.'}" + then,
            "an unquoted key": '{verdict: "REVISE", reason: "Rows are lost."}' + then,
            "a revise cut off inside the object it nests": '{"verdict":"REVISE","reason":"x","details":' + passing,
            "a malformed pass before a readable revise": '{"verdict":"PASS","reason":"a "quoted" word"} Then: ' + json.dumps({"verdict": "REVISE", "reason": "no"}),
            "curly quotes": passing + ' {\u201cverdict\u201d: \u201cREVISE\u201d}',
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                extract_verdict(answer, VERDICTS)

    def test_a_fragment_dangling_after_the_verdict_is_unreadable_whatever_it_started_with(self):
        passing = json.dumps(PASS)
        for label, tail in {
            "cut off before its verdict key": '\nFinal: {"reason": "The loader drops rows and misses',
            "cut off inside a nested object": '\nFinal: {"reason": "x", "details": {"a": 1}, "ver',
            "single-quoted": "\nActually: {'verdict': 'REVISE', 'reason': 'no'}",
            "unquoted keys": '\nActually: {verdict: "REVISE", reason: "no"}',
            "a trailing comma": '\nActually: {"verdict": "REVISE", "reason": "no",}',
            "a comment inside": '\nActually: {"verdict": "REVISE", /* why */ "reason": "no"}',
            "nothing but a key": '\nFinal: {"details"',
            "prose braces first, then a cut-off object": ' See `{ x }`. Final: {"reason": "cut',
            "an object-shaped snippet": " I also changed {mode: 1}.",
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                extract_verdict(passing + tail, VERDICTS)

    def test_format_reminders_and_agreeing_fragments_do_not_make_an_answer_unreadable(self):
        revise = json.dumps({"verdict": "REVISE", "reason": "Fix the loader."})
        passing = json.dumps(PASS)
        judged = json.dumps({"verdict": "NOT_MET", "score": 40, "reason": "gaps remain"})
        for label, answer, allowed, verdict in (
            ("the format after the answer", revise + '\nThe format was {"verdict":"PASS" or "REVISE","reason":"x"}.', VERDICTS, "REVISE"),
            ("the judge format with a range", '{"verdict":"MET" or "NOT_MET","score":0-100,"gaps":[]} ' + judged, ("MET", "NOT_MET"), "NOT_MET"),
            ("the judge format after the answer", judged + ' (format {"verdict":"MET" or "NOT_MET","score":0-100})', ("MET", "NOT_MET"), "NOT_MET"),
            ("a malformed object that says the same thing", '{"verdict":"REVISE","reason":"a "b" c"} Again: ' + revise, VERDICTS, "REVISE"),
            ("a malformed duplicate of the same pass", passing + ' {"verdict":"PASS","reason":"x",}', VERDICTS, "PASS"),
            ("code braces in the explanation after", passing + " I also fixed `if (ok) { return }`.", VERDICTS, "PASS"),
            ("code braces before", "In `def f() { x }` I see no issue. " + passing, VERDICTS, "PASS"),
        ):
            with self.subTest(label):
                self.assertEqual(extract_verdict(answer, allowed)["verdict"], verdict)

    def test_unambiguous_answers_still_read(self):
        revise = json.dumps({"verdict": "REVISE", "reason": "Fix the loader."})
        passing = json.dumps({"verdict": "pass", "reason": "Looks right."})
        quoted = json.dumps({"verdict": "REVISE", "reason": 'It answered {"verdict": "PASS"} earlier.'})
        for label, answer, verdict in (
            ("pure json", revise, "REVISE"),
            ("a verdict quoted inside the reason", quoted, "REVISE"),
            ("prose around one object", f"I read everything. {passing} Thanks!", "PASS"),
            ("one fenced block", f"Here:\n```json\n{passing}\n```\nDone.", "PASS"),
            ("the same verdict twice", f"{passing} Again: " + json.dumps({"verdict": "PASS", "reason": "second wording"}), "PASS"),
            ("a template echo before the answer", 'Format: {"verdict":"PASS" or "REVISE","reason":"x"}. ' + revise, "REVISE"),
            ("prose braces and an unrelated object", "Use { carefully. " + passing + ' {"note": 1}', "PASS"),
        ):
            with self.subTest(label):
                self.assertEqual(extract_verdict(answer, VERDICTS)["verdict"], verdict)

    def test_only_the_answers_that_decide_something_fail_closed(self):
        decoy = json.dumps({"objective": "an example", "acceptance": ["x"]})
        real = json.dumps({"objective": "the real brief", "acceptance": ["y"]})
        self.assertEqual(extract_object(f"Example: {decoy}\nNow the real one: {real}", "objective")["objective"], "the real brief")
        with self.assertRaises(ValueError):
            extract_object(f"Example: {decoy}\nNow the real one: {real}", "objective", fail_closed=True)
        for key in ("verdict", "milestones"):
            with self.subTest(key), self.assertRaises(ValueError):
                extract_object(json.dumps({key: "PASS"}) + " then " + json.dumps({key: "REVISE"}), key)

    def test_a_plan_followed_by_the_echoed_template_is_not_the_template(self):
        real = json.dumps({"milestones": [{"title": "Build the loader", "task": "Write loader.py"}]})
        template = json.dumps({"milestones": [{"title": "...", "task": "..."}]})
        with self.assertRaises(ValueError):
            parse_wave(f"{real}\nThe template was {template}", 3)
        with self.assertRaises(ValueError):
            extract_object(f"{real} {template}", "milestones")
        broken = '{"milestones":[{"title":"Build the "loader"","task":"Write loader.py"}]}'
        with self.assertRaises(ValueError):
            parse_wave(f"{broken}\nThe format is {template}", 3)  # the malformed real plan must not lose to the echoed template
        with self.assertRaises(ValueError):
            parse_wave(f"{template}\nThe plan: {broken}", 3)
        self.assertEqual([m["title"] for m in parse_wave(f"Plan:\n{real}", 3)[0]], ["Build the loader"])

    def test_an_ambiguous_review_never_promotes_a_build(self):
        answer = ('{"verdict":"REVISE","reason":"The "forbidden" rows are loaded."} If I were satisfied I would answer '
                  '{"verdict":"PASS","reason":"ok"}.')
        run = self.configured(self.worker(), reviewer=self.script("reviewer", [answer]), attempts=1, retry=FAST)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "review_passed"), [])
        self.assertEqual(git(self.repo, "branch", "--list", "nightweaver/*"), "", "no result branch was made")
        self.assertIn("Reviewer unavailable", self.state(run)["last_error"])


class GitTimeouts(FixRepairBase):
    def test_the_first_git_call_of_a_run_uses_the_plans_limit(self):
        slow = self.configured(self.worker(), git_timeout=600)
        plain = self.configured(self.worker())
        seen = []

        def probe(path):
            seen.append(engine.GIT_TIMEOUT)
            raise ValueError("stop here")

        engine.GIT_TIMEOUT = 90
        with patch("nightweaver.engine.common_dir", side_effect=probe):
            self.assertEqual(Run(self.store, slow).run(), "blocked")
            engine.GIT_TIMEOUT = 3600  # a dispatcher that has just run a plan with a long limit
            self.assertEqual(Run(self.store, plain).run(), "blocked")
        self.assertEqual(seen, [600, 90])

    def test_a_process_that_resumes_into_judging_planning_or_promoting_uses_the_plans_limit(self):
        route = {"kind": "command", "argv": fixtures.fixture("good")}
        seen = []

        def finish(run, state):
            seen.append((state["phase"], engine.GIT_TIMEOUT))
            run.set("fixture_done", {}, phase="done")

        for phase in ("judging", "planning"):
            run = self.configured(self.worker(), git_timeout=600, pursue={"judge": route, "planner": route})
            self.store.transition(run, "fixture_restart", {}, phase=phase, step=1)
            engine.GIT_TIMEOUT = 90
            target = "nightweaver.pursuit.judge" if phase == "judging" else "nightweaver.pursuit.plan_wave"
            with patch(target, side_effect=finish):
                self.assertEqual(Run(self.store, run).run(), "done")
        run = self.configured(self.worker(), git_timeout=600)
        self.store.transition(run, "fixture_restart", {}, phase="promoting", step=0)
        engine.GIT_TIMEOUT = 90
        with patch.object(Run, "promote", autospec=True, side_effect=lambda self_, state: (
                seen.append(("promoting", engine.GIT_TIMEOUT)), self_.set("fixture_done", {}, phase="done"))):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen, [("judging", 600), ("planning", 600), ("promoting", 600)])

    def test_a_timeout_kills_the_whole_process_tree_and_returns_at_once(self):
        pulse = self.root / "pulse.txt"
        parent = self.root / "parent.py"
        # The parent starts a grandchild that inherits its handles and never exits, as git's reset --hard does during a checkout.
        parent.write_text("\n".join(["import subprocess, sys, time",
                                     f"subprocess.Popen([sys.executable, {str(self.helper)!r}, 'pulse', {str(pulse)!r}])",
                                     "time.sleep(120)"]), encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            engine.run_owned([sys.executable, str(parent)], 6, dict(os.environ))
        self.assertLess(time.monotonic() - started, 40, "subprocess.run would wait for the grandchild, which never ends")
        self.assertEqual(caught.exception.timeout, 6)
        if not pulse.exists():
            self.skipTest("the grandchild had not started before the timeout")
        time.sleep(1)
        size = pulse.stat().st_size
        time.sleep(1)
        self.assertEqual(pulse.stat().st_size, size, "the grandchild is gone too")

    def test_a_timed_out_worktree_add_leaves_no_locked_entry_so_retry_works(self):
        path = self.root / "worktrees" / "01"
        path.parent.mkdir()
        real = engine.run_owned

        def timing_out(argv, limit, env):
            real(argv, limit, env)  # the checkout completes, but git still holds its initializing lock when the parent is killed
            git(self.repo, "worktree", "lock", "--reason", "initializing", str(path))
            raise subprocess.TimeoutExpired(argv, limit)

        with patch.object(engine, "run_owned", timing_out), self.assertRaises(subprocess.TimeoutExpired):
            engine.git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        listed = [Path(line[9:]).resolve() for line in git(self.repo, "worktree", "list", "--porcelain").splitlines()
                  if line.startswith("worktree ")]
        self.assertEqual(listed, [self.repo.resolve()], "the locked entry was unlocked, removed and pruned")
        self.assertFalse(path.exists())
        engine.git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        self.assertTrue((path / "result.txt").is_file(), "the same path can be checked out again")

    def test_the_run_blocks_with_a_message_a_retry_can_act_on(self):
        run = self.configured(self.worker(), git_timeout=60)
        real = engine.run_owned

        def timing_out(argv, limit, env):
            real(argv, limit, env)
            git(self.repo, "worktree", "lock", "--reason", "initializing", argv[-2])
            raise subprocess.TimeoutExpired(argv, limit)

        with patch.object(engine, "run_owned", timing_out):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIn("took more than 900 seconds", self.state(run)["last_error"])
        self.assertEqual(Run(self.store, run).run(retry=True), "done", "--retry works without a manual git worktree unlock")


HOLDER = """
import sys, time
from pathlib import Path
admin = Path(sys.argv[1])
(admin / "locked").write_text("initializing")
held = open(admin / "index.lock", "w")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
held.close()
(admin / "index.lock").unlink()
(admin / "locked").unlink()
"""


def reap(process):
    process.kill()
    process.wait(timeout=20)
    if process.stdout:
        process.stdout.close()


class WorktreeAddSurvivesACrash(FixRepairBase):
    def slow_checkout(self, files=4, pause=2):
        """Commit files whose checkout is slow, as a huge repository's is: each one waits in a smudge filter."""
        slow = self.root / "slow.py"
        slow.write_text(f"import sys, time\ntime.sleep({pause})\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        (self.repo / ".gitattributes").write_text("*.big filter=slow\n", encoding="utf-8")
        for number in range(files):
            (self.repo / f"f{number}.big").write_text(f"data {number}", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Slow files")
        git(self.repo, "config", "filter.slow.smudge", f'"{Path(sys.executable).as_posix()}" "{slow.as_posix()}"')
        git(self.repo, "config", "filter.slow.clean", "cat")

    def admin(self, path):
        return Path(path.joinpath(".git").read_text(encoding="utf-8").partition(":")[2].strip())

    def half_made(self, path, *, index_lock=True):
        """A checkout the way a dead git leaves it: locked as initializing, an index lock nobody holds, files missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        admin = self.admin(path)
        (admin / "locked").write_text("initializing", encoding="utf-8")
        if index_lock:
            (admin / "index.lock").write_text("", encoding="utf-8")
        (path / "result.txt").unlink()
        return admin

    def listed(self):
        return [Path(line[9:]).resolve() for line in git(self.repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]

    def test_a_killed_runner_leaves_git_to_finish_its_checkout_and_the_resume_completes(self):
        self.slow_checkout()
        run = self.configured(self.worker())
        env = {**os.environ, "PYTHONPATH": str(Path(engine.__file__).resolve().parents[1])}
        runner = subprocess.Popen([sys.executable, "-m", "nightweaver", "--home", str(self.store.root), "run", run], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lock = self.repo / ".git" / "worktrees" / "01" / "locked"
        try:
            deadline = time.monotonic() + 60
            while not lock.exists() and runner.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(lock.exists(), "the checkout started")
            time.sleep(1)  # git is in the middle of it
            subprocess.run(["taskkill", "/F", "/PID", str(runner.pid)], capture_output=True)
        finally:
            runner.kill()
            runner.wait(timeout=20)
        deadline = time.monotonic() + 60
        while lock.exists() and time.monotonic() < deadline:
            time.sleep(.2)
        self.assertFalse(lock.exists(), "git finished its checkout and released its lock after the runner died")
        path = self.store.directory(run) / "worktrees" / "01"
        self.assertEqual(sorted(name for name in os.listdir(path) if name.endswith(".big")), [f"f{n}.big" for n in range(4)])
        self.assertEqual(Run(self.store, run).run(), "done", "the resume used the finished checkout")
        self.assertEqual(self.head_names(run), [".gitattributes", ".gitignore", "f0.big", "f1.big", "f2.big", "f3.big", "result.txt", "untouched.txt"])

    def test_closing_the_checkout_job_does_not_kill_git_but_terminating_it_does(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        survivor = subprocess.Popen(sleeper, creationflags=subprocess.CREATE_NO_WINDOW)
        victim = subprocess.Popen(sleeper, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(reap, survivor)
        self.addCleanup(reap, victim)
        with engine.SurvivingJob() as job:
            job.attach(survivor.pid)
        time.sleep(.5)
        self.assertIsNone(survivor.poll(), "the runner dying closes the handle; git is left alone to finish")
        with engine.SurvivingJob() as job:
            job.attach(victim.pid)
            job.terminate()
            victim.wait(timeout=20)
        self.assertIsNotNone(victim.poll(), "a timeout still ends everything in the job")

    def test_a_checkout_that_nothing_is_making_is_cleared_and_made_again_by_the_run(self):
        run = self.configured(self.worker())
        controller = Run(self.store, run)
        controller.gitdir = common_dir(controller.repository)
        path = self.store.directory(run) / "worktrees" / "01"
        admin = self.half_made(path)
        self.assertEqual(engine.initializing(path), admin)
        with patch.object(engine, "SETTLE_POLL", .05):
            self.assertEqual(controller.worktree(self.state(run)), path)
        self.assertIsNone(engine.initializing(path))
        self.assertTrue((path / "result.txt").is_file(), "a whole checkout, not the half-made one")
        self.assertEqual(self.listed().count(path.resolve()), 1)
        self.assertEqual(len(self.events(run, "worktree_ready")), 1)
        self.assertEqual(Run(self.store, run).run(), "done")

    def test_a_locked_checkout_with_no_index_lock_is_given_up_on_after_a_few_looks(self):
        path = self.root / "worktrees" / "01"
        admin = self.half_made(path, index_lock=False)
        with patch.object(engine, "SETTLE_POLL", .01), patch.object(engine, "SETTLE_QUIET", 3):
            self.assertFalse(engine.settle_worktree(self.repo, path))
        self.assertFalse(path.exists())
        self.assertFalse(admin.exists())
        self.assertEqual(self.listed(), [self.repo.resolve()])
        engine.git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        self.assertTrue((path / "result.txt").is_file(), "the same path can be checked out again")

    def test_a_checkout_that_git_is_still_making_is_waited_for_not_cleared(self):
        path = self.root / "worktrees" / "01"
        path.parent.mkdir()
        git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        (path / "sentinel.txt").write_text("still here", encoding="utf-8")
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.admin(path)), "3"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        with patch.object(engine, "SETTLE_POLL", .2):
            self.assertTrue(engine.settle_worktree(self.repo, path, wait=60))
        self.assertGreater(time.monotonic() - started, 1.5, "it waited for the holder to finish")
        holder.wait(timeout=20)
        self.assertEqual((path / "sentinel.txt").read_text(encoding="utf-8"), "still here", "nothing was cleared")
        self.assertIsNone(engine.initializing(path))

    def test_a_checkout_that_never_finishes_is_cleared_when_the_wait_runs_out(self):
        path = self.root / "worktrees" / "01"
        path.parent.mkdir()
        git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        admin = self.admin(path)
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(admin), "60"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with patch.object(engine, "SETTLE_POLL", .1):
            self.assertFalse(engine.settle_worktree(self.repo, path, wait=1))
        self.assertFalse(path.exists())

    def test_a_finished_or_absent_checkout_is_left_alone(self):
        path = self.root / "worktrees" / "01"
        self.assertFalse(engine.settle_worktree(self.repo, path))
        path.parent.mkdir()
        git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        self.assertTrue(engine.settle_worktree(self.repo, path))
        self.assertTrue((path / "result.txt").is_file())
        with patch.object(engine, "time", SimpleNamespace(sleep=lambda _seconds: None)):  # tests that fake the clock must not need it here
            self.assertTrue(engine.settle_worktree(self.repo, path))

    def test_an_empty_folder_git_never_linked_is_cleared_and_a_folder_with_files_is_not(self):
        path = self.root / "worktrees" / "01"
        path.mkdir(parents=True)
        self.assertFalse(engine.settle_worktree(self.repo, path))
        self.assertFalse(path.exists())
        other = path.parent / "02"
        other.mkdir()
        (other / "work.txt").write_text("someone's files", encoding="utf-8")
        self.assertTrue(engine.settle_worktree(self.repo, other), "a folder with files is not ours to clear")
        self.assertTrue((other / "work.txt").is_file())


class IdleDefault(FixRepairBase):
    def health(self, run, role="worker"):
        seen = {}

        def fake(config, mode, path, log, prompt, timeout, paused, session=None, evidence=None, health=None, **_rest):
            seen.update(health or {})
            return 0, None, ""

        with patch("nightweaver.engine.invoke", side_effect=fake):
            Run(self.store, run).call_provider({"kind": "command", "argv": ["x"]}, role, self.repo, self.root / f"{role}.jsonl", "prompt")
        return seen

    def test_only_plans_written_by_this_version_get_the_default(self):
        new = self.configured(self.worker())
        self.assertIs(self.store.get(new)["spec"]["idle_default"], True)
        for role in ("worker", "reviewer", "judge", "planner"):
            self.assertEqual(self.health(new, role)["idle_timeout"], 900, role)
        saved = self.configured(self.worker())
        self.rewrite_spec(saved, idle_default=None)  # a plan saved by 0.18 has no marker
        for role in ("worker", "reviewer", "judge", "planner"):
            self.assertNotIn("idle_timeout", self.health(saved, role), f"{role} keeps the behavior the plan was approved with")

    def test_an_explicit_idle_timeout_always_wins(self):
        pinned = self.configured(self.worker(), budgets={"idle_timeout": 45})
        self.assertEqual(self.health(pinned)["idle_timeout"], 45)
        self.rewrite_spec(pinned, idle_default=None)
        self.assertEqual(self.health(pinned)["idle_timeout"], 45)

    def test_a_saved_plan_survives_a_silent_stretch_that_a_new_plan_does_not(self):
        silent = [sys.executable, "-c", "import time; time.sleep(6)"]
        new = Run(self.store, self.configured(silent))
        saved_id = self.configured(silent)
        self.rewrite_spec(saved_id, idle_default=None)
        saved = Run(self.store, saved_id)
        with patch.object(engine, "IDLE_DEFAULT", 2):
            cut, _, _ = new.call_provider({"kind": "command", "argv": silent}, "worker", self.repo, self.root / "new.jsonl", "prompt")
        self.assertEqual(cut, 125)
        with patch.object(engine, "IDLE_DEFAULT", 2):
            kept, _, _ = saved.call_provider({"kind": "command", "argv": silent}, "worker", self.repo, self.root / "old.jsonl", "prompt")
        self.assertEqual(kept, 0)


class ReviewerFailuresAreNotTheWorkers(FixRepairBase):
    def blocked_by_reviewer(self, outcomes, *, attempts=6, **extra):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, attempts=attempts,
                              retry={**FAST, "repeated_failure_limit": 3}, **extra)
        patcher, prompts = self.reviewer_outcomes(outcomes)
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "blocked")
        return run, prompts

    def assert_worker_uncharged(self, run, prompts, asked):
        state = self.state(run)
        self.assertEqual(self.events(run, "attempt_rejected"), [], "nothing is charged to the worker")
        self.assertEqual((self.calls(run, "worker"), state["attempts_used"], len(self.events(run, "gate_started"))), (1, 1, 1))
        self.assertEqual(len(prompts), asked)
        self.assertEqual(state["blocked_from"], "reviewing")
        self.assertTrue(state["last_error"].startswith("Reviewer unavailable: "), state["last_error"])
        self.assertEqual(len(self.events(run, "provider_retry_scheduled")), 2, "the review is retried twice, as any transient is")
        self.assertEqual(self.events(run, "provider_unavailable")[0]["role"], "reviewer")
        self.assertEqual(self.events(run, "repeated_failure_stopped"), [])

    def test_an_idle_stall_a_timeout_and_a_crash_are_reviewer_transients(self):
        for code, wording in ((125, "idle timeout"), (124, "time limit"), (126, "output limit"), (1, "exited with code 1")):
            with self.subTest(code):
                run, prompts = self.blocked_by_reviewer([(code, "no output for a long time")])
                self.assert_worker_uncharged(run, prompts, 3)
                self.assertIn(wording, self.state(run)["last_error"])
                self.assertEqual([e["exit"] for e in self.events(run, "reviewer_failed")], [code] * 3)

    def test_two_unreadable_answers_are_a_reviewer_transient_too(self):
        run, prompts = self.blocked_by_reviewer([(0, "no json at all")])
        self.assert_worker_uncharged(run, prompts, 6)
        self.assertEqual([e["count"] for e in self.events(run, "review_unparseable")], [1, 2] * 3)
        self.assertIn("could not be read after two tries", self.state(run)["last_error"])

    def test_a_pass_that_gives_no_reason_is_asked_again_once_and_then_the_reviewer_is_unavailable(self):
        run, prompts = self.blocked_by_reviewer([(0, json.dumps({"verdict": "PASS", "reason": "  "}))])
        self.assert_worker_uncharged(run, prompts, 6)
        self.assertEqual({e["problem"] for e in self.events(run, "review_unparseable")}, {"passed the build without a reason"})
        recovered = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, retry=FAST)
        patcher, prompts = self.reviewer_outcomes([(0, json.dumps({"verdict": "PASS", "reason": ""})), (0, json.dumps(PASS))])
        with patcher:
            self.assertEqual(Run(self.store, recovered).run(), "done")
        self.assertEqual((len(prompts), self.calls(recovered, "worker"), self.events(recovered, "attempt_rejected")), (2, 1, []))

    def test_a_revise_with_a_reason_is_still_the_workers_rejection(self):
        run = self.configured(self.worker("worker", "ready", "notes{n}.txt"),
                              reviewer=self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}, PASS]),
                              attempts=3, retry=FAST)
        self.assertEqual(Run(self.store, run).run(), "done")
        rejected = self.events(run, "attempt_rejected")
        self.assertEqual([r["phase"] for r in rejected], ["review"])
        self.assertTrue(rejected[0]["issue"].startswith("Independent review requested changes.\nRename the helper."))
        self.assertEqual(rejected[0]["finding"], "Rename the helper.")

    def test_one_reviewer_stall_costs_one_retry_and_nothing_else(self):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, attempts=1, retry=FAST)
        patcher, prompts = self.reviewer_outcomes([(125, "idle"), (0, json.dumps(PASS))])
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual((len(prompts), self.calls(run, "worker"), self.events(run, "attempt_rejected")), (2, 1, []))
        self.assertEqual(len(self.events(run, "provider_retry_scheduled")), 1)
        self.assertEqual(self.state(run)["attempts_used"], 0, "an accepted milestone resets the count")

    def test_retry_resumes_the_review_without_another_worker_call(self):
        run, _ = self.blocked_by_reviewer([(125, "idle")])
        patcher, prompts = self.reviewer_outcomes([(0, json.dumps(PASS))])
        with patcher:
            self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual((len(prompts), self.calls(run, "worker")), (1, 1))

    def test_a_reviewer_fallback_takes_over_before_the_run_blocks(self):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")},
                              reviewer_fallbacks=[{"kind": "command", "argv": fixtures.fixture("review")}], attempts=2, retry=FAST)
        patcher, prompts = self.reviewer_outcomes([(125, "idle"), (0, json.dumps(PASS))])
        with patcher:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.events(run, "provider_fallback")), 1)
        self.assertEqual(self.events(run, "attempt_rejected"), [])


class FeedbackReopensTheReview(FixRepairBase):
    def test_operator_feedback_after_a_rejection_makes_the_reviewer_look_again_at_an_unchanged_build(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}, PASS])
        run = self.configured(self.worker(), reviewer=reviewer, attempts=6, retry={**FAST, "repeated_failure_limit": 3})
        self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
        self.assertEqual([r["phase"] for r in self.events(run, "attempt_rejected")], ["review"])
        self.store.feedback(run, "Keep the name; the reviewer must not ask for a rename.")
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([r["phase"] for r in self.events(run, "attempt_rejected")], ["review"], "no no_progress rejection")
        second = self.prompts("reviewer")[1]
        self.assertIn("Keep the name; the reviewer must not ask for a rename.", second)
        self.assertEqual(self.calls(run, "reviewer"), 2)

    def test_feedback_that_arrives_while_the_reviewer_is_working_is_not_taken_as_seen(self):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")},
                              attempts=6, retry={**FAST, "repeated_failure_limit": 3})
        prompts = []

        def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, **kwargs):
            if mode != "reviewer":
                return real_invoke(config, mode, cwd, log, prompt, timeout, paused, session, **kwargs)
            prompts.append(prompt)
            if len(prompts) == 1:
                self.store.feedback(run, "Keep the name; do not ask for a rename.")  # said while the reviewer reads the build
                return 0, None, json.dumps({"verdict": "REVISE", "reason": "Rename the helper."})
            return 0, None, json.dumps(PASS)

        with patch("nightweaver.engine.invoke", side_effect=invoke):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([r["phase"] for r in self.events(run, "attempt_rejected")], ["review"], "no no_progress rejection")
        self.assertEqual(len(prompts), 2, "the reviewer was asked again, with the new instructions")
        self.assertNotIn("Keep the name", prompts[0])
        self.assertIn("Keep the name; do not ask for a rename.", prompts[1])
        self.assertIn("Keep the name; do not ask for a rename.", self.prompts("worker")[1], "the worker was told too")

    def test_feedback_that_arrives_while_a_worker_call_is_in_flight_reopens_the_review_too(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}, PASS])
        run = self.configured(self.worker(), reviewer=reviewer, attempts=6, retry={**FAST, "repeated_failure_limit": 3})
        workers = []

        def invoke(config, mode, *args, **kwargs):
            if mode == "worker":
                workers.append(mode)
                if len(workers) == 2:
                    self.store.feedback(run, "Keep the name; the reviewer must not ask for a rename.")  # the prompt is already built
            return real_invoke(config, mode, *args, **kwargs)

        with patch("nightweaver.engine.invoke", side_effect=invoke):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertNotIn("no_progress", [r["phase"] for r in self.events(run, "attempt_rejected")])
        self.assertEqual(len(self.prompts("reviewer")), 2, "the reviewer judged the unchanged build again")
        self.assertIn("Keep the name; the reviewer must not ask for a rename.", self.prompts("reviewer")[1])

    def test_a_rejection_records_the_feedback_its_judge_worked_from(self):
        run = self.configured(self.worker(), attempts=6, retry={**FAST, "repeated_failure_limit": 9})
        controller = Run(self.store, run)
        controller.gitdir = common_dir(controller.repository)
        controller.worktree(self.state(run))
        self.store.transition(run, "fixture_review", {}, phase="reviewing", attempt=1, attempts_used=1)
        self.store.feedback(run, "First instruction.")
        self.store.feedback(run, "Second instruction.")
        controller.reject("Rename.", "review", finding="Rename.", seen=0)
        controller.reject("Rename again.", "review", finding="Rename again.")
        controller.reject("Nothing changed.", "no_progress")
        recorded = [(item["phase"], item["feedback"]) for item in self.state(run)["rejected_trees"]]
        self.assertEqual(recorded, [("review", 0), ("review", 2), ("no_progress", 2)])

    def test_without_feedback_the_unchanged_build_is_still_short_circuited(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}, PASS])
        run = self.configured(self.worker(), reviewer=reviewer, attempts=6, retry={**FAST, "repeated_failure_limit": 2})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual([r["phase"] for r in self.events(run, "attempt_rejected")], ["review", "no_progress", "no_progress"])
        self.assertEqual(len(self.prompts("reviewer")), 1)

    def test_retry_after_the_stop_asks_the_reviewer_again(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Rename the helper."}, PASS])
        run = self.configured(self.worker(), reviewer=reviewer, attempts=6, retry={**FAST, "repeated_failure_limit": 2})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.state(run)["rejected_trees"][-1]["phase"], "no_progress")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(len(self.prompts("reviewer")), 2, "the reviewer was asked again about the unchanged build")
        self.assertEqual(self.state(run).get("rejected_trees"), [])


class AdviceAndFamilies(FixRepairBase):
    def test_the_local_advisory_runs_once_per_candidate_tree(self):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, retry=FAST,
                              advisory_ollama={"model": "local"}, budgets={"max_calls": 4})
        patcher, prompts = self.reviewer_outcomes([ProviderUnavailable("blip", "transient"), (0, json.dumps(PASS))])
        with patcher, patch("nightweaver.engine.advisory", return_value="Check the empty-input path") as advise:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(advise.call_count, 1, "the transient retry did not ask the local model again")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(len(self.events(run, "local_advice")), 1)
        self.assertTrue(all("A local model suggested checking: Check the empty-input path" in p for p in prompts), "the retry still carries the note")

    def test_an_optional_advisory_that_failed_is_not_asked_again_about_the_same_tree(self):
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, retry=FAST,
                              advisory_ollama={"model": "local"})
        patcher, _ = self.reviewer_outcomes([ProviderUnavailable("blip", "transient"), (0, json.dumps(PASS))])
        with patcher, patch("nightweaver.engine.advisory", side_effect=OSError("local server offline")) as advise:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(advise.call_count, 1)
        self.assertEqual(len(self.events(run, "local_advice_unavailable")), 1)

    def test_a_changed_tree_is_advised_again(self):
        reviewer = self.script("reviewer", [{"verdict": "REVISE", "reason": "Add notes."}, PASS])
        run = self.configured(self.fx("worker", self.root / "w.count", "ready", "notes{n}.txt"), reviewer=reviewer, attempts=3, retry=FAST,
                              advisory_ollama={"model": "local"})
        with patch("nightweaver.engine.advisory", return_value="Check it") as advise:
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(advise.call_count, 2, "each candidate tree is advised once")

    def test_a_command_route_has_no_known_vendor_and_never_warns(self):
        self.assertEqual([family({"kind": k}) for k in ("codex", "claude", "deepseek", "gemini")], ["openai", "anthropic", "deepseek", "google"])
        self.assertIsNone(family({"kind": "command"}))
        self.assertIsNone(family({"kind": "something-new"}))
        run = self.configured(self.worker(), reviewer={"kind": "command", "argv": fixtures.fixture("review")})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.events(run, "independence_degraded"), [])

    def test_a_known_family_still_warns_once(self):
        def invoke(config, mode, *args, **kwargs):
            if mode == "reviewer":
                return 0, None, json.dumps(PASS)
            if config["kind"] == "codex":
                config = {"kind": "command", "argv": self.worker()}
            return real_invoke(config, mode, *args, **kwargs)

        with patch("nightweaver.engine.invoke", side_effect=invoke):
            run = self.configured(self.worker(), reviewer={"kind": "codex"},
                                  milestones=[{"title": "First", "task": "Do first", "worker": {"kind": "codex"}}])
            self.assertEqual(Run(self.store, run).run(), "done")
        warned = self.events(run, "independence_degraded")
        self.assertEqual([(w["role"], w["worker"]["family"], w["reviewer_or_judge"]["family"]) for w in warned], [("reviewer", "openai", "openai")])


if __name__ == "__main__":
    unittest.main()
