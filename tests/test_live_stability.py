"""Exercise the live controller, its durable receipts, and its benchmark verdicts."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_windows as fixtures
from nightweaver.benchmark import scored
from nightweaver.diagnostics import preflight, replay_checks
from nightweaver.engine import Run, git
from nightweaver.insights import diagnosis
from nightweaver.reports import metrics, recovery_metrics
from nightweaver.spec import gate_spec


CHECKER = """
from pathlib import Path
import json, sys, time
counter, script, wait = map(Path, sys.argv[1:4])
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
outcomes = json.loads(script.read_text())
value = outcomes[min(index, len(outcomes)-1)]
print('sample', index + 1, flush=True)
if value == 'wait':
    wait.touch()
    time.sleep(60)
if value == 'mutate':
    Path('result.txt').write_text('changed by a gate')
sys.exit(int(value == 'fail'))
"""


@unittest.skipUnless(os.name == "nt", "Windows-native runner")
class LiveStability(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def prepare(self, outcomes, *, count=2, pin=False, **plan):
        self.counter = self.root / "counter"
        self.script = self.root / "sequence"
        self.ready = self.root / "waiting"
        self.check = self.root / "probe.py"
        self.check.write_bytes(CHECKER.encode())
        self.script.write_text(json.dumps(outcomes))
        gate = {
            "name": "repeatable",
            "stability_runs": count,
            "argv": [sys.executable, str(self.check), str(self.counter), str(self.script), str(self.ready)],
        }
        if pin:
            gate["pinned_inputs"] = [
                {"path": str(self.check), "sha256": sha256(self.check.read_bytes()).hexdigest()}
            ]
        return self.configured(fixtures.fixture("good"), gates=[gate], **plan)

    def samples(self, rid):
        return [e["details"] for e in self.store.events(rid, limit=None) if e["kind"] == "gate_finished"]

    def test_transient_recovers_without_worker_and_replay_honors_policy(self):
        rid = self.prepare(["fail", "pass", "pass", "pass"])
        self.assertEqual(Run(self.store, rid).run(), "done")
        receipts = self.samples(rid)
        self.assertEqual([r["sample"] for r in receipts], [1, 2, 3, 4])
        self.assertEqual(len(set(r["tree"] for r in receipts)), 1)
        self.assertEqual(len(set(r["log"] for r in receipts)), 4)
        for r in receipts:
            self.assertEqual(sha256(Path(r["log"]).read_bytes()).hexdigest(), r["log_sha256"])
        decision = next(e["details"] for e in self.store.events(rid) if e["kind"] == "checks_passed")
        self.assertEqual([r["exit"] for r in decision["checks"]], [0, 0])
        self.assertEqual([r["exit"] for r in decision["checks"][0]["prior_mixed_round"]], [1, 0])
        m = metrics(self.store, rid)
        self.assertEqual(
            (m["provider_calls"], m["failed_attempts"], m["gate_confirmation_recoveries"]), (1, 0, 1)
        )
        self.assertEqual(replay_checks(self.store, rid)["stability"], {"repeatable": "all_passed"})
        self.assertEqual(self.counter.read_text(), "6")
        self.assertEqual(len(replay_checks(self.store, rid, repeat=1)["gates"]), 1)

    def test_two_mixed_rounds_block_resuming_rechecks_the_same_candidate(self):
        rid = self.prepare(["pass", "fail", "fail", "pass"])
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        m = metrics(self.store, rid)
        self.assertEqual(m["outcome"], "verification_inconsistent")
        self.assertFalse(scored(m))
        self.assertEqual(m["provider_calls"], 1)
        self.assertEqual(
            diagnosis(self.store, rid)["last_failure_event"]["kind"], "verification_inconsistent"
        )
        self.assertEqual(self.store.get(rid)["state"]["accepted"], self.head)
        self.script.write_text('["pass"]')
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        receipts = self.samples(rid)
        self.assertEqual([r["sample"] for r in receipts], [1, 2, 3, 4, 1, 2])
        self.assertEqual(len({r["verification_batch"] for r in receipts}), 2)
        self.assertEqual(len({r["tree"] for r in receipts}), 1)
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_confirmed_failure_returns_to_worker_and_advances(self):
        rid = self.configured(
            fixtures.fixture("implement"),
            gates=[{"name": "value", "argv": fixtures.fixture("gate"), "stability_runs": 2}],
            milestones=[
                {"title": "Prepare fixture", "task": "Set the value"},
                {
                    "title": "Deliver update",
                    "task": "Continue first milestone",
                    "worker": {"kind": "command", "argv": fixtures.fixture("continue")},
                },
            ],
            acceptance=["The second milestone creates delivery.txt."],
        )
        self.assertEqual(Run(self.store, rid).run(), "done")
        m = metrics(self.store, rid)
        self.assertEqual((m["provider_calls"], m["failed_attempts"], m["accepted_milestones"]), (3, 1, 2))
        self.assertEqual([r["exit"] for r in self.samples(rid)], [2, 2, 0, 0, 0, 0])

    def interrupt_after_first(self, rid, *, terminate=False):
        cmd = [sys.executable, "-m", "nightweaver", "--home", str(self.store.root), "run", rid]
        with subprocess.Popen(
            cmd,
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as child:
            try:
                deadline = time.monotonic() + 35
                while not self.ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.08)
                self.assertTrue(self.ready.exists())
                if terminate:
                    child.kill()
                    child.wait(15)
                else:
                    self.store.pause(rid, True)
                    self.assertEqual(child.wait(15), 2)
            finally:
                if child.poll() is None:
                    child.kill()
        self.assertEqual(len(self.samples(rid)), 1)
        if not terminate:
            self.store.pause(rid, False)

    def test_restart_preserves_the_first_failure_and_distinct_logs(self):
        rid = self.prepare(["fail", "wait", "pass", "pass", "pass"], pin=True)
        self.interrupt_after_first(rid)
        self.assertEqual(Run(self.store, rid).run(), "done")
        receipts = self.samples(rid)
        self.assertEqual([r["sample"] for r in receipts], [1, 2, 3, 4])
        self.assertEqual([r["exit"] for r in receipts], [1, 0, 0, 0])
        self.assertEqual(len({r["verification_batch"] for r in receipts}), 1)
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_restart_checks_recorded_evidence_before_using_it(self):
        rid = self.prepare(["pass", "wait", "pass"])
        self.interrupt_after_first(rid)
        Path(self.samples(rid)[0]["log"]).write_text("edited receipt")
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertIn("Saved gate evidence changed", self.store.get(rid)["state"]["last_error"])
        self.assertEqual(self.counter.read_text(), "2")
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_hard_stop_and_changed_pin_preserve_first_receipt(self):
        rid = self.prepare(["fail", "wait", "pass", "pass", "pass"], pin=True)
        self.interrupt_after_first(rid, terminate=True)
        first = self.samples(rid)[0]
        self.check.write_bytes(b"raise SystemExit(0)")
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertIn("pinned check input", self.store.get(rid)["state"]["last_error"])
        self.assertEqual(self.samples(rid), [first])
        self.check.write_bytes(CHECKER.encode())
        self.assertEqual(Run(self.store, rid).run(retry=True), "done")
        self.assertEqual(metrics(self.store, rid)["provider_calls"], 1)

    def test_mixed_then_confirmed_failure_repairs_within_attempt_budget(self):
        rid = self.prepare(["pass", "fail", "fail", "fail", "pass", "pass"], attempts=2)
        self.assertEqual(Run(self.store, rid).run(), "done")
        receipts = self.samples(rid)
        self.assertEqual([r["attempt"] for r in receipts], [1, 1, 1, 1, 2, 2])
        m = metrics(self.store, rid)
        self.assertEqual(
            (m["provider_calls"], m["gate_confirmation_rounds"], m["gate_confirmation_recoveries"]),
            (2, 1, 0),
        )

    def test_source_mutation_is_never_an_agreeing_pass(self):
        rid = self.prepare(["mutate"], attempts=1)
        self.assertEqual(Run(self.store, rid).run(), "blocked")
        self.assertIn("source_error", self.samples(rid)[0])
        self.assertEqual(self.counter.read_text(), "1")
        self.assertEqual(self.store.get(rid)["state"]["accepted"], self.head)

    def test_preflight_pins_checkout_bytes_from_the_committed_attributes(self):
        git(self.repo, "config", "core.autocrlf", "false")
        name = "gate[1].ps1"
        raw = b"Write-Output ready\r\n"
        attrs = self.repo / ".gitattributes"
        attrs.write_bytes(b"*.ps1 text eol=crlf\n")
        (self.repo / name).write_bytes(raw)
        git(self.repo, "add", "--", name, ".gitattributes")
        git(self.repo, "commit", "-qm", "Pin a CRLF fixture")
        gate = {
            "name": "value",
            "argv": fixtures.fixture("gate"),
            "pinned_inputs": [{"path": name, "sha256": sha256(raw).hexdigest()}],
        }
        rid = self.configured(fixtures.fixture("good"), gates=[gate])
        attrs.write_bytes(b"*.ps1 text eol=lf\n")
        (self.repo / name).write_bytes(b"uncommitted change\n")
        self.assertTrue(preflight(self.store, self.store.get(rid)["spec"])["ready"])
        self.assertEqual(Run(self.store, rid).run(), "done")
        self.assertEqual((self.repo / name).read_bytes(), b"uncommitted change\n")


class StabilityContract(unittest.TestCase):
    def test_range_and_single_sample_compatibility(self):
        gate = {"name": "check", "argv": [sys.executable, "--version"]}
        self.assertEqual(gate_spec({**gate, "stability_runs": 1}), gate_spec(gate))
        for value in (0, 6, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate_spec({**gate, "stability_runs": value})

    def test_unusable_evidence_cannot_make_a_test_look_inconsistent(self):
        plain = {"name": "A", "tree": "T", "contract_sha256": "C", "exit": 0}
        for extra in ({"artifact_error": "missing"}, {"exit": None}, {"source_error": "changed"}):
            timeline = [{"kind": "gate_finished", "details": row} for row in ({**plain, **extra}, plain)]
            self.assertEqual(recovery_metrics(timeline)["mixed_gate_results"], [])
