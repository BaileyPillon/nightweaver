"""Regression coverage for evidence replay, recovery and repeated-trial measurements."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import csv
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_windows as fixtures
from nightweaver.benchmark import reliability, select_trials, summarize
from nightweaver.benchmark_exports import compare_suites, export_csv
from nightweaver.checks import evidence_folder, run_gate
from nightweaver.cli import main, with_live_status
from nightweaver.context import capture
from nightweaver.diagnostics import preflight, replay_checks
from nightweaver.engine import Run, fork_run, git
from nightweaver.insights import (
    budget_status,
    diagnosis,
    export_trace,
    failure_excerpt,
    gate_stats,
    provider_totals,
)
from nightweaver.providers import usage
from nightweaver.spec import gate_spec


@unittest.skipUnless(os.name == "nt", "Windows-native runner")
class NativeReliability(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def test_pinned_verifier_blocks_then_resumes_at_check(self):
        verifier = self.root / "operator.py"
        original = "assert open('result.txt').read() == 'ready'"
        verifier.write_bytes(original.encode())
        gate = {
            "name": "criterion",
            "argv": [sys.executable, str(verifier)],
            "pinned_inputs": [{"path": str(verifier), "sha256": sha256(original.encode()).hexdigest()}],
        }
        rid = self.configured(fixtures.fixture("good"), gates=[gate])
        verifier.write_text("raise SystemExit(0)", encoding="utf-8")
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        receipt = next(e["details"] for e in self.store.events(rid) if e["kind"] == "gate_finished")
        self.assertTrue(receipt["input_error"])
        self.assertIsNone(receipt["exit"])
        verifier.write_bytes(original.encode())
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        self.assertEqual(self.store.get(rid)["state"]["provider_calls"], 1)
        before = self.store.events(rid)
        result = replay_checks(self.store, rid, repeat=2)
        self.assertTrue(result["passed"])
        self.assertEqual(result["stability"], {"criterion": "all_passed"})
        self.assertEqual(len({g["log"] for g in result["gates"]}), 2)
        self.assertEqual((result["model_calls"], self.store.events(rid)), (0, before))
        self.assertEqual(
            diagnosis(self.store, rid)["last_failed_gate"]["seq"],
            next(e["seq"] for e in before if e["kind"] == "gate_finished"),
        )
        stats = gate_stats(before)[0]
        self.assertEqual((stats["executions"], stats["failures"], stats["timed"]), (2, 1, 2))
        self.assertGreaterEqual(stats["p95_ms"], stats["median_ms"])
        for e in before:
            if e["kind"] in ("gate_started", "gate_finished"):
                self.assertEqual((e["details"]["step"], e["details"]["attempt"]), (1, 1))
                self.assertEqual(len(e["details"]["contract_sha256"]), 64)
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["--home", str(self.store.root), "budget", rid]), 0)
        self.assertEqual(json.loads(out.getvalue())["used"]["max_calls"], 1)

    def test_repeats_detect_mixed_results_and_replay_source_drift(self):
        flag = self.root / "count"
        code = "from pathlib import Path; import sys; f=Path(sys.argv[1]); n=int(f.read_text()) if f.exists() else 0; f.write_text(str(n+1)); sys.exit(n%2)"
        rid = self.configured(
            fixtures.fixture("good"),
            gates=[{"name": "alternator", "argv": [sys.executable, "-c", code, str(flag)]}],
        )
        self.assertEqual(Run(self.store, rid).run(), "done")
        r = replay_checks(self.store, rid, repeat=3)
        self.assertEqual(r["stability"], {"alternator": "mixed"})
        self.assertFalse(r["passed"])
        rid2 = self.configured(fixtures.fixture("good"), gate="drift")
        self.store.transition(rid2, "milestone_accepted", {"commit": self.head, "step": 1}, phase="done")
        r = replay_checks(self.store, rid2)
        self.assertTrue(r["gates"][0]["changed_source"])
        self.assertFalse(r["passed"])

    def test_evidence_recheck_preserves_original_bytes_and_broken_receipt(self):
        folder = self.root / "evidence"
        gate = gate_spec(
            {"name": "receipt", "argv": fixtures.fixture("emit"), "artifacts": ["receipt.txt"]}
        )
        a = run_gate(gate, self.repo, evidence_folder(folder, 1), self.head)
        gate["argv"] = [sys.executable, "-c", "open('receipt.txt','w').write('different')"]
        b = run_gate(gate, self.repo, evidence_folder(folder, 1), self.head)
        self.assertEqual(Path(a["artifacts"][0]["path"]).read_text(), "evidence")
        self.assertNotEqual(a["log"], b["log"])
        self.assertNotEqual(a["artifacts"][0]["sha256"], b["artifacts"][0]["sha256"])
        browser = gate_spec(
            {"kind": "browser", "name": "browser", "browser": {"page": "index.html", "heading": "test"}}
        )

        def broken(argv, *args, **kwargs):
            directory = Path(argv[-1])
            directory.mkdir()
            (directory / "browser.json").write_text("{")
            return 0

        with patch("nightweaver.checks.execute", side_effect=broken):
            row = run_gate(browser, self.repo, evidence_folder(folder, 2), self.head)
        self.assertTrue(row["artifact_error"])

    def test_fork_baseline_context_map_and_encoding(self):
        rid = self.configured(
            fixtures.fixture("good"),
            expected_base=self.head,
            milestones=[
                {"title": "first", "task": "Write the result"},
                {"title": "second", "task": "Carry it forward"},
            ],
        )
        (self.repo / "café[1].txt").write_text("archive", encoding="utf-8")
        git(self.repo, "add", "--", "café[1].txt")
        git(self.repo, "commit", "-qm", "New checkpoint")
        tip = git(self.repo, "rev-parse", "HEAD")
        self.store.transition(rid, "milestone_accepted", {"step": 1, "commit": tip}, step=1, accepted=tip)
        fork = self.store.get(fork_run(self.store, rid, 1))
        self.assertEqual((fork["spec"]["expected_base"], fork["state"]["base"]), (tip, tip))
        spec = {"repository": str(self.repo), "context_files": ["café[1].txt"], "repository_map": True}
        pack = capture(spec, tip)
        self.assertIn("café[1].txt", pack["repository_map"])
        self.assertEqual(pack["files"][0]["content"], "archive")
        (self.repo / "bad.txt").write_bytes(b"abc\xff")
        git(self.repo, "add", "bad.txt")
        git(self.repo, "commit", "-qm", "Non UTF8 input")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            capture({**spec, "context_files": ["bad.txt"]}, git(self.repo, "rev-parse", "HEAD"))

    def test_redacted_status_unknown_run_and_preflight(self):
        rid = self.configured(
            fixtures.fixture("good"),
            objective="password=fixtureprivate",
            gates=[{"name": "missing", "argv": ["nightweaver-no-such-tool-073.exe"]}],
        )
        row = self.store.get(rid)
        self.assertNotIn("fixtureprivate", json.dumps(with_live_status(self.store, row)))
        with self.assertRaisesRegex(ValueError, "not found"):
            self.store.events("bbbbbbbbbbbb")
        from nightweaver import diagnostics

        real = diagnostics.git

        def identity(repo, *args):
            if args[:1] == ("var",):
                raise ValueError("identity unavailable")
            return real(repo, *args)

        with patch.object(diagnostics, "git", side_effect=identity):
            r = preflight(self.store, row["spec"])
        self.assertFalse(r["ready"])
        self.assertEqual(len(r["errors"]), 3)
        self.assertEqual(r["gate_executables"]["nightweaver-no-such-tool-073.exe"], "missing")

    def test_trace_and_prompt_context(self):
        rid = self.configured(
            fixtures.fixture("good"), budgets={"max_calls": 4}, objective="private objective"
        )
        for char in "ABC":
            self.store.feedback(rid, char * 11000)
        notes = self.store.feedback_text(rid)
        self.assertIn("Earlier operator updates", notes)
        self.assertNotIn("A" * 100, notes)
        self.assertIn("B" * 11000, notes)
        self.assertIn("C" * 11000, notes)
        row = self.store.get(rid)
        prompt = Run(self.store, rid).worker_prompt(
            {**row["state"], "last_error": "failed", "log": "old/gate.log"}, row["spec"]["milestones"][0]
        )
        self.assertIn("old/gate.log", prompt)
        self.assertIn("Budget remaining", prompt)
        out = self.root / "trace.jsonl"
        receipt = export_trace(self.store, rid, out)
        trace = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(receipt["events"], len(trace) - 1)
        self.assertNotIn("private objective", out.read_text())
        self.assertNotIn("B" * 100, out.read_text())
        self.assertEqual([r["seq"] for r in trace[1:]], sorted(r["seq"] for r in trace[1:]))


class Measurements(unittest.TestCase):
    def test_usage_and_error_digest(self):
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "log"
            f.write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": -5, "output_tokens": 9}})
            )
            self.assertIsNone(usage(f, "codex")["observed_tokens"])
            f.write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 20, "cached_input_tokens": 12, "output_tokens": 3},
                    }
                )
            )
            u = usage(f, "codex")
            self.assertEqual((u["observed_tokens"], u["token_channels"]["cached_input_tokens"]), (23, 12))
            events = [
                {
                    "kind": "provider_call_finished",
                    "details": {"provider": "codex", "role": "worker", "model": "fixture", **v},
                }
                for v in (u, {"observed_tokens": None})
            ]
            route = provider_totals(events)[0]
            self.assertEqual(
                (route["calls"], route["measured_calls"], route["observed_tokens"]), (2, 1, 23)
            )
            f.write_text("\x1b[31mFAIL verify draft\x1b[0m\n" + "progress line\n" * 3000 + "end")
            message = failure_excerpt(f)
            self.assertIn("FAIL verify draft", message)
            self.assertTrue(message.endswith("end"))
            self.assertNotIn("\x1b", message)
            self.assertLessEqual(len(message), 3600)
        budget = budget_status(
            {"budgets": {"max_seconds": 6, "max_calls": 3}},
            {"started_at": "2026-01-01T00:00:00+00:00", "provider_calls": 1},
            datetime(2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc),
        )
        self.assertEqual(budget["remaining"], {"max_calls": 2, "max_seconds": 2})

    def test_reliability_weights_intervals_selector_and_csv(self):
        base = {
            "task": "a",
            "contender": "x",
            "baseline": "sha",
            "contract_sha256": "contract",
            "route_sha256": "route",
            "mode": "standalone",
            "elapsed_seconds": 2,
            "provider_calls": 1,
            "observed_tokens": None,
            "usage_unknown_calls": 1,
            "usage_pending_calls": 0,
            "unassisted_acceptance": False,
            "run_id": "id",
        }
        rows = [
            {
                **base,
                "repeat": i + 1,
                "accepted": ok,
                "phase": "done" if ok else "blocked",
                "outcome": "accepted" if ok else "rejected",
            }
            for i, ok in enumerate((True, False, True))
        ]
        rows += [
            {**rows[0], "task": "b", "accepted": False, "phase": "blocked", "outcome": "rejected"},
            {**rows[0], "repeat": 4, "accepted": False, "phase": "blocked", "outcome": "provider_blocked"},
        ]
        stats = reliability(rows, 2)
        self.assertEqual((stats[0]["pass_at_k"], stats[0]["excluded"]), (1, 1))
        self.assertAlmostEqual(stats[0]["pass_all_k"], 1 / 3)
        self.assertIsNone(stats[1]["pass_at_k"])
        measures = summarize(rows)[0]
        self.assertEqual(measures["macro_task_acceptance"], 1 / 3)
        lo, hi = measures["scored_wilson_95"]
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertEqual(len(select_trials(rows, "b", "x", 1)), 1)
        with self.assertRaises(ValueError):
            select_trials(rows, "absent")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "trial.csv"
            with patch(
                "nightweaver.benchmark_exports.suite_report",
                return_value={"runs": [{**rows[0], "task": "=1+1"}]},
            ):
                export_csv(None, "bench", out)
            with out.open(encoding="utf-8-sig", newline="") as source:
                exported = list(csv.DictReader(source))
            self.assertEqual(exported[0]["task"], "'=1+1")
        older = {**rows[0], "route_sha256": None}
        with patch("nightweaver.benchmark_exports.suite_report", return_value={"runs": [older]}):
            self.assertEqual(compare_suites(None, "a", "b")["counts"], {"unknown_inputs": 1})
