"""Plateau rescue: diagnose a stalled pursuit, fork divergent strategies from the best build and keep one only if it beats it."""

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_windows as fixtures

from nightweaver import rescue
from nightweaver.cli import main
from nightweaver.engine import Run, git
from nightweaver.providers import DIAGNOSIS_SCHEMA, ProviderUnavailable, adapter, invoke
from nightweaver.readiness import diagnose
from nightweaver.reports import metrics
from nightweaver.spec import anchor_provider_paths, validate

BUILD = str(Path(__file__).with_name("rescue_fixture.py").resolve())
SCRIPT = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
PAST = "2020-01-01T00:00:00+00:00"
NAMES = ["Bold rethink", "Deep polish", "Change rendering"]
INCUMBENT = rescue.INCUMBENT
GOAL, DIAGNOSIS, COMPARE = "Independently judge whether this software goal", "You diagnose an autonomous coding run", "Independently compare two candidate builds"


def judged(score, met=False):
    return {"verdict": "MET" if met else "NOT_MET", "score": score, "reason": f"Reason {score}",
            "gaps": [] if met else [{"title": f"Gap at {score}", "detail": f"Detail {score}"}],
            "criteria": [{"criterion": "Looks right", "met": met, "note": "Observed"}]}


def diagnosis(*names, text="Every wave kept polishing the same layout"):
    return {"diagnosis": text, "strategies": [{"name": name, "approach": f"Rework it as {name} with its own composition."} for name in names or NAMES[:2]]}


