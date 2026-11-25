"""0.19.1 runtime: a gate that times out is run again with more time before a run blocks, and a run's time budget is spent in active time,
not while the run waits blocked for a person or paused."""

from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

import test_windows as fixtures

from nightweaver import engine
from nightweaver.checks import contract_hash, evidence_folder, observed_pass, run_gate
from nightweaver.engine import BASELINE_TIMEOUT, GATE_TIMEOUT, BudgetExceeded, Run, gate_timed_out, park_plan, timeout_advice
from nightweaver.insights import budget_report, budget_status
from nightweaver.providers import ProviderUnavailable
from nightweaver.reports import metrics
from nightweaver.spec import validate
from nightweaver.store import Store, idle_from_events, run_clock

T0 = "2026-11-23T08:00:00+00:00"
SLOW_ONCE = ("import os, sys, time\n"
             "marker = sys.argv[1]\n"
             "if not os.path.exists(marker):\n"
             "    open(marker, 'w').close()\n"
             "    time.sleep(60)\n"
             "raise SystemExit(0 if open('result.txt').read() == 'ready' else 2)\n")
ALWAYS_SLOW = "import time; time.sleep(60)"
LIMIT = 3  # seconds a test gate is given first: a loaded machine takes a while to start even a fast one, and the retry gets twice this


