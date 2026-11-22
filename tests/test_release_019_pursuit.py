"""Goal pursuit: an outer loop that judges the build, plans more waves and keeps the best result."""

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
from nightweaver.engine import Run, git
from nightweaver.providers import JUDGE_SCHEMA, adapter, invoke, metadata
from nightweaver.reports import metrics
from nightweaver.spec import validate

HELPER = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a7d1d8c10000000049454e44ae426082")


def helper(mode, *args):
    return [sys.executable, HELPER, mode, *map(str, args)]


def judged(score, met=False, gaps=None, **extra):
    gaps = [] if met else [{"title": f"Gap at {score}", "detail": "Detail " + str(score)}] if gaps is None else gaps
    return {"verdict": "MET" if met else "NOT_MET", "score": score, "reason": f"Reason {score}", "gaps": gaps,
            "criteria": [{"criterion": "Looks right", "met": met, "note": "Observed"}], **extra}


def plan(*titles):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles]}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class PursuitTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def cli(self, *args, code=0, raw=False):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return stdout.getvalue() if raw else json.loads(stdout.getvalue()) if stdout.getvalue() else stderr.getvalue()

    def script(self, name, items):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return path

    def route(self, name, items):
        return {"kind": "command", "argv": helper("script", self.script(name, items))}

    def prompts(self, name):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def pursue(self, judge, planner=None, extra_pursue=None, **extra):
        self.worker_log = self.root / "worker.prompts"
        block = {"judge": self.route("judge", judge), "planner": self.route("planner", planner or [plan("Next improvement")]),
                 **(extra_pursue or {})}
        return self.configured(helper("build", self.worker_log), pursue=block, **extra)

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def lineage(self, ref):
        return git(self.repo, "show", f"{ref}:lineage.txt").splitlines()

    def test_not_met_then_planned_wave_then_met(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("Polish layout")],
                          reviewer={"kind": "command", "argv": fixtures.fixture("review")})
        self.assertEqual(Run(self.store, run).run(), "done")
        row = self.store.get(run)
        self.assertEqual([m["title"] for m in row["spec"]["milestones"]], ["Prepare fixture", "Polish layout"])
        self.assertEqual(row["spec"]["milestones"][1]["gates"], row["spec"]["milestones"][0]["gates"])
        kinds = [e["kind"] for e in self.store.events(run, limit=None)]
        for kind in ("goal_judged", "wave_planned", "goal_met", "run_completed", "wave_completed"):
            self.assertIn(kind, kinds)
        self.assertEqual((kinds.count("goal_judged"), kinds.count("wave_completed"), kinds.count("run_completed")), (2, 2, 1))
        self.assertLess(kinds.index("goal_met"), kinds.index("run_completed"))
        first, second = self.events(run, "goal_judged")
        self.assertEqual((first["verdict"], first["score"], first["wave"]), ("NOT_MET", 60, 1))
        self.assertEqual(first["gaps"][0]["title"], "Gap at 60")
        self.assertEqual(first["ledger"], [{"name": "read fixture value", "passed": True}])
        self.assertEqual((second["verdict"], second["wave"]), ("MET", 2))
        planned = self.events(run, "wave_planned")[0]
        self.assertEqual((planned["wave"], planned["titles"], planned["first_step"]), (2, ["Polish layout"], 2))
        state = row["state"]
        self.assertEqual((state["phase"], state["wave"], state["step"], state["best_score"]), ("done", 2, 2, 90))
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Polish layout"])
        self.assertEqual(self.store.events(run, limit=None, kinds=["run_completed"])[0]["details"]["commit"], state["accepted"])
        evidence = self.store.directory(run) / "evidence" / "judge"
        self.assertTrue((evidence / "01" / "judgment.json").is_file())
        self.assertFalse(list((self.store.directory(run) / "worktrees").glob("judge-*")))
        counts = {call["role"]: 0 for call in self.events(run, "provider_call_finished")}
        for call in self.events(run, "provider_call_finished"):
            counts[call["role"]] += 1
        self.assertEqual(counts, {"worker": 2, "reviewer": 2, "judge": 2, "planner": 1})
        self.assertEqual(state["provider_calls"], 7)
        # Prompts: the judge sees the goal, ledger and rubric; the planner sees the gaps; the next worker sees the pursuit.
        judge_prompt = self.prompts("judge")[0]
        for text in ("Produce a fixture whose independent check passes", "PASS read fixture value", "Prepare fixture", "do not reward claims"):
            self.assertIn(text, judge_prompt)
        self.assertIn("Wave 1: 60/100 NOT_MET", self.prompts("judge")[1])
        planner_prompt = self.prompts("planner")[0]
        for text in ("Gap at 60", "read fixture value", "1..3 milestones", "Do not plan work that is already done"):
            self.assertIn(text, planner_prompt)
        second_worker = self.worker_log.read_text(encoding="utf-8").split("\n=====\n")[1]
        self.assertIn("Pursuit\nThis run keeps iterating", second_worker)
        self.assertIn("Gap at 60", second_worker)
        for review in (self.store.directory(run) / "evidence").rglob("review-*.input.txt"):
            self.assertNotIn("This is the final milestone", review.read_text(encoding="utf-8"))
        self.assertEqual(metrics(self.store, run)["outcome"], "accepted")
        self.assertEqual(metrics(self.store, run)["pursuit"]["waves"], 2)

    def test_plateau_stops_on_the_best_build(self):
        # The plateau rescue is on by default; with max_rescues 0 a plateau stops the pursuit exactly as in 0.19.
        run = self.pursue([judged(60), judged(62), judged(61), judged(60)], [plan("Round")], extra_pursue={"max_waves": 10, "rescue": {"max_rescues": 0}})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        judgments = self.events(run, "goal_judged")
        self.assertEqual([j["score"] for j in judgments], [60, 62, 61, 60])
        self.assertEqual(state["best_commit"], judgments[1]["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), judgments[1]["commit"])
        self.assertEqual((state["blocked_from"], state["stale_judgments"], state["accepted"]), ("judging", 2, judgments[1]["commit"]))
        self.assertTrue(state["last_error"].startswith("Goal not met after 4 waves (best score 62/100). Top gaps: Gap at 62"), state["last_error"])
        stopped = self.events(run, "pursuit_stopped")[0]
        self.assertEqual((stopped["reason"], stopped["best_score"], stopped["waves"]), ("plateau", 62, 4))
        reverted = self.events(run, "pursuit_reverted")[0]
        self.assertEqual((reverted["from"], reverted["to"]), (judgments[3]["commit"], judgments[1]["commit"]))
        self.assertEqual(git(self.repo, "rev-parse", reverted["discarded_ref"]), judgments[3]["commit"])
        self.assertNotIn("run_completed", [e["kind"] for e in self.store.events(run, limit=None)])
        self.assertFalse(list((self.store.directory(run) / "worktrees").glob("judge-*")))
        measured = metrics(self.store, run)
        self.assertEqual((measured["outcome"], measured["pursuit"]["stop_reason"], measured["block_reason"]), ("pursuit_stopped", "plateau", state["last_error"]))
        # Operator views accept a stopped pursuit.
        self.assertEqual(self.cli("status", run)["state"]["phase"], "blocked")
        self.assertEqual(self.cli("list", "--compact")[0]["state"]["best_score"], 62)
        self.assertEqual(self.cli("handoff", run)["pursuit"]["best_score"], 62)
        self.assertEqual(self.cli("attention")[0]["reason"], "blocked")
        brief = self.root / "handoff.md"
        self.cli("handoff", run, "--markdown", brief)
        self.assertIn("## Goal pursuit", brief.read_text(encoding="utf-8"))
        self.assertIn("pursuit_stopped", self.cli("diagnose-run", run)["last_failure_event"]["kind"])
        report = self.cli("report", run)
        self.assertIn("Goal pursuit", Path(report["report"]).read_text(encoding="utf-8"))
        text = self.cli("pursuit", run, raw=True)
        for expected in ("Wave 4", "Judged NOT_MET 60/100", "Stopped: plateau", "Reverted to"):
            self.assertIn(expected, text)
        data = self.cli("pursuit", run, "--json")
        self.assertEqual([w["judgment"]["score"] for w in data["waves"]], [60, 62, 61, 60])
        self.assertEqual((data["best_score"], data["stopped"]["reason"], data["met"]), (62, "plateau", False))
        self.assertEqual([len(w["milestones"]) for w in data["waves"]], [1, 1, 1, 1])
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")

    def test_regression_discards_the_wave_and_the_next_builds_on_the_best(self):
        run = self.pursue([judged(70), judged(50), judged(90, met=True)], [plan("Wave two work"), plan("Wave three work")])
        self.assertEqual(Run(self.store, run).run(), "done")
        reverted = self.events(run, "pursuit_reverted")
        self.assertEqual(len(reverted), 1)
        first, second, third = self.events(run, "goal_judged")
        self.assertEqual((reverted[0]["wave"], reverted[0]["from"], reverted[0]["to"]), (2, second["commit"], first["commit"]))
        self.assertEqual(reverted[0]["discarded_ref"], f"refs/heads/nightweaver/{run}-discarded-w02")
        self.assertEqual(self.lineage(reverted[0]["discarded_ref"]), ["Prepare fixture", "Wave two work"])
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Wave three work"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), third["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"{third['commit']}^"), first["commit"])
        note = self.prompts("planner")[1]
        self.assertIn("discarded", note)
        self.assertIn("Gap at 70", note)
        self.assertNotIn("Wave two work (", note)
        third_worker = self.worker_log.read_text(encoding="utf-8").split("\n=====\n")[2]
        self.assertIn("- Prepare fixture (", third_worker)
        self.assertNotIn("Wave two work (", third_worker)
        self.assertIn("Wave three work", self.prompts("judge")[2])
        self.assertNotIn("Wave two work", self.prompts("judge")[2])

    def test_wave_limit_stops(self):
        run = self.pursue([judged(60), judged(65)], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        stopped = self.events(run, "pursuit_stopped")[0]
        self.assertEqual((stopped["reason"], stopped["best_score"], stopped["waves"]), ("wave_limit", 65, 2))
        self.assertEqual(self.events(run, "pursuit_reverted"), [])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), state["best_commit"])
        self.assertEqual(len(self.store.get(run)["spec"]["milestones"]), 2)
        self.assertIn("Goal not met after 2 waves (best score 65/100)", state["last_error"])

    def test_restart_while_judging_redoes_the_judgment(self):
        run = self.pursue([judged(90, met=True)])
        with patch("nightweaver.pursuit.judge", side_effect=SystemExit), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["step"], state["wave"]), ("judging", 1, 1))
        self.assertEqual(self.events(run, "wave_completed")[0]["wave"], 1)
        self.store.transition(run, "provider_call_started", {"call": 9}, pending_call={
            "call": 9, "role": "judge", "provider": "command", "model": None, "started": state["started_at"],
            "log": str(self.root / "missing-judge.jsonl"), "step": 1, "attempt": 0}, provider_calls=9)
        for command in (("status", run), ("list", "--compact"), ("metrics", run), ("handoff", run), ("budget", run), ("timeline", run),
                        ("diagnose-run", run), ("recovery", run), ("fleet",), ("call-ledger", run), ("attention",)):
            self.cli(*command)
        self.assertEqual(self.cli("list", "--phase", "judging")[0]["id"], run)
        before = self.cli("pursuit", run, raw=True)
        self.assertIn("Phase judging", before)
        self.assertIn("Wave 1 (initial plan)", before)
        self.assertIsNone(self.cli("pursuit", run, "--json")["waves"][0]["judgment"])
        self.assertEqual(self.cli("metrics", run)["phase"], "judging")
        self.assertTrue(Path(self.cli("report", run)["report"]).is_file())
        self.assertEqual(Run(self.store, run).run(), "done")
        recovered = [c for c in self.events(run, "provider_call_finished") if c.get("recovered")]
        self.assertEqual([c["role"] for c in recovered], ["judge"])
        self.assertEqual(self.store.get(run)["state"]["pending_call"], None)
        self.assertEqual(self.events(run, "goal_met")[0]["score"], 90)

    def test_restart_while_planning_replans(self):
        run = self.pursue([judged(60), judged(90, met=True)], [plan("Second")])
        with patch("nightweaver.pursuit.plan_wave", side_effect=SystemExit), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        self.assertEqual(self.store.get(run)["state"]["phase"], "planning")
        self.assertTrue(list((self.store.directory(run) / "worktrees").glob("judge-*")))
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.store.get(run)["spec"]["milestones"]), 2)

    def test_failed_ledger_check_blocks_met_even_when_the_judge_says_met(self):
        gates = [{"name": "ready", "argv": fixtures.fixture("gate"), "timeout": 20},
                 {"name": "finished build keeps two milestones", "argv": helper("never_judge", 2), "timeout": 20}]
        run = self.pursue([judged(95, met=True)], [plan("Second milestone")], gates=gates)
        self.assertEqual(Run(self.store, run).run(), "done")
        first, second = self.events(run, "goal_judged")
        self.assertEqual((first["verdict"], first["score"], second["wave"]), ("MET", 95, 2))
        self.assertEqual([(c["name"], c["passed"]) for c in first["ledger"]],
                         [("ready", True), ("finished build keeps two milestones", False)])
        self.assertEqual(len(self.events(run, "goal_met")), 1)
        self.assertEqual(self.events(run, "goal_met")[0]["wave"], 2)
        planner = self.prompts("planner")[0]
        self.assertIn("Check failed: finished build keeps two milestones", planner)
        self.assertIn("finished build keeps two milestones", self.prompts("judge")[0])
        self.assertIn("FAIL finished build keeps two milestones", self.prompts("judge")[0])
        self.assertEqual(len(self.events(run, "judge_gate_finished")), 4)

    def test_unscorable_ledger_check_counts_as_a_failure_and_does_not_crash(self):
        gates = [{"name": "ready", "argv": fixtures.fixture("gate"), "timeout": 20},
                 {"name": "receipt", "argv": helper("artifact_unless_judge"), "timeout": 20, "artifacts": ["receipt.txt"]}]
        run = self.pursue([judged(95, met=True)], gates=gates, extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        first, second = self.events(run, "goal_judged")
        self.assertEqual([c["passed"] for c in first["ledger"]], [True, False])
        self.assertIn("receipt.txt", first["ledger"][1]["note"])
        unscored = [e for e in self.events(run, "judge_gate_finished") if e["name"] == "receipt"]
        self.assertTrue(all(e["exit"] == 0 and e["artifact_error"] for e in unscored))
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "wave_limit")
        self.assertEqual(self.events(run, "goal_met"), [])

    def test_screenshots_and_references_reach_the_judge(self):
        reference = self.root / "refs" / "target.png"
        reference.parent.mkdir()
        reference.write_bytes(PNG)
        gates = [{"name": "ready", "argv": fixtures.fixture("gate"), "timeout": 20},
                 {"name": "screenshot", "argv": helper("shot"), "timeout": 20, "artifacts": []}]
        run = self.pursue([judged(90, met=True)], gates=gates, extra_pursue={"references": [str(reference)]})
        self.assertEqual(Run(self.store, run).run(), "done")
        judgment = self.events(run, "goal_judged")[0]
        self.assertEqual(len(judgment["screenshots"]), 1)
        self.assertTrue(judgment["screenshots"][0].endswith("screen.png"))
        self.assertTrue(Path(judgment["screenshots"][0]).is_relative_to(self.store.directory(run) / "evidence" / "judge" / "01"))
        prompt = self.prompts("judge")[0]
        self.assertIn(judgment["screenshots"][0], prompt)
        self.assertIn(str(reference.resolve()), prompt)
        self.assertIn("OPEN each image file", prompt)

    def test_invalid_judge_output_retries_once_then_blocks(self):
        run = self.pursue(["this is not json", {"verdict": "MET", "score": 101, "gaps": []}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        invalid = self.events(run, "judge_invalid")
        self.assertEqual([e["attempt"] for e in invalid], [1, 2])
        self.assertEqual((state["phase"], state["blocked_from"], state["judge_attempts"]), ("blocked", "judging", 2))
        self.assertIn("no valid judgment", state["last_error"])
        self.assertIn("score must be an integer", state["last_error"])
        self.assertEqual(self.events(run, "goal_judged"), [])
        self.assertEqual(metrics(self.store, run)["outcome"], "execution_blocked")
        self.assertEqual(len(self.events(run, "judge_gate_finished")), 1, "the retry reuses the ledger")
        self.assertIn("Your previous answer was rejected", self.prompts("judge")[1])
        # A retry grants fresh attempts at the same judgment.
        self.script("judge", ["bad", "bad", judged(90, met=True)])
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(self.store.get(run)["state"]["judge_attempts"], 0)

    def test_one_invalid_judge_answer_is_retried(self):
        run = self.pursue(["{\"verdict\": \"MAYBE\", \"score\": 50}", judged(90, met=True)])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.events(run, "judge_invalid")), 1)
        self.assertEqual(len(self.events(run, "goal_judged")), 1)

    def test_invalid_plan_retries_once_then_blocks_in_planning(self):
        run = self.pursue([judged(60), judged(90, met=True)], ["not a plan", {"milestones": []}])
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], state["plan_attempts"]), ("planning", 2))
        self.assertEqual(len(self.events(run, "plan_invalid")), 2)
        self.assertEqual(len(self.store.get(run)["spec"]["milestones"]), 1)
        self.script("planner", ["x", "x", {"milestones": [{"title": "Fixed", "task": "Do it", "extra": 1}]}, plan("Fixed")])
        self.root.joinpath("planner.pos").write_text("2")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(self.lineage(f"nightweaver/{run}"), ["Prepare fixture", "Fixed"])

    def test_add_waves_resumes_a_stopped_run(self):
        run = self.pursue([judged(60), judged(65), judged(90, met=True)], [plan("More")], extra_pursue={"max_waves": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.store.get(run)["state"]["stale_judgments"], 0)
        result = self.cli("pursuit", run, "--add-waves", 2, "--json")
        self.assertEqual(result["extension"], {"max_waves": 3, "resumed": True, "phase": "judging"})
        self.assertEqual(result["max_waves"], 3)
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["last_error"]), ("judging", None, ""))
        self.assertEqual(self.events(run, "pursuit_extended")[0]["waves"], 2)
        self.assertEqual(self.cli("run", run)["phase"], "done")
        self.assertEqual(self.store.get(run)["spec"]["pursue"]["max_waves"], 3)
        text = self.cli("pursuit", run, raw=True)
        self.assertIn("Goal met.", text)
        self.cli("pursuit", run, "--add-waves", 1, code=1)
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            self.store.extend_pursuit(run, 21)

    def test_add_waves_on_a_run_without_pursuit_is_refused(self):
        run = self.configured(fixtures.fixture("good"))
        self.cli("pursuit", run, "--add-waves", 1, code=1)
        self.cli("pursuit", run, code=1)

    def test_judge_provider_fallback_and_budget(self):
        from nightweaver.providers import ProviderUnavailable
        calls = []
        real = __import__("nightweaver.engine", fromlist=["invoke"]).invoke

        def flaky(config, mode, *args, **kwargs):
            calls.append((config["kind"], mode))
            if mode == "judge" and config["kind"] == "claude":
                raise ProviderUnavailable("signed out", "authentication")
            return real(config, mode, *args, **kwargs)

        run = self.pursue([judged(90, met=True)])
        row = self.store.get(run)
        spec = row["spec"]
        spec["pursue"]["judge_fallbacks"] = [spec["pursue"]["judge"]]
        spec["pursue"]["judge"] = {"kind": "claude"}
        with self.store.connect() as db:
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertIn(("claude", "judge"), calls)
        self.assertEqual(self.events(run, "provider_fallback")[0]["from"]["role"], "judge")
        self.assertEqual(self.store.get(run)["state"]["judge_route"], 0, "the route resets when the judgment is recorded")
        # Judge calls count against the call budget.
        run = self.pursue([judged(90, met=True)], budgets={"max_calls": 1})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], state["provider_calls"]), ("judging", 1))
        self.assertIn("provider-call budget", state["last_error"])
        self.assertEqual(metrics(self.store, run)["outcome"], "budget_exhausted")


