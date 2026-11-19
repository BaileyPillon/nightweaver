"""Windows benchmark integrity, repeatability and operator CLI regressions."""

import csv
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.benchmark import create_suite, read_suite, reliability
from nightweaver.benchmark_analysis import (
    efficiency,
    instability,
    latency,
    qualify,
    task_intervals,
)
from nightweaver.engine import create_run, git
from nightweaver.reports import metrics


@unittest.skipUnless(os.name == "nt", "Windows benchmark runner")
class BenchmarkWindows(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def suite(self, *, contenders=("good",), tasks=("art",), repetitions=1, **options):
        path = self.root / "suite.json"
        plan = self.plan(
            attempts=1,
            budgets={"max_seconds": 90, "max_calls": 2},
            context_files=["result.txt"],
        )
        spec = {
            "name": "Windows trial",
            "schedule": "balanced",
            "repetitions": repetitions,
            "tasks": [{"name": t, "plan": plan.name, "tags": [t]} for t in tasks],
            "contenders": [
                {"name": c, "worker": {"kind": "command", "argv": fixtures.fixture(c)}}
                for c in contenders
            ],
            **options,
        }
        path.write_text(json.dumps(spec))
        return path

    def test_schedule_rotates_positions_fingerprint_tags_and_budgets(self):
        suite = self.suite(
            contenders=("good", "bad", "implement"),
            tasks=("art", "save"),
            repetitions=3,
        )
        preview = self.cli("benchmark", "preview", suite)
        self.assertEqual(self.cli("benchmark", "preview", suite), preview)
        self.assertEqual(len(self.store.runs()), 0)
        for tag in ("art", "save"):
            rows = [r for r in preview["runs"] if tag in r["tags"]]
            for position in range(3):
                self.assertEqual(
                    {r["contender"] for r in rows[position::3]},
                    {"good", "bad", "implement"},
                )
        self.assertEqual([r["max_calls"] for r in preview["task_budgets"]], [18, 18])
        sid = self.cli("benchmark", "create", suite)["benchmark_id"]
        report = self.cli("benchmark", "report", sid, "--tag", "save")
        self.assertEqual(report["matrix_sha256"], preview["matrix_sha256"])
        self.assertEqual({r["task"] for r in report["runs"]}, {"save"})
        self.assertEqual(len(report["runs"]), 9)
        self.assertIn(
            "tag", self.cli("benchmark", "report", sid, "--tag", "missing", code=1)
        )

    def test_swapped_registration_order_membership_and_unknown_legacy_identity(self):
        source = self.suite(repetitions=2)
        original = self.cli("benchmark", "create", source)
        other = self.cli("benchmark", "create", source)
        sid, path = original["benchmark_id"], Path(original["manifest"])
        manifest = read_suite(self.store, sid)
        foreign = read_suite(self.store, other["benchmark_id"])["runs"][0]["run_id"]
        for mode in (
            "foreign",
            "order",
            "duplicate_json",
            "wrong_tag",
            "missing_trial",
        ):
            value = json.loads(json.dumps(manifest))
            if mode == "foreign":
                value["runs"][0]["run_id"] = foreign
            elif mode == "order":
                value["runs"].reverse()
            elif mode == "wrong_tag":
                value["runs"][0]["tags"] = ["elsewhere"]
            elif mode == "missing_trial":
                value["runs"].pop()
                value["planned_trials"] -= 1
            data = json.dumps(value)
            if mode == "duplicate_json":
                data = '{"seed":300,' + data[1:]
            path.write_text(data)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                read_suite(self.store, sid)
            self.cli("benchmark", "run", sid, code=1)
        self.assertTrue(all(r["state"]["phase"] == "queued" for r in self.store.runs()))
        del manifest["runs"][0]["baseline"]
        path.write_text(json.dumps(manifest))
        report = self.cli("benchmark", "report", sid)
        self.assertIsNone(report["runs"][0]["baseline"])
        self.assertEqual(
            self.cli("benchmark", "audit", sid, code=4)["verdict"], "inconclusive"
        )

    def test_equal_commit_substitution_and_project_alias_contract(self):
        source = self.suite()
        raw = json.loads((self.root / "plan.json").read_text())
        direct = self.cli("benchmark", "preview", source)["runs"][0]["contract_sha256"]
        self.store.bind_project("alias", self.repo, self.repo / ".git")
        raw.pop("repository")
        raw["project"] = "alias"
        (self.root / "plan.json").write_text(json.dumps(raw))
        self.assertEqual(
            self.cli("benchmark", "preview", source)["runs"][0]["contract_sha256"],
            direct,
        )

        def replace_checkout(store, spec):
            old = self.repo.with_name("old")
            self.repo.rename(old)
            git(old, "clone", "--local", str(old), str(self.repo))
            return create_run(store, spec)

        with (
            patch("nightweaver.benchmark.create_run", side_effect=replace_checkout),
            self.assertRaisesRegex(ValueError, "replaced"),
        ):
            create_suite(self.store, source)
        self.assertFalse(self.store.runs())

    def test_audit_context_and_execution_selections(self):
        source = self.suite(tasks=("art", "save"))
        sid = self.cli("benchmark", "create", source)["benchmark_id"]
        self.assertEqual(self.cli("benchmark", "failures", sid), [])
        self.assertEqual(self.cli("benchmark", "audit", sid)["verdict"], "pass")
        self.assertEqual(
            self.cli("benchmark", "shard", sid, "--shard", "3/3")["trials"], 0
        )
        self.assertIn(
            "no trials", self.cli("benchmark", "run", sid, "--shard", "3/3", code=1)
        )
        self.cli("benchmark", "run", sid, "--tag", "art")
        report_path = self.root / "report.html"
        report = self.cli("benchmark", "report", sid, "--output", report_path)
        self.assertIn("Scored / planned", report_path.read_text(encoding="utf8"))
        (ok,) = [r for r in report["runs"] if r["accepted"]]
        self.assertEqual(ok["tags"], ["art"])
        self.assertEqual(report["summaries"][0]["token_coverage_fraction"], 0)
        self.assertEqual(
            (
                report["summaries"][0]["first_pass_acceptances"],
                report["summaries"][0]["accepted_after_repair"],
            ),
            (1, 0),
        )
        self.assertEqual(
            self.cli(
                "benchmark",
                "qualify",
                sid,
                "--contender",
                "good",
                "--minimum-percent",
                80,
                code=4,
            )["complete_tasks"],
            1,
        )
        found = self.cli("benchmark", "footprint", sid)
        (size,) = [m for m in found if m["status"] == "measured"]
        self.assertEqual(
            (size["files"], size["text_additions"], size["text_deletions"]), (1, 1, 1)
        )
        self.assertEqual(
            self.cli("benchmark", "efficiency", sid)[0]["calls_per_acceptance"], None
        )
        self.assertEqual(self.cli("benchmark", "latency", sid)[0]["finished"], 1)
        self.assertEqual(
            self.cli("benchmark", "instability", sid)[0]["pattern"], "insufficient"
        )
        for command, ext in (("jsonl", "jsonl"), ("junit", "xml"), ("csv", "csv")):
            self.cli("benchmark", command, sid, "--output", self.root / ("out." + ext))
        lines = [
            json.loads(line)
            for line in (self.root / "out.jsonl")
            .read_text(encoding="utf8")
            .splitlines()
        ]
        self.assertEqual([line["trial"] for line in lines], report["runs"])
        self.assertEqual(
            {
                p.get("value")
                for p in ET.parse(self.root / "out.xml").findall(
                    ".//property[@name='run_id']"
                )
            },
            {r["run_id"] for r in report["runs"]},
        )
        with (self.root / "out.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertIn("gate_unscored", next(csv.DictReader(stream)))
        self.store.transition(
            ok["run_id"],
            "gate_finished",
            {"name": "missing", "artifact_error": "unavailable"},
        )
        measurement = metrics(self.store, ok["run_id"])
        self.assertEqual(
            (measurement["gate_unscored"], measurement["gate_failures"]), (1, 0)
        )
        saved = self.store.directory(ok["run_id"]) / "context.json"
        saved.write_text("{}")
        audit = self.cli("benchmark", "audit", sid, code=3)
        self.assertEqual(audit["model_calls"], 0)
        self.assertTrue(
            any(
                r["run_id"] == ok["run_id"] and r["status"] == "failed"
                for r in audit["trials"]
            )
        )
        self.assertEqual(saved.read_text(), "{}")

    def test_review_configuration_rejected_before_registration(self):
        source = self.suite()
        raw = json.loads(source.read_text())
        for value in ({}, None, False):
            raw["contenders"][0]["reviewer"] = value
            source.write_text(json.dumps(raw))
            self.cli("benchmark", "create", source, code=1)
        self.assertFalse(self.store.runs())


class Statistics(unittest.TestCase):
    def row(self, contender, task, repeat, accepted):
        return {
            "contender": contender,
            "task": task,
            "repeat": repeat,
            "run_id": f"{contender}.{task}.{repeat}",
            "mode": "standalone",
            "route_sha256": contender,
            "contract_sha256": task,
            "baseline": "sha",
            "phase": "done" if accepted else "blocked",
            "outcome": "accepted" if accepted else "rejected",
            "accepted": accepted,
            "elapsed_seconds": repeat,
            "provider_calls": 1,
            "observed_tokens": 10,
            "usage_pending_calls": 0,
            "usage_unknown_calls": 0,
        }

    def test_task_units_coverage_and_inconclusive_threshold(self):
        rows = [
            self.row(name, task, i + 1, name == "A" or task == "hard")
            for name in ("A", "B")
            for task, count in (("easy", 9), ("hard", 1))
            for i in range(count)
        ]
        (pair,) = task_intervals(rows, 42, 500)
        self.assertEqual(pair["macro_gap_left_minus_right_pp"], 50)
        self.assertEqual(pair["task_bootstrap_95_pp"], [0, 100])
        self.assertEqual(pair, task_intervals(rows[::-1], 42, 500)[0])
        self.assertEqual(qualify(rows, "B", 60)["verdict"], "below_floor")
        partial = [
            dict(r, outcome="provider_blocked") if r["run_id"] == "B.hard.1" else r
            for r in rows
        ]
        (pair,) = task_intervals(partial, 42)
        self.assertEqual(
            (pair["excluded_tasks"], pair["task_bootstrap_95_pp"]), (["hard"], None)
        )
        self.assertEqual(qualify(partial, "B", 0)["verdict"], "inconclusive")
        self.assertEqual(
            reliability([dict(rows[0], route_sha256=None)], 2)[0]["unavailable_reason"],
            "missing_identity",
        )
        with self.assertRaises(ValueError):
            task_intervals(rows, 42, 0)

    def test_mixed_retries_cost_and_latency_coverage(self):
        a = self.row("A", "gallery", 1, True)
        b = self.row("A", "gallery", 2, False)
        self.assertEqual(instability([a, b])[0]["pattern"], "mixed")
        self.assertEqual(
            instability([a, dict(b, baseline="other")])[0]["pattern"], "insufficient"
        )
        self.assertEqual(efficiency([a, b])[0]["tokens_per_acceptance"], 20)
        self.assertIsNone(
            efficiency([a, dict(b, observed_tokens=None, usage_unknown_calls=1)])[0][
                "tokens_per_acceptance"
            ]
        )
        sample = latency([dict(a, elapsed_seconds=0.0), b])[0]["metrics"][
            "elapsed_seconds"
        ]
        self.assertEqual(sample, {"observed": 2, "p50": 1.0, "p95_nearest_rank": 2})