class Clock:
    """A settable clock for the store and the engine: every stamp either of them writes or reads comes from it."""

    def __init__(self, start=T0):
        self.start = self.now = datetime.fromisoformat(start)

    def __call__(self):
        return self.now.isoformat(timespec="milliseconds")

    def at(self, seconds):
        self.now = self.start + timedelta(seconds=seconds)
        return self()

    def stamp(self, seconds):
        """The stamp of a moment, without going there."""
        return (self.start + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")

    def __enter__(self):
        self.stack = ExitStack()
        for name in ("nightweaver.store.timestamp", "nightweaver.engine.timestamp"):
            self.stack.enter_context(patch(name, self))
        return self

    def __exit__(self, *_error):
        self.stack.close()


def event(kind, seconds, **details):
    return {"kind": kind, "time": (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat(), "details": details}


@unittest.skipUnless(os.name == "nt", "Windows controller")
class GateTimeoutRetry(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def gate(self, code, timeout=LIMIT, **extra):
        marker = str(self.root / "ran-once")
        return {"name": "slow gate", "argv": [sys.executable, "-c", code, marker], "timeout": timeout, **extra}

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def test_a_timeout_is_run_again_once_with_double_the_time_and_then_passes(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.gate(SLOW_ONCE)], attempts=1)
        self.assertEqual(Run(self.store, run).run(), "done")
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["name"], retried["old_timeout"], retried["new_timeout"], retried["retry"]), ("slow gate", LIMIT, 2 * LIMIT, 1))
        (finished,) = self.events(run, "gate_finished")
        self.assertEqual((finished["exit"], finished["timeout_retried"]), (0, {"limits": [LIMIT, 2 * LIMIT]}))
        planned = self.store.get(run)["spec"]["milestones"][0]["gates"][0]
        self.assertEqual(finished["contract_sha256"], contract_hash(planned), "a saved sample is matched by the plan's gate, not by the retry's")
        self.assertEqual(planned["timeout"], LIMIT)
        self.assertEqual(len(self.events(run, "gate_started")), 1)
        self.assertNotEqual(retried["log"], finished["log"], "each run of the gate keeps its own evidence")
        self.assertTrue(os.path.isfile(retried["log"]) and os.path.isfile(finished["log"]))
        self.assertEqual((self.events(run, "attempt_rejected"), len(self.events(run, "worker_started"))), ([], 1), "a timeout is not a worker failure")

    def test_a_second_timeout_blocks_and_says_which_gate_what_limits_and_how_to_raise_it(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.gate(ALWAYS_SLOW)], attempts=3)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["old_timeout"], retried["new_timeout"]), (LIMIT, 2 * LIMIT))
        state = self.store.get(run)["state"]
        for text in ("'slow gate'", f"timeout of {LIMIT} s", f"again at {2 * LIMIT} s", '"timeout"', "in the plan", "as a new run",
                     "keeps the plan it was created with", "--retry"):
            self.assertIn(text, state["last_error"])
        self.assertEqual(blocked_issue(self, run), state["last_error"])
        self.assertEqual((state["phase"], state["blocked_from"], state["attempts_used"]), ("blocked", "checking", 1))
        self.assertEqual(self.events(run, "attempt_rejected"), [], "the worker is not asked again and no attempt is spent")
        self.assertEqual(len(self.events(run, "worker_started")), 1)
        (blocked,) = self.events(run, "blocked")
        self.assertEqual(blocked["during"], "checking")
        self.assertEqual(metrics(self.store, run)["outcome"], "execution_blocked")
        (finished,) = self.events(run, "gate_finished")
        self.assertIsNone(observed_pass(finished), "a gate that never finished scores nothing")

    def test_no_retries_keeps_the_0_19_behavior(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.gate(ALWAYS_SLOW)], attempts=3, retry={"gate_timeout_retries": 0})
        self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "gate_timeout_retried"), [])
        self.assertEqual(self.store.get(run)["state"]["last_error"], GATE_TIMEOUT)
        (finished,) = self.events(run, "gate_finished")
        self.assertNotIn("timeout_retried", finished)

    def test_a_retry_in_a_stability_batch_keeps_every_sample_matching_the_plan(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.gate(SLOW_ONCE, stability_runs=2)], attempts=1)
        self.assertEqual(Run(self.store, run).run(), "done")
        planned = self.store.get(run)["spec"]["milestones"][0]["gates"][0]
        finished = self.events(run, "gate_finished")
        self.assertEqual([f["sample"] for f in finished], [1, 2])
        self.assertEqual({f["contract_sha256"] for f in finished}, {contract_hash(planned)})
        self.assertEqual(len(self.events(run, "gate_timeout_retried")), 1)

    def test_the_receipt_of_a_real_timeout_is_what_the_engine_recognizes(self):
        spec = {"name": "p", "argv": [sys.executable, "-c", ALWAYS_SLOW], "timeout": 1, "artifacts": []}
        receipt = run_gate(spec, self.repo, evidence_folder(self.root / "evidence", 1), self.head)
        self.assertEqual((receipt["exit"], receipt["artifact_error"]), (None, GATE_TIMEOUT))
        self.assertTrue(gate_timed_out(receipt))
        child = run_gate({**spec, "argv": [sys.executable, "-c", "raise SystemExit(124)"], "timeout": 30}, self.repo,
                         evidence_folder(self.root / "evidence", 2), self.head)
        self.assertEqual(child["exit"], 124)
        self.assertFalse(gate_timed_out(child), "a gate that itself exits 124 failed; the controller did not stop it")

    def observe(self, run, *, retries=None, timeout=10, receipt=None):
        """observe_gate on a fake run_gate that answers `receipt(check, folder)`; returns (the gate's receipt, the timeouts it was run with)."""
        runner = Run(self.store, run)
        if retries == "absent":  # a plan saved by 0.19 has no such field
            runner.spec["retry"].pop("gate_timeout_retries")
        elif retries is not None:
            runner.spec["retry"]["gate_timeout_retries"] = retries
        seen = []

        def fake(check, path, folder, snapshot, **_options):
            seen.append(check["timeout"])
            return (receipt or timeout_receipt)(check, folder)
        check = {**runner.spec["milestones"][0]["gates"][0], "timeout": timeout}
        with patch("nightweaver.engine.run_gate", fake):
            result = runner.observe_gate(check, self.root, self.store.get(run)["state"], runner.spec["milestones"][0], "tree",
                                         {"step": 1, "attempt": 1, "gate_index": 1})
        return result, seen

    def test_the_limit_doubles_on_every_retry(self):
        run = self.configured(fixtures.fixture("good"))
        receipt, seen = self.observe(run, retries=3)
        self.assertEqual(seen, [10, 20, 40, 80])
        self.assertEqual([(e["old_timeout"], e["new_timeout"]) for e in self.events(run, "gate_timeout_retried")], [(10, 20), (20, 40), (40, 80)])
        self.assertIn("on 3 retries", receipt["artifact_error"])
        self.assertIn("the last at 80 s", receipt["artifact_error"])
        self.assertIn("timeout of 10 s", receipt["artifact_error"])

    def test_the_retried_limit_stops_at_four_hours(self):
        run = self.configured(fixtures.fixture("good"))
        receipt, seen = self.observe(run, retries=3, timeout=7200)
        self.assertEqual(seen, [7200, 14400], "a limit that cannot grow is not retried")
        self.assertIn("again at 14400 s", receipt["artifact_error"])
        self.assertEqual(engine.GATE_TIMEOUT_CAP, 4 * 3600)
        self.assertIn("longest", timeout_advice(7200), "a gate already at the plan's longest timeout is told to be made faster, not longer")
        self.assertIn("raise", timeout_advice(120).lower())
        self.assertNotIn("longest", timeout_advice(120))

    def test_a_plan_saved_by_0_19_gets_the_default_of_one_retry(self):
        run = self.configured(fixtures.fixture("good"))
        receipt, seen = self.observe(run, retries="absent")
        self.assertEqual(seen, [10, 20])

    def test_only_the_controller_stopping_a_gate_at_its_wall_clock_limit_is_retried(self):
        run = self.configured(fixtures.fixture("good"))
        for label, answer in (("a gate that exits 124 itself", lambda check, folder: {**timeout_receipt(check, folder), "exit": 124,
                                                                                    "execution_error": False, "artifact_error": None}),
                              ("a gate over its log limit", lambda check, folder: {**timeout_receipt(check, folder), "artifact_error": "Gate exceeded its log-size limit"}),
                              ("a gate that passed", lambda check, folder: {**timeout_receipt(check, folder), "exit": 0, "execution_error": False,
                                                                           "artifact_error": None})):
            with self.subTest(label):
                _, seen = self.observe(run, receipt=answer)
                self.assertEqual(seen, [10])
        self.assertEqual(self.events(run, "gate_timeout_retried"), [])

    def test_the_retry_field_validates_and_defaults(self):
        base = {"repository": str(self.repo), "title": "T", "objective": "O", "worker": {"kind": "codex"},
                "gates": [{"name": "g", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "One", "task": "Do"}]}
        self.assertEqual(validate(base)["retry"]["gate_timeout_retries"], 1)
        for value in (0, 1, 2, 3):
            self.assertEqual(validate({**base, "retry": {"gate_timeout_retries": value}})["retry"]["gate_timeout_retries"], value)
        for value in (4, -1, True, "1", 1.5, None):
            with self.subTest(value), self.assertRaises(ValueError):
                validate({**base, "retry": {"gate_timeout_retries": value}})

    def stability_gate(self, samples=3):
        return {"name": "slow gate", "argv": [sys.executable, "-c", "pass"], "timeout": LIMIT, "stability_runs": samples}

    def needs_double(self, seen, *, limit=2 * LIMIT, hook=None):
        """A run_gate that only finishes when given `limit` seconds or more; `seen` collects the timeouts it was run with."""
        def fake(check, path, folder, snapshot, **_options):
            seen.append(check["timeout"])
            if hook:
                hook(len(seen))
            return passing_receipt(check, folder, snapshot) if check["timeout"] >= limit else timeout_receipt(check, folder, snapshot)
        return fake

    def test_the_raised_limit_is_kept_for_the_other_samples_of_a_stability_batch(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.stability_gate()], attempts=1)
        seen = []
        with patch("nightweaver.engine.run_gate", self.needs_double(seen)):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen, [LIMIT, 2 * LIMIT, 2 * LIMIT, 2 * LIMIT], "the first sample pays for the old limit once; the others start at the new one")
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["sample"], retried["old_timeout"], retried["new_timeout"]), (1, LIMIT, 2 * LIMIT))
        planned = self.store.get(run)["spec"]["milestones"][0]["gates"][0]
        finished = self.events(run, "gate_finished")
        self.assertEqual([f["sample"] for f in finished], [1, 2, 3])
        self.assertEqual({f["contract_sha256"] for f in finished}, {contract_hash(planned)}, "every sample is matched by the plan's gate")
        self.assertEqual([f["timeout_retried"] for f in finished],
                         [{"limits": [LIMIT, 2 * LIMIT]}] + [{"limits": [LIMIT, 2 * LIMIT], "carried": True}] * 2)

    def test_the_retries_are_those_of_the_verification_not_of_each_sample(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.stability_gate()], attempts=1)
        seen = []
        with patch("nightweaver.engine.run_gate", self.needs_double(seen, limit=10 * LIMIT)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(seen, [LIMIT, 2 * LIMIT], "one retry in all, however many samples the batch has")
        self.assertEqual(len(self.events(run, "gate_timeout_retried")), 1)
        self.assertIn(f"timeout of {LIMIT} s, and again at {2 * LIMIT} s", self.store.get(run)["state"]["last_error"])

    def test_a_resumed_verification_starts_at_the_limit_its_saved_samples_needed(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.stability_gate()], attempts=1)
        seen = []
        with patch("nightweaver.engine.run_gate", self.needs_double(seen, hook=lambda n: self.store.pause(run, True) if n == 3 else None)):
            self.assertEqual(Run(self.store, run).run(), "paused")
        self.assertEqual(seen, [LIMIT, 2 * LIMIT, 2 * LIMIT])
        self.store.pause(run, False)
        resumed = []
        with patch("nightweaver.engine.run_gate", self.needs_double(resumed)):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(resumed, [2 * LIMIT], "the third sample is not run at the old limit again")
        self.assertEqual(len(self.events(run, "gate_timeout_retried")), 1)

    def test_another_gate_or_another_tree_starts_at_its_own_plan_limit(self):
        run = self.configured(fixtures.fixture("good"))
        runner, seen = Run(self.store, run), []
        check = {**runner.spec["milestones"][0]["gates"][0], "timeout": LIMIT}
        other = {**check, "name": "other gate"}
        location = {"step": 1, "attempt": 1, "gate_index": 1}
        state, step = self.store.get(run)["state"], runner.spec["milestones"][0]
        with patch("nightweaver.engine.run_gate", self.needs_double(seen)):
            for gate, tree in ((check, "tree"), (other, "tree"), (check, "other tree"), (check, "tree")):
                runner.observe_gate(gate, self.root, state, step, tree, location)
        self.assertEqual(seen, [LIMIT, 2 * LIMIT, LIMIT, 2 * LIMIT, LIMIT, 2 * LIMIT, 2 * LIMIT])

    def test_the_helper_a_judge_ledger_would_call_retries_in_a_fresh_folder(self):
        run = self.configured(fixtures.fixture("good"))
        runner, seen, folders = Run(self.store, run), [], []
        check = {**runner.spec["milestones"][0]["gates"][0], "timeout": LIMIT}

        def fresh():
            folders.append(self.root / f"ledger-retry-{len(folders)}")
            folders[-1].mkdir()
            return folders[-1]
        first = self.root / "ledger-first"
        first.mkdir()
        with patch("nightweaver.engine.run_gate", self.needs_double(seen)):
            receipt, made_in = runner.run_timed_gate(check, self.root, first, "tree", {"wave": 2, "ledger_index": 3}, fresh, "base")
        self.assertEqual((seen, made_in, observed_pass(receipt)), ([LIMIT, 2 * LIMIT], folders[0], True))
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["wave"], retried["ledger_index"], retried["log"], retried["next_log"]),
                         (2, 3, str(first / "gate.log"), str(folders[0] / "gate.log")))
        self.assertEqual(receipt["contract_sha256"], contract_hash(check))

    PROOF_SUITE = ("from pathlib import Path\nfrom xml.etree import ElementTree as ET\nimport sys, time\n"
                   "marker = Path(sys.argv[2])\nold = Path('result.txt').read_text().strip() == 'original'\n"
                   "if old and (not marker.exists() or sys.argv[3] == 'always'):\n    marker.write_text('x')\n    time.sleep(60)\n"
                   "s = ET.Element('testsuite', name='screening')\nfix = ET.SubElement(s, 'testcase', classname='Film', name='restore', time='0.001')\n"
                   "ET.SubElement(s, 'testcase', classname='Film', name='save', time='0.002')\n"
                   "if old: ET.SubElement(fix, 'failure', message='lost saved film')\n"
                   "ET.ElementTree(s).write(sys.argv[1], encoding='utf-8')\n")

    def proof_gate(self, how):
        """A proof gate whose run on the baseline commit (where result.txt says 'original') is slow: once, or every time."""
        script = self.root / "suite.py"
        script.write_text(self.PROOF_SUITE, encoding="utf-8")
        return {"name": "watchlist", "argv": [sys.executable, str(script), "{junit}", str(self.root / "baseline-ran"), how], "timeout": LIMIT,
                "junit": {"min_passed": 2},
                "proof": {"repairs": [{"classname": "Film", "name": "restore", "message_contains": "saved film"}],
                          "preserves": [{"classname": "Film", "name": "save"}], "samples": 1}}

    def test_a_proof_gate_whose_baseline_run_times_out_is_retried_not_blocked(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.proof_gate("once")], attempts=1)
        self.assertEqual(Run(self.store, run).run(), "done")
        (retried,) = self.events(run, "gate_timeout_retried")
        self.assertEqual((retried["old_timeout"], retried["new_timeout"]), (LIMIT, 2 * LIMIT))
        (finished,) = self.events(run, "gate_finished")
        self.assertEqual((finished["timeout_retried"], finished["proof"]["passed"]), ({"limits": [LIMIT, 2 * LIMIT]}, True))
        self.assertEqual(self.events(run, "attempt_rejected"), [])

    def test_a_proof_baseline_that_times_out_twice_blocks_with_the_limits_and_the_advice(self):
        run = self.configured(fixtures.fixture("good"), gates=[self.proof_gate("always")], attempts=1)
        self.assertEqual(Run(self.store, run).run(), "blocked")
        issue = self.store.get(run)["state"]["last_error"]
        for text in ("'watchlist'", "(its run on the proof baseline)", f"timeout of {LIMIT} s", f"again at {2 * LIMIT} s", "as a new run", "--retry"):
            self.assertIn(text, issue)
        self.assertEqual(len(self.events(run, "gate_timeout_retried")), 1)
        self.assertEqual(self.events(run, "attempt_rejected"), [])

    def test_only_a_baseline_run_the_controller_stopped_counts_as_a_timeout(self):
        run = self.configured(fixtures.fixture("good"))
        baseline = lambda check, folder: {**timeout_receipt(check, folder, error=BASELINE_TIMEOUT), "exit": 0}
        self.assertTrue(gate_timed_out(baseline({"name": "g"}, self.root)))
        for other in ("Baseline did not establish the requested proof: baseline-1/restore: expected a clean failed observation",
                      "Baseline did not establish the requested proof: Gate exceeded its log-size limit", "Cannot verify baseline: x"):
            self.assertFalse(gate_timed_out({**baseline({"name": "g"}, self.root), "artifact_error": other}), other)
        self.assertFalse(gate_timed_out({**baseline({"name": "g"}, self.root), "execution_error": False}))
        receipt, seen = self.observe(run, receipt=baseline)
        self.assertEqual(seen, [10, 20])
        self.assertIn("(its run on the proof baseline) exceeded its wall-clock timeout of 10 s, and again at 20 s", receipt["artifact_error"])

    def test_the_receipt_of_a_gate_that_timed_out_for_good_carries_no_advice_for_another_caller(self):
        run = self.configured(fixtures.fixture("good"))
        receipt, _ = self.observe(run)
        self.assertTrue(receipt["timeout_retried"]["timed_out"])
        self.assertNotIn("--retry", receipt["artifact_error"], "an exploration or a judge shows this text where nothing is blocked or resumable")
        self.assertIn("The worker's change was not judged.", receipt["artifact_error"])