class PursuitSpecTests(unittest.TestCase):
    def raw(self, **pursue):
        return {"repository": str(Path(__file__).resolve().parents[1]), "title": "T", "objective": "O", "worker": {"kind": "command", "argv": ["x"]},
                "gates": [{"name": "g", "argv": ["x"]}], "milestones": [{"title": "M", "task": "T"}], "acceptance": ["Beautiful"],
                "pursue": {"judge": {"kind": "codex"}, **pursue}}

    def test_defaults_are_filled(self):
        pursue = validate(self.raw())["pursue"]
        self.assertEqual(pursue, {"judge": {"kind": "codex"}, "judge_fallbacks": [], "planner": {"kind": "codex"}, "max_waves": 3, "wave_size": 3,
                                  "target_score": 85, "plateau": 2, "regression_margin": 5, "references": [], "criteria": ["Beautiful"],
                                  "rescue": {"strategies": 3, "max_rescues": 1}})
        self.assertIsNot(pursue["planner"], pursue["judge"])
        full = validate(self.raw(planner={"kind": "claude", "effort": "high"}, criteria=["Fast"], max_waves=20, wave_size=5, target_score=0,
                                 plateau=5, regression_margin=50, judge_fallbacks=[{"kind": "gemini"}]))["pursue"]
        self.assertEqual((full["planner"], full["criteria"], full["max_waves"], full["judge_fallbacks"]),
                         ({"kind": "claude", "effort": "high"}, ["Fast"], 20, [{"kind": "gemini"}]))

    def test_invalid_blocks_are_rejected(self):
        folder = Path(__file__).resolve().parent
        cases = {
            "no judge": ({}, "requires a judge"), "unknown": ({"typo": 1}, "Unknown pursue fields"),
            "kind": ({"judge": {"kind": "ollama"}}, "Invalid judge adapter"),
            "waves low": ({"max_waves": 0}, "max_waves must be an integer between 1 and 20"),
            "waves high": ({"max_waves": 21}, "max_waves"), "waves type": ({"max_waves": True}, "max_waves"),
            "size": ({"wave_size": 6}, "wave_size"), "target": ({"target_score": 101}, "target_score"), "plateau": ({"plateau": 0}, "plateau"),
            "margin": ({"regression_margin": 51}, "regression_margin"), "criteria": ({"criteria": ["ok", " "]}, "criteria"),
            "criteria type": ({"criteria": "x"}, "criteria"),
            "fallbacks": ({"judge_fallbacks": [{"kind": "codex"}] * 6}, "at most 5"),
            "planner": ({"planner": {"kind": "codex", "effort": "huge"}}, "Unsupported effort"),
            "missing image": ({"references": ["missing.png"]}, "not found"),
            "image type": ({"references": [str(Path(__file__))]}, "must be .png"),
            "too many": ({"references": [str(folder / "pursuit_fixture.py")] * 7}, "at most 6"),
            "not text": ({"references": [3]}, "paths of image files"),
        }
        for label, (pursue, message) in cases.items():
            raw = self.raw(**pursue)
            if label == "no judge":
                raw["pursue"] = {}
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                validate(raw)
        with self.assertRaisesRegex(ValueError, "pursue must be an object"):
            validate({**self.raw(), "pursue": []})

    def test_reference_images_resolve_against_the_plan_folder(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "look").mkdir()
            (folder / "look" / "A.PNG").write_bytes(PNG)
            (folder / "b.webp").write_bytes(PNG)
            pursue = validate(self.raw(references=["look/A.PNG", "b.webp", "look/../look/A.PNG"]), folder)["pursue"]
            self.assertEqual(pursue["references"], [str((folder / "look" / "A.PNG").resolve()), str((folder / "b.webp").resolve())])


