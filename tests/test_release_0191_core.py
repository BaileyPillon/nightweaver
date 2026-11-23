"""0.19.1 core: wave gates keep final-only checks off early milestones, objectives come from a file, and commits always have an identity."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_release_019_explore as explore_tests
import test_windows as fixtures

from nightweaver.cli import main
from nightweaver.engine import FALLBACK_IDENTITY, Run, commit_tree, git, identity_error, identity_message, require_identity
from nightweaver.pursuit import wave_gates
from nightweaver.spec import validate

HELPER = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
READ = "read fixture value"
FINAL = "only the finished build"
LAST = ("Prepare fixture", "Prepare more", "Third")  # titles that end a build the final check accepts


def helper(mode, *args):
    return [sys.executable, HELPER, mode, *map(str, args)]


def judged(score, met=False):
    return {"verdict": "MET" if met else "NOT_MET", "score": score, "reason": f"Reason {score}",
            "gaps": [] if met else [{"title": f"Gap at {score}", "detail": "Detail"}],
            "criteria": [{"criterion": "Looks right", "met": met, "note": "Observed"}]}


def plan(*titles):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles]}


def final_gate(name=FINAL):
    """Passes only on a build whose newest milestone is one of LAST: a stand-in for 'every study has result.json', which a freeze-only milestone cannot meet."""
    code = f"import sys; sys.exit(0 if open('lineage.txt').read().splitlines()[-1] in {LAST!r} else 1)"
    return {"name": name, "argv": [sys.executable, "-c", code], "timeout": 20}


def gate(name):
    return {"name": name, "argv": fixtures.fixture("gate"), "timeout": 20}


def names(spec):
    return [[g["name"] for g in step["gates"]] for step in spec["milestones"]]


@unittest.skipUnless(os.name == "nt", "Windows controller")
class WaveGateTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    cli = explore_tests.ExploreTests.cli
    script = explore_tests.ExploreTests.script

    def route(self, name, items):
        return {"kind": "command", "argv": helper("script", self.script(name, items))}

    def prompts(self, name):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def pursue(self, judge, planner, **extra):
        block = {"judge": self.route("judge", judge), "planner": self.route("planner", planner)}
        return self.configured(helper("build", self.root / "worker.prompts"), pursue=block, **extra)

    def rewrite_spec(self, run, **changes):
        with self.store.connect() as db:
            spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (run,)).fetchone()["spec"])
            for key, value in changes.items():
                spec.pop(key, None) if value is None else spec.update({key: value})
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))

    def test_only_the_last_milestone_of_a_wave_carries_the_final_checks(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("First", "Second", "Third")], final_gates=[final_gate()])
        self.assertEqual(Run(self.store, run).run(), "done")
        spec = self.store.get(run)["spec"]
        self.assertEqual(spec["final_checks"], [FINAL])
        self.assertEqual(names(spec), [[READ, FINAL], [READ], [READ], [READ, FINAL]])
        self.assertEqual(git(self.repo, "show", f"nightweaver/{run}:lineage.txt").splitlines(), ["Prepare fixture", "First", "Second", "Third"])
        self.assertEqual([e["details"]["step"] for e in self.store.events(run, limit=None, kinds=["milestone_accepted"])], [1, 2, 3, 4])
        # The planner is told which checks bind every milestone and which bind only the last one.
        every, last = self.prompts("planner")[0].split("Checks the last milestone you plan must also pass.")
        self.assertIn(READ, every)
        self.assertNotIn(FINAL, every.split("Checks every new milestone must pass")[1])
        self.assertIn(FINAL, last)

    def test_a_plan_without_final_checks_gives_every_new_milestone_the_plans_checks(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("First", "Second", "Third")])
        self.assertEqual(Run(self.store, run).run(), "done")
        spec = self.store.get(run)["spec"]
        self.assertEqual(spec["final_checks"], [])
        self.assertEqual(names(spec), [[READ]] * 4)
        self.assertNotIn("Checks the last milestone you plan must also pass", self.prompts("planner")[0])

    def test_a_spec_made_by_019_derives_the_final_checks_and_resumes(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("First", "Second", "Third")], final_gates=[final_gate()],
                          milestones=[{"title": "Prepare fixture", "task": "Set the value"}, {"title": "Prepare more", "task": "Extend it"}])
        self.rewrite_spec(run, final_checks=None)  # 0.19 recorded no such key
        self.assertNotIn("final_checks", self.store.get(run)["spec"])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(names(self.store.get(run)["spec"]), [[READ], [READ, FINAL], [READ], [READ], [READ, FINAL]])

    def test_a_pursuit_with_only_final_checks_plans_single_milestone_waves(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("Third")], gates=[], final_gates=[final_gate()])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(names(self.store.get(run)["spec"]), [[FINAL], [FINAL]])
        self.assertIn("1..1 milestones", self.prompts("planner")[0])

    def spec(self, **extra):
        data = {"title": "Retention", "objective": "Keep checks apart", "repository": str(self.repo), "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                "pursue": {"judge": {"kind": "command", "argv": fixtures.fixture("good")}}, **extra}
        return validate(data)

    def test_retained_checks_follow_their_retention_into_new_waves(self):
        spec = self.spec(gates=[gate("Plan")], final_gates=[gate("Final")], milestones=[
            {"title": "M1", "task": "t", "gates": [gate("Own final")], "retain_gates": "final"},
            {"title": "M2", "task": "t", "gates": [gate("Own subsequent")], "retain_gates": "subsequent"},
            {"title": "M3", "task": "t"}])
        self.assertEqual(names(spec), [["Plan", "Own final"], ["Plan", "Own subsequent"], ["Plan", "Final", "Own final", "Own subsequent"]])
        self.assertEqual(spec["final_checks"], ["Final", "Own final"])
        each, final = wave_gates(spec)
        self.assertEqual(([g["name"] for g in each], [g["name"] for g in final]), (["Plan", "Own subsequent"], ["Final", "Own final"]))

    def test_checks_the_last_milestone_owns_stay_with_every_new_milestone(self):
        spec = self.spec(gates=[gate("Plan")], milestones=[{"title": "M1", "task": "t"}, {"title": "M2", "task": "t", "gates": [gate("Own")]}])
        self.assertEqual(spec["final_checks"], [])
        self.assertEqual([[g["name"] for g in half] for half in wave_gates(spec)], [["Plan", "Own"], []])

    def test_plans_without_a_pursuit_record_nothing(self):
        data = {"title": "Plain", "objective": "No pursuit", "repository": str(self.repo), "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                "gates": [gate("Plan")], "final_gates": [gate("Final")], "milestones": [{"title": "M1", "task": "t"}]}
        self.assertNotIn("final_checks", validate(data))

    def test_derivation_for_a_spec_made_by_019(self):
        spec = self.spec(gates=[gate("Plan")], final_gates=[gate("Final")], milestones=[{"title": "M1", "task": "t"}, {"title": "M2", "task": "t"},
                                                                                     {"title": "M3", "task": "t"}])
        self.assertEqual([[g["name"] for g in half] for half in wave_gates(spec)], [["Plan"], ["Final"]])
        legacy = {key: value for key, value in spec.items() if key != "final_checks"}
        self.assertEqual(wave_gates(legacy), wave_gates(spec))
        # A wave 0.19 had already planned carries the final check on every milestone; the earlier milestones still tell the two apart.
        legacy["milestones"] = [*legacy["milestones"], *({**legacy["milestones"][-1], "title": f"W{n}"} for n in range(3))]
        self.assertEqual(wave_gates(legacy), wave_gates(spec))
        # One milestone gives nothing to compare with: every check stays per-milestone, as 0.19 had it.
        single = {**legacy, "milestones": legacy["milestones"][-1:]}
        self.assertEqual([[g["name"] for g in half] for half in wave_gates(single)], [["Plan", "Final"], []])


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ObjectiveFileTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = explore_tests.ExploreTests.cli

    def template(self):
        path = self.root / "template.json"
        path.write_text(json.dumps({"repository": str(self.repo), "title": "Template", "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                                    "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}]}), encoding="utf-8")
        return path

    def long_objective(self):
        text = "Build the whole study atlas ✓ é\n" + "x" * 32500
        path = self.root / "objective.txt"
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8").replace(b"\n", b"\r\n"))  # a Windows editor's BOM and CRLF
        return path, text.replace("\r\n", "\n")

    def refused(self, *args):
        with self.assertRaises(SystemExit) as raised, redirect_stderr(StringIO()) as errors:
            main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(raised.exception.code, 2)
        return errors.getvalue()

    def test_goal_reads_a_long_objective_from_a_file(self):
        path, text = self.long_objective()
        prompt = self.cli("goal", self.template(), "--objective-file", path, "--prompt-only")["prompt"]
        self.assertIn(text, prompt)
        self.assertGreater(len(prompt), 32767)

    def test_goal_launch_stores_the_file_objective(self):
        path, text = self.long_objective()
        planned = self.root / "planned.json"
        planned.write_text(json.dumps(plan("Deliver")), encoding="utf-8")
        result = self.cli("goal", self.template(), "--objective-file", path, "--plan-from", planned, "--enqueue")
        self.assertEqual(self.store.get(result["run_id"])["spec"]["objective"], text)

    def test_oneshot_reads_the_objective_from_a_file(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_it(self): pass\n", encoding="utf-8")
        git(self.repo, "add", "tests/test_a.py")
        git(self.repo, "commit", "-m", "Declare a check")
        path, text = self.long_objective()
        raw = self.cli("oneshot", self.repo, "--objective-file", path, "--preview", "--worker", "codex")
        self.assertEqual(raw["objective"], text.strip())
        self.assertEqual(self.cli("oneshot", self.repo, "Short goal", "--preview", "--worker", "codex")["objective"], "Short goal")

    def test_the_file_and_the_positional_objective_exclude_each_other(self):
        path, _ = self.long_objective()
        self.assertIn("cannot be combined", self.refused("goal", self.template(), "Goal", "--objective-file", path, "--prompt-only"))
        self.assertIn("cannot be combined", self.refused("oneshot", self.repo, "Goal", "--objective-file", path, "--preview"))
        self.assertIn("--objective-file", self.refused("goal", self.template(), "--prompt-only"))
        self.assertIn("--objective-file", self.refused("oneshot", self.repo, "--preview"))

    def test_a_missing_empty_or_non_utf8_file_is_a_clear_error(self):
        empty, binary = self.root / "empty.txt", self.root / "binary.txt"
        empty.write_text(" \r\n", encoding="utf-8")
        binary.write_bytes(b"\xff\xfe\x00bad")
        for path, message in ((self.root / "absent.txt", "Cannot read the objective file"), (empty, "is empty"), (binary, "must be UTF-8")):
            with self.subTest(path.name):
                self.assertIn(message, self.cli("goal", self.template(), "--objective-file", path, "--prompt-only", code=1))


@contextmanager
def no_identity(repo, root, **env):
    """Git resolves no identity for `repo`: none configured in it, none global or system-wide, and no guess from the user and host names."""
    saved = {}
    for key in ("user.name", "user.email"):
        try:
            saved[key] = git(repo, "config", "--get", key)
            git(repo, "config", "--unset", key)
        except ValueError:
            pass
    config = root / "no-identity.gitconfig"
    config.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
    try:
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", **env}):
            for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
                if name not in env:
                    os.environ.pop(name, None)
            yield
    finally:
        for key, value in saved.items():  # the folder is shared with the rest of the test
            git(repo, "config", key, value)


def author_of(repo, commit):
    return git(repo, "show", "-s", "--format=%an <%ae>|%cn <%ce>", commit)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class IdentityTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured
    cli = explore_tests.ExploreTests.cli
    script = explore_tests.ExploreTests.script
    sequence = explore_tests.ExploreTests.sequence
    table = explore_tests.ExploreTests.table
    builder = explore_tests.ExploreTests.builder
    explored = explore_tests.ExploreTests.explored

    def plan_file(self):
        path = self.root / "plan.json"
        path.write_text(json.dumps({"repository": str(self.repo), "title": "Plan", "objective": "Do it", "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                                    "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "M", "task": "T"}]}), encoding="utf-8")
        return path

    def template(self):
        path = self.root / "template.json"
        path.write_text(json.dumps({"repository": str(self.repo), "title": "Template", "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                                    "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}]}), encoding="utf-8")
        return path

    def fix(self, repo=None):
        repo = repo or self.repo.resolve()  # the commands resolve the folder; the temporary folder's own name may be an 8.3 short one
        return [f'git -C "{repo}" config user.name', f'git -C "{repo}" config user.email']

    def test_the_message_names_the_exact_fix(self):
        self.assertIsNone(identity_error(self.repo))
        require_identity(self.repo)
        with no_identity(self.repo, self.root):
            message = identity_error(self.repo)
            for command in self.fix(self.repo):
                self.assertIn(command, message)
            self.assertIn("author or committer identity", message)
            with self.assertRaisesRegex(ValueError, "config user.email"):
                require_identity(self.repo)
        self.assertIn("git -C plain config user.name", identity_message("plain", {"author": "Author identity unknown"}))
        self.assertIsNone(identity_error(self.root / "not a repository"))

    def test_launching_commands_stop_before_any_model_call(self):
        planned = self.root / "planned.json"
        planned.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}), encoding="utf-8")
        plan = self.plan_file()
        template = self.template()
        launches = (("goal", template, "Goal", "--enqueue"), ("goal", template, "Goal", "--start"), ("add", plan, "--enqueue"),
                    ("oneshot", self.repo, "Goal", "--direct", "--enqueue"), ("oneshot", self.repo, "Goal", "--wait"))
        with no_identity(self.repo, self.root), patch("nightweaver.cli.invoke", side_effect=AssertionError("a model was called")) as invoked, \
                patch("nightweaver.cli.background", side_effect=AssertionError("a runner was started")):
            for launch in launches:
                with self.subTest(launch[0] + " " + str(launch[-1])):
                    message = self.cli(*launch, code=1)
                    for command in self.fix():
                        self.assertIn(command, message)
            wish = patch("nightweaver.wish.run_wish", side_effect=AssertionError("the brief writer ran"))
            with wish:
                message = self.cli("wish", self.repo, "Make it", "--start", code=1)
            self.assertIn(self.fix()[0], message)
            # Nothing that does not launch is refused.
            self.cli("goal", template, "Goal", "--prompt-only")
            invoked.assert_not_called()
        self.assertEqual(self.store.runs(), [])
        # With an identity the same commands go through.
        result = self.cli("goal", template, "Goal", "--plan-from", planned, "--enqueue")
        self.assertEqual(self.store.get(result["run_id"])["state"]["phase"], "queued")

    def test_doctor_and_preflight_report_the_missing_identity(self):
        plan = self.plan_file()
        with no_identity(self.repo, self.root):
            report = self.cli("doctor", "--plan", plan, code=3)
            self.assertEqual(report["identity"]["status"], "blocked")
            for command in self.fix():
                self.assertIn(command, report["identity"]["action"])
            report = self.cli("preflight", plan, code=3)
            self.assertFalse(report["ready"])
            identity = [error for error in report["errors"] if "identity" in error]
            self.assertEqual(len(identity), 2)
            for error in identity:
                for command in self.fix():
                    self.assertIn(command, error)
        self.assertEqual(self.cli("doctor", "--plan", plan)["identity"], {"status": "ready"})
        report = self.cli("preflight", plan)
        self.assertEqual([error for error in report["errors"] if "identity" in error], [])

    def test_commit_tree_uses_the_fallback_only_for_the_side_git_cannot_fill(self):
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        configured = commit_tree(self.repo, tree, self.head, "Configured")
        self.assertEqual(author_of(self.repo, configured), "Nightweaver Fixture <fixture@example.invalid>|Nightweaver Fixture <fixture@example.invalid>")
        name, email = FALLBACK_IDENTITY
        with no_identity(self.repo, self.root):
            commit = commit_tree(self.repo, tree, self.head, "Fallback")
            self.assertEqual(author_of(self.repo, commit), f"{name} <{email}>|{name} <{email}>")
        with no_identity(self.repo, self.root, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.invalid"):
            mixed = commit_tree(self.repo, tree, self.head, "Mixed")
            self.assertEqual(author_of(self.repo, mixed), f"Ada <ada@example.invalid>|{name} <{email}>")
        self.assertEqual((name, email), ("Nightweaver", "nightweaver@localhost"))

    def test_a_run_in_a_repository_without_an_identity_is_accepted_and_says_so(self):
        run = self.configured(fixtures.fixture("good"), reviewer={"kind": "command", "argv": fixtures.fixture("review")})
        with no_identity(self.repo, self.root):
            self.assertEqual(Run(self.store, run).run(), "done")
        (accepted,) = self.store.events(run, limit=None, kinds=["milestone_accepted"])
        self.assertEqual(accepted["details"]["identity_fallback"], {"name": "Nightweaver", "email": "nightweaver@localhost", "roles": ["author", "committer"]})
        self.assertEqual(author_of(self.repo, accepted["details"]["commit"]), "Nightweaver <nightweaver@localhost>|Nightweaver <nightweaver@localhost>")
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), accepted["details"]["commit"])

    def test_a_run_with_an_identity_records_no_fallback(self):
        run = self.configured(fixtures.fixture("good"))
        self.assertEqual(Run(self.store, run).run(), "done")
        (accepted,) = self.store.events(run, limit=None, kinds=["milestone_accepted"])
        self.assertNotIn("identity_fallback", accepted["details"])
        self.assertEqual(author_of(self.repo, accepted["details"]["commit"]), "Nightweaver Fixture <fixture@example.invalid>|Nightweaver Fixture <fixture@example.invalid>")

    def test_explored_directions_are_committed_with_the_fallback_too(self):
        run = self.explored()
        with no_identity(self.repo, self.root):
            self.assertEqual(Run(self.store, run).run(), "done")
        built = [e["details"] for e in self.store.events(run, limit=None, kinds=["direction_built"])]
        self.assertEqual(len(built), 3)
        for item in built:
            self.assertEqual(author_of(self.repo, item["commit"]), "Nightweaver <nightweaver@localhost>|Nightweaver <nightweaver@localhost>")
        first, second = [e["details"] for e in self.store.events(run, limit=None, kinds=["milestone_accepted"])]
        self.assertEqual(first["identity_fallback"]["roles"], ["author", "committer"])
        self.assertIn("identity_fallback", second)


if __name__ == "__main__":
    unittest.main()