def timeout_receipt(check, folder, tree="tree", error=GATE_TIMEOUT):
    return {"name": check["name"], "tree": tree, "log": str(folder / "gate.log"), "contract_sha256": contract_hash(check), "artifacts": [],
            "exit": None, "execution_error": True, "artifact_error": error, "duration_ms": 1}


def passing_receipt(check, folder, tree="tree"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gate.log").write_text("ok\n", encoding="utf-8")
    return {"name": check["name"], "tree": tree, "log": str(folder / "gate.log"), "contract_sha256": contract_hash(check), "artifacts": [],
            "exit": 0, "duration_ms": 1}


def blocked_issue(case, run):
    (blocked,) = [e["details"] for e in case.store.events(run, limit=None, kinds=["blocked"])]
    return blocked["issue"]


class StretchesFromEvents(unittest.TestCase):
    def total(self, *events):
        done, since = idle_from_events(events, T0)
        return done, since and (since - datetime.fromisoformat(T0)).total_seconds()

    def test_a_block_ends_at_the_retry_and_a_pause_at_the_resume(self):
        self.assertEqual(self.total(event("blocked", 100), event("blocked_run_retried", 400), event("pause_requested", 500),
                                    event("resume_requested", 800)), (600, None))
        for kind in ("blocked", "budget_exhausted", "provider_unavailable", "dispatcher_run_failed", "verification_inconsistent",
                     "repeated_failure_stopped", "pursuit_stopped", "local_advice_required"):
            with self.subTest(kind):
                self.assertEqual(self.total(event(kind, 100), event("blocked_run_retried", 160)), (60, None))

    def test_a_stretch_that_is_still_open_is_reported_by_its_start(self):
        self.assertEqual(self.total(event("blocked", 100), event("blocked_run_retried", 200), event("provider_unavailable", 300)), (100, 300))
        self.assertEqual(self.total(event("pause_requested", 50)), (0, 50))

    def test_a_block_and_a_pause_that_overlap_count_once(self):
        self.assertEqual(self.total(event("blocked", 100), event("pause_requested", 200), event("resume_requested", 300),
                                    event("blocked_run_retried", 400)), (300, None))
        self.assertEqual(self.total(event("pause_requested", 100), event("blocked", 200), event("blocked_run_retried", 250),
                                    event("resume_requested", 400)), (300, None))

    def test_parking_and_other_events_are_not_idle(self):
        self.assertEqual(self.total(event("provider_parked", 100), event("judge_invalid", 150), event("attempt_rejected", 200),
                                    event("operator_feedback", 250), event("provider_call_finished", 300)), (0, None))

    def test_a_pursuit_extension_ends_a_block_only_when_it_resumed_the_run(self):
        self.assertEqual(self.total(event("blocked", 100), event("pursuit_extended", 200, resumed=True, phase="judging")), (100, None))
        self.assertEqual(self.total(event("blocked", 100), event("pursuit_extended", 200, resumed=False, phase="blocked")), (0, 100))
        self.assertEqual(self.total(event("blocked", 100), event("pursuit_extended", 200, phase="planning")), (100, None), "an older event that said only where it resumed")

    def test_a_runner_that_stopped_after_the_resume_saw_a_stale_pause(self):
        self.assertEqual(self.total(event("pause_requested", 100), event("resume_requested", 200), event("runner_stopped", 210)), (100, None))
        self.assertEqual(self.total(event("runner_stopped", 100), event("resume_requested", 200)), (100, None), "a pause that was set without an event")

    def test_stretches_before_the_clock_started_are_not_counted(self):
        self.assertEqual(self.total(event("pause_requested", -500), event("resume_requested", -100), event("blocked", -50),
                                    event("blocked_run_retried", 30)), (30, None))

    def test_the_clock_counts_the_ledger_and_the_open_stretch(self):
        now = datetime.fromisoformat(T0) + timedelta(seconds=1000)
        state = {"started_at": T0, "paused_seconds": 300, "idle_since": (now - timedelta(seconds=200)).isoformat()}
        self.assertEqual(run_clock(state, now), (1000, 500))
        self.assertEqual(run_clock({"started_at": T0}, now), (1000, 1000), "a state without a ledger measures wall time")
        self.assertEqual(run_clock({"started_at": T0, "paused_seconds": 5000}, now), (1000, 0))

    def test_budget_status_shows_active_beside_wall_time(self):
        now = datetime.fromisoformat(T0) + timedelta(seconds=1000)
        status = budget_status({"budgets": {"max_seconds": 800, "max_calls": 3}}, {"started_at": T0, "paused_seconds": 600, "provider_calls": 1}, now)
        self.assertEqual(status["used"]["max_seconds"], 400)
        self.assertEqual(status["remaining"], {"max_calls": 2, "max_seconds": 400})
        self.assertEqual(status["time"], {"wall_seconds": 1000, "active_seconds": 400, "idle_seconds": 600})
        plain = budget_status({"budgets": {"max_seconds": 6}}, {"started_at": T0}, datetime.fromisoformat(T0) + timedelta(seconds=4))
        self.assertEqual((plain["remaining"], plain["time"]["idle_seconds"]), ({"max_seconds": 2}, 0))
        self.assertEqual(budget_status({}, {})["time"], {"wall_seconds": 0, "active_seconds": 0, "idle_seconds": 0})


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ActiveTimeBudget(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def started(self, clock, seconds=3600):
        run = self.configured(fixtures.fixture("good"), budgets={"max_seconds": seconds}, attempts=1)
        self.store.transition(run, "run_started", {}, started_at=clock.at(0))
        return run

    def spent(self, run, store=None):
        try:
            Run(store or self.store, run).check_budget()
        except BudgetExceeded as error:
            return str(error)
        return None

    def state(self, run):
        return self.store.get(run)["state"]

    def strip_ledger(self, run):
        """The state 0.19 saved: it kept no ledger."""
        with closing(sqlite3.connect(self.store.root / "runs.sqlite3")) as db, db:
            state = json.loads(db.execute("SELECT state FROM runs WHERE id=?", (run,)).fetchone()[0])
            for key in ("paused_seconds", "idle_since"):
                state.pop(key, None)
            db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run))

    def age(self, run, seconds):
        """Move everything the run recorded `seconds` into the past, as if that long had gone by since."""
        def back(stamp):
            return (datetime.fromisoformat(stamp) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
        with closing(sqlite3.connect(self.store.root / "runs.sqlite3")) as db, db:
            state = json.loads(db.execute("SELECT state FROM runs WHERE id=?", (run,)).fetchone()[0])
            for key in ("started_at", "idle_since"):
                if state.get(key):
                    state[key] = back(state[key])
            db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run))
            for seq, stamp in db.execute("SELECT seq, time FROM events WHERE run_id=?", (run,)).fetchall():
                db.execute("UPDATE events SET time=? WHERE seq=?", (back(stamp), seq))

    def test_a_blocked_stretch_is_not_spent(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(1000)
            self.store.transition(run, "blocked", {"issue": "waiting"}, phase="blocked", blocked_from="ready")
            self.assertEqual(self.state(run)["idle_since"], clock(), "the ledger is written with the phase it follows")
            clock.at(1000 + 34200)  # a night and a morning waiting for a person
            self.assertIsNone(self.spent(run), "a blocked run's budget stands still")
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            state = self.state(run)
            self.assertEqual((state["paused_seconds"], state["idle_since"]), (34200, None))
            clock.at(35200 + 2000)
            self.assertIsNone(self.spent(run), "3000 s of work is inside 3600 s although 37200 s of wall time have passed")
            self.assertEqual(run_clock(self.state(run), clock.now), (37200, 3000))
            clock.at(35200 + 2600)
            message = self.spent(run)
            self.assertIn("Run elapsed-time budget exhausted", message)
            self.assertIn("9.5 h blocked or paused is not counted", message)

    def test_a_paused_stretch_is_not_spent(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(900)
            self.assertTrue(self.store.pause(run, True))
            self.assertIsNotNone(self.state(run)["idle_since"])
            clock.at(900 + 20000)
            self.assertIsNone(self.spent(run))
            self.assertTrue(self.store.pause(run, False))
            self.assertEqual(self.state(run)["paused_seconds"], 20000)
            clock.at(20900 + 2000)
            self.assertIsNone(self.spent(run))
            clock.at(20900 + 2700)
            self.assertIsNotNone(self.spent(run))

    def test_a_pause_inside_a_block_is_counted_once(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(100)
            self.store.transition(run, "blocked", {}, phase="blocked", blocked_from="ready")
            clock.at(200)
            self.store.pause(run, True)
            clock.at(300)
            self.store.pause(run, False)
            self.assertIsNotNone(self.state(run)["idle_since"], "still blocked after the resume")
            self.assertEqual(self.state(run)["paused_seconds"], 0)
            clock.at(400)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            self.assertEqual(self.state(run)["paused_seconds"], 300)

    def test_a_blocked_run_that_is_paused_for_good_stays_idle_after_it_is_retried(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(100)
            self.store.transition(run, "blocked", {}, phase="blocked", blocked_from="ready")
            self.store.pause(run, True)
            clock.at(500)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            self.assertEqual(self.state(run)["paused_seconds"], 0, "paused still: the stretch continues")
            clock.at(900)
            self.store.pause(run, False)
            self.assertEqual(self.state(run)["paused_seconds"], 800)

    def test_time_parked_on_a_usage_limit_still_counts(self):
        with Clock() as clock:
            run = self.started(clock, 3600)
            clock.at(100)
            self.store.transition(run, "provider_parked", {"issue": "limit"}, retry_at=clock.stamp(4000), parks=1)
            clock.at(3000)
            self.assertIsNone(self.spent(run))
            clock.at(3700)
            self.assertIsNotNone(self.spent(run), "the run waited on its own clock; the budget ran")
            state = self.state(run)
            self.assertEqual((state["paused_seconds"], state["idle_since"]), (0, None))

    def test_a_park_is_planned_against_the_budget_that_is_left_after_the_idle_time(self):
        with Clock() as clock:
            run = self.started(clock, 3600)
            clock.at(1000)
            self.store.transition(run, "blocked", {}, phase="blocked", blocked_from="ready")
            clock.at(1000 + 50000)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            clock.at(51000 + 2000)  # 3000 s of the 3600 s are spent: 600 s are left
            state, spec, now = self.state(run), self.store.get(run)["spec"], clock.now
            soon = park_plan(spec, state, ProviderUnavailable("limit", "usage_limit", reset_at=now + timedelta(seconds=300)), now, "codex")
            self.assertIsNone(soon["block"], "300 s and the 90 s margin fit in the 600 s left")
            late = park_plan(spec, state, ProviderUnavailable("limit", "usage_limit", reset_at=now + timedelta(seconds=900)), now, "codex")
            self.assertIn("after this run's time budget", late["block"])

    def test_the_ledger_survives_a_crash_and_a_restart(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(500)
            self.store.transition(run, "blocked", {}, phase="blocked", blocked_from="ready")
            clock.at(40500)  # the runner is gone; another process opens the same database later
            reopened = Store(self.root / "private data")
            self.assertIsNone(self.spent(run, reopened))
            self.assertEqual(run_clock(reopened.get(run)["state"], clock.now), (40500, 500))
            reopened.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            clock.at(40560)
            crashed_again = Store(self.root / "private data")
            self.assertEqual(run_clock(crashed_again.get(run)["state"], clock.now), (40560, 560))
            self.assertEqual(crashed_again.get(run)["state"]["paused_seconds"], 40000)

    def test_a_run_blocked_for_a_day_resumes_inside_its_budget(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, budgets={"max_seconds": 300})
        with patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("Selected profile needs login", "authentication")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertIsNotNone(self.state(run)["idle_since"])
        self.age(run, 100000)  # more than a day of waiting for someone to log in
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        state = self.state(run)
        self.assertGreaterEqual(state["paused_seconds"], 100000)
        self.assertEqual(state["phase"], "done")
        status = budget_report(self.store, run)
        self.assertGreater(status["time"]["wall_seconds"], 100000)
        self.assertLess(status["time"]["active_seconds"], 300)
        self.assertGreaterEqual(status["time"]["idle_seconds"], 100000)
        self.assertEqual(status["used"]["max_seconds"], status["time"]["active_seconds"])
        self.assertEqual([e for e in self.store.events(run, limit=None, kinds=["active_time_derived"])], [], "a run made by 0.19.1 has its ledger from the start")

    def test_a_run_made_by_0_19_is_read_from_its_events_on_resume(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, budgets={"max_seconds": 300})
        with patch("nightweaver.engine.invoke", side_effect=ProviderUnavailable("Selected profile needs login", "authentication")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.strip_ledger(run)
        self.age(run, 100000)
        self.assertEqual(Run(self.store, run).run(retry=True), "done")
        (derived,) = [e["details"] for e in self.store.events(run, limit=None, kinds=["active_time_derived"])]
        self.assertEqual((derived["source"], derived["paused_seconds"]), ("events", 0))
        self.assertIsNotNone(derived["idle_since"], "it was blocked when it was resumed")
        state = self.state(run)
        self.assertGreaterEqual(state["paused_seconds"], 100000)
        self.assertIsNone(state["idle_since"])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(self.store.events(run, limit=None, kinds=["active_time_derived"])), 1, "derived once")

    def timeline(self, clock, run):
        """blocked 300 s, paused 300 s, parked 1000 s (counts), done at 2000 s: 600 s idle."""
        clock.at(100)
        self.store.transition(run, "blocked", {"issue": "x"}, phase="blocked", blocked_from="ready")
        clock.at(400)
        self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
        clock.at(500)
        self.store.pause(run, True)
        clock.at(800)
        self.store.pause(run, False)
        clock.at(900)
        self.store.transition(run, "provider_parked", {"issue": "limit"}, retry_at=clock.stamp(1900), parks=1)
        clock.at(2000)
        self.store.transition(run, "run_completed", {"commit": "c"}, phase="done")

    def test_metrics_and_the_budget_report_show_active_and_wall_time(self):
        with Clock() as clock:
            run = self.started(clock, 3600)
            self.timeline(clock, run)
            self.assertEqual(self.state(run)["paused_seconds"], 600)
            measured = metrics(self.store, run)
            self.assertEqual((measured["elapsed_seconds"], measured["active_seconds"], measured["idle_seconds"]), (2000, 1400, 600))
            report = budget_report(self.store, run)
            self.assertEqual(report["time"], {"wall_seconds": 2000, "active_seconds": 1400, "idle_seconds": 600})
            self.assertEqual((report["used"]["max_seconds"], report["remaining"]["max_seconds"]), (1400, 2200))

    def test_a_0_19_run_reports_the_same_numbers_without_being_resumed(self):
        with Clock() as clock:
            run = self.started(clock, 3600)
            self.timeline(clock, run)
            self.strip_ledger(run)
            self.assertNotIn("paused_seconds", self.state(run))
            self.assertEqual(budget_report(self.store, run)["time"], {"wall_seconds": 2000, "active_seconds": 1400, "idle_seconds": 600})
            self.assertEqual(metrics(self.store, run)["idle_seconds"], 600)
            self.assertNotIn("paused_seconds", self.state(run), "reading never writes")

    def test_a_0_19_state_is_derived_by_the_first_change_made_to_it(self):
        with Clock() as clock:
            run = self.started(clock, 3600)
            clock.at(100)
            self.store.transition(run, "blocked", {"issue": "x"}, phase="blocked", blocked_from="ready")
            clock.at(400)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            clock.at(500)
            self.store.pause(run, True)
            clock.at(800)
            self.store.pause(run, False)
            clock.at(1000)
            self.store.transition(run, "blocked", {"issue": "y"}, phase="blocked", blocked_from="ready")
            self.strip_ledger(run)
            clock.at(5000)  # blocked since 1000
            self.assertEqual(self.store.derive_clock(run), True)
            state = self.state(run)
            self.assertEqual((state["paused_seconds"], state["idle_since"]), (600, clock.at(1000)))
            self.assertEqual(self.store.derive_clock(run), False)
            clock.at(5000)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            self.assertEqual(self.state(run)["paused_seconds"], 4600)
            # The same, with no derive_clock call: any writer settles a state without a ledger.
            other = self.started(clock, 3600)
            clock.at(100)
            self.store.transition(other, "blocked", {}, phase="blocked", blocked_from="ready")
            self.strip_ledger(other)
            clock.at(700)
            self.store.transition(other, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            self.assertEqual(self.state(other)["paused_seconds"], 600)

    def derived(self, run):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=["active_time_derived"])]

    def blocked_0_19_run(self, clock, run=None):
        """A run made by 0.19: blocked at 100 s and still blocked (a pause from 150 to 250 s inside it), no ledger; it is 5000 s."""
        run = run or self.started(clock)
        clock.at(100)
        self.store.transition(run, "blocked", {"issue": "x"}, phase="blocked", blocked_from="ready")
        clock.at(150)
        self.store.pause(run, True)
        clock.at(250)
        self.store.pause(run, False)
        self.strip_ledger(run)
        clock.at(5000)
        return run

    def test_whichever_writer_reaches_a_0_19_state_first_says_it_derived_the_ledger(self):
        # settle_gates is revise_spec, and in a pursuit it runs before anything else touches the run: the CROW case.
        writers = {
            "revise_spec": lambda run: self.store.revise_spec(run, lambda spec: None, "gates_split", {}),
            "transition": lambda run: self.store.transition(run, "feedback_noted", {}),
            "pause": lambda run: self.store.pause(run, True),
            "derive_clock": lambda run: self.store.derive_clock(run),
        }
        for name, write in writers.items():
            with self.subTest(name), Clock() as clock:
                run = self.blocked_0_19_run(clock)
                write(run)
                (derived,) = self.derived(run)
                self.assertEqual((derived["source"], derived["paused_seconds"], derived["idle_since"]), ("events", 0, clock.stamp(100)),
                                 "what the events said, not what the writer then made of it")
                self.assertEqual(self.state(run)["paused_seconds"], 0)
                self.assertEqual(self.state(run)["idle_since"], clock.stamp(100), "blocked since 100 s, and nothing here ended it")
                self.assertFalse(self.store.derive_clock(run), "once")
                self.store.transition(run, "feedback_noted", {})
                self.assertEqual(len(self.derived(run)), 1)

    def test_the_derived_event_comes_before_the_event_of_the_writer_that_caused_it(self):
        with Clock() as clock:
            run = self.blocked_0_19_run(clock)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            kinds = [e["kind"] for e in self.store.events(run, limit=None)]
            self.assertEqual(kinds[-2:], ["active_time_derived", "blocked_run_retried"])
            self.assertEqual(self.state(run)["paused_seconds"], 4900, "blocked 100 to 5000 s, the pause inside it counted once")

    def test_a_pursuit_made_by_0_19_derives_its_ledger_before_its_gates_are_settled(self):
        with Clock() as clock:
            run = self.blocked_0_19_run(clock)
            runner = Run(self.store, run)
            runner.spec["pursue"] = {"max_waves": 1}
            seen = []

            def settle(arg):
                seen.append((self.derived(run), "paused_seconds" in self.state(run)))
                raise RuntimeError("stop here")
            with patch("nightweaver.pursuit.settle_gates", settle), self.assertRaises(RuntimeError):
                runner.run(retry=True)
            ((derived, has_ledger),) = seen
            self.assertTrue(has_ledger)
            self.assertEqual([d["paused_seconds"] for d in derived], [0])

    def test_a_state_that_says_blocked_is_trusted_over_events_that_miss_the_block(self):
        with Clock() as clock:
            run = self.started(clock)
            clock.at(100)
            self.store.transition(run, "mystery_stop", {}, phase="blocked", blocked_from="ready")  # no event the clock knows
            self.strip_ledger(run)
            clock.at(700)
            self.store.transition(run, "blocked_run_retried", {}, phase="ready", blocked_from=None)
            self.assertEqual(self.state(run)["paused_seconds"], 600, "idle since the run last changed")

    def test_the_clock_waits_for_the_run_to_start(self):
        with Clock() as clock:
            run = self.configured(fixtures.fixture("good"), budgets={"max_seconds": 60})
            self.store.pause(run, True)
            clock.at(500)
            self.store.pause(run, False)
            self.assertNotIn("paused_seconds", self.state(run), "a run that has not started has no clock to keep")
            self.store.transition(run, "run_started", {}, started_at=clock.at(600))
            self.assertEqual((self.state(run)["paused_seconds"], self.state(run)["idle_since"]), (0, None))

    def test_a_fresh_run_does_not_pay_an_event_for_its_ledger(self):
        run = self.configured(fixtures.fixture("good"), attempts=1)
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(self.state(run)["paused_seconds"], 0)
        self.assertEqual(self.store.events(run, limit=None, kinds=["active_time_derived"]), [])


if __name__ == "__main__":
    unittest.main()
