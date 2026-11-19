"""Cumulative acceptance and structured test evidence through Windows entry points."""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.checks import evidence_folder, observed_pass, run_gate
from nightweaver.contracts import check_schedule, lint_plan
from nightweaver.engine import artifact_stamp, preserve_artifacts
from nightweaver.junit import contract, grade
from nightweaver.spec import gate_spec, validate


def xml(cases, attrs=""):
    return f"<testsuite {attrs}>{cases}</testsuite>".encode()


def case(name="read", extra="", result=""):
    return f'<testcase classname="library" name="{name}" {extra}>{result}</testcase>'


class GradeTests(unittest.TestCase):
    def test_zero_skipped_and_failed_evidence(self):
        for body in (
            "",
            case(result="<skipped/>"),
            case(result='<failure message="wrong value"/>'),
            case(result="<error/>"),
        ):
            with self.subTest(body=body):
                result = grade(xml(body), contract({}))
                self.assertFalse(result["passed"])
        both = xml(case() + case("absent", result="<skipped/>"))
        self.assertTrue(grade(both, contract({}))["passed"])
        self.assertFalse(grade(both, contract({"max_skipped": 0}))["passed"])
        self.assertFalse(grade(both, contract({"min_passed": 2}))["passed"])
        self.assertFalse(
            grade(
                both,
                contract({"required": [{"classname": "library", "name": "absent"}]}),
            )["passed"]
        )
        disabled = grade(
            xml(case(extra='status="notrun" result="suppressed"')), contract({})
        )
        self.assertEqual(disabled["counts"]["skipped"], 1)
        self.assertFalse(disabled["passed"])
        self.assertEqual(
            grade(xml(case(extra='status="failed"')), contract({}))["counts"]["failed"],
            1,
        )

    def test_nested_report_counts_namespaces_and_measured_duration(self):
        data = (
            b'<testsuites xmlns="urn:x" tests="2">'
            + xml(case(extra='time="0.0011"'))
            + xml(case("fast", 'time="0"'))
            + b"</testsuites>"
        )
        policy = contract(
            {"required": [{"classname": "library", "name": "fast"}], "max_case_ms": 2}
        )
        result = grade(data, policy)
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["passed"], 2)
        self.assertEqual([r["ms"] for r in result["slowest"]], [2, 0])
        self.assertFalse(grade(data, contract({"max_case_ms": 1}))["passed"])
        self.assertFalse(grade(xml(case()), contract({"max_case_ms": 2}))["passed"])

    def test_ambiguous_or_invalid_report_is_unscored(self):
        for data in (
            b"<truncated",
            xml(case() + case()),
            xml(case(), 'tests="2"'),
            xml(case(result="<error/><skipped/>")),
            xml(case(extra='time="NaN"')),
            b"<not-tests/>",
            b"<!DOCTYPE testsuite><testsuite/>",
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                grade(data, contract({}))
        for invalid in (
            {"min_passed": 0},
            {"max_skipped": True},
            {"required": [{"name": "x"}]},
            {"unknown": 1},
        ):
            with self.assertRaises(ValueError):
                contract(invalid)

    def test_same_metadata_changed_artifact_is_fresh(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "data.txt"
            file.write_bytes(b"before")
            stamp = artifact_stamp(root, "data.txt")
            before = {"data.txt": stamp}
            times = file.stat()
            file.write_bytes(b"after!")
            os.utime(file, ns=(times.st_atime_ns, times.st_mtime_ns))
            result = preserve_artifacts(root, root / "evidence", ["data.txt"], before)
            self.assertEqual(Path(result[0]["path"]).read_bytes(), b"after!")
            with self.assertRaisesRegex(ValueError, "fresh"):
                preserve_artifacts(
                    root,
                    root / "other",
                    ["data.txt"],
                    {"data.txt": artifact_stamp(root, "data.txt")},
                )


@unittest.skipUnless(os.name == "nt", "Windows command verification")
class WindowsEvidence(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def test_limits_separate_child_exit_from_controller_stop(self):
        for program, timeout, maximum, expected in [
            ("raise SystemExit(124)", 5, 4096, False),
            ("raise SystemExit(126)", 5, 4096, False),
            ("import time;time.sleep(10)", 1, 4096, None),
            ("print('x'*10000)", 5, 1024, None),
        ]:
            with self.subTest(program=program):
                g = gate_spec(
                    {
                        "name": "process",
                        "argv": [sys.executable, "-c", program],
                        "timeout": timeout,
                    }
                )
                receipt = run_gate(
                    g,
                    self.repo,
                    evidence_folder(self.root / "evidence", 1),
                    self.head,
                    max_log_bytes=maximum,
                )
                self.assertIs(observed_pass(receipt), expected)
                self.assertEqual(bool(receipt.get("execution_error")), expected is None)

    def test_scheduling_final_subsequent_and_conflicts(self):
        def gate(name):
            return {
                "name": name,
                "argv": fixtures.fixture("gate"),
                "timeout": 7,
                "stability_runs": 2,
            }

        steps = [
            {"title": name, "task": "work", "gates": [gate(name)]}
            for name in ("A", "B", "C", "D")
        ]
        steps[0]["retain_gates"] = "final"
        steps[1]["retain_gates"] = "subsequent"
        p = self.plan(gates=[], milestones=steps, budgets={"max_seconds": 55})
        raw = json.loads(p.read_text())
        result = self.cli("check-schedule", p)
        scheduled = [(r["milestone"], r["gate"]) for r in result["checks"]]
        self.assertEqual(
            scheduled,
            [(1, "A"), (2, "B"), (3, "C"), (3, "B"), (4, "D"), (4, "A"), (4, "B")],
        )
        self.assertEqual(
            (result["retained_samples"], result["timeout_seconds"]), (6, 98)
        )
        spec = validate(raw)
        self.assertIn(
            "combined_gate_timeouts_exceed_run_budget",
            [f["code"] for f in lint_plan(spec)["findings"]],
        )
        self.assertEqual(
            self.cli("preflight", p)["check_schedule"], check_schedule(spec)
        )
        steps[3]["gates"].append(gate("A"))
        self.assertEqual(
            len(validate({**raw, "milestones": steps})["milestones"][-1]["gates"]), 3
        )
        steps[3]["gates"][-1]["timeout"] = 8
        with self.assertRaisesRegex(ValueError, "Conflicting retained"):
            validate({**raw, "milestones": steps})

    def test_command_verdict_and_unreadable_report(self):
        for sequence, (body, code, outcome) in enumerate(
            [
                (xml(case(result="<failure/>")), 0, False),
                (xml(case()), 2, False),
                (xml(case()), 0, True),
                (b"<partial", 0, None),
                (None, 0, None),
            ],
            1,
        ):
            with self.subTest(sequence=sequence):
                prog = (
                    "import sys;from pathlib import Path;"
                    + (f"Path(sys.argv[1]).write_bytes({body!r});" if body else "")
                    + f"sys.exit({code})"
                )
                g = gate_spec(
                    {
                        "name": "graded",
                        "argv": [sys.executable, "-c", prog, "{junit}"],
                        "junit": {},
                    }
                )
                receipt = run_gate(
                    g,
                    self.repo,
                    evidence_folder(self.root / "samples", sequence),
                    self.head,
                )
                self.assertIs(observed_pass(receipt), outcome)
                self.assertEqual(receipt["exit"], code)
                self.assertEqual(len(receipt["artifacts"]), int(body is not None))
                if outcome is None:
                    self.assertTrue(receipt["execution_error"])

    def test_nonobject_and_duplicate_catalog_receipts_block(self):
        g = {
            "name": "catalog",
            "kind": "catalog",
            "catalog": {},
            "timeout": 5,
            "artifacts": [],
        }
        duplicate = (
            f'{{"schema":"nightweaver.catalog.v1","base":"{self.head}","tree":"{self.head}",'
            '"passed":false,"passed":true}'
        ).encode()
        for data in (b"[]", duplicate):

            def run(argv, *args, data=data, **kwargs):
                Path(argv[-1]).write_bytes(data)
                return 0

            with patch("nightweaver.checks.execute", side_effect=run):
                record = run_gate(
                    g,
                    self.repo,
                    evidence_folder(self.root / "catalogs", 1),
                    self.head,
                    baseline=self.head,
                )
            self.assertTrue(record["execution_error"])
            self.assertIsNone(observed_pass(record))

    def test_retroactive_replay_distinguishes_same_named_contracts(self):
        worker = {
            "kind": "command",
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path;Path('result.txt').write_text('second')",
            ],
        }
        gate1 = {
            "name": "contract",
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path;assert Path('result.txt').read_text()!='second'",
            ],
        }
        gate2 = {
            "name": "contract",
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path;assert Path('result.txt').read_text()=='second'",
            ],
        }
        shared = {"name": "shared", "argv": [sys.executable, "-c", "pass"]}
        plan = self.plan(
            gates=[shared],
            milestones=[
                {"title": "First", "task": "Set result", "gates": [gate1]},
                {
                    "title": "Second",
                    "task": "Change result",
                    "gates": [gate2],
                    "worker": worker,
                },
            ],
        )
        rid = self.cli("add", plan)["run_id"]
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        positive = self.cli("replay-checks", rid)
        self.assertTrue(positive["passed"])
        through = self.cli("replay-checks", rid, "--through", code=3)
        self.assertEqual(len(through["gates"]), 3)
        self.assertEqual(
            through["stability"],
            {
                "M1 / shared": "all_passed",
                "M1 / contract": "all_failed",
                "M2 / contract": "all_passed",
            },
        )
        self.assertEqual(through["gates"][0]["scheduled_milestones"], [1, 2])
        self.assertEqual(through["model_calls"], 0)
        early = self.cli(
            "replay-checks", rid, "--through", "--milestone", "1", "--gate", "contract"
        )
        self.assertTrue(early["passed"])
        self.assertNotEqual(early["commit"], through["commit"])

    def test_retained_test_repairs_regression_and_records_attempts(self):
        checker = self.root / "write_report.py"
        checker.write_text(
            "from pathlib import Path\nimport sys\n"
            "state=Path('result.txt').read_text()\n"
            "failure='<failure message=\"lost accepted state\"/>' if state=='regressed' else ''\n"
            'Path(sys.argv[1]).write_text(\'<testsuite><testcase classname="journal" name="restore">\''
            "+failure+'</testcase></testsuite>')\n"
        )
        worker = {
            "kind": "command",
            "argv": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path;import sys;"
                    "issue=sys.stdin.read().partition('Last observed failure (if any)')[2];"
                    "Path('result.txt').write_text('repaired' if 'lost accepted state' in issue else 'regressed')"
                ),
            ],
        }
        retained = {
            "name": "Restore journal",
            "argv": [sys.executable, str(checker), "{junit}"],
            "junit": {},
        }
        local = {"name": "Local check", "argv": [sys.executable, "-c", "pass"]}
        p = self.plan(
            gates=[],
            milestones=[
                {
                    "title": "First",
                    "task": "Write",
                    "gates": [retained],
                    "retain_gates": "subsequent",
                },
                {
                    "title": "Second",
                    "task": "Extend",
                    "gates": [local],
                    "worker": worker,
                },
            ],
        )
        rid = self.cli("add", p)["run_id"]
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        observations = self.cli("test-history", rid)["observations"]
        self.assertEqual(
            [(o["step"], o["attempt"], o["passed"], o["exit"]) for o in observations],
            [(1, 1, True, 0), (2, 1, False, 0), (2, 2, True, 0)],
        )
        self.assertIn("journal / restore", observations[1]["report"]["issues"][0])
        self.assertTrue(self.cli("replay-checks", rid, "--through")["passed"])
        self.assertEqual(self.cli("audit-evidence", rid)["status"], "verified")
        md, html = self.root / "result.md", self.root / "result.html"
        self.cli("ci-summary", rid, "--output", md)
        self.cli("report", rid, "--output", html)
        self.assertIn("2 / 3", md.read_text(encoding="utf-8"))
        self.assertIn("Test results by attempt", html.read_text(encoding="utf-8"))
