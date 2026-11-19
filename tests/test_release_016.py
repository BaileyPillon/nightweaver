"""Catalog acceptance on real Git revisions and the native OneShot controller."""

import json
import os
import sys
import unittest
from unittest.mock import patch

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.catalog import check_catalog, validate_contract, validate_receipt
from nightweaver.checks import evidence_folder, observed_pass, run_gate
from nightweaver.engine import git


class Contracts(unittest.TestCase):
    def test_impossible_rules_fail_at_intake(self):
        for policy in (
            {"key": ""},
            {"lengths": {"/p": {"min": 2, "max": 1}}},
            {"unique_by": [["/a", "/b"], ["/b", "/a"]]},
            {"unique_by": [[]]},
            {"required": ["/a", "/a"]},
            {"required_ids": [True]},
            {"choices": {"/v": [False]}},
            {"max_removed": False},
            {"unique_items": ["v"]},
            {"required_values": {"/v": []}},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_contract({"path": "rows.json", **policy})

    def test_receipt_cross_checks(self):
        good = {
            "schema": "nightweaver.catalog.v1",
            "passed": True,
            "tree": "t",
            "base": "b",
            "baseline_records": 10,
            "candidate_records": 10,
            "added_ids": 1,
            "removed_ids": 1,
            "changed_records": 1,
            "unchanged_records": 8,
            "violations": {},
            "examples": [],
            "omitted_examples": 0,
        }
        validate_receipt(good, "b", "t", 0)
        for updates in (
            {"violations": {"lost_items": 1}},
            {"candidate_records": True},
            {"removed_ids": -1},
            {"added_ids": 2},
            {"changed_records": 0},
            {"omitted_examples": 2},
            {"input_error": "wrong input"},
            {"tree": "other", "input_error": "other run"},
            {"examples": [{"reason": "unknown"}]},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_receipt({**good, **updates}, "b", "t", 0)
        validate_receipt(
            {**good, "input_error": "unavailable", "passed": False}, "b", "t", 2
        )

    def test_unavailable_candidate_object_is_unscored(self):
        cfg = validate_contract({"path": "rows.json"})
        with patch(
            "nightweaver.catalog.object_data",
            side_effect=[{}, OSError("cannot read object")],
        ):
            r = check_catalog(".", "base", "other", cfg)
        self.assertIn("cannot read object", r["input_error"])
        self.assertEqual(r["violations"], {})


@unittest.skipUnless(os.name == "nt", "Windows-native orchestration")
class CatalogRuns(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    plan = previous.OperatorRelease.plan
    cli = previous.OperatorRelease.cli

    def test_planner_auth_error_points_to_goal_intake(self):
        from nightweaver.providers import ProviderUnavailable

        with patch(
            "nightweaver.cli.invoke",
            side_effect=ProviderUnavailable("renew", "authentication"),
        ):
            result = self.cli("goal", self.plan(), "Review", code=1)
        self.assertIn("before run creation", result)
        self.assertIn("--profile-dir", result)
        self.assertNotIn("--retry", result)
        self.assertEqual(self.store.runs(), [])

    def commit(self, rows):
        (self.repo / "rows.json").write_text(
            rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8"
        )
        git(self.repo, "add", "rows.json")
        git(self.repo, "commit", "-qm", "Update catalog")
        return git(self.repo, "rev-parse", "HEAD")

    def configuration(self, **kwargs):
        return validate_contract({"path": "rows.json", **kwargs})

    def test_exact_numbers_and_integer_types(self):
        base = self.commit('[{"id":1,"v":1.00000000000000000000001,"n":10,"zero":0}]')
        changed = self.commit(
            '[{"id":1,"v":1.00000000000000000000002,"n":10.0,"zero":1e-400}]'
        )
        r = check_catalog(
            self.repo,
            base,
            changed,
            self.configuration(
                preserve_fields=["/v", "/n", "/zero"], fields={"/n": "integer"}
            ),
        )
        self.assertEqual(r["violations"], {"changed_field": 2})
        self.assertEqual({a["field"] for a in r["examples"]}, {"/v", "/zero"})
        self.assertEqual(r["changed_records"], 1)
        equal = self.commit(
            '[{"zero":0.000,"n":1e1,"v":1.000000000000000000000010,"id":1}]'
        )
        r = check_catalog(
            self.repo, base, equal, self.configuration(preserve_fields=[""])
        )
        self.assertTrue(r["passed"])
        self.assertEqual(r["unchanged_records"], 1)

    def test_presence_domains_lengths_and_compound_keys(self):
        cfg = self.configuration(
            required=["/nullable"],
            choices={"/tier": ["one", "two"]},
            lengths={"/text": {"min": 2, "max": 5}, "/tags": {"max": 2}},
            unique_by=[["/tier", "/title"]],
            unique_items=["/tags"],
            required_values={"/tags": ["blue", "red"]},
        )
        rows = [
            {
                "id": 1,
                "tier": "one",
                "title": "uno",
                "tags": ["blue", "red"],
                "text": "ab",
                "nullable": None,
            },
            {
                "id": 2,
                "tier": "two",
                "title": "uno",
                "tags": ["red"],
                "text": "xyz",
                "nullable": False,
            },
        ]
        a = self.commit(rows)
        self.assertTrue(check_catalog(self.repo, a, a, cfg)["passed"])
        rows[0]["tags"] = ["red", "red", "red"]
        rows[1].update(tier="one", text="x")
        del rows[1]["nullable"]
        rows.append(
            {
                "id": 3,
                "tier": "other",
                "title": None,
                "tags": [],
                "text": "hi",
                "nullable": None,
            }
        )
        b = self.commit(rows)
        r = check_catalog(self.repo, a, b, cfg)
        self.assertEqual(
            r["violations"],
            {
                "nonunique_items": 1,
                "duplicate_key": 1,
                "missing_field": 1,
                "invalid_unique_key": 1,
                "outside_choices": 1,
                "missing_value": 1,
                "field_length": 2,
            },
        )
        self.assertEqual(r["examples"][-1]["reason"], "field_length")

    def test_array_retention_and_deletion_policy(self):
        a = self.commit(
            [
                {"id": 1, "tags": [True, 1, 1]},
                {"id": "2", "tags": []},
                {"id": 3, "tags": []},
            ]
        )
        b = self.commit([{"id": 1, "tags": [1, True, "new"]}])
        cfg = self.configuration(
            preserve_ids=False,
            required_ids=["2"],
            max_removed=1,
            preserve_items=["/tags"],
        )
        r = check_catalog(self.repo, a, b, cfg)
        self.assertEqual(
            r["violations"], {"removal_budget": 1, "required_id": 1, "lost_items": 1}
        )
        c = self.commit(
            [{"id": 1, "tags": [1, "new", 1, True]}, {"id": "2", "tags": []}]
        )
        self.assertTrue(check_catalog(self.repo, a, c, cfg)["passed"])
        bad = self.commit([{"id": 1, "tags": None}])
        self.assertIn("input_error", check_catalog(self.repo, bad, c, cfg))

    def test_high_volume_diagnostics_keep_rare_rules_and_id_types(self):
        rows = [{"id": v, "title": "ok"} for v in [1, "1", *range(2, 21)]]
        a = self.commit(rows)
        b = self.commit([{"id": "new"}])
        r = check_catalog(
            self.repo, a, b, self.configuration(fields={"/title": "string"})
        )
        self.assertEqual(len(r["examples"]), 12)
        self.assertEqual(
            {ex["reason"] for ex in r["examples"]}, {"missing_id", "field_type"}
        )
        ids = [ex for ex in r["examples"] if ex.get("id") == "1"]
        self.assertEqual({ex["id_type"] for ex in ids}, {"string", "integer"})
        self.assertEqual(r["omitted_examples"], 10)

    def test_profile_and_revision_override_use_committed_data(self):
        a = self.commit([{"id": 1, "v": "ab"}, {"id": 2, "v": None}, {"id": 3}])
        b = self.commit([{"id": 1, "v": "blank"}])
        g = {"name": "library", "kind": "catalog", "catalog": {"path": "rows.json"}}
        p = self.plan(expected_base=b, gates=[g])
        (self.repo / "rows.json").write_text("dirty")
        profile = self.cli("catalog-profile", p, "--ref", a, "--field", "/v")
        field = profile["profiles"][0]["fields"][0]
        self.assertEqual(field["counts"], {"string": 1, "null": 1, "missing": 1})
        self.assertEqual(
            (field["min_length"], field["max_length"], field["distinct_present"]),
            (2, 2, 2),
        )
        check = self.cli("catalog-check", p, "--before", a, "--ref", b, code=3)
        self.assertEqual(check["checks"][0]["removed_ids"], 2)
        self.assertEqual(check["base"], a)
        self.assertEqual((self.repo / "rows.json").read_text(), "dirty")

    def test_unreadable_baseline_receipt_is_archived_and_classified(self):
        g = {
            "name": "lib",
            "kind": "catalog",
            "catalog": self.configuration(),
            "artifacts": [],
            "timeout": 20,
        }
        r = run_gate(
            g,
            self.repo,
            evidence_folder(self.root / "receipts", 1),
            self.head,
            baseline=self.head,
        )
        self.assertIsNone(observed_pass(r))
        self.assertTrue(r["input_error"])
        self.assertEqual([a["name"] for a in r["artifacts"]], ["catalog.json"])

    def test_retained_catalog_repair_and_reporting(self):
        self.commit([{"id": "kept", "tags": ["windows"]}])
        gate = {
            "name": "search filters",
            "kind": "catalog",
            "catalog": {"path": "rows.json", "preserve_items": ["/tags"]},
        }
        worker = {
            "kind": "command",
            "argv": [
                sys.executable,
                "-c",
                (
                    "import sys,json;from pathlib import Path;p=sys.stdin.read().partition('Last observed failure (if any)')[2];"
                    "tags=['windows','qa'] if 'lost_items' in p else [];Path('rows.json').write_text(json.dumps([{'id':'kept','tags':tags}]))"
                ),
            ],
        }
        p = self.plan(
            gates=[],
            worker=worker,
            milestones=[
                {
                    "title": "Tags",
                    "task": "Update filters",
                    "gates": [gate],
                    "retain_gates": "subsequent",
                },
                {
                    "title": "Guide",
                    "task": "Write guide",
                    "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                },
            ],
        )
        from nightweaver.catalog import check_catalog as check

        with patch("nightweaver.catalog.check_catalog", wraps=check) as probe:
            pre = self.cli("preflight", p)
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(pre["catalog_baselines"]), 2)
        result = self.cli("add", p, "--start", "--wait")
        rid = result["run_id"]
        self.assertEqual(result["phase"], "done")
        history = self.cli("catalog-history", rid)["observations"]
        self.assertEqual([h["passed"] for h in history], [False, True, True])
        self.assertEqual(history[0]["examples"][0]["field"], "/tags")
        self.assertEqual(history[-1]["changed_records"], 1)
        for command, extension, term in (
            ("report", "html", "Catalog changes by attempt"),
            ("ci-summary", "md", "modified / unchanged"),
        ):
            out = self.root / ("report." + extension)
            self.cli(command, rid, "--output", out)
            self.assertIn(term, out.read_text(encoding="utf-8"))
        self.assertTrue(self.cli("replay-checks", rid, "--through")["passed"])
        self.assertEqual(self.cli("audit-evidence", rid)["status"], "verified")
