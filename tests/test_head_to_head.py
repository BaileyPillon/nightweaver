import json
from contextlib import closing
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from nightweaver.benchmark import paired_outcomes
from nightweaver.providers import invoke, ProviderUnavailable
from nightweaver.spec import provider, IMPLEMENTERS
from examples.agtx_contender import merged_result


class PairMeasurements(unittest.TestCase):
    def test_export_requires_finished_tasks_and_a_merged_product_diff(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            repo, db, output = root / "repo", root / "board.db", root / "merged.patch"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Test",
                                                "-c", "user.email=test@example.invalid", *args]).strip()
            (repo / "product.txt").write_text("baseline")
            git("add", ".")
            git("commit", "-qm", "baseline")
            base = git("rev-parse", "HEAD").decode()
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("CREATE TABLE tasks(id,status,agent)")
                conn.execute("INSERT INTO tasks VALUES('a','review','codex')")
            (repo / "product.txt").write_text("accepted product")
            (repo / "oneshot-state.md").write_text("orchestrator notes")
            git("add", ".")
            git("commit", "-qm", "merged fixture")
            self.assertFalse(merged_result(repo, db, base, output)["board_complete"])
            self.assertFalse(output.exists())
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE tasks SET status='done'")
            self.assertTrue(merged_result(repo, db, base, output)["board_complete"])
            self.assertIn(b"accepted product", output.read_bytes())
            self.assertNotIn(b"orchestrator notes", output.read_bytes())

    def test_quality_gap_and_speed_use_different_matched_denominators(self):
        rows = []
        for repeat, (left, right) in enumerate([
                ("accepted", "accepted"), ("accepted", "rejected"), ("provider_blocked", "accepted"),
                ("accepted", "unfinished")]):
            for name, outcome, seconds in (("A", left, 20), ("B", right, 80)):
                phase = {"accepted": "done", "unfinished": "working"}.get(outcome, "blocked")
                rows.append(dict(contender=name, task="fixture", baseline="sha", contract_sha256="checks", repeat=repeat,
                                 phase=phase, outcome=outcome, accepted=outcome == "accepted", elapsed_seconds=seconds))
        pair, = paired_outcomes(rows)
        self.assertEqual(pair["scored_pairs"], 2)
        self.assertEqual(pair["acceptance_gap_left_minus_right_pp"], 50)
        self.assertEqual(pair["timed_success_pairs"], 1)
        self.assertEqual(pair["median_right_seconds_over_left"], 4)
        self.assertEqual(pair["inconclusive_pairs"], 1)
        self.assertEqual(pair["unfinished_pairs"], 1)

    def test_environment_exit_is_opt_in_and_never_conflicts_with_timeout(self):
        with self.assertRaises(ValueError):
            provider({"kind": "command", "argv": ["tool"], "unavailable_exit_code": 124}, IMPLEMENTERS, "worker")
        if os.name != "nt":
            return
        with TemporaryDirectory() as folder:
            root = Path(folder)
            route = {"kind": "command", "argv": [sys.executable, "-c", "print('account unavailable');exit(78)"]}
            code, _, _ = invoke(route, "worker", root, root / "ordinary.log", "task", 20, lambda: False)
            self.assertEqual(code, 78)
            with self.assertRaisesRegex(ProviderUnavailable, "account unavailable"):
                invoke({**route, "unavailable_exit_code": 78}, "worker", root, root / "external.log", "task", 20, lambda: False)

    def test_generator_builds_requested_contenders_with_equal_limits(self):
        if os.name != "nt":
            self.skipTest("Windows comparison generator")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            skill = root / "SKILL.md"
            skill.write_text("fixture")
            out = root / "pilot"
            generator = Path(__file__).resolve().parents[1] / "examples/create_comparison.py"
            subprocess.run([sys.executable, str(generator), str(out), "--agtx-skill", str(skill), "--seconds", "420"],
                           check=True, capture_output=True)
            suite = json.loads((out / "suite.json").read_text())
            self.assertEqual([c["mode"] for c in suite["contenders"]], ["standalone", "standalone", "external"])
            self.assertIn("claude coordinator", suite["contenders"][2]["name"])
            plan = json.loads((out / "plan.json").read_text())
            self.assertEqual(plan["agent_timeout"], 420)
            self.assertIn("check.py", plan["protected_paths"])
