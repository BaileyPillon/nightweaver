"""Checked OneShot intake and content-preservation gates on Windows."""

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.catalog import at, validate_contract
from nightweaver.engine import Run, git
from nightweaver.spec import read_plan, strict_json


@unittest.skipUnless(os.name == "nt", "Windows runner")
class ReleaseAcceptance(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def reply(self, steps):
        path = self.root / "response.json"
        path.write_text(json.dumps({"milestones": [{"title": x, "task": "Implement " + x} for x in steps]}))
        return path

    def test_fixed_policies_preview_and_offline_replay(self):
        steps = [
            {
                "title": "A",
                "task": "Follow original task",
                "paths": ["result.txt"],
                "gates": [{"name": "local", "argv": fixtures.fixture("gate")}],
            }
        ]
        plan = self.plan(milestones=steps, context_files=["result.txt"], protected_paths=["fixed.txt"])
        before = plan.read_bytes()
        with patch("nightweaver.cli.invoke", side_effect=AssertionError("unexpected model call")):
            preview = self.cli("goal", plan, "Deliver", "--prompt-only")
            self.assertIn("Pinned project context", preview["prompt"])
            for term in ("original", "local", "max_seconds", "fixed.txt"):
                self.assertIn(term, preview["prompt"])
            self.assertTrue(preview["fixed_milestones"])
            proposed = self.cli("goal", plan, "Deliver", "--plan-from", self.reply(["generated"]))
            step = proposed["milestones"][0]
            self.assertEqual(
                (step["title"], step["paths"], step["gates"]), ("A", steps[0]["paths"], steps[0]["gates"])
            )
            self.assertIn("Follow original task", step["task"])
            self.assertIn("Implement generated", step["task"])
            self.assertIn(
                "fixed milestone count",
                self.cli("goal", plan, "Deliver", "--plan-from", self.reply(["a", "b"]), code=1),
            )
            self.assertEqual(self.store.runs(), [])
            self.assertEqual(plan.read_bytes(), before)
            run = self.cli("goal", plan, "Deliver", "--plan-from", self.reply(["fix"]), "--start", "--wait")
            self.assertEqual(run["phase"], "done")
            self.assertEqual(len(self.store.events(run["run_id"], limit=None, kinds=["gate_finished"])), 2)

    def test_final_only_combines_all_planner_tasks(self):
        plan = self.plan(gates=[], final_gates=[{"name": "final", "argv": fixtures.fixture("gate")}])
        out = self.cli("goal", plan, "Deliver", "--plan-from", self.reply(["first", "second"]))
        self.assertEqual(len(out["milestones"]), 1)
        self.assertIn("Implement first", out["milestones"][0]["task"])
        self.assertIn("Implement second", out["milestones"][0]["task"])
        self.assertEqual(out["final_gates"][0]["name"], "final")

    def test_invalid_template_overrides_dependencies_and_ambiguous_json_precede_planning(self):
        with patch("nightweaver.cli.invoke", side_effect=AssertionError("unexpected paid call")):
            for kwargs in (
                {"budgets": {"max_seconds": -1}},
                {"reviewer": {}},
                {"advisory_ollama": {}},
                {"milestones": [{"title": "Bad", "task": "Fail", "worker": {}}]},
            ):
                self.assertTrue(self.cli("goal", self.plan(**kwargs), "task", code=1))
            self.assertIn(
                "not found",
                self.cli("goal", self.plan(), "task", "--enqueue", "--after", "000000000000", code=1),
            )
            path = self.plan()
            data = path.read_text().replace('"title": "Gallery"', '"title": "discard", "title": "Gallery"')
            path.write_text(data)
            for cmd in (("goal", path, "x"), ("add", path), ("preflight", path)):
                self.assertIn("Duplicate JSON", self.cli(*cmd, code=1))
        for text in ('{"x":NaN}', "[1e10000]"):
            with self.assertRaises(ValueError):
                strict_json(text)

    def test_same_commit_different_repository_during_planning(self):
        def replacement(*args):
            original = self.root / "original"
            self.repo.rename(original)
            git(original, "clone", "--local", str(original), str(self.repo))
            return 0, None, '{"milestones":[{"title":"ok","task":"ok"}]}'

        with patch("nightweaver.cli.invoke", side_effect=replacement):
            message = self.cli("goal", self.plan(), "verify", "--enqueue", code=1)
        self.assertIn("replaced during planning", message)
        self.assertEqual(self.store.runs(), [])

    def test_relative_route_paths_and_github_intake_preserve_scope(self):
        gate = {"name": "retained", "argv": fixtures.fixture("gate")}
        plan = self.plan(
            worker={"kind": "deepseek", "api_key_file": "keys/local.txt"},
            milestones=[{"title": "bounded", "task": "Original", "paths": ["result.txt"], "gates": [gate]}],
            reviewer={"kind": "claude", "profile_dir": "profiles/review"},
        )
        self.assertEqual(
            Path(read_plan(plan)["milestones"][0]["worker"]["api_key_file"]), self.root.resolve() / "keys/local.txt"
        )
        output = self.root / "elsewhere" / "issue.json"
        with (
            patch("nightweaver.intake.shutil.which", return_value="gh"),
            patch("nightweaver.intake.subprocess.run") as read,
        ):
            read.return_value.returncode = 0
            read.return_value.stdout = (
                b'{"number":7,"title":"New issue","body":"Need work","url":"https://example.invalid/7"}'
            )
            self.cli("issue", plan, "--repo", "sample/repo", "--number", 7, "--output", output)
        spec = read_plan(output)
        self.assertIn("Need work", spec["objective"])
        self.assertEqual(Path(spec["reviewer"]["profile_dir"]), self.root.resolve() / "profiles/review")
        self.assertEqual(spec["milestones"][0]["gates"][-1]["name"], "retained")
        self.assertEqual(spec["milestones"][0]["paths"], ["result.txt"])

    def test_context_integrity_restart_map_priority_and_diagnostics(self):
        for i in range(242):
            (self.repo / f"file{i:03}.txt").write_text("a")
        (self.repo / "zz.txt").write_text("target")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "map")
        plan = self.plan(
            context_files=["result.txt"],
            repository_map=True,
            milestones=[{"title": "Ready", "task": "Change", "paths": ["result.txt", "zz.txt"]}],
        )
        rid = self.cli("add", plan)["run_id"]
        packfile = self.store.directory(rid) / "context.json"
        original = packfile.read_bytes()
        pack = json.loads(original)
        self.assertIn("zz.txt", pack["repository_map"])
        self.assertEqual(len(pack["repository_map"]), 240)
        pack["files"][0]["content"] = "forged"
        packfile.write_text(json.dumps(pack))
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertEqual(self.cli("metrics", rid)["provider_calls"], 0)
        packfile.unlink()
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        self.assertEqual(packfile.read_bytes(), original)
        missing = self.cli("preflight", self.plan(context_files=["missing.txt"]), code=3)
        self.assertFalse(missing["ready"])
        self.assertTrue(any("Committed context" in m for m in missing["errors"]))

    def test_scope_lint_receipts_and_event_query_index(self):
        pin = hashlib.sha256((self.repo / "result.txt").read_bytes()).hexdigest()
        gate = {
            "name": "verify",
            "argv": fixtures.fixture("gate"),
            "pinned_inputs": [{"path": "result.txt", "sha256": pin}],
        }
        plan = self.plan(
            gates=[gate],
            protected_paths=["docs/private"],
            reviewer={"kind": "claude"},
            budgets={"max_calls": 1},
            milestones=[
                {"title": "A", "task": "Edit", "paths": ["docs"]},
                {"title": "B", "task": "Edit", "paths": ["docs/private"]},
            ],
        )
        scope = self.cli("scope", plan, "DOCS/guide.md", "docs/private/a.txt", "else.txt")["paths"]
        self.assertEqual([r["permitted_milestones"] for r in scope], [[1], [], []])
        self.assertTrue(scope[1]["protected"])
        lint = self.cli("plan-lint", plan, code=3)
        self.assertEqual(lint["minimum_calls_for_acceptance"], 4)
        self.assertEqual(
            {r["code"] for r in lint["findings"]}, {"insufficient_calls", "entire_scope_protected"}
        )
        plan = self.plan(
            gates=[gate],
            budgets={"max_seconds": 60},
            worker_fallbacks=[{"kind": "command", "argv": fixtures.fixture("good")}],
        )
        self.assertTrue(self.cli("plan-lint", plan)["feasible"])
        self.assertEqual(len(self.cli("plan-lint", plan, "--strict", code=3)["findings"]), 2)
        self.assertEqual(self.cli("preflight", plan)["verified_pins"], gate["pinned_inputs"])
        with self.store.connect() as db:
            query = db.execute(
                "EXPLAIN QUERY PLAN SELECT seq FROM events WHERE run_id=? AND kind=? ORDER BY seq",
                ("0", "gate_finished"),
            ).fetchall()
        self.assertTrue(any("events_for_kind" in str(r[3]) for r in query))

    def test_catalog_rules_compare_immutable_git_and_fail_closed(self):
        rows = [
            {"id": "one", "meta": {"source": "a"}, "title": "A", "links": ["two"]},
            {"id": "two", "meta": {"source": "b"}, "title": "B"},
        ]
        file = self.repo / "library.js"

        def commit(items, extra=""):
            file.write_text("window.LIB = " + json.dumps({"lib/list": items}) + ";" + extra, encoding="utf-8")
            git(self.repo, "add", "library.js")
            git(self.repo, "commit", "-qm", "library")
            return git(self.repo, "rev-parse", "HEAD")

        baseline = commit(rows)
        config = {
            "path": "library.js",
            "assignment": "window.LIB",
            "records": "/lib~1list",
            "fields": {"/title": "nonempty_string"},
            "preserve_fields": ["/meta/source"],
            "references": ["/links"],
            "min_records": 2,
        }
        gate = {"name": "catalog", "kind": "catalog", "catalog": config}
        plan = self.plan(expected_base=baseline, gates=[gate])
        self.assertTrue(self.cli("catalog-check", plan)["passed"])
        bad = [{**rows[0], "title": " ", "meta": {"source": "changed"}}, {**rows[1], "id": "new"}]
        altered = commit(bad)
        file.write_text("dirty copy")
        check = self.cli("catalog-check", plan, "--ref", altered, code=3)["checks"][0]
        self.assertEqual((check["candidate_records"], check["added_ids"], check["removed_ids"]), (2, 1, 1))
        self.assertEqual(
            check["violations"],
            {"missing_id": 1, "field_type": 1, "changed_field": 1, "dangling_reference": 1},
        )
        duplicate = commit([rows[0], rows[0]])
        self.assertIn(
            "Duplicate",
            self.cli("catalog-check", plan, "--ref", duplicate, code=3)["checks"][0]["diagnostic"],
        )
        invalid = commit(rows, "window.DO_NOT_RUN = true;")
        self.assertEqual(
            self.cli("catalog-check", plan, "--ref", invalid, code=3)["checks"][0]["violations"],
            {"invalid_catalog": 1},
        )
        for key, value in (("assignment", "window.run()"), ("records", "bad"), ("references", ["/bad~9"])):
            with self.assertRaises(ValueError):
                validate_contract({**config, key: value})
        self.assertEqual(at({"a~": [False]}, "/a~0/0"), False)

    def test_catalog_live_repair_replay_ci_and_worker_brief(self):
        file = self.repo / "rows.json"
        file.write_text('[{"id":"keep","v":1}]')
        git(self.repo, "add", "rows.json")
        git(self.repo, "commit", "-qm", "records")
        worker = {
            "kind": "command",
            "argv": [
                sys.executable,
                "-c",
                """from pathlib import Path
import sys
prompt=sys.stdin.read()
assert 'stability_runs' in prompt and 'Next milestone after acceptance: Second' in prompt
Path('rows.json').write_text('[{"id":"keep","v":1},{"id":"added","v":1}]' if 'missing_id' in prompt else '[{"id":"swap","v":1}]')
""",
            ],
        }
        gate = {
            "name": "verify records",
            "kind": "catalog",
            "catalog": {"path": "rows.json", "preserve_fields": ["/v"]},
            "stability_runs": 2,
        }
        plan = self.plan(
            worker=worker,
            gates=[gate],
            attempts=2,
            milestones=[
                {"title": "First", "task": "Add data"},
                {
                    "title": "Second",
                    "task": "Write result",
                    "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                },
            ],
        )
        run = self.cli("add", plan, "--start", "--wait")
        rid = run["run_id"]
        history = self.cli("catalog-history", rid)["observations"]
        self.assertEqual([x["passed"] for x in history], [False, False, True, True, True, True])
        self.assertEqual(history[0]["removed_ids"], 1)
        summary = self.root / "summary.md"
        self.cli("ci-summary", rid, "--output", summary)
        self.assertIn("4 / 6", summary.read_text())
        replay = self.cli("replay-checks", rid, "--gate", "verify records")
        self.assertTrue(replay["passed"])
        for obs in replay["gates"]:
            self.assertEqual(hashlib.sha256(Path(obs["log"]).read_bytes()).hexdigest(), obs["log_sha256"])
        self.assertEqual(self.cli("audit-evidence", rid)["status"], "verified")