def plan(*titles):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles]}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class RescueTests(unittest.TestCase):
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

    def sequence(self, name, items):
        return {"kind": "command", "argv": [sys.executable, SCRIPT, "script", str(self.script(name, items))]}

    def table(self, name="table", **plan_):
        """One judge route for goal judgments, the diagnosis and comparisons. Without scores the build is judged 60, 55, then MET."""
        plan_.setdefault("goal", [judged(60), judged(55), judged(90, met=True)])
        plan_.setdefault("diagnosis", [diagnosis()])
        plan_.setdefault("order", [NAMES[0], INCUMBENT, NAMES[1]])
        return {"kind": "command", "argv": [sys.executable, BUILD, "judge", str(self.script(name, plan_))]}

    def builder(self, tag="zq-builder", failing="-", log="worker"):
        return {"kind": "command", "argv": [sys.executable, BUILD, "build", str(self.root / f"{log}.prompts"), failing, tag]}

    def prompts(self, name):
        path = self.root / f"{name}.prompts"
        return [part.strip() for part in path.read_text(encoding="utf-8").split("\n=====\n") if part.strip()] if path.exists() else []

    def asked(self, name, marker):
        return [p for p in self.prompts(name) if marker in p]

    def stalled(self, *, judge=None, worker=None, planner=None, rescue=None, plateau=1, extra_pursue=None, **extra):
        """A pursuit that plateaus after its second judgment: 60, then 55. Two strategies by default."""
        block = {"judge": judge or self.table(), "planner": planner or self.sequence("planner", [plan("Next improvement")]), "plateau": plateau,
                 "max_waves": 4, "rescue": {"strategies": 2} if rescue is None else rescue, **(extra_pursue or {})}
        return self.configured((worker or self.builder())["argv"], pursue=block, **extra)

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def kinds(self, run):
        return [e["kind"] for e in self.store.events(run, limit=None)]

    def show(self, ref, name):
        return git(self.repo, "show", f"{ref}:{name}")

    def refs(self, run):
        return git(self.repo, "for-each-ref", "--format=%(refname)", f"refs/heads/nightweaver/{run}-rescue-*").split()

    def rescue_worktrees(self, run):
        return sorted(p.name for p in (self.store.directory(run) / "worktrees").glob("rescue-*"))

    def workers(self, name="worker"):
        return self.prompts(name)

    # ---------------------------------------------------------------- the main flow

    def test_a_strategy_that_beats_the_best_build_becomes_the_next_milestone_and_is_judged(self):
        run = self.stalled()
        self.assertEqual(Run(self.store, run).run(), "done")
        first, second, third = self.events(run, "goal_judged")
        self.assertEqual([(j["wave"], j["score"]) for j in (first, second, third)], [(1, 60), (2, 55), (3, 90)])
        timeline = [k for k in self.kinds(run) if k.startswith("rescue_") or k in ("goal_judged", "pursuit_reverted", "wave_planned", "milestone_accepted", "goal_met")]
        self.assertEqual(timeline, ["milestone_accepted", "goal_judged", "wave_planned", "milestone_accepted", "goal_judged", "pursuit_reverted", "rescue_started",
                                    "rescue_diagnosis_started", "rescue_diagnosed", "rescue_candidate_started", "rescue_worker_finished", "rescue_candidate_built",
                                    "rescue_candidate_started", "rescue_worker_finished", "rescue_candidate_built", "rescue_compared", "rescue_compared",
                                    "rescue_compared", "rescue_compared", "rescue_chosen", "wave_planned", "milestone_accepted", "goal_judged", "goal_met"])
        (started,) = self.events(run, "rescue_started")
        self.assertEqual((started["wave"], started["best_score"], started["best_commit"], started["stale_judgments"]), (2, 60, first["commit"], 1))
        self.assertEqual((started["rescue"], started["max_rescues"], started["strategies"]), (1, 1, 2))
        reverted = self.events(run, "pursuit_reverted")[0]
        self.assertEqual((reverted["from"], reverted["to"]), (second["commit"], first["commit"]))
        (found,) = self.events(run, "rescue_diagnosed")
        self.assertEqual((found["role"], [s["name"] for s in found["strategies"]], found["diagnosis"]),
                         ("judge", NAMES[:2], "Every wave kept polishing the same layout"))
        built = self.events(run, "rescue_candidate_built")
        self.assertEqual([b["index"] for b in built], [1, 2])
        for index, build in enumerate(built, 1):
            ref = f"refs/heads/nightweaver/{run}-rescue-w02-{index:02d}"
            self.assertEqual((build["ref"], git(self.repo, "rev-parse", ref)), (ref, build["commit"]))
            self.assertEqual(git(self.repo, "rev-parse", f"{build['commit']}^"), first["commit"], "strategies fork from the best build, not the discarded wave")
            self.assertEqual(self.show(ref, "strategy.txt"), NAMES[index - 1])
            self.assertEqual(self.show(ref, "lineage.txt").splitlines(), ["Prepare fixture", f"Rescue: {NAMES[index - 1]}"])
        compared = self.events(run, "rescue_compared")
        self.assertEqual([tuple(c["order"]) for c in compared], [(0, 1), (1, 0), (0, 2), (2, 0)])
        self.assertEqual({c["stage"] for c in compared}, {"incumbent"})
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual((chosen["winner"], chosen["name"], chosen["commit"], chosen["tied"], chosen["position_bias_pairs"]), (1, NAMES[0], built[0]["commit"], [], []))
        self.assertEqual([(r["index"], r["score"], r["incumbent"], r["qualified"]) for r in chosen["scores"]], [(1, 2.0, 0.0, True), (2, 0.0, 2.0, False)])
        self.assertEqual((chosen["comparisons"], chosen["invalid_comparisons"], chosen["passed"], chosen["strategies"]), (4, 0, 2, 2))
        planned = self.events(run, "wave_planned")
        self.assertEqual((planned[1]["wave"], planned[1]["titles"], planned[1]["first_step"], planned[1]["rescue"], planned[1]["strategy"]),
                         (3, [f"Rescue: {NAMES[0]}"], 3, True, NAMES[0]))
        spec = self.store.get(run)["spec"]
        last = spec["milestones"][-1]
        self.assertEqual((last["title"], last["gates"], last["paths"], last["worker"]), (f"Rescue: {NAMES[0]}", spec["milestones"][0]["gates"],
                                                                                        spec["milestones"][0]["paths"], spec["milestones"][0]["worker"]))
        self.assertIn("Rework it as Bold rethink", last["task"])
        self.assertIn("Every wave kept polishing", last["task"])
        accepted = self.events(run, "milestone_accepted")
        self.assertEqual((accepted[2]["step"], accepted[2]["commit"]), (3, built[0]["commit"]), "the winning commit itself was promoted")
        self.assertEqual(accepted[2]["verified_checks"], ["read fixture value"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), built[0]["commit"])
        self.assertEqual(self.show(f"nightweaver/{run}", "lineage.txt").splitlines(), ["Prepare fixture", f"Rescue: {NAMES[0]}"])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["wave"], state["step"], state["rescues_used"], state["best_score"], state["stale_judgments"]), ("done", 3, 3, 1, 90, 0))
        self.assertEqual((state["rescue"]["stage"], state["rescue"]["winner"], state["rescue"]["step"]), ("done", 1, 2))
        self.assertEqual(self.rescue_worktrees(run), [])
        self.assertEqual(self.events(run, "pursuit_stopped"), [])
        # What each model was asked.
        diagnosis_prompt = self.asked("table", DIAGNOSIS)[0]
        for text in ("exactly 2 genuinely different strategies", "bold rethink", "Wave 1: Prepare fixture -> NOT_MET 60/100", "Wave 2: Next improvement -> NOT_MET 55/100",
                     "discarded; the best build was restored", "The best build scored 60/100", "the last 1 judgments did not beat it", "Gap at 60",
                     "Recent rejection reasons\n- None", "Criteria that define done", "Remain read-only", "Return only JSON"):
            self.assertIn(text, diagnosis_prompt)
        self.assertNotIn("Gap at 55", diagnosis_prompt.split("Recent rejection")[0].split("Gaps in the best build")[1])
        self.assertNotIn("zq-builder", diagnosis_prompt)
        workers = self.workers()
        self.assertEqual(len(workers), 4)
        for name, prompt in zip(NAMES, workers[2:]):
            for text in (f"Milestone 3/3: Rescue: {name}", f"Strategy {name}: Rework it as {name}", "Every wave kept polishing the same layout", "Gap at 60",
                         "Criteria that define done", "do not hedge toward the approach that already stalled", "read fixture value"):
                self.assertIn(text, prompt)
            self.assertNotIn("Gap at 55", prompt)
            self.assertNotIn("Next improvement", prompt, "the discarded wave is not part of the rescue's history")
        pair = self.asked("table", COMPARE)
        self.assertEqual(len(pair), 4)
        for text in (f'Candidate A: strategy "{INCUMBENT}"', f'Candidate B: strategy "{NAMES[0]}"', "The best build so far, unchanged.", "Rework it as Bold rethink",
                     "demanding owner", "none were produced", "Open gaps in that build: Gap at 60"):
            self.assertIn(text, pair[0])
        self.assertIn(f'Candidate A: strategy "{NAMES[0]}"', pair[1])
        self.assertNotIn("zq-builder", pair[0])
        self.assertIn("- Rescue: Bold rethink (", self.asked("table", GOAL)[2])
        self.assertNotIn("Next improvement", self.asked("table", GOAL)[2])
        counts = {}
        for call in self.events(run, "provider_call_finished"):
            counts[call["role"]] = counts.get(call["role"], 0) + 1
        self.assertEqual(counts, {"worker": 4, "planner": 1, "judge": 8})
        self.assertEqual(state["provider_calls"], 13)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)
        # Measurement and reports.
        measured = metrics(self.store, run)
        self.assertEqual(measured["rescue"], {"attempted": 1, "won": 1, "failed": 0, "skipped": 0, "outcome": "won", "winner": NAMES[0], "score_before": 60,
                                              "score_after": 90, "rescues": [{"wave": 2, "outcome": "won", "winner": NAMES[0], "score_before": 60, "score_after": 90}]})
        self.assertEqual(measured["outcome"], "accepted")
        text = self.cli("pursuit", run, raw=True)
        for expected in ("Wave 3 (rescue: Bold rethink)", "Plateau rescue at best 60/100 - strategy Bold rethink chosen (4 judge calls)",
                         "Diagnosis: Every wave kept polishing the same layout", "1. Bold rethink [command] passed, score 2 - WINNER", "2. Deep polish [command] passed, score 0",
                         "The next judgment scored 90/100 (best before the rescue 60/100)", "Goal met."):
            self.assertIn(expected, text)
        data = self.cli("pursuit", run, "--json")
        self.assertEqual((data["waves"][1]["rescue"]["winner"], data["waves"][1]["rescue"]["next_score"], data["waves"][2]["rescue_strategy"]), (1, 90, NAMES[0]))
        self.assertNotIn("rescue", data["waves"][0])
        html = Path(self.cli("report", run)["report"]).read_text(encoding="utf-8")
        for expected in ("Plateau rescue after wave 2", "Strategy Bold rethink chosen", "Every wave kept polishing", "Winner", "1 attempted, 1 won"):
            self.assertIn(expected, html)
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")
        self.assertTrue(self.cli("db-check")["ok"])

    def test_when_no_strategy_beats_the_best_build_the_pursuit_stops_as_before(self):
        run = self.stalled(judge=self.table(order=[INCUMBENT, *NAMES[:2]], goal=[judged(60), judged(55), judged(50)]))
        self.assertEqual(Run(self.store, run).run(), "blocked")
        first, second = self.events(run, "goal_judged")
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual((chosen["winner"], chosen["tied"]), (None, []))
        self.assertEqual([(r["index"], r["score"], r["incumbent"], r["qualified"]) for r in chosen["scores"]], [(1, 0.0, 2.0, False), (2, 0.0, 2.0, False)])
        (failed,) = self.events(run, "rescue_failed")
        self.assertEqual(failed["reason"], "No strategy beat the best build")
        (stopped,) = self.events(run, "pursuit_stopped")
        self.assertEqual((stopped["reason"], stopped["best_score"], stopped["best_commit"], stopped["waves"]), ("plateau", 60, first["commit"], 2))
        self.assertEqual(len(self.events(run, "wave_planned")), 1, "no rescue milestone was appended")
        self.assertEqual(len(self.store.get(run)["spec"]["milestones"]), 2)
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["rescues_used"], state["accepted"], state["stale_judgments"]), ("blocked", "judging", 1, first["commit"], 1))
        self.assertTrue(state["last_error"].startswith("Goal not met after 2 waves (best score 60/100). Top gaps: Gap at 60"), state["last_error"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), first["commit"], "the result branch was already on the best build")
        self.assertEqual(len(self.refs(run)), 2, "the strategies' commits are kept")
        self.assertEqual(self.rescue_worktrees(run), [])
        self.assertEqual(metrics(self.store, run)["outcome"], "pursuit_stopped")
        self.assertEqual((metrics(self.store, run)["rescue"]["outcome"], metrics(self.store, run)["rescue"]["score_after"]), ("failed", None))
        text = self.cli("pursuit", run, raw=True)
        for expected in ("no strategy beat the best build", "Stopped: plateau", "Reverted to"):
            self.assertIn(expected, text)
        self.assertEqual(self.cli("diagnose-run", run)["last_failure_event"]["kind"], "pursuit_stopped")
        # The one rescue is spent: granting waves resumes at the judge and the next plateau stops without a second rescue.
        self.assertEqual(self.cli("pursuit", run, "--add-waves", 1, "--json")["extension"]["resumed"], True)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(len(self.events(run, "rescue_started")), 1)
        self.assertEqual(len(self.events(run, "pursuit_stopped")), 2)
        self.assertEqual(self.store.get(run)["state"]["rescues_used"], 1)

    def test_screenshots_reach_the_diagnosis_and_the_judge_under_their_own_candidate(self):
        gates = [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20},
                 {"name": "screenshot", "argv": [sys.executable, BUILD, "shot"], "timeout": 20, "artifacts": []}]
        run = self.stalled(gates=gates, rescue={"strategies": 2}, judge=self.table(order=[INCUMBENT, *NAMES[:2]]))
        self.assertEqual(Run(self.store, run).run(), "blocked")
        first = self.events(run, "goal_judged")[0]
        (best_shot,) = first["screenshots"]
        self.assertTrue(Path(best_shot).is_relative_to(self.store.directory(run) / "evidence" / "judge" / "01"))
        built = {b["index"]: b for b in self.events(run, "rescue_candidate_built")}
        shots = {i: b["screenshots"] for i, b in built.items()}
        folder = self.store.directory(run) / "evidence" / "rescue" / "w02"
        self.assertTrue(all(len(v) == 1 and v[0].endswith("screen.png") and Path(v[0]).is_relative_to(folder / f"{i:02d}") for i, v in shots.items()))
        diagnosis_prompt = self.asked("table", DIAGNOSIS)[0]
        self.assertIn(f"- {best_shot}", diagnosis_prompt)
        self.assertIn("OPEN each image file", diagnosis_prompt)
        first_call, second_call = self.asked("table", COMPARE)[:2]
        for prompt, order in ((first_call, (0, 1)), (second_call, (1, 0))):
            images = {0: best_shot, 1: shots[1][0]}
            a, b = (prompt.index(f"Candidate {x}:") for x in "AB")
            self.assertTrue(a < prompt.index(images[order[0]]) < b < prompt.index(images[order[1]]), "each image is listed under its own candidate")
            self.assertIn("OPEN each image file", prompt)
        self.assertEqual({(g["rescue"], g["gate_index"]) for g in self.events(run, "gate_finished") if "rescue" in g}, {(1, 1), (1, 2), (2, 1), (2, 2)})
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")

    def test_a_second_rescue_starts_after_a_winning_one_stalls_and_the_workers_rotate(self):
        later = ["Change rendering", "Fresh start"]
        judge = self.table(goal=[judged(60), judged(55), judged(58), judged(50)], diagnosis=[diagnosis(*NAMES[:2]), diagnosis(*later)],
                           order=[NAMES[0], INCUMBENT, NAMES[1], *later])
        run = self.stalled(judge=judge, rescue={"strategies": 2, "max_rescues": 2, "workers": [self.builder("alpha", log="alpha"), self.builder("beta", log="beta")]})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual([(j["wave"], j["score"]) for j in self.events(run, "goal_judged")], [(1, 60), (2, 55), (3, 58)])
        first, second = self.events(run, "rescue_started")
        self.assertEqual((first["wave"], first["rescue"], first["max_rescues"]), (2, 1, 2))
        self.assertEqual((second["wave"], second["rescue"], second["best_score"], second["best_commit"]), (3, 2, 60, first["best_commit"]))
        first_chosen, second_chosen = self.events(run, "rescue_chosen")
        self.assertEqual((first_chosen["winner"], second_chosen["winner"]), (1, None))
        self.assertEqual(self.events(run, "wave_planned")[1]["titles"], [f"Rescue: {NAMES[0]}"])
        self.assertEqual(len(self.store.get(run)["spec"]["milestones"]), 3, "only the winning rescue appended a milestone")
        self.assertEqual(len(self.events(run, "pursuit_reverted")), 2, "the wave that won its rescue but scored below the best build was discarded")
        built = self.events(run, "rescue_candidate_built")
        self.assertEqual(len(built), 4)
        self.assertEqual([git(self.repo, "rev-parse", f"{b['commit']}^") for b in built], [first["best_commit"]] * 4, "every rescue forks from the best build")
        self.assertEqual(sorted(r.rsplit("/", 1)[1].removeprefix(f"{run}-") for r in self.refs(run)),
                         ["rescue-w02-01", "rescue-w02-02", "rescue-w03-01", "rescue-w03-02"])
        for ref, tag in ((f"{run}-rescue-w02-01", "alpha"), (f"{run}-rescue-w02-02", "beta"), (f"{run}-rescue-w03-01", "alpha"), (f"{run}-rescue-w03-02", "beta")):
            self.assertIn(f'data-built-by="{tag}"', self.show(f"nightweaver/{ref}", "index.html"))
        self.assertEqual((len(self.workers("alpha")), len(self.workers("beta")), len(self.workers())), (2, 2, 2), "the milestones use their own worker; a won rescue is promoted, not rebuilt")
        earlier = self.asked("table", DIAGNOSIS)[1]
        for text in ("Earlier rescue attempts on this run", f"Wave 2: {NAMES[0]} (won), {NAMES[1]} (did not beat the best build)", f"Wave 3: Rescue: {NAMES[0]} -> NOT_MET 58/100"):
            self.assertIn(text, earlier)
        state = self.store.get(run)["state"]
        self.assertEqual((state["rescues_used"], state["phase"], state["blocked_from"], state["wave"], state["accepted"]), (2, "blocked", "judging", 3, first["best_commit"]))
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau")
        measured = metrics(self.store, run)["rescue"]
        self.assertEqual((measured["attempted"], measured["won"], measured["failed"], measured["outcome"], measured["winner"]), (2, 1, 1, "failed", None))
        self.assertEqual([(r["wave"], r["outcome"], r["score_before"], r["score_after"]) for r in measured["rescues"]], [(2, "won", 60, 58), (3, "failed", 60, None)])
        self.assertEqual([w.get("rescue", {}).get("status") for w in self.cli("pursuit", run, "--json")["waves"]], [None, "chosen", "failed"])
        self.assertEqual(self.rescue_worktrees(run), [])

    def test_order_dependent_comparisons_do_not_earn_a_strategy_the_win(self):
        run = self.stalled(judge=self.table(always="A"))
        self.assertEqual(Run(self.store, run).run(), "blocked")
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual(chosen["winner"], None)
        self.assertEqual(chosen["position_bias_pairs"], [[0, 1], [0, 2]])
        self.assertEqual([(r["score"], r["incumbent"], r["qualified"]) for r in chosen["scores"]], [(1.0, 1.0, False), (1.0, 1.0, False)], "half the points is not more than half")
        self.assertEqual([c["winner"] for c in self.events(run, "rescue_compared")], [0, 1, 0, 2])
        self.assertEqual(len(self.events(run, "pursuit_stopped")), 1)
        text = self.cli("pursuit", run, raw=True)
        self.assertIn("Position bias in pairs: best build vs 1, best build vs 2", text)
        self.assertEqual(self.cli("pursuit", run, "--json")["waves"][1]["rescue"]["position_bias_pairs"], [[0, 1], [0, 2]])

    def test_qualifiers_with_equal_scores_meet_head_to_head(self):
        run = self.stalled(judge=self.table(order=[NAMES[1], NAMES[0], INCUMBENT]))
        self.assertEqual(Run(self.store, run).run(), "done")
        compared = self.events(run, "rescue_compared")
        self.assertEqual([(tuple(c["order"]), c["stage"]) for c in compared], [((0, 1), "incumbent"), ((1, 0), "incumbent"), ((0, 2), "incumbent"), ((2, 0), "incumbent"),
                                                                               ((1, 2), "tiebreak"), ((2, 1), "tiebreak")])
        (chosen,) = self.events(run, "rescue_chosen")
        self.assertEqual((chosen["winner"], chosen["name"], chosen["tied"]), (2, NAMES[1], [1, 2]), "the head-to-head winner beats the lower index")
        self.assertEqual([r["score"] for r in chosen["scores"]], [2.0, 2.0])
        self.assertEqual(self.events(run, "milestone_accepted")[2]["commit"], self.events(run, "rescue_candidate_built")[1]["commit"])
        self.assertEqual(self.events(run, "wave_planned")[1]["titles"], [f"Rescue: {NAMES[1]}"])

    def test_a_strategy_that_fails_its_checks_never_reaches_the_judge_and_a_bad_diagnosis_is_retried(self):
        run = self.stalled(judge=self.table(diagnosis=["this is not json", diagnosis()]), worker=self.builder(failing=",".join(NAMES[:2])))
        self.assertEqual(Run(self.store, run).run(), "blocked")
        (invalid,) = self.events(run, "rescue_diagnosis_invalid")
        self.assertEqual(invalid["attempt"], 1)
        self.assertIn("No JSON object", invalid["issue"])
        self.assertIn("Your previous answer was rejected", self.asked("table", DIAGNOSIS)[1])
        failed = self.events(run, "rescue_candidate_failed")
        self.assertEqual([f["index"] for f in failed], [1, 2])
        self.assertTrue(all("read fixture value exited 2" in f["reason"] and f["failed_checks"] == ["read fixture value"] and f["commit"] is None for f in failed))
        for kind in ("rescue_candidate_built", "rescue_compared", "rescue_chosen"):
            self.assertEqual(self.events(run, kind), [], kind)
        (done,) = self.events(run, "rescue_failed")
        self.assertEqual(done["reason"], "No strategy passed its checks")
        self.assertEqual([s["name"] for s in done["strategies"]], NAMES[:2])
        self.assertEqual(self.refs(run), [])
        self.assertEqual(len(self.asked("table", COMPARE)), 0)
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau")
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["rescues_used"]), ("blocked", "judging", 1))
        self.assertEqual(self.rescue_worktrees(run), [])
        self.assertIn("no strategy beat the best build (No strategy passed its checks)", self.cli("pursuit", run, raw=True))

    def test_two_unreadable_diagnoses_skip_the_rescue_and_the_judge_falls_back(self):
        run = self.stalled(judge=self.table(name="primary"))
        spec = self.store.get(run)["spec"]
        spec["pursue"]["judge_fallbacks"] = [self.table(name="table", diagnosis=["no json at all", {"diagnosis": "x", "strategies": []}])]
        with self.store.connect() as db:
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
        flaky = self.failing("signed out", [("diagnosis", None)], "authentication", config="primary.json")
        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        (fallback,) = self.events(run, "provider_fallback")
        self.assertEqual(fallback["from"]["role"], "judge")
        self.assertEqual([i["attempt"] for i in self.events(run, "rescue_diagnosis_invalid")], [1])
        (skipped,) = self.events(run, "rescue_skipped")
        self.assertIn("attempt 2 of 2", skipped["reason"])
        self.assertIn("exactly 2 strategies", skipped["reason"])
        for kind in ("rescue_diagnosed", "rescue_candidate_started", "rescue_chosen", "rescue_failed"):
            self.assertEqual(self.events(run, kind), [], kind)
        self.assertEqual(len(self.workers()), 2, "no strategy was built")
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau")
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["rescues_used"], state["judge_route"]), ("blocked", "judging", 1, 0))
        self.assertEqual(metrics(self.store, run)["rescue"]["outcome"], "skipped")
        self.assertIn("Plateau rescue at best 60/100 - skipped:", self.cli("pursuit", run, raw=True))
        self.assertEqual(self.cli("diagnose-run", run)["recorded_failure_stages"]["pursuit_stopped"], 1)

    def failing(self, message, picks, kind="usage_limit", config=None):
        """Raise ProviderUnavailable once for each (mode, marker) in picks when it first reaches a provider; a marker must appear in the prompt."""
        real, seen = invoke, []

        def route(conf, mode, cwd, log, prompt, *args, **kwargs):
            for pick in picks:
                if (mode == pick[0] and pick not in seen and (pick[1] is None or pick[1] in prompt)
                        and (config is None or conf["argv"][-1].endswith(config))):
                    seen.append(pick)
                    raise ProviderUnavailable(message, kind)
            return real(conf, mode, cwd, log, prompt, *args, **kwargs)
        return route

    def test_the_planner_diagnoses_when_it_comes_from_another_vendor_family_than_the_worker(self):
        real, calls = invoke, []
        scripted = self.sequence("diagnoser", [plan("Next improvement"), diagnosis()])

        def route(conf, mode, *args, **kwargs):
            calls.append((conf["kind"], mode))
            return real(scripted if conf["kind"] == "claude" else conf, mode, *args, **kwargs)

        run = self.stalled(planner={"kind": "claude"}, worker=self.builder(failing=",".join(NAMES[:2])))
        with patch("nightweaver.engine.invoke", side_effect=route):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual([c for c in calls if c[1] == "diagnosis"], [("claude", "diagnosis")])
        (found,) = self.events(run, "rescue_diagnosed")
        self.assertEqual((found["role"], found["diagnoser"]), ("planner", {"kind": "claude"}))
        self.assertNotIn("planner", [e["role"] for e in self.events(run, "independence_degraded")], "a diagnosis from another family needs no warning")
        diagnoses = [c for c in self.events(run, "provider_call_finished") if c["provider"] == "claude"]
        self.assertEqual([c["role"] for c in diagnoses], ["planner", "planner"], "the wave planner and the diagnosis")
        self.assertEqual(len(self.asked("diagnoser", DIAGNOSIS)), 1)
        self.assertEqual(self.events(run, "pursuit_stopped")[0]["reason"], "plateau")

    # ---------------------------------------------------------------- the rescue is off or never configured

    def test_max_rescues_zero_keeps_the_pursuit_exactly_as_it_was(self):
        for label, rescue_block in (("disabled", {"max_rescues": 0}), ("planned before rescues existed", None)):
            with self.subTest(label):
                run = self.stalled(rescue={"max_rescues": 0})
                if rescue_block is None:
                    spec = self.store.get(run)["spec"]
                    del spec["pursue"]["rescue"]
                    with self.store.connect() as db:
                        db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
                self.assertEqual(Run(self.store, run).run(), "blocked")
                kinds = self.kinds(run)
                self.assertEqual([k for k in kinds if "rescue" in k], [])
                self.assertEqual(kinds.count("goal_judged"), 2)
                (stopped,) = self.events(run, "pursuit_stopped")
                self.assertEqual((stopped["reason"], stopped["best_score"], stopped["waves"]), ("plateau", 60, 2))
                state = self.store.get(run)["state"]
                self.assertEqual((state["phase"], state["blocked_from"], state.get("rescue"), state.get("rescues_used")), ("blocked", "judging", None, None))
                self.assertEqual(len(self.asked("table", DIAGNOSIS)), 0)
                self.assertNotIn("rescue", metrics(self.store, run))
                self.assertEqual(self.cli("list", "--phase", "rescuing"), [])
                self.assertNotIn("rescue", self.cli("pursuit", run, raw=True).lower())
                self.root.joinpath("table.goal.pos").unlink(missing_ok=True)
                self.root.joinpath("table.prompts").unlink(missing_ok=True)
                self.root.joinpath("worker.prompts").unlink(missing_ok=True)
                self.root.joinpath("planner.pos").unlink(missing_ok=True)

    # ---------------------------------------------------------------- crash and resume

    def crash(self, run, target, **options):
        with patch(target, **options), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        return self.store.get(run)["state"]

    def test_resume_in_every_stage_repeats_no_finished_work(self):
        run = self.stalled()
        # Entering the diagnosis.
        state = self.crash(run, "nightweaver.rescue.diagnose", side_effect=SystemExit)
        self.assertEqual((state["phase"], state["rescue"]["stage"], state["step"], state["wave"]), ("rescuing", "diagnose", 2, 2))
        self.assertEqual(len(self.events(run, "rescue_started")), 1)
        self.assertEqual(self.asked("table", DIAGNOSIS), [])
        self.assertEqual(self.cli("list", "--phase", "rescuing")[0]["id"], run)
        for command in (("status", run), ("list", "--compact"), ("metrics", run), ("handoff", run), ("budget", run), ("timeline", run), ("diagnose-run", run),
                        ("recovery", run), ("fleet",), ("call-ledger", run), ("attention",), ("db-check",)):
            self.cli(*command)
        self.assertEqual(self.cli("status", run)["state"]["phase"], "rescuing")
        self.assertEqual(self.cli("metrics", run)["rescue"]["outcome"], "in progress")
        self.assertIn("Plateau rescue at best 60/100 - in progress", self.cli("pursuit", run, raw=True))
        self.assertEqual(self.cli("pursuit", run, "--json")["waves"][1]["rescue"]["status"], "in_progress")
        self.assertTrue(Path(self.cli("report", run)["report"]).is_file())
        with closing(sqlite3.connect(self.store.root / "runs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT json_extract(state,'$.phase') FROM runs WHERE id=?", (run,)).fetchone()[0], "rescuing")
        # Between the two builds.
        real = rescue.checkout

        def stop_at_two(*args, **kwargs):
            if args[2] == "rescue-02":
                raise SystemExit
            return real(*args, **kwargs)
        state = self.crash(run, "nightweaver.rescue.checkout", side_effect=stop_at_two)
        self.assertEqual((state["rescue"]["stage"], [b["index"] for b in state["rescue"]["builds"]], state["rescue"]["diagnosis"]),
                         ("build", [1], "Every wave kept polishing the same layout"))
        self.assertEqual((len(self.asked("table", DIAGNOSIS)), len(self.workers())), (1, 3))
        self.assertEqual(self.cli("db-check")["ok"], True)
        # In the middle of the comparisons.
        calls, original = [], rescue.comparison_prompt

        def stop_third(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise SystemExit
            return original(*args, **kwargs)
        state = self.crash(run, "nightweaver.rescue.comparison_prompt", side_effect=stop_third)
        self.assertEqual((state["rescue"]["stage"], len(state["rescue"]["comparisons"]), len(state["rescue"]["builds"])), ("select", 2, 2))
        self.assertEqual((len(self.workers()), len(self.asked("table", COMPARE))), (4, 2))
        # After the choice, before the milestone is appended.
        state = self.crash(run, "nightweaver.rescue.promote", side_effect=SystemExit)
        self.assertEqual((state["phase"], state["rescue"]["stage"], state["rescue"]["winner"], len(self.store.get(run)["spec"]["milestones"])), ("rescuing", "promote", 1, 2))
        self.assertEqual(len(self.asked("table", COMPARE)), 4)
        self.assertEqual(self.rescue_worktrees(run), [])
        # After the milestone is appended, before it is promoted.
        state = self.crash(run, "nightweaver.engine.Run.promote", side_effect=SystemExit)
        built = self.events(run, "rescue_candidate_built")[0]
        self.assertEqual((state["phase"], state["candidate"], state["candidate_tree"], state["checks_sha256"], state["attempt"], state["attempts_used"]),
                         ("promoting", built["commit"], built["tree"], built["checks_sha256"], 1, 0))
        self.assertEqual((len(self.store.get(run)["spec"]["milestones"]), state["wave"], state["stale_judgments"], state["rescues_used"]), (3, 3, 0, 1))
        evidence = self.store.directory(run) / "evidence"
        self.assertEqual((evidence / "03" / "01" / "checks.json").read_bytes(), (evidence / "rescue" / "w02" / "01" / "checks.json").read_bytes())
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), self.events(run, "goal_judged")[0]["commit"], "the result branch moves only at promotion")
        # Operator feedback arrives: the appended milestone goes through the ordinary flow instead of the staged commit.
        self.store.feedback(run, "Make the hero bolder")
        self.assertEqual(Run(self.store, run).run(), "done")
        (rejected,) = self.events(run, "attempt_rejected")
        self.assertEqual(rejected["phase"], "feedback")
        normal = self.workers()[4]
        for text in ("Milestone 3/3: Rescue: Bold rethink", "Rework it as Bold rethink", "Why progress stalled: Every wave kept polishing", "Make the hero bolder"):
            self.assertIn(text, normal)
        self.assertNotIn("Strategy Bold rethink", normal)
        self.assertNotEqual(self.events(run, "milestone_accepted")[2]["commit"], built["commit"])
        # Nothing finished was repeated.
        self.assertEqual(len(self.asked("table", DIAGNOSIS)), 1)
        self.assertEqual(sum("Plateau rescue" in p for p in self.workers()), 2)
        self.assertEqual([tuple(c["order"]) for c in self.events(run, "rescue_compared")], [(0, 1), (1, 0), (0, 2), (2, 0)])
        self.assertEqual(len(self.events(run, "rescue_candidate_built")), 2)
        self.assertEqual(len(self.events(run, "goal_judged")), 3)
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")
        self.assertEqual(self.store.get(run)["state"]["phase"], "done")

    # ---------------------------------------------------------------- providers and parking

    def clock(self, run, seen):
        def sleeper(_seconds):
            seen.append(self.store.get(run)["state"])
            self.store.transition(run, "fixture_clock", {}, retry_at=PAST)
        return patch("nightweaver.engine.time", SimpleNamespace(sleep=sleeper))

    def test_usage_limits_park_every_rescue_stage_and_the_run_resumes(self):
        run = self.stalled()
        seen = []
        flaky = self.failing("You've hit your limit · resets in 2 hours", [("diagnosis", None), ("worker", "Plateau rescue"), ("comparison", None)])
        with patch("nightweaver.engine.invoke", side_effect=flaky), self.clock(run, seen):
            self.assertEqual(Run(self.store, run).run(), "done")
        parked = self.events(run, "provider_parked")
        self.assertEqual([(p["role"], p["park"], p["parsed"]) for p in parked], [("judge", 1, True), ("worker", 1, True), ("judge", 1, True)])
        self.assertEqual([(s["phase"], s["parks"], s["rescue"]["stage"]) for s in seen], [("rescuing", 1, "diagnose"), ("rescuing", 1, "build"), ("rescuing", 1, "select")])
        self.assertEqual(self.events(run, "provider_unavailable"), [])
        self.assertEqual(len(self.events(run, "rescue_candidate_built")), 2)
        self.assertEqual(len(self.asked("table", DIAGNOSIS)), 1)
        self.assertEqual(self.store.get(run)["state"]["parks"], 0)
        self.assertEqual(self.events(run, "milestone_accepted")[2]["step"], 3)

    def test_the_budget_runs_out_in_the_middle_of_a_rescue_and_retry_resumes_it(self):
        run = self.stalled(budgets={"max_calls": 7})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["provider_calls"]), ("blocked", "rescuing", 7))
        self.assertIn("provider-call budget", state["last_error"])
        self.assertEqual((state["rescue"]["stage"], [b["index"] for b in state["rescue"]["builds"]]), ("build", [1]))
        self.assertEqual(metrics(self.store, run)["outcome"], "budget_exhausted")
        self.assertEqual(self.events(run, "pursuit_stopped"), [])
        spec = self.store.get(run)["spec"]
        spec["budgets"]["max_calls"] = 40
        with self.store.connect() as db:
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        state = self.store.get(run)["state"]
        self.assertEqual((state["provider_calls"], state["rescues_used"]), (13, 1))
        self.assertEqual(len(self.asked("table", DIAGNOSIS)), 1)
        self.assertEqual(sum("Plateau rescue" in p for p in self.workers()), 2, "the finished strategy was not built again")
        self.assertEqual(len(self.events(run, "rescue_compared")), 4)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class RescueCliTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = RescueTests.cli

    def commit_tests(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_it(self): pass\n", encoding="utf-8")
        git(self.repo, "add", "tests/test_a.py")
        git(self.repo, "commit", "-m", "Declare a check")

    def test_oneshot_rescue_strategies_flag_builds_the_spec_and_leaves_the_alternate_worker_alone(self):
        from nightweaver.store import Store
        self.commit_tests()
        raw = self.cli("oneshot", self.repo, "Build a beautiful page", "--preview", "--pursue", "--rescue-strategies", "4")
        self.assertEqual(raw["pursue"]["rescue"], {"strategies": 4})
        self.assertEqual(self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--rescue-strategies", "0")["pursue"]["rescue"], {"max_rescues": 0})
        self.assertNotIn("rescue", self.cli("oneshot", self.repo, "Build", "--preview", "--pursue")["pursue"])
        both = self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--rescue", "claude", "--rescue-strategies", "2", "--worker", "codex")
        self.assertEqual((both["pursue"]["rescue"], [w["kind"] for w in both["worker_fallbacks"]]), ({"strategies": 2}, ["claude"]))
        for flags, expected in ((["--rescue-strategies", "2"], {"strategies": 2, "max_rescues": 1}), (["--rescue-strategies", "0"], {"strategies": 3, "max_rescues": 0}), ([], {"strategies": 3, "max_rescues": 1})):
            stored = self.cli("oneshot", self.repo, "Build", "--direct", "--enqueue", "--pursue", *flags)
            self.assertEqual(Store(self.store.root).get(stored["run_id"])["spec"]["pursue"]["rescue"], expected, flags)

    def test_oneshot_rescue_strategies_misuse_is_refused(self):
        self.commit_tests()
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            self.cli("oneshot", self.repo, "Build", "--preview", "--rescue-strategies", "3")
        for count in ("1", "5", "-1"):
            self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--rescue-strategies", count, code=1)


class RescueSpecTests(unittest.TestCase):
    def raw(self, rescue=None, **pursue):
        block = {"judge": {"kind": "codex"}, **pursue, **({} if rescue is None else {"rescue": rescue})}
        return {"repository": str(Path(__file__).resolve().parents[1]), "title": "T", "objective": "O", "worker": {"kind": "claude", "model": "m"},
                "gates": [{"name": "g", "argv": ["x"]}], "milestones": [{"title": "M", "task": "T"}], "acceptance": ["Beautiful"], "pursue": block}

    def test_the_rescue_is_on_by_default_and_a_normalized_block_validates_again(self):
        self.assertEqual(validate(self.raw())["pursue"]["rescue"], {"strategies": 3, "max_rescues": 1})
        self.assertEqual(validate(self.raw({}))["pursue"]["rescue"], {"strategies": 3, "max_rescues": 1})
        self.assertEqual(validate(self.raw({"max_rescues": 0}))["pursue"]["rescue"], {"strategies": 3, "max_rescues": 0})
        full = validate(self.raw({"strategies": 4, "max_rescues": 3, "workers": [{"kind": "codex", "effort": "high"}, {"kind": "command", "argv": ["x"]}]}))["pursue"]["rescue"]
        self.assertEqual(full, {"strategies": 4, "max_rescues": 3, "workers": [{"kind": "codex", "effort": "high"}, {"kind": "command", "argv": ["x"]}]})
        self.assertEqual(validate(self.raw(full))["pursue"]["rescue"], full)
        low = validate(self.raw({"strategies": 2, "max_rescues": 0}))["pursue"]["rescue"]
        self.assertEqual(low, {"strategies": 2, "max_rescues": 0})
        both = validate(self.raw({"strategies": 2}, explore={"directions": 2}))["pursue"]
        self.assertEqual((both["explore"]["directions"], both["rescue"]["strategies"]), (2, 2))

    def test_invalid_blocks_are_rejected(self):
        cases = {
            "unknown": ({"typo": 1}, "Unknown pursue.rescue fields"), "low": ({"strategies": 1}, "strategies must be an integer between 2 and 4"),
            "high": ({"strategies": 5}, "strategies"), "bool": ({"strategies": True}, "strategies"), "text": ({"strategies": "3"}, "strategies"),
            "rescues low": ({"max_rescues": -1}, "max_rescues must be an integer between 0 and 3"), "rescues high": ({"max_rescues": 4}, "max_rescues"),
            "rescues text": ({"max_rescues": "1"}, "max_rescues"), "no workers": ({"workers": []}, "at least one worker"),
            "many workers": ({"workers": [{"kind": "codex"}] * 5}, "at most 4"), "wrong kind": ({"workers": [{"kind": "ollama"}]}, "Invalid rescue worker adapter"),
            "effort": ({"workers": [{"kind": "codex", "effort": "huge"}]}, "Unsupported effort"), "workers type": ({"workers": "codex"}, "rescue workers must be a list"),
        }
        for label, (block, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                validate(self.raw(block))
        for value in (3, False, [], "on"):
            with self.subTest(value), self.assertRaisesRegex(ValueError, "rescue must be an object"):
                validate(self.raw(value))
        raw = self.raw({})
        raw["rescue"] = {}
        del raw["pursue"]
        with self.assertRaisesRegex(ValueError, "Unknown plan fields: rescue"):
            validate(raw)

    def test_worker_paths_resolve_against_the_plan_folder(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = self.raw({"workers": [{"kind": "claude", "profile_dir": "profiles/a"}]})
            anchor_provider_paths(raw, folder)
            expected = str((folder / "profiles" / "a").resolve())
            self.assertEqual(raw["pursue"]["rescue"]["workers"][0]["profile_dir"], expected)
            self.assertEqual(validate(raw, folder)["pursue"]["rescue"]["workers"][0]["profile_dir"], expected)

    def test_doctor_checks_the_rescue_workers(self):
        spec = validate(self.raw({"workers": [{"kind": "claude", "model": "alpha"}, {"kind": "codex"}]}))
        from nightweaver.store import Store
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            roles = [role for route in diagnose(Store(directory), spec)["providers"] for role in route["roles"]]
        self.assertIn("rescue worker 1", roles)
        self.assertIn("rescue worker 2", roles)


def synthetic(*calls, builds=(1, 2)):
    """A rescue state with the judge's answers already recorded. A call is (first, second, winner or None, invalid?)."""
    comparisons = [{"pair": sorted((a, b)), "order": [a, b], "winner": winner, "result": "tie" if winner is None else "A", **({"invalid": True} if bad else {})}
                   for a, b, winner, *bad in calls]
    return ({"comparisons": comparisons, "builds": [{"index": i, "name": f"S{i}", "passed": True} for i in builds]},
            [{"index": i, "name": f"S{i}", "passed": True, "failed_checks": []} for i in builds])


class RescueUnitTests(unittest.TestCase):
    def test_parse_diagnosis_is_tolerant_of_prose_and_strict_about_the_set(self):
        good = json.dumps(diagnosis("Alpha", "Beta", "Gamma", text="  Stuck on one layout  "), indent=1)
        text, strategies = rescue.parse_diagnosis("Sure!\n```json\n" + good + "\n```\nHope it helps.", 3)
        self.assertEqual((text, [s["name"] for s in strategies]), ("Stuck on one layout", ["Alpha", "Beta", "Gamma"]))
        extra = json.dumps({**diagnosis("A one", "B two"), "note": "ignored"})
        self.assertEqual(len(rescue.parse_diagnosis(extra, 2)[1]), 2)
        cases = ((good, 2, "exactly 2"), ("no json", 3, "No JSON object"), ('{"strategies": "x"}', 3, "exactly 3"), ('{"strategies": []}', 2, "exactly 2"),
                 (json.dumps({"strategies": diagnosis("A", "B")["strategies"]}), 2, "diagnosis must be text"),
                 (json.dumps({"diagnosis": " ", "strategies": diagnosis("A", "B")["strategies"]}), 2, "diagnosis must be text"),
                 (json.dumps(diagnosis("Same", "same")), 2, "distinct"),
                 (json.dumps({"diagnosis": "d", "strategies": [{"name": "a", "approach": "b"}, {"name": "c"}]}), 2, "approach"),
                 (json.dumps({"diagnosis": "d", "strategies": ["a", "b"]}), 2, "object"))
        for answer, count, message in cases:
            with self.subTest(answer[:30]), self.assertRaisesRegex(ValueError, message):
                rescue.parse_diagnosis(answer, count)
        long = rescue.parse_diagnosis(json.dumps({"diagnosis": "d" * 4000, "strategies": [{"name": "n" * 200, "approach": "b" * 5000}, {"name": "m", "approach": "c"}]}), 2)
        self.assertEqual((len(long[0]), len(long[1][0]["name"]), len(long[1][0]["approach"])), (3000, 80, 3000))

    def test_a_strategy_must_score_more_than_half_the_points_against_the_incumbent(self):
        cases = {"wins both": ([(0, 1, 1), (1, 0, 1)], (2.0, 0.0, True)), "loses both": ([(0, 1, 0), (1, 0, 0)], (0.0, 2.0, False)),
                 "splits (order dependent)": ([(0, 1, 0), (1, 0, 1)], (1.0, 1.0, False)), "ties both": ([(0, 1, None), (1, 0, None)], (1.0, 1.0, False)),
                 "win and tie": ([(0, 1, 1), (1, 0, None)], (1.5, 0.5, True)), "tie and loss": ([(0, 1, None), (1, 0, 0)], (0.5, 1.5, False))}
        for label, (calls, expected) in cases.items():
            ex, passed = synthetic(*calls, builds=(1,))
            rows, leaders = rescue.standing(ex, passed)
            with self.subTest(label):
                self.assertEqual((rows[0]["score"], rows[0]["incumbent"], rows[0]["qualified"]), expected)
                self.assertEqual(leaders, [1] if expected[2] else [])
        ex, passed = synthetic((0, 1, 1), (1, 0, 0, True), builds=(1,))
        self.assertEqual(rescue.standing(ex, passed)[0][0]["score"], 1.0, "an unreadable call counts for nobody")
        ex, passed = synthetic((0, 1, None, True), (1, 0, None, True), builds=(1,))
        self.assertEqual((rescue.standing(ex, passed)[0][0]["qualified"], rescue.decide(ex, passed)[0]), (False, None))

    def test_decide_picks_the_top_score_then_the_head_to_head_then_the_lower_index(self):
        incumbent_loses = [(0, 1, 1), (1, 0, 1), (0, 2, 2), (2, 0, 2), (0, 3, 3), (3, 0, 3)]
        ex, passed = synthetic(*incumbent_loses, builds=(1, 2, 3))
        self.assertEqual(rescue.schedule(ex, passed)[6:], [(1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2)], "three tied leaders meet pairwise in both orders")
        ex, passed = synthetic(*incumbent_loses, (1, 2, 2), (2, 1, 2), (1, 3, 3), (3, 1, 3), (2, 3, 3), (3, 2, 3), builds=(1, 2, 3))
        self.assertEqual(rescue.decide(ex, passed)[0], 3)
        ex, passed = synthetic(*incumbent_loses, (1, 2, 1), (2, 1, 2), (1, 3, 1), (3, 1, 3), (2, 3, 2), (3, 2, 3), builds=(1, 2, 3))
        self.assertEqual(rescue.decide(ex, passed)[0], 1, "an order-dependent head to head cannot separate them: the lower index wins")
        ex, passed = synthetic((0, 1, 1), (1, 0, 1), (0, 2, 2), (2, 0, 0), builds=(1, 2))
        self.assertEqual(rescue.schedule(ex, passed), [(0, 1), (1, 0), (0, 2), (2, 0)], "a single leader needs no head to head")
        self.assertEqual(rescue.decide(ex, passed)[0], 1, "the strategy that split its two calls does not qualify")
        ex, passed = synthetic((0, 1, 1), (1, 0, None), (0, 2, 2), (2, 0, 2), builds=(1, 2))
        self.assertEqual((rescue.decide(ex, passed)[0], len(rescue.schedule(ex, passed))), (2, 4), "the higher score wins without a head to head")
        ex, passed = synthetic((0, 1, 0), (1, 0, 0), builds=(1,))
        self.assertEqual(rescue.decide(ex, passed), (None, rescue.standing(ex, passed)[0], []))
        self.assertEqual(rescue.schedule(synthetic(builds=(1, 2))[0], synthetic(builds=(1, 2))[1]), [(0, 1), (1, 0), (0, 2), (2, 0)], "nothing is known before the first call")

    def test_a_rescue_needs_budget_in_the_plan_and_room_for_a_milestone(self):
        def run(rescue_block, milestones=2):
            pursue = {"judge": {}, **({} if rescue_block is None else {"rescue": rescue_block})}
            return SimpleNamespace(spec={"pursue": pursue, "milestones": [{}] * milestones})
        block = {"strategies": 3, "max_rescues": 2}
        self.assertTrue(rescue.available(run(block), {}))
        self.assertTrue(rescue.available(run(block), {"rescues_used": 1}))
        self.assertFalse(rescue.available(run(block), {"rescues_used": 2}))
        self.assertFalse(rescue.available(run({"strategies": 3, "max_rescues": 0}), {}))
        self.assertFalse(rescue.available(run(None), {}), "a run planned before rescues existed never rescues")
        self.assertTrue(rescue.available(run(block, 49), {}))
        self.assertFalse(rescue.available(run(block, 50), {}), "the milestone limit stops the pursuit instead")

    def test_the_planner_diagnoses_unless_it_shares_the_workers_vendor_family(self):
        def run(planner, worker):
            return SimpleNamespace(spec={"pursue": {"planner": planner}, "milestones": [{"worker": worker}]},
                                   route=lambda state, step, role: {"planner": planner, "judge": {"kind": "judge"}}[role])
        self.assertEqual(rescue.diagnoser(run({"kind": "claude"}, {"kind": "codex"}), {})[0], "planner")
        self.assertEqual(rescue.diagnoser(run({"kind": "claude"}, {"kind": "claude", "model": "x"}), {}), ("judge", {"kind": "judge"}))
        self.assertEqual(rescue.diagnoser(run({"kind": "deepseek"}, {"kind": "claude"}), {})[0], "planner")
        self.assertEqual(rescue.diagnoser(run({"kind": "command", "argv": ["a"]}, {"kind": "command", "argv": ["b"]}), {})[0], "judge")

    def test_rescues_are_read_from_the_events(self):
        def event(kind, **details):
            return {"kind": kind, "details": details}
        strategies = [{"name": "Alpha", "approach": "a"}, {"name": "Beta", "approach": "b"}]
        rows = [{"index": 1, "name": "Alpha", "score": 2.0, "incumbent": 0.0, "qualified": True}, {"index": 2, "name": "Beta", "score": 0.0, "incumbent": 2.0, "qualified": False}]
        events = [event("goal_judged", wave=2, score=55, commit="c2"),
                  event("rescue_started", wave=2, best_score=60, best_commit="c1", stale_judgments=1),
                  event("rescue_diagnosed", wave=2, diagnosis="Stuck", diagnoser={"kind": "claude"}, strategies=strategies),
                  event("rescue_candidate_built", index=1, name="Alpha", approach="a", worker={"kind": "codex"}, passed=True, commit="x", seconds=3.5),
                  event("rescue_candidate_failed", index=2, name="Beta", approach="b", worker={"kind": "codex"}, passed=False, reason="exit 2\nbad", seconds=1),
                  event("rescue_compared", invalid=True), event("rescue_compared"), event("rescue_compared"),
                  event("rescue_chosen", winner=1, name="Alpha", scores=rows, position_bias_pairs=[[0, 2]]),
                  event("goal_judged", wave=3, score=71, commit="c3"), event("goal_judged", wave=4, score=99, commit="c4"),
                  event("rescue_started", wave=4, best_score=71, best_commit="c3", stale_judgments=1),
                  event("rescue_skipped", reason="unreadable")]
        first, second = rescue.collect(events)
        self.assertEqual((first["status"], first["winner"], first["winner_name"], first["next_score"], first["judge_calls"], first["invalid_comparisons"]), ("chosen", 1, "Alpha", 71, 3, 1))
        self.assertEqual([(r["index"], r["passed"], r["score"], r["winner"], r["reason"]) for r in first["strategies"]], [(1, True, 2.0, True, None), (2, False, 0.0, False, "exit 2\nbad")])
        self.assertEqual((second["status"], second["reason"], second["next_score"], second["strategies"]), ("skipped", "unreadable", None, []))
        self.assertEqual(rescue.summary(events), {"attempted": 2, "won": 1, "failed": 0, "skipped": 1, "outcome": "skipped", "winner": None, "score_before": 71, "score_after": None,
                                                   "rescues": [{"wave": 2, "outcome": "won", "winner": "Alpha", "score_before": 60, "score_after": 71},
                                                               {"wave": 4, "outcome": "skipped", "winner": None, "score_before": 71, "score_after": None}]})
        self.assertIsNone(rescue.summary([event("goal_judged", wave=1, score=1, commit="c")]))
        lines = "\n".join(rescue.format_lines(first))
        for expected in ("Plateau rescue at best 60/100 - strategy Alpha chosen (3 judge calls)", "Diagnosis: Stuck", "1. Alpha [codex] passed, score 2 - WINNER",
                         "2. Beta [codex] failed: exit 2", "Position bias in pairs: best build vs 2", "The next judgment scored 71/100"):
            self.assertIn(expected, lines)

    def test_the_diagnosis_mode_is_read_only_and_structured(self):
        argv, _ = adapter({"kind": "claude"}, "diagnosis")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), DIAGNOSIS_SCHEMA)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(adapter({"kind": "codex"}, "diagnosis")[0][2], "read-only")
        self.assertIn("plan", adapter({"kind": "gemini"}, "diagnosis")[0])
        argv, _ = adapter({"kind": "codex"}, "diagnosis", images=["a.png", "b.png"])
        self.assertEqual([argv[i + 1] for i, v in enumerate(argv) if v == "--image"], ["a.png", "b.png"])


if __name__ == "__main__":
    unittest.main()