class PursuitProviderTests(unittest.TestCase):
    def test_codex_judge_attaches_every_image_before_the_stdin_marker(self):
        argv, _ = adapter({"kind": "codex", "effort": "high", "model": "gpt-x"}, "judge", images=["a.png", "b dir/b.jpg"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual([argv[i + 1] for i, v in enumerate(argv) if v == "--image"], ["a.png", "b dir/b.jpg"])
        self.assertEqual(argv[-1], "-")
        last = max(i for i, v in enumerate(argv) if v == "--image")
        self.assertTrue(argv[last + 2].startswith("--"), "a flag must end the greedy --image list")
        for mode in ("worker", "reviewer"):
            self.assertNotIn("--image", adapter({"kind": "codex"}, mode)[0])
        self.assertEqual(adapter({"kind": "codex"}, "replanner")[0][2], "read-only")

    def test_claude_judge_is_read_only_with_the_judge_schema(self):
        for kind in ("claude", "deepseek"):
            with patch("nightweaver.providers.deepseek_key", return_value="k"), patch("nightweaver.providers.default_home", return_value=Path(os.environ["TEMP"])):
                argv, _ = adapter({"kind": kind, "profile_dir": os.environ["TEMP"]}, "judge")
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
            self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), JUDGE_SCHEMA)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        planner, _ = adapter({"kind": "claude"}, "replanner")
        self.assertEqual(planner[planner.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("milestones", planner[planner.index("--json-schema") + 1])
        legacy, _ = adapter({"kind": "claude"}, "planner")
        self.assertEqual(legacy[legacy.index("--tools") + 1], "")
        self.assertNotIn("--json-schema", legacy)
        self.assertIn("plan", adapter({"kind": "gemini"}, "judge")[0])
        reviewer, _ = adapter({"kind": "claude"}, "reviewer")
        self.assertEqual(json.loads(reviewer[reviewer.index("--json-schema") + 1])["required"], ["verdict", "reason"])

    def test_structured_judge_and_plan_answers_are_captured_from_claude_logs(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            log = Path(directory) / "judge.jsonl"
            verdict = judged(70)
            log.write_text(json.dumps({"type": "system", "session_id": "s1"}) + "\n" + json.dumps(
                {"type": "result", "result": "", "structured_output": verdict}) + "\n", encoding="utf-8")
            for mode in ("judge", "replanner", "reviewer"):
                session, answer = metadata(log, "claude", mode)
                self.assertEqual((session, json.loads(answer)), ("s1", verdict))
            self.assertEqual(metadata(log, "claude", "worker")[1], "")

    def test_invoke_exposes_evidence_and_reference_folders(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, refs = root / "evidence", root / "refs"
            shot, ref = evidence / "ledger" / "01" / "s.png", refs / "t.png"
            for path in (shot, ref):
                path.parent.mkdir(parents=True)
                path.write_bytes(b"x")
            for kind, flag in (("claude", "--add-dir"), ("deepseek", "--add-dir"), ("gemini", "--include-directories"), ("codex", None)):
                with (patch("nightweaver.providers.check_provider", return_value=kind), patch("nightweaver.providers.execute", return_value=0) as run,
                      patch("nightweaver.providers.deepseek_key", return_value="k"), patch("nightweaver.providers.default_home", return_value=root)):
                    invoke({"kind": kind}, "judge", root, root / "log.jsonl", "p", 5, lambda: False, evidence=evidence, images=[shot, ref, ref])
                argv = run.call_args.args[0]
                if flag:
                    self.assertEqual([argv[i + 1] for i, v in enumerate(argv) if v == flag], [str(evidence), str(refs)])
                else:
                    self.assertEqual([argv[i + 1] for i, v in enumerate(argv) if v == "--image"], [str(shot), str(ref), str(ref)])
                    self.assertEqual(argv[-1], "-")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class PursuitCliTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = PursuitTests.cli

    def commit_tests(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_it(self): pass\n", encoding="utf-8")
        git(self.repo, "add", "tests/test_a.py")
        git(self.repo, "commit", "-m", "Declare a check")

    def test_oneshot_pursuit_flags_build_the_spec(self):
        self.commit_tests()
        reference = self.root / "target.jpg"
        reference.write_bytes(PNG)
        raw = self.cli("oneshot", self.repo, "Build a beautiful page", "--preview", "--worker", "codex", "--pursue", "--reference", reference,
                       "--waves", "4", "--target-score", "90", "--reviewer", "codex", "--reviewer-model", "gpt-x", "--planner-kind", "claude",
                       "--planner-effort", "high", "--judge-profile", self.root)
        self.assertEqual(raw["pursue"]["judge"], {"kind": "claude", "profile_dir": str(self.root.resolve())})
        self.assertEqual((raw["pursue"]["planner"], raw["pursue"]["max_waves"], raw["pursue"]["target_score"]),
                         ({"kind": "claude", "effort": "high"}, 4, 90))
        self.assertEqual(raw["pursue"]["references"], [str(reference.resolve())])
        self.assertEqual(raw["reviewer"], {"kind": "codex", "model": "gpt-x"})
        self.assertEqual(raw["budgets"], {"max_calls": 80, "max_seconds": 21600})
        plain = self.cli("oneshot", self.repo, "Build", "--preview", "--worker", "claude", "--pursue")
        self.assertEqual(plain["pursue"], {"judge": {"kind": "codex"}})
        self.assertNotIn("pursue", self.cli("oneshot", self.repo, "Build", "--preview"))
        self.assertEqual(self.cli("oneshot", self.repo, "Build", "--preview")["budgets"], {"max_calls": 15, "max_seconds": 3600})
        stored = self.cli("oneshot", self.repo, "Build", "--direct", "--enqueue", "--pursue", "--judge", "gemini", "--waves", "2")
        from nightweaver.store import Store
        spec = Store(self.store.root).get(stored["run_id"])["spec"]
        self.assertEqual((spec["pursue"]["judge"], spec["pursue"]["max_waves"], spec["pursue"]["wave_size"]), ({"kind": "gemini"}, 2, 3))
        self.assertEqual(spec["budgets"]["max_calls"], 80)

    def test_oneshot_pursuit_flag_misuse_is_refused(self):
        self.commit_tests()
        for flags in (["--judge", "codex"], ["--waves", "2"], ["--reference", "x.png"], ["--target-score", "80"],
                      ["--pursue", "--planner-model", "m"], ["--reviewer-model", "m"]):
            with self.subTest(flags), self.assertRaises(SystemExit):
                with redirect_stderr(StringIO()):
                    self.cli("oneshot", self.repo, "Build", "--preview", *flags)
        self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--waves", "0", code=1)
        self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--reference", self.root / "absent.png", code=1)

    def test_goal_template_passes_pursue_through(self):
        reference = self.root / "look.png"
        reference.write_bytes(PNG)
        template = {"repository": str(self.repo), "title": "Template", "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                    "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}],
                    "pursue": {"judge": {"kind": "command", "argv": fixtures.fixture("good")}, "references": ["look.png"], "max_waves": 2}}
        path = self.root / "template.json"
        path.write_text(json.dumps(template), encoding="utf-8")
        planned = self.root / "planned.json"
        planned.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}), encoding="utf-8")
        result = self.cli("goal", path, "Deliver result", "--plan-from", planned, "--enqueue")
        from nightweaver.store import Store
        spec = Store(self.store.root).get(result["run_id"])["spec"]
        self.assertEqual(spec["pursue"]["references"], [str(reference.resolve())])
        self.assertEqual((spec["pursue"]["max_waves"], spec["pursue"]["planner"]["kind"]), (2, "command"))


if __name__ == "__main__":
    unittest.main()
