"""Design-direction tournament: divergent builds of milestone 1, pairwise judging, and promotion of the winner."""

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

from nightweaver import explore
from nightweaver.cli import main
from nightweaver.engine import Run, fork_run, git
from nightweaver.providers import ProviderUnavailable, adapter, invoke
from nightweaver.readiness import diagnose
from nightweaver.reports import metrics
from nightweaver.spec import anchor_provider_paths, validate

BUILD = str(Path(__file__).with_name("explore_fixture.py").resolve())
SCRIPT = str(Path(__file__).with_name("pursuit_fixture.py").resolve())
PAST = "2020-01-01T00:00:00+00:00"
NAMES = ["Brutalist", "Soft Pastel", "Neon Terminal"]
FAST = {"initial_seconds": 1, "max_seconds": 1}


def directions(*names, **extra):
    return {"directions": [{"name": name, "brief": f"A bold {name} look with its own palette and type."} for name in names or NAMES], **extra}


def plan(*titles):
    return {"milestones": [{"title": title, "task": f"Do {title}"} for title in titles]}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ExploreTests(unittest.TestCase):
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

    def table(self, order=None, name="table", **extra):
        return {"kind": "command", "argv": [sys.executable, BUILD, "judge", str(self.script(name, {"order": order or ["Soft Pastel", "Neon Terminal", "Brutalist"], **extra}))]}

    def builder(self, tag="zq-builder", failing="-", log="worker"):
        return {"kind": "command", "argv": [sys.executable, BUILD, "build", str(self.root / f"{log}.prompts"), failing, tag]}

    def prompts(self, name):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def workers(self, name="worker"):
        return self.prompts(name) if self.root.joinpath(f"{name}.prompts").exists() else []

    def explored(self, *, answers=None, judge=None, explore=None, planner=None, milestones=2, worker=None, **extra):
        """A pursuing run whose first milestone starts with the tournament. One judge route serves comparisons and the goal judgment."""
        block = {"judge": judge or self.table(), "planner": planner or self.sequence("planner", [answers or directions(), plan("Next improvement")]),
                 "explore": explore if explore is not None else {}}
        steps = [{"title": "Prepare fixture", "task": "Set the value"}, {"title": "Second milestone", "task": "Extend it"}][:milestones]
        return self.configured((worker or self.builder())["argv"], pursue=block, milestones=steps, **extra)

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def kinds(self, run):
        return [e["kind"] for e in self.store.events(run, limit=None)]

    def show(self, ref, name):
        return git(self.repo, "show", f"{ref}:{name}")

    def run_id_ref(self, run, suffix=""):
        return f"refs/heads/nightweaver/{run}{suffix}"

    def test_three_directions_are_ranked_and_the_winner_is_promoted_as_milestone_one(self):
        run = self.explored()
        self.assertEqual(Run(self.store, run).run(), "done")
        planned = self.events(run, "direction_planned")
        self.assertEqual([(p["index"], p["name"]) for p in planned], list(enumerate(NAMES, 1)))
        built = self.events(run, "direction_built")
        self.assertEqual([b["index"] for b in built], [1, 2, 3])
        self.assertTrue(all(b["passed"] and b["commit"] and b["screenshots"] == [] and b["seconds"] >= 0 for b in built))
        compared = self.events(run, "direction_compared")
        self.assertEqual([tuple(c["order"]) for c in compared], [(1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2)])
        self.assertEqual({tuple(c["pair"]) for c in compared}, {(1, 2), (1, 3), (2, 3)})
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["winner"], chosen["name"], chosen["chosen_by"], chosen["mode"], chosen["judged"]), (2, "Soft Pastel", "judge", "pairwise", True))
        self.assertEqual([(r["index"], r["score"]) for r in chosen["ranking"]], [(2, 4.0), (3, 2.0), (1, 0.0)])
        self.assertEqual((chosen["position_bias_pairs"], chosen["comparisons"], chosen["invalid_comparisons"]), ([], 6, 0))
        # The winner was promoted through the normal path: milestone 1 is its commit, milestone 2 builds on it.
        accepted = self.events(run, "milestone_accepted")
        self.assertEqual((accepted[0]["step"], accepted[0]["commit"]), (1, built[1]["commit"]))
        self.assertEqual(git(self.repo, "rev-parse", f"{accepted[1]['commit']}^"), built[1]["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), accepted[1]["commit"])
        self.assertEqual(accepted[0]["verified_checks"], ["read fixture value"])
        for index, name in enumerate(NAMES, 1):
            self.assertEqual(self.show(f"nightweaver/{run}-direction-{index:02d}", "design.txt"), name)
            self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}-direction-{index:02d}"), built[index - 1]["commit"])
        self.assertEqual(self.show(f"nightweaver/{run}", "lineage.txt").splitlines(), ["Prepare fixture", "Second milestone"])
        self.assertEqual(self.show(f"nightweaver/{run}", "design.txt"), "Soft Pastel")
        # Prompts: each builder got the normal milestone prompt plus its direction; the judge got both briefs, in both orders.
        workers = self.workers()
        self.assertEqual(len(workers), 4)
        for name, prompt in zip(NAMES, workers):
            self.assertIn("Milestone 1/2: Prepare fixture", prompt)
            self.assertIn(f"Design direction {name}: A bold {name} look", prompt)
            self.assertIn("Make this direction unmistakable; do not hedge toward a generic look.", prompt)
        self.assertNotIn("Design direction", workers[3])
        pair = self.prompts("table")[0]
        for text in ("Candidate A: design direction \"Brutalist\"", "Candidate B: design direction \"Soft Pastel\"", "A bold Brutalist look",
                     "A bold Soft Pastel look", "demanding owner", "Judge only what you can see in the images and source", "none were produced"):
            self.assertIn(text, pair)
        self.assertIn('Candidate A: design direction "Soft Pastel"', self.prompts("table")[1])
        self.assertNotIn("zq-builder", pair, "the builder is never named to the judge")
        directions_prompt = self.prompts("planner")[0]
        for text in ("exactly 3 genuinely divergent", "palette, typography, composition and motion", "Milestone 1: Prepare fixture"):
            self.assertIn(text, directions_prompt)
        # Housekeeping: worktrees are gone, evidence and refs stay, the run did not touch the checkout.
        self.assertEqual(list((self.store.directory(run) / "worktrees").glob("explore-*")), [])
        for index in (1, 2, 3):
            self.assertTrue((self.store.directory(run) / "evidence" / "explore" / f"{index:02d}" / "checks.json").is_file())
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["explored"], state["step"], state["judge_route"]), ("done", True, 2, 0))
        self.assertEqual(state["exploration"]["winner"], 2)
        counts = {}
        for call in self.events(run, "provider_call_finished"):
            counts[call["role"]] = counts.get(call["role"], 0) + 1
        self.assertEqual(counts, {"planner": 1, "worker": 4, "judge": 7})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)
        # Only a run's first milestone is explored: a fork from a later checkpoint is not.
        forked = fork_run(self.store, run, 1)
        self.assertNotIn("explore", self.store.get(forked)["spec"]["pursue"])
        self.assertIn("explore", self.store.get(fork_run(self.store, run, 0))["spec"]["pursue"])

    def test_position_bias_is_recorded_when_the_two_orders_disagree(self):
        run = self.explored(judge=self.table(always="A"))
        self.assertEqual(Run(self.store, run).run(), "done")
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual(chosen["position_bias_pairs"], [[1, 2], [1, 3], [2, 3]])
        self.assertEqual([(r["index"], r["score"]) for r in chosen["ranking"]], [(1, 2.0), (2, 2.0), (3, 2.0)], "ties fall back to the lower index")
        self.assertEqual((chosen["winner"], chosen["chosen_by"]), (1, "judge"))
        text = self.cli("pursuit", run, raw=True)
        self.assertIn("Position bias in pairs: 1 vs 2, 1 vs 3, 2 vs 3", text)
        self.assertEqual(self.cli("pursuit", run, "--json")["exploration"]["position_bias_pairs"], [[1, 2], [1, 3], [2, 3]])

    def test_scripted_answers_are_mapped_back_through_the_order(self):
        tie = {"winner": "tie", "reason": "Equal"}
        goal = {"verdict": "MET", "score": 90, "reason": "Fine", "gaps": [], "criteria": [{"criterion": "Looks right", "met": True, "note": "Observed"}]}
        # Calls in order: (1,2) (2,1) (1,3) (3,1) (2,3) (3,2); then the goal judgment.
        third = [{"winner": "B", "reason": "Third"}, {"winner": "A", "reason": "Third"}, {"winner": "B", "reason": "Third"}, {"winner": "A", "reason": "Third"}]
        run = self.explored(judge=self.sequence("judge", [tie, tie, *third, goal]))
        self.assertEqual(Run(self.store, run).run(), "done")
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual([(r["index"], r["score"]) for r in chosen["ranking"]], [(3, 4.0), (1, 1.0), (2, 1.0)])
        self.assertEqual((chosen["winner"], chosen["position_bias_pairs"]), (3, []))
        self.assertEqual([(c["order"], c["result"], c["winner"]) for c in self.events(run, "direction_compared")],
                         [([1, 2], "tie", None), ([2, 1], "tie", None), ([1, 3], "B", 3), ([3, 1], "A", 3), ([2, 3], "B", 3), ([3, 2], "A", 3)])
        self.assertEqual(self.events(run, "milestone_accepted")[0]["commit"], self.events(run, "direction_built")[2]["commit"])

    def test_single_mode_asks_for_one_ranking_of_every_candidate(self):
        run = self.explored(explore={"comparisons": "single"})
        self.assertEqual(Run(self.store, run).run(), "done")
        (compared,) = self.events(run, "direction_compared")
        self.assertEqual((compared["mode"], compared["order"], compared["ranking"], compared["winner"]), ("single", [1, 2, 3], [2, 3, 1], 2))
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["mode"], [(r["index"], r["score"]) for r in chosen["ranking"]]), ("single", [(2, 2.0), (3, 1.0), (1, 0.0)]))
        prompt = self.prompts("table")[0]
        self.assertIn("Rank all candidates from best to worst", prompt)
        for label, name in zip("ABC", NAMES):
            self.assertIn(f'Candidate {label}: design direction "{name}"', prompt)
        self.assertEqual(len(self.prompts("table")), 2, "one ranked call and the goal judgment")

    def test_a_single_passing_direction_wins_without_any_judge_call(self):
        run = self.explored(worker=self.builder(failing="Brutalist,Neon Terminal"))
        self.assertEqual(Run(self.store, run).run(), "done")
        failed = self.events(run, "direction_failed")
        self.assertEqual([f["index"] for f in failed], [1, 3])
        self.assertTrue(all("read fixture value exited 2" in f["reason"] and f["failed_checks"] == ["read fixture value"] and f["commit"] is None for f in failed))
        self.assertEqual([b["index"] for b in self.events(run, "direction_built")], [2])
        self.assertEqual(self.events(run, "direction_compared"), [])
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["winner"], chosen["judged"], chosen["passed"], chosen["directions"]), (2, False, 1, 3))
        self.assertEqual([r["index"] for r in chosen["ranking"]], [2])
        self.assertEqual(len(self.prompts("table")), 1, "only the goal judgment reached the judge")
        self.assertTrue(self.prompts("table")[0].startswith("Independently judge whether this software goal"))
        self.assertEqual(self.show(f"nightweaver/{run}", "design.txt"), "Soft Pastel")
        refs = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/nightweaver/")
        self.assertEqual([r for r in refs.splitlines() if "-direction-" in r], [f"refs/heads/nightweaver/{run}-direction-02"])

    def test_no_passing_direction_continues_with_the_normal_milestone_flow(self):
        run = self.explored(explore={"workers": [self.builder("bad", failing=",".join(NAMES), log="explorer")]})
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([f["index"] for f in self.events(run, "direction_failed")], [1, 2, 3])
        (failed,) = self.events(run, "exploration_failed")
        self.assertEqual([d["name"] for d in failed["directions"]], NAMES)
        for kind in ("direction_built", "direction_compared", "direction_chosen"):
            self.assertEqual(self.events(run, kind), [], kind)
        self.assertEqual(len(self.workers("explorer")), 3)
        normal = self.workers()
        self.assertEqual(len(normal), 2)
        self.assertNotIn("Design direction", normal[0])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["explored"], state["last_error"]), ("done", True, ""))
        accepted = self.events(run, "milestone_accepted")
        self.assertEqual(self.show(accepted[0]["commit"], "lineage.txt").splitlines(), ["Prepare fixture"])
        self.assertEqual(git(self.repo, "for-each-ref", "refs/heads/nightweaver/", "--format=%(refname)").count("-direction-"), 0)
        self.assertEqual(self.events(run, "attempt_rejected"), [])
        self.assertEqual(len(self.prompts("table")), 1)
        self.assertEqual(self.cli("pursuit", run, "--json")["exploration"]["status"], "failed")

    def test_invalid_directions_are_retried_once_then_the_tournament_is_skipped(self):
        run = self.explored(planner=self.sequence("planner", ["this is not json", {"directions": []}, plan("Next improvement")]))
        self.assertEqual(Run(self.store, run).run(), "done")
        (invalid,) = self.events(run, "directions_invalid")
        self.assertEqual(invalid["attempt"], 1)
        (skipped,) = self.events(run, "exploration_skipped")
        self.assertIn("attempt 2 of 2", skipped["reason"])
        self.assertIn("exactly 3 directions", skipped["reason"])
        self.assertIn("Your previous answer was rejected", self.prompts("planner")[1])
        self.assertEqual(self.events(run, "direction_planned") + self.events(run, "direction_built"), [])
        workers = self.workers()
        self.assertEqual(len(workers), 2)
        self.assertTrue(all("Design direction" not in prompt for prompt in workers))
        self.assertEqual(self.store.get(run)["state"]["explored"], True)
        self.assertEqual(self.list_explore_worktrees(run), [])
        self.assertEqual(self.cli("pursuit", run, "--json")["exploration"]["status"], "skipped")

    def test_one_invalid_directions_answer_is_retried(self):
        run = self.explored(planner=self.sequence("planner", ['{"directions": [{"name": "Only one", "brief": "x"}]}', directions()]))
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.events(run, "directions_invalid")), 1)
        self.assertEqual(len(self.events(run, "direction_planned")), 3)
        self.assertEqual(self.events(run, "exploration_skipped"), [])

    def test_prose_around_the_directions_is_tolerated(self):
        text = "Here are my directions:\n```json\n" + json.dumps(directions(extra="ignored")) + "\n```\nHope that helps."
        run = self.explored(planner=self.sequence("planner", [text, plan("Next improvement")]))
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([p["name"] for p in self.events(run, "direction_planned")], NAMES)
        self.assertEqual(self.events(run, "directions_invalid"), [])

    def test_workers_rotate_and_the_brief_reaches_the_planner(self):
        run = self.explored(explore={"workers": [self.builder("alpha", log="alpha"), self.builder("beta", log="beta")],
                                     "brief": "Differ by decade of origin: 1970s, 1990s, 2020s."})
        self.assertEqual(Run(self.store, run).run(), "done")
        built = {b["index"]: b for b in self.events(run, "direction_built")}
        for index, tag in ((1, "alpha"), (2, "beta"), (3, "alpha")):
            self.assertIn(f'data-built-by="{tag}"', self.show(built[index]["ref"], "index.html"))
        self.assertEqual((len(self.workers("alpha")), len(self.workers("beta"))), (2, 1))
        self.assertIn("Differ by decade of origin", self.prompts("planner")[0])
        self.assertEqual(self.events(run, "exploration_started")[0]["brief"], "Differ by decade of origin: 1970s, 1990s, 2020s.")

    def test_screenshots_reach_the_judge_under_their_own_candidate(self):
        gates = [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20},
                 {"name": "screenshot", "argv": [sys.executable, SCRIPT, "shot"], "timeout": 20, "artifacts": []}]
        run = self.explored(gates=gates, explore={"directions": 2}, answers=directions("Brutalist", "Soft Pastel"), milestones=1,
                            judge=self.table(["Soft Pastel", "Brutalist"]))
        self.assertEqual(Run(self.store, run).run(), "done")
        built = {b["index"]: b for b in self.events(run, "direction_built")}
        shots = {i: b["screenshots"] for i, b in built.items()}
        self.assertTrue(all(len(v) == 1 and v[0].endswith("screen.png") for v in shots.values()))
        folder = self.store.directory(run) / "evidence" / "explore"
        self.assertEqual({i: Path(v[0]).parent for i, v in shots.items()}, {1: folder / "01", 2: folder / "02"})
        first, second = self.prompts("table")[:2]
        for prompt, order in ((first, (1, 2)), (second, (2, 1))):
            a, b = (prompt.index(f"Candidate {x}:") for x in "AB")
            self.assertTrue(a < prompt.index(shots[order[0]][0]) < b < prompt.index(shots[order[1]][0]), "each image is listed under its own candidate")
            self.assertIn("OPEN each image file", prompt)
        finished = self.events(run, "gate_finished")
        self.assertEqual({(g["explore"], g["gate_index"]) for g in finished}, {(1, 1), (1, 2), (2, 1), (2, 2)})
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")

    def test_stability_runs_are_sampled_per_direction_and_verified_at_promotion(self):
        gates = [{"name": "read fixture value", "argv": fixtures.fixture("gate"), "timeout": 20, "stability_runs": 2}]
        run = self.explored(gates=gates, explore={"directions": 2}, answers=directions("Brutalist", "Soft Pastel"), milestones=1,
                            judge=self.table(["Soft Pastel", "Brutalist"]))
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual([(g["explore"], g["sample"]) for g in self.events(run, "gate_finished")], [(1, 1), (1, 2), (2, 1), (2, 2)])
        manifest = json.loads((self.store.directory(run) / "evidence" / "01" / "01" / "checks.json").read_text(encoding="utf-8"))
        self.assertEqual([r["sample"] for r in manifest], [1, 2])
        self.assertEqual(self.events(run, "milestone_accepted")[0]["commit"], self.events(run, "direction_built")[1]["commit"])

    def test_owner_choice_overrides_only_within_the_top_two(self):
        run = self.explored()
        with patch("nightweaver.explore.owner_choice", return_value=3) as hook:
            self.assertEqual(Run(self.store, run).run(), "done")
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["winner"], chosen["chosen_by"]), (3, "owner"))
        self.assertEqual([r["index"] for r in chosen["ranking"]], [2, 3, 1])
        self.assertEqual(self.events(run, "milestone_accepted")[0]["commit"], self.events(run, "direction_built")[2]["commit"])
        self.assertEqual(self.show(f"nightweaver/{run}", "design.txt"), "Neon Terminal")
        (ranking,) = [call.args[1] for call in hook.call_args_list]
        self.assertEqual([(r["index"], r["name"]) for r in ranking], [(2, "Soft Pastel"), (3, "Neon Terminal"), (1, "Brutalist")])
        self.assertIsNone(explore.owner_choice(None, []), "the hook leaves the choice to the judge until it is wired")

    def test_an_owner_pick_outside_the_top_two_is_ignored(self):
        run = self.explored()
        with patch("nightweaver.explore.owner_choice", return_value=1):
            self.assertEqual(Run(self.store, run).run(), "done")
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["chosen_by"], chosen["winner"]), ("judge", 2))

    def test_an_unreadable_comparison_is_retried_then_recorded_as_an_unscored_tie(self):
        goal = {"verdict": "MET", "score": 90, "reason": "Fine", "gaps": [], "criteria": [{"criterion": "Looks right", "met": True, "note": "Observed"}]}
        run = self.explored(judge=self.sequence("judge", ["no verdict here", "still none", {"winner": "A", "reason": "ok"}, goal]),
                            explore={"directions": 2}, answers=directions("Brutalist", "Soft Pastel"), milestones=1)
        self.assertEqual(Run(self.store, run).run(), "done")
        (invalid,) = self.events(run, "comparison_invalid")
        self.assertEqual(invalid["attempt"], 1)
        compared = self.events(run, "direction_compared")
        self.assertEqual([(c["order"], c.get("invalid"), c["winner"]) for c in compared], [([1, 2], True, None), ([2, 1], None, 2)])
        self.assertIn("Your previous answer was rejected", self.prompts("judge")[1])
        (chosen,) = self.events(run, "direction_chosen")
        self.assertEqual((chosen["comparisons"], chosen["invalid_comparisons"], chosen["winner"]), (1, 1, 2))
        self.assertEqual(chosen["position_bias_pairs"], [], "an unscored call cannot show bias")

    # ---------------------------------------------------------------- crash and resume

    def crash(self, run, target, **options):
        with patch(target, **options), self.assertRaises(SystemExit):
            Run(self.store, run).run()
        return self.store.get(run)["state"]

    def finish(self, run):
        self.assertEqual(Run(self.store, run).run(), "done")

    def stop_at_two(self):
        real = explore.worktree

        def stop(run, state, index, **kwargs):
            if index == 2:
                raise SystemExit
            return real(run, state, index, **kwargs)
        return stop

    def list_explore_worktrees(self, run):
        return sorted(p.name for p in (self.store.directory(run) / "worktrees").glob("explore-*"))

    def test_resume_in_the_directions_stage(self):
        run = self.explored()
        state = self.crash(run, "nightweaver.explore.plan_directions", side_effect=SystemExit)
        self.assertEqual((state["phase"], state["exploration"]["stage"], state["step"]), ("exploring", "directions", 0))
        self.assertEqual(self.list_explore_worktrees(run), [])
        self.finish(run)
        self.assertEqual(len(self.prompts("planner")), 1)
        self.assertEqual(len(self.events(run, "direction_built")), 3)

    def test_resume_between_directions_builds_each_direction_once(self):
        run = self.explored()
        state = self.crash(run, "nightweaver.explore.worktree", side_effect=self.stop_at_two())
        self.assertEqual((state["exploration"]["stage"], [b["index"] for b in state["exploration"]["builds"]]), ("build", [1]))
        self.assertEqual(len(self.workers()), 1)
        self.assertEqual(self.cli("db-check")["ok"], True)
        self.finish(run)
        self.assertEqual(len([p for p in self.workers() if "Design direction" in p]), 3)
        self.assertEqual([b["index"] for b in self.events(run, "direction_built")], [1, 2, 3])
        self.assertEqual(len(self.events(run, "direction_compared")), 6)
        self.assertEqual(len(self.prompts("planner")), 1)

    def test_resume_after_a_worker_finished_checks_it_without_rebuilding(self):
        run = self.explored()
        real = explore.gate

        def stop_once(run_, state, step, path, snapshot, out, index, number, check):
            if index == 2:
                raise SystemExit
            return real(run_, state, step, path, snapshot, out, index, number, check)

        state = self.crash(run, "nightweaver.explore.gate", side_effect=stop_once)
        self.assertEqual([b["index"] for b in state["exploration"]["builds"]], [1])
        self.assertEqual(state["exploration"]["current"]["index"], 2)
        self.assertEqual(len(self.workers()), 2)
        self.finish(run)
        self.assertEqual(sum("Design direction Soft Pastel" in p for p in self.workers()), 1, "the finished worker was not asked again")
        self.assertEqual([b["index"] for b in self.events(run, "direction_built")], [1, 2, 3])
        self.assertEqual(self.show(f"nightweaver/{run}-direction-02", "design.txt"), "Soft Pastel")

    def test_resume_mid_tournament_repeats_no_comparison(self):
        run = self.explored()
        real, calls = explore.comparison_prompt, []

        def stop_third(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise SystemExit
            return real(*args, **kwargs)

        state = self.crash(run, "nightweaver.explore.comparison_prompt", side_effect=stop_third)
        self.assertEqual((state["exploration"]["stage"], len(state["exploration"]["comparisons"])), ("tournament", 2))
        self.assertEqual(len(self.prompts("table")), 2)
        self.finish(run)
        self.assertEqual(len(self.prompts("table")), 7, "six comparisons and the goal judgment, none repeated")
        self.assertEqual([tuple(c["order"]) for c in self.events(run, "direction_compared")], [(1, 2), (2, 1), (1, 3), (3, 1), (2, 3), (3, 2)])
        self.assertEqual(len(self.events(run, "direction_built")), 3)
        self.assertEqual(self.events(run, "direction_chosen")[0]["winner"], 2)

    def test_resume_after_the_choice_promotes_the_winner(self):
        run = self.explored()
        state = self.crash(run, "nightweaver.engine.Run.promote", side_effect=SystemExit)
        self.assertEqual((state["phase"], state["explored"], state["attempt"], state["attempts_used"]), ("promoting", True, 1, 0))
        built = self.events(run, "direction_built")[1]
        self.assertEqual((state["candidate"], state["candidate_tree"], state["checks_sha256"]), (built["commit"], built["tree"], built["checks_sha256"]))
        evidence = self.store.directory(run) / "evidence"
        self.assertEqual((evidence / "01" / "01" / "checks.json").read_bytes(), (evidence / "explore" / "02" / "checks.json").read_bytes())
        self.assertEqual(self.list_explore_worktrees(run), [])
        self.assertEqual(git(self.repo, "branch", "--list", f"nightweaver/{run}"), "", "the result branch moves only at promotion")
        self.finish(run)
        self.assertEqual(self.events(run, "milestone_accepted")[0]["commit"], built["commit"])

    def test_promotion_refuses_a_winner_whose_saved_evidence_changed(self):
        run = self.explored()
        self.crash(run, "nightweaver.engine.Run.promote", side_effect=SystemExit)
        log = next((self.store.directory(run) / "evidence" / "explore" / "02").rglob("gate.log"))
        log.write_text(log.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual(state["blocked_from"], "checking")
        self.assertIn("Saved gate evidence changed", state["last_error"])
        self.assertEqual(git(self.repo, "branch", "--list", f"nightweaver/{run}"), "")

    def test_promotion_keeps_the_result_branch_compare_and_swap(self):
        run = self.explored()
        other = git(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "someone else")
        git(self.repo, "update-ref", f"refs/heads/nightweaver/{run}", other)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual(state["blocked_from"], "promoting")
        self.assertIn("result branch moved outside this run", state["last_error"])
        self.assertEqual(git(self.repo, "rev-parse", f"nightweaver/{run}"), other)
        self.assertEqual(len(self.events(run, "direction_built")), 3)
        self.assertEqual(self.events(run, "milestone_accepted"), [])

    def test_operator_feedback_during_the_tournament_sends_the_choice_back_to_the_normal_flow(self):
        run = self.explored()
        self.crash(run, "nightweaver.engine.Run.promote", side_effect=SystemExit)
        self.store.feedback(run, "Make the hero bolder")
        self.finish(run)
        (rejected,) = self.events(run, "attempt_rejected")
        self.assertEqual(rejected["phase"], "feedback")
        self.assertIn("Make the hero bolder", self.workers()[3])
        self.assertNotIn("Design direction", self.workers()[3])
        self.assertNotEqual(self.events(run, "milestone_accepted")[0]["commit"], self.events(run, "direction_built")[1]["commit"])

    # ---------------------------------------------------------------- providers and parking

    def failing(self, message, roles, kind="usage_limit", config=None):
        real, seen = invoke, []

        def route(conf, mode, *args, **kwargs):
            if mode in roles and mode not in seen and (config is None or conf["argv"][-1].endswith(config)):
                seen.append(mode)
                raise ProviderUnavailable(message, kind)
            return real(conf, mode, *args, **kwargs)
        return route

    def clock(self, run, seen):
        def sleeper(_seconds):
            seen.append(self.store.get(run)["state"])
            self.store.transition(run, "fixture_clock", {}, retry_at=PAST)
        return patch("nightweaver.engine.time", SimpleNamespace(sleep=sleeper))

    def test_usage_limits_park_every_exploration_stage_and_the_run_resumes(self):
        run = self.explored()
        seen = []
        flaky = self.failing("You've hit your limit · resets in 2 hours", ("directions", "worker", "comparison"))
        with patch("nightweaver.engine.invoke", side_effect=flaky), self.clock(run, seen):
            self.assertEqual(Run(self.store, run).run(), "done")
        parked = self.events(run, "provider_parked")
        self.assertEqual([(p["role"], p["park"], p["parsed"]) for p in parked], [("planner", 1, True), ("worker", 1, True), ("judge", 1, True)])
        self.assertEqual([(s["phase"], s.get("attempts_used", 0), s["parks"]) for s in seen], [("exploring", 0, 1)] * 3)
        self.assertEqual([s["exploration"]["stage"] for s in seen], ["directions", "build", "tournament"])
        self.assertEqual(self.events(run, "provider_unavailable"), [])
        self.assertEqual(len(self.events(run, "direction_built")), 3)
        self.assertEqual(len(self.workers()), 4)
        self.assertEqual(self.store.get(run)["state"]["parks"], 0)

    def test_an_exploration_worker_has_no_fallback_route(self):
        run = self.explored(worker_fallbacks=[self.builder("fallback", log="fallback")])
        flaky = self.failing("The Claude login expired", ("worker",), "authentication")
        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual((state["blocked_from"], state["provider_issue"]["role"], state["exploration"]["stage"]), ("exploring", "worker", "build"))
        self.assertEqual(self.events(run, "provider_fallback"), [])
        self.assertEqual(self.workers("fallback"), [])
        self.assertEqual(metrics(self.store, run)["outcome"], "provider_blocked")
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        self.assertEqual(len(self.events(run, "direction_built")), 3)

    def test_the_judge_falls_back_during_the_tournament(self):
        run = self.explored(judge=self.table(name="primary"))
        spec = self.store.get(run)["spec"]
        spec["pursue"]["judge_fallbacks"] = [self.table(name="table")]
        with self.store.connect() as db:
            db.execute("UPDATE runs SET spec=? WHERE id=?", (json.dumps(spec), run))
        flaky = self.failing("signed out", ("comparison",), "authentication", config="primary.json")
        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(), "done")
        (fallback,) = self.events(run, "provider_fallback")
        self.assertEqual(fallback["from"]["role"], "judge")
        self.assertEqual(len(self.prompts("table")), 6, "the fallback judged every comparison")
        self.assertEqual(len(self.prompts("primary")), 1, "the primary judges the goal once the tournament is over")
        self.assertEqual(self.store.get(run)["state"]["judge_route"], 0)

    # ---------------------------------------------------------------- operator views

    def test_operator_views_accept_the_exploring_phase_and_report_the_tournament(self):
        run = self.explored()
        self.crash(run, "nightweaver.explore.worktree", side_effect=self.stop_at_two())
        for command in (("status", run), ("list", "--compact"), ("metrics", run), ("handoff", run), ("budget", run), ("timeline", run),
                        ("diagnose-run", run), ("recovery", run), ("fleet",), ("call-ledger", run), ("attention",), ("db-check",)):
            self.cli(*command)
        self.assertEqual(self.cli("status", run)["state"]["phase"], "exploring")
        self.assertEqual(self.cli("pursuit", run, "--json")["exploration"]["status"], "in_progress")
        self.assertTrue(Path(self.cli("report", run)["report"]).is_file())
        with closing(sqlite3.connect(self.store.root / "runs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT json_extract(state,'$.phase') FROM runs WHERE id=?", (run,)).fetchone()[0], "exploring")
        self.finish(run)
        data = self.cli("pursuit", run, "--json")["exploration"]
        self.assertEqual((data["status"], data["winner"], data["chosen_by"], data["judge_calls"]), ("chosen", 2, "judge", 6))
        self.assertEqual([(d["index"], d["name"], d["passed"], d["score"], d["winner"]) for d in data["directions"]],
                         [(1, "Brutalist", True, 0.0, False), (2, "Soft Pastel", True, 4.0, True), (3, "Neon Terminal", True, 2.0, False)])
        text = self.cli("pursuit", run, raw=True)
        for expected in ("Design exploration - winner chosen by judge (6 judge calls)", "2. Soft Pastel [command] passed, score 4 - WINNER",
                         "1. Brutalist [command] passed, score 0"):
            self.assertIn(expected, text)
        html = Path(self.cli("report", run)["report"]).read_text(encoding="utf-8")
        for expected in ("Design exploration", "Soft Pastel", "Winner", "winner chosen by judge", "3 of 3 directions passed their checks"):
            self.assertIn(expected, html)
        measured = metrics(self.store, run)
        self.assertEqual(measured["exploration"], {"directions": 3, "built": 3, "failed": 0, "comparisons": 6, "winner": 2, "chosen_by": "judge",
                                                  "position_bias_pairs": 0, "outcome": "chosen"})
        self.assertEqual(measured["outcome"], "accepted")
        self.assertEqual(self.cli("audit-evidence", run)["status"], "verified")

    def test_doctor_checks_the_exploration_workers(self):
        run = self.explored(explore={"workers": [self.builder("alpha", log="alpha")]})
        spec = self.store.get(run)["spec"]
        roles = [role for route in diagnose(self.store, spec)["providers"] for role in route["roles"]]
        self.assertIn("exploration worker 1", roles)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ExploreCliTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = ExploreTests.cli

    def commit_tests(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_it(self): pass\n", encoding="utf-8")
        git(self.repo, "add", "tests/test_a.py")
        git(self.repo, "commit", "-m", "Declare a check")

    def test_oneshot_explore_flags_build_the_spec(self):
        self.commit_tests()
        raw = self.cli("oneshot", self.repo, "Build a beautiful page", "--preview", "--worker", "codex", "--model", "gpt-x", "--effort", "high",
                       "--profile-dir", self.root, "--pursue", "--explore", "3", "--explore-workers", "claude,codex, deepseek")
        self.assertEqual(raw["pursue"]["explore"], {"directions": 3, "workers": [
            {"kind": "claude"}, {"kind": "codex", "profile_dir": str(self.root.resolve()), "model": "gpt-x", "effort": "high"}, {"kind": "deepseek"}]})
        bare = self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--explore", "2")
        self.assertEqual(bare["pursue"]["explore"], {"directions": 2})
        self.assertNotIn("explore", self.cli("oneshot", self.repo, "Build", "--preview", "--pursue")["pursue"])
        stored = self.cli("oneshot", self.repo, "Build", "--direct", "--enqueue", "--pursue", "--explore", "2", "--explore-workers", "gemini,claude")
        from nightweaver.store import Store
        block = Store(self.store.root).get(stored["run_id"])["spec"]["pursue"]["explore"]
        self.assertEqual(block, {"directions": 2, "workers": [{"kind": "gemini"}, {"kind": "claude"}], "comparisons": "pairwise"})
        defaulted = self.cli("oneshot", self.repo, "Build", "--direct", "--enqueue", "--pursue", "--explore", "4")
        block = Store(self.store.root).get(defaulted["run_id"])["spec"]["pursue"]["explore"]
        self.assertEqual((block["directions"], block["workers"][0]["kind"], len(block["workers"])), (4, "codex", 1), "the milestone worker builds by default")

    def test_oneshot_explore_flag_misuse_is_refused(self):
        self.commit_tests()
        for flags in (["--explore", "3"], ["--explore-workers", "claude"], ["--pursue", "--explore-workers", "claude"],
                      ["--pursue", "--explore", "2", "--explore-workers", "ollama"], ["--pursue", "--explore", "2", "--explore-workers", "claude,,codex"]):
            with self.subTest(flags), self.assertRaises(SystemExit):
                with redirect_stderr(StringIO()):
                    self.cli("oneshot", self.repo, "Build", "--preview", *flags)
        for count in ("1", "5", "0"):
            self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--explore", count, code=1)
        self.cli("oneshot", self.repo, "Build", "--preview", "--pursue", "--explore", "2", "--explore-workers", "codex,codex,codex,codex,codex", code=1)


class ExploreSpecTests(unittest.TestCase):
    def raw(self, **explore):
        return {"repository": str(Path(__file__).resolve().parents[1]), "title": "T", "objective": "O", "worker": {"kind": "claude", "model": "m"},
                "gates": [{"name": "g", "argv": ["x"]}], "milestones": [{"title": "M", "task": "T"}], "acceptance": ["Beautiful"],
                "pursue": {"judge": {"kind": "codex"}, "explore": explore}}

    def test_defaults_are_filled_from_the_first_milestone(self):
        block = validate(self.raw())["pursue"]["explore"]
        self.assertEqual(block, {"directions": 3, "workers": [{"kind": "claude", "model": "m"}], "comparisons": "pairwise"})
        raw = self.raw()
        raw["milestones"] = [{"title": "M", "task": "T", "worker": {"kind": "gemini"}}, {"title": "N", "task": "U"}]
        self.assertEqual(validate(raw)["pursue"]["explore"]["workers"], [{"kind": "gemini"}])
        full = validate(self.raw(directions=4, workers=[{"kind": "codex", "effort": "high"}, {"kind": "command", "argv": ["x"]}], brief=" Differ in era ",
                                 comparisons="single"))["pursue"]["explore"]
        self.assertEqual(full, {"directions": 4, "workers": [{"kind": "codex", "effort": "high"}, {"kind": "command", "argv": ["x"]}],
                                "brief": "Differ in era", "comparisons": "single"})
        plain = self.raw()
        del plain["pursue"]["explore"]
        self.assertNotIn("explore", validate(plain)["pursue"])
        again = validate({**self.raw(), "pursue": {"judge": {"kind": "codex"}, "explore": validate(self.raw(directions=2))["pursue"]["explore"]}})
        self.assertEqual(again["pursue"]["explore"]["directions"], 2, "a normalized block validates again")

    def test_invalid_blocks_are_rejected(self):
        cases = {
            "unknown": ({"typo": 1}, "Unknown pursue.explore fields"), "low": ({"directions": 1}, "directions must be an integer between 2 and 4"),
            "high": ({"directions": 5}, "directions"), "bool": ({"directions": True}, "directions"), "text": ({"directions": "3"}, "directions"),
            "no workers": ({"workers": []}, "at least one worker"), "many workers": ({"workers": [{"kind": "codex"}] * 5}, "at most 4"),
            "wrong kind": ({"workers": [{"kind": "ollama"}]}, "Invalid explore worker adapter"),
            "effort": ({"workers": [{"kind": "codex", "effort": "huge"}]}, "Unsupported effort"),
            "workers type": ({"workers": "codex"}, "explore workers must be a list"),
            "brief": ({"brief": " "}, "explore brief"), "brief type": ({"brief": 3}, "explore brief"), "long brief": ({"brief": "x" * 4001}, "explore brief"),
            "comparisons": ({"comparisons": "bracket"}, "pairwise or single"),
        }
        for label, (block, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                validate(self.raw(**block))
        with self.assertRaisesRegex(ValueError, "explore must be an object"):
            validate({**self.raw(), "pursue": {"judge": {"kind": "codex"}, "explore": 3}})
        raw = self.raw()
        raw["explore"] = {}
        del raw["pursue"]
        with self.assertRaisesRegex(ValueError, "Unknown plan fields: explore"):
            validate(raw)

    def test_worker_paths_resolve_against_the_plan_folder(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = self.raw(workers=[{"kind": "claude", "profile_dir": "profiles/a"}])
            anchor_provider_paths(raw, folder)
            expected = str((folder / "profiles" / "a").resolve())
            self.assertEqual(raw["pursue"]["explore"]["workers"][0]["profile_dir"], expected)
            self.assertEqual(validate(raw, folder)["pursue"]["explore"]["workers"][0]["profile_dir"], expected)


class ExploreUnitTests(unittest.TestCase):
    def test_parse_directions_is_tolerant_of_prose_and_strict_about_the_set(self):
        good = json.dumps(directions("A one", "B two", "C three", note="x"))
        self.assertEqual([d["name"] for d in explore.parse_directions("Sure!\n```json\n" + good + "\n```\nDone.", 3)], ["A one", "B two", "C three"])
        cases = ((good, 2, "exactly 2"), ("no json", 3, "No JSON object"), ('{"directions": "x"}', 3, "exactly 3"),
                 (json.dumps(directions("Same", "same", "Other")), 3, "distinct"),
                 (json.dumps({"directions": [{"name": "a", "brief": "b"}, {"name": "c"}, {"name": "d", "brief": "e"}]}), 3, "brief"),
                 (json.dumps({"directions": ["a", "b", "c"]}), 3, "object"))
        for answer, count, message in cases:
            with self.subTest(answer[:30]), self.assertRaisesRegex(ValueError, message):
                explore.parse_directions(answer, count)
        long = explore.parse_directions(json.dumps({"directions": [{"name": "n" * 200, "brief": "b" * 5000}, {"name": "m", "brief": "c"}]}), 2)
        self.assertEqual((len(long[0]["name"]), len(long[0]["brief"])), (80, 3000))

    def test_parse_comparison_and_ranking(self):
        for answer, expected in (('{"winner":"A","reason":"r"}', "A"), ('{"winner":"Candidate B","reason":"r"}', "B"), ('{"winner":"tie"}', "TIE"),
                                 ('prose {"winner":"TIE","reason":"x"} prose', "TIE"), ('{"winner":" b "}', "B")):
            self.assertEqual(explore.parse_comparison(answer)[0], expected)
        for answer in ('{"winner":"C"}', '{"winner":3}', '{"verdict":"A"}', '{"winner":"A","reason":4}', ""):
            with self.subTest(answer), self.assertRaises(ValueError):
                explore.parse_comparison(answer)
        self.assertEqual(explore.parse_ranking('{"ranking":["B","Candidate A","C"],"reason":"r"}', ["A", "B", "C"]), (["B", "A", "C"], "r"))
        self.assertEqual(explore.parse_ranking('{"ranking":[{"candidate":"B"},{"candidate":"A"}]}', ["A", "B"])[0], ["B", "A"])
        for answer in ('{"ranking":["A","A","B"]}', '{"ranking":["A","B"]}', '{"ranking":"A"}', '{"ranking":["A","B","tie"]}'):
            with self.subTest(answer), self.assertRaises(ValueError):
                explore.parse_ranking(answer, ["A", "B", "C"])

    def test_rank_scores_wins_and_ties_and_flags_disagreement_between_orders(self):
        passed = [{"index": i, "name": f"D{i}", "failed_checks": []} for i in (1, 2, 3)]

        def call(first, second, winner):
            return {"pair": sorted([first, second]), "order": [first, second], "winner": winner, "result": "tie" if winner is None else "A"}

        ex = {"comparisons": [call(1, 2, None), call(2, 1, 1), call(1, 3, 3), call(3, 1, 3), call(2, 3, 2), call(3, 2, 3)]}
        ranking, bias = explore.rank(ex, passed)
        self.assertEqual([(r["index"], r["score"]) for r in ranking], [(3, 3.0), (1, 1.5), (2, 1.5)])
        self.assertEqual(bias, [[1, 2], [2, 3]], "a tie against a win, and two opposite wins, are both order-dependent")
        ex["comparisons"].append({"pair": [1, 2], "order": [1, 2], "winner": None, "result": "tie", "invalid": True})
        self.assertEqual(explore.rank(ex, passed)[0], ranking, "unscored calls do not count")
        mixed = [{**p, "failed_checks": ["x"] if p["index"] == 1 else []} for p in passed]
        flat, _ = explore.rank({"comparisons": []}, mixed)
        self.assertEqual([r["index"] for r in flat], [2, 3, 1], "equal scores prefer fewer failed checks, then the lower index")
        single, none = explore.rank({"comparisons": [{"mode": "single", "order": [1, 2, 3], "ranking": [3, 1, 2], "winner": 3}]}, passed)
        self.assertEqual(([(r["index"], r["score"]) for r in single], none), ([(3, 2.0), (1, 1.0), (2, 0.0)], []))


class ExploreProviderTests(unittest.TestCase):
    def test_comparison_modes_are_read_only_and_structured(self):
        from nightweaver.providers import COMPARISON_SCHEMA, DIRECTIONS_SCHEMA, RANKING_SCHEMA
        for mode, schema in (("directions", DIRECTIONS_SCHEMA), ("comparison", COMPARISON_SCHEMA), ("ranking", RANKING_SCHEMA)):
            argv, _ = adapter({"kind": "claude"}, mode)
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
            self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), schema)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(adapter({"kind": "codex"}, mode)[0][2], "read-only")
            self.assertIn("plan", adapter({"kind": "gemini"}, mode)[0])
        argv, _ = adapter({"kind": "codex"}, "comparison", images=["a.png", "b.png"])
        self.assertEqual([argv[i + 1] for i, v in enumerate(argv) if v == "--image"], ["a.png", "b.png"])


if __name__ == "__main__":
    unittest.main()
