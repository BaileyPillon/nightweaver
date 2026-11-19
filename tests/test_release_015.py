"""Witness the same symptom before repair and protect the known-good case."""

import json
import os
import sys
import unittest
from hashlib import sha256
from pathlib import Path

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.engine import git
from nightweaver.junit import contract, grade
from nightweaver.proof import proof_contract


def xml(cases, attrs=""):
    return f"<testsuite {attrs}>{cases}</testsuite>"


def case(name, body="", attrs=""):
    return f'<testcase classname="Film" name="{name}" {attrs}>{body}</testcase>'


def evaluate(data, **policy):
    return grade(data.encode(), contract(policy))


class ReportDialect(unittest.TestCase):
    def test_misplaced_failures_and_metadata(self):
        for node in (
            "<error/>",
            "<unexpected>" + case("broken", "<failure/>") + "</unexpected>",
        ):
            with (
                self.subTest(node=node),
                self.assertRaisesRegex(ValueError, "Misplaced"),
            ):
                evaluate(xml(case("ok") + node))
        self.assertTrue(
            evaluate(
                xml(case("ok") + "<system-out>example: &lt;error/&gt;</system-out>")
            )["passed"]
        )

    def test_google_disabled_is_distinct_from_runtime_skipped(self):
        data = xml(
            case("ok")
            + case("off", attrs='status="notrun" result="suppressed"')
            + case("later", "<skipped/>"),
            'disabled="1" skipped="1" tests="3"',
        )
        report = evaluate(data, max_skipped=1)
        self.assertFalse(report["passed"])
        self.assertEqual(
            (report["counts"]["skipped"], report["disabled_cases"]), (2, 1)
        )
        with self.assertRaisesRegex(ValueError, "disabled"):
            evaluate(data.replace('disabled="1"', 'disabled="0"'))

    def test_suites_disambiguate_tests_and_selectors(self):
        data = (
            "<testsuites>"
            + xml(case("save"), 'name="desktop"')
            + xml(case("save"), 'name="phone"')
            + "</testsuites>"
        )
        selector = {"classname": "Film", "name": "save"}
        self.assertTrue(
            evaluate(data, required=[{**selector, "suite": ["phone"]}])["passed"]
        )
        self.assertIn("ambiguous", evaluate(data, required=[selector])["issues"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            evaluate(data.replace("phone", "desktop"))

    def test_recovered_and_exhausted_retries(self):
        measured = evaluate(
            xml(case("save", "<flakyFailure/><flakyError/>")), max_flaky=0
        )
        self.assertFalse(measured["passed"])
        self.assertEqual((measured["flaky_cases"], measured["retry_failures"]), (1, 2))
        self.assertTrue(evaluate(xml(case("save", "<flakyError/>")))["passed"])
        self.assertFalse(
            evaluate(xml(case("save", "<failure/><rerunFailure/>")))["passed"]
        )
        for bad in (
            "<rerunError/>",
            "<flakyError/><failure/>",
            "<flakyError/><skipped/>",
        ):
            with self.assertRaisesRegex(ValueError, "retry"):
                evaluate(xml(case("save", bad)))

    def test_exact_timing_and_repair_feedback(self):
        data = xml(
            case("a", attrs='time="0.00100000000000000000000000000001"')
            + case("b", attrs='time="0.002"')
            + case("off", "<skipped/>", 'time="10"')
        )
        r = evaluate(data, max_case_ms=1, max_total_case_ms=3)
        self.assertFalse(r["passed"])
        self.assertEqual(
            (r["timed_cases"], r["executed_cases"], r["measured_case_ms"]), (2, 2, 4)
        )
        self.assertNotIn("off", [c["name"] for c in r["slowest"]])
        self.assertFalse(
            evaluate(xml(case("unknown")), max_total_case_ms=1000)["passed"]
        )
        many = evaluate(
            xml("".join(case(str(i), "<failure/>") for i in range(14))),
            required=[{"classname": "Film", "name": "important"}],
        )
        self.assertIn("important", many["issues"][0])
        self.assertEqual((len(many["issues"]), many["omitted_issues"]), (12, 4))

    def test_invalid_or_overlapping_contracts(self):
        target = {"classname": "Film", "name": "save"}
        for raw in (
            {"repairs": []},
            {"repairs": [target], "samples": True},
            {"repairs": [target, target]},
            {"repairs": [target], "preserves": [{**target, "suite": ["phone"]}]},
            {"repairs": [{**target, "message_contains": " "}]},
            {"repairs": [{**target, "suite": "phone"}]},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                proof_contract(raw)


@unittest.skipUnless(os.name == "nt", "native Windows orchestration")
class ProofRuns(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def test_fixed_planner_echo_keeps_operator_checks(self):
        from nightweaver.spec import read_plan

        p = self.plan(
            milestones=[{"title": "Build", "task": "Save", "paths": ["result.txt"]}]
        )
        fixed = read_plan(p)["milestones"][0]
        item = {
            "title": "Build",
            "task": "Write the result",
            "paths": fixed["paths"],
            "gates": fixed["gates"],
        }
        answer = self.root / "response.json"
        answer.write_text(json.dumps({"milestones": [item]}))
        result = self.cli("goal", p, "Build", "--plan-from", answer)
        self.assertEqual(result["milestones"][0]["paths"], fixed["paths"])
        item["gates"] = []
        answer.write_text(json.dumps({"milestones": [item]}))
        self.assertIn(
            "changed fixed milestone",
            self.cli("goal", p, "Build", "--plan-from", answer, code=1),
        )

    def proof_plan(self, extra="", **policy):
        script = self.root / "suite.py"
        script.write_text(
            """from pathlib import Path
from xml.etree import ElementTree as ET
import sys
old=Path("result.txt").read_text().strip()=="original"
s=ET.Element("testsuite",name="screening")
fix=ET.SubElement(s,"testcase",classname="Film",name="restore",time="0.001")
control=ET.SubElement(s,"testcase",classname="Film",name="save",time="0.002")
if old: ET.SubElement(fix,"failure",message="lost saved film")
"""
            + extra
            + "\nET.ElementTree(s).write(sys.argv[1],encoding='utf-8')\n",
            encoding="utf-8",
        )
        proof = {
            "repairs": [
                {
                    "classname": "Film",
                    "name": "restore",
                    "message_contains": "saved film",
                }
            ],
            "preserves": [{"classname": "Film", "name": "save"}],
            "samples": 2,
            **policy,
        }
        g = {
            "name": "watchlist",
            "argv": [sys.executable, str(script), "{junit}"],
            "junit": {"min_passed": 2},
            "proof": proof,
            "pinned_inputs": [
                {"path": str(script), "sha256": sha256(script.read_bytes()).hexdigest()}
            ],
            "timeout": 8,
        }
        return self.plan(gates=[g]), script

    def test_autonomous_repair_baseline_then_replay_without_model(self):
        p, _ = self.proof_plan()
        raw = json.loads(p.read_text())
        raw["worker"] = {
            "kind": "command",
            "argv": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path;import sys;p=sys.stdin.read();"
                    "Path('result.txt').write_text('fixed' if 'lost saved film' in p.partition('Last observed failure (if any)')[2] else 'original\\n')"
                ),
            ],
        }
        # A newline-only change can normalize away on Windows. Make the first
        # attempt a tracked change outside the data file, keeping the fault live.
        raw["worker"]["argv"][-1] += ";Path('marker.txt').write_text('checked')"
        p.write_text(json.dumps(raw))
        plan = self.cli("check-schedule", p)
        self.assertEqual(
            (plan["samples"], plan["baseline_samples"], plan["timeout_seconds"]),
            (1, 2, 24),
        )
        base = self.cli("check-baseline", p, "--gate", "watchlist")
        self.assertEqual(base["status"], "reproduced")
        self.assertEqual(
            [s["tracked"][0]["status"] for s in base["baseline"]["samples"]],
            ["failed", "failed"],
        )
        rid = self.cli("add", p)["run_id"]
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        rows = self.cli("test-history", rid)["observations"]
        self.assertEqual([r["passed"] for r in rows], [False, True])
        self.assertTrue(rows[-1]["proof"]["passed"])
        by_case = self.cli(
            "case-history", rid, "--name", "restore", "--classname", "Film"
        )
        self.assertEqual(
            [r["case"]["status"] for r in by_case["observations"]], ["failed", "passed"]
        )
        ref = "nightweaver/" + rid
        matched = self.cli(
            "compare-revisions", p, "--gate", "watchlist", "--after", ref
        )
        self.assertEqual((matched["status"], matched["model_calls"]), ("verified", 0))
        stale = self.cli(
            "check-baseline", p, "--gate", "watchlist", "--before", ref, code=3
        )
        self.assertEqual(stale["status"], "not_reproduced")
        self.assertEqual(self.cli("replay-checks", rid, "--through")["passed"], True)
        audit = self.cli("audit-evidence", rid)
        self.assertTrue(
            any(a["name"] == "baseline-1/junit.xml" for a in audit["files"])
        )
        self.assertEqual(audit["status"], "verified")
        md = self.root / "report.md"
        html = self.root / "report.html"
        self.cli("ci-summary", rid, "--output", md)
        self.cli("report", rid, "--output", html)
        self.assertIn("4 / 4", md.read_text(encoding="utf-8"))
        self.assertIn("Regression proof", html.read_text(encoding="utf-8"))
        e = self.store.events(rid, limit=None, kinds=["gate_finished"])[-1]["details"]
        candidate_xml = next(a for a in e["artifacts"] if a["name"] == "junit.xml")
        Path(candidate_xml["path"]).write_text("<testsuite/>")
        self.assertIn(
            "digest mismatch",
            self.cli("case-history", rid, "--name", "restore", "--classname", "Film")[
                "observations"
            ][-1]["error"],
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)

    def test_fork_after_repair_preserves_the_original_regression_witness(self):
        plan, _ = self.proof_plan(samples=1)
        raw = json.loads(plan.read_text())
        raw["milestones"] = [
            {
                "title": "Repair",
                "task": "Restore film",
                "gates": raw.pop("gates"),
                "retain_gates": "subsequent",
            },
            {
                "title": "Guide",
                "task": "Write guide",
                "worker": {
                    "kind": "command",
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path;Path('guide.txt').write_text('Guide')",
                    ],
                },
            },
        ]
        plan.write_text(json.dumps(raw))
        rid = self.cli("add", plan)["run_id"]
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        fork = self.cli("fork", rid, "--milestone", 1)["run_id"]
        self.assertNotEqual(self.store.get(fork)["state"]["base"], self.head)
        self.assertEqual(self.cli("run", fork)["phase"], "done")
        observed = self.cli("test-history", fork)["observations"][-1]
        self.assertEqual(observed["proof"]["baseline"]["commit"], self.head)
        self.assertTrue(self.cli("replay-checks", fork, "--through")["passed"])

    def test_wrong_symptom_missing_control_and_dirty_baseline(self):
        for code, expected in [
            ("if old: fix[0].set('message','different cause')", "not_reproduced"),
            ("if old: s.remove(control)", "not_reproduced"),
            ("if old: fix[0].tag='error'", "not_reproduced"),
            ("if old: Path('result.txt').write_text('changed')", "inconclusive"),
        ]:
            with self.subTest(code=code):
                p, _ = self.proof_plan(code, samples=1)
                result = self.cli("check-baseline", p, "--gate", "watchlist", code=3)
                self.assertEqual(result["status"], expected)
                self.assertEqual((self.repo / "result.txt").read_text(), "original")
        p, _ = self.proof_plan(
            "if old: fix[0].set('message','different cause')", samples=1
        )
        rid = self.cli("add", p)["run_id"]
        self.assertEqual(self.cli("run", rid, code=3)["phase"], "blocked")
        self.assertEqual(self.cli("metrics", rid)["worker_attempts"], 1)

    def test_repeated_baseline_and_renamed_candidate(self):
        counter = self.root / "counter.txt"
        toggle = f"p=Path({str(counter)!r})\nif old and p.exists(): fix.remove(fix[0])\np.touch()"
        p, _ = self.proof_plan(toggle)
        result = self.cli("check-baseline", p, "--gate", "watchlist", code=3)
        self.assertEqual(
            [s["passed"] for s in result["baseline"]["samples"]], [True, False]
        )
        p, _ = self.proof_plan("if not old: s.set('name','renamed')", samples=1)
        (self.repo / "result.txt").write_text("fixed")
        git(self.repo, "add", "result.txt")
        git(self.repo, "commit", "-m", "Candidate")
        result = self.cli(
            "compare-revisions",
            p,
            "--gate",
            "watchlist",
            "--before",
            self.head,
            "--after",
            "HEAD",
            code=3,
        )
        self.assertEqual(result["status"], "candidate_failed")
        self.assertIn(
            "suite identity changed", result["samples"][0]["junit"]["issues"][0]
        )
