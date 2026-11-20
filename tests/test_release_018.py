import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_windows as fixtures

from nightweaver.cli import main, with_live_status
from nightweaver.engine import Run, git
from nightweaver.inspection import wait_for_run
from nightweaver.operations import attention
from nightweaver.process import BusyRun, run_lock, runner_active
from nightweaver.recovery import recovery_status, supervise
from nightweaver.repair import failure_brief
from nightweaver.reports import metrics, recovery_metrics, run_report


@unittest.skipUnless(os.name == "nt", "Uses native Windows process ownership")
class RecoveryStudy(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def cli(self, *args, code=0):
        output = io.StringIO()
        with redirect_stdout(output):
            actual = main(["--home", str(self.store.root), *args])
        self.assertEqual(actual, code, output.getvalue())
        return json.loads(output.getvalue())

    def interrupt_second_milestone(self, limit, lose_guard=False):
        marker = self.root / "fixture-child.pid"
        foundation = [
            sys.executable,
            "-c",
            "from pathlib import Path;Path('foundation.txt').write_text('accepted')",
        ]
        check = [
            sys.executable,
            "-c",
            "from pathlib import Path;assert Path('foundation.txt').read_text()=='accepted'",
        ]
        run = self.configured(
            foundation,
            title="Recovery across two milestones",
            gates=[],
            milestones=[
                {
                    "title": "Establish the baseline",
                    "task": "Create foundation.txt",
                    "paths": ["foundation.txt"],
                    "gates": [
                        {"name": "Foundation has the accepted value", "argv": check}
                    ],
                    "retain_gates": "subsequent",
                },
                {
                    "title": "Deliver after interruption",
                    "task": "Complete result.txt",
                    "paths": ["result.txt"],
                    "worker": {
                        "kind": "command",
                        "argv": fixtures.fixture("interrupt", str(marker)),
                    },
                    "gates": [
                        {"name": "Delivery is ready", "argv": fixtures.fixture("gate")}
                    ],
                },
            ],
        )
        command = [
            sys.executable,
            "-m",
            "nightweaver",
            "--home",
            str(self.store.root),
            "guard",
            run,
            "--max-restarts",
            str(limit),
        ]
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        ) as guard:
            deadline = time.monotonic() + 40
            while (
                not marker.exists()
                and guard.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertTrue(marker.is_file(), self.store.get(run)["state"])
            before = recovery_status(self.store, run)
            self.assertEqual(before["checkpoint"]["completed"], 1)
            self.assertTrue(before["guard_active"])
            with self.assertRaises(BusyRun):
                supervise(self.store, run)
            pid = self.store.events(run, limit=1, kinds=["guard_child_started"])[0][
                "details"
            ]["pid"]
            if lose_guard:
                guard.terminate()
                guard.wait(timeout=8)
            # Use the same exit number as a lock-contention return to prove that
            # the disposition requires a matching child receipt, not just a code.
            os.kill(pid, 4)
            out, err = guard.communicate(timeout=45)
            if lose_guard:
                deadline = time.monotonic() + 8
                while (
                    runner_active(self.store.directory(run) / "runner.lock")
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                launched = self.cli("dispatch", "--recover-guards")
                self.assertEqual([r["run_id"] for r in launched], [run])
                self.assertEqual(wait_for_run(self.store, run, 45)[1], 0)
                deadline = time.monotonic() + 8
                while (
                    runner_active(self.store.directory(run) / "guard.lock")
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                return run, self.cli("restarts", run)
            self.assertEqual(guard.returncode, 0 if limit else 5, out + err)
        events = self.cli("restarts", run)
        exits = [e["details"] for e in events if e["kind"] == "guard_child_exited"]
        self.assertTrue(exits[0]["unexpected"])
        self.assertEqual(exits[0]["accepted"], before["checkpoint"]["accepted"])
        self.assertEqual(exits[0]["exit"], 4)
        return run, exits

    def test_real_process_death_resumes_same_committed_milestone(self):
        run, exits = self.interrupt_second_milestone(1)
        self.assertEqual(len(exits), 2)
        self.assertNotEqual(exits[0]["pid"], exits[1]["pid"])
        self.assertEqual(self.cli("recovery", run)["remaining"], 0)
        self.assertEqual(
            self.store.accepted_milestones(run)[0]["commit"], exits[0]["accepted"]
        )
        self.assertEqual(len(self.store.accepted_milestones(run)), 2)
        self.assertEqual(
            git(self.repo, "show", f"nightweaver/{run}:result.txt"), "ready"
        )
        self.assertEqual(metrics(self.store, run)["supervised_restarts"], 1)
        self.assertEqual(self.cli("recovery-queue"), [])

    def test_lifetime_cap_survives_new_supervisor_and_wait(self):
        run, exits = self.interrupt_second_milestone(0)
        self.assertEqual(len(exits), 1)
        self.assertEqual(self.cli("recovery-queue")[0]["disposition"], "limit_reached")
        self.assertEqual(wait_for_run(self.store, run, 0)[1], 5)
        self.assertEqual(self.cli("guard", run, code=5)["phase"], "limit_reached")
        self.assertEqual(
            len(self.store.events(run, limit=None, kinds=["guard_child_started"])), 1
        )
        self.assertEqual(self.cli("guard", run, "--max-restarts", "1")["phase"], "done")
        self.assertEqual(self.cli("recovery", run)["restarts_used"], 1)

    def test_dispatcher_recovers_loss_of_both_processes_only_for_enrolled_run(self):
        ordinary = self.configured(fixtures.fixture("good"))
        run, events = self.interrupt_second_milestone(1, lose_guard=True)
        uncertain = [
            e["details"] for e in events if e["kind"] == "guard_unobserved_exit"
        ]
        self.assertEqual(len(uncertain), 1)
        self.assertNotIn("exit", uncertain[0])
        self.assertEqual(
            uncertain[0]["accepted"], self.store.accepted_milestones(run)[0]["commit"]
        )
        self.assertEqual(len(self.store.accepted_milestones(run)), 2)
        self.assertEqual(self.cli("recovery", run)["remaining"], 0)
        self.assertEqual(self.store.get(ordinary)["state"]["phase"], "queued")

    def test_dedicated_launch_from_public_entrypoints(self):
        raw = {
            "repository": str(self.repo),
            "title": "Public launch",
            "objective": "Deliver",
            "worker": {"kind": "command", "argv": fixtures.fixture("good")},
            "gates": [{"name": "Ready", "argv": fixtures.fixture("gate")}],
            "milestones": [{"title": "Write result", "task": "Write result.txt"}],
        }
        path = self.root / "plan.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        launch = self.cli(
            "add", str(path), "--start", "--guard-restarts", "0", "--wait"
        )
        self.assertEqual(launch["phase"], "done")
        self.assertEqual(self.cli("recovery", launch["run_id"])["restart_limit"], 0)
        plan = self.root / "planned.json"
        plan.write_text(
            json.dumps(
                {"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}
            ),
            encoding="utf-8",
        )
        raw.pop("milestones")
        path.write_text(json.dumps(raw), encoding="utf-8")
        launch = self.cli(
            "goal",
            str(path),
            "Deliver result",
            "--plan-from",
            str(plan),
            "--start",
            "--guard-restarts",
            "1",
        )
        self.assertIn("supervisor_pid", launch)
        self.assertEqual(wait_for_run(self.store, launch["run_id"], 40)[1], 0)
        deadline = time.monotonic() + 8
        while (
            runner_active(self.store.directory(launch["run_id"]) / "guard.lock")
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_delivery.py").write_text(
            "from pathlib import Path\nimport unittest\nclass Delivery(unittest.TestCase):\n"
            "    def test_delivery(self):\n        self.assertEqual(Path('result.txt').read_text(), 'ready')\n",
            encoding="utf-8",
        )
        git(self.repo, "add", "tests/test_delivery.py")
        git(self.repo, "commit", "-m", "Declare the repository's delivery check")
        # Only substitute the model route. Discovery, the foreground CLI, Git,
        # supervisor, worker process and independent gate run as in a OneShot.
        with patch(
            "nightweaver.cli.planner_route",
            return_value={"kind": "command", "argv": fixtures.fixture("good")},
        ):
            result = self.cli(
                "oneshot",
                str(self.repo),
                "Deliver ready",
                "--direct",
                "--guard-restarts",
                "1",
                "--wait",
            )
        self.assertEqual(result["phase"], "done")
        self.assertEqual(len(self.store.accepted_milestones(result["run_id"])), 1)

    def test_busy_pause_and_queue_have_no_run_side_effects(self):
        run = self.configured(fixtures.fixture("good"))
        self.store.pause(run, True)
        self.assertEqual(supervise(self.store, run), "paused")
        self.assertEqual(Run(self.store, run).run(), "paused")
        self.assertNotIn("started_at", self.store.get(run)["state"])
        self.store.pause(run, False)
        lock = self.repo / ".git" / "nightweaver.lock"
        with run_lock(lock):
            self.assertEqual(supervise(self.store, run), "contended")
            self.assertNotIn("started_at", self.store.get(run)["state"])
            self.store.transition(
                run,
                "blocked",
                phase="blocked",
                attempts_used=3,
                pending_call={"call": 1},
            )
            original = self.store.get(run)
            with self.assertRaises(BusyRun):
                Run(self.store, run).run(retry=True)
            self.assertEqual(self.store.get(run), original)
        self.store.pause(run, True)
        self.assertEqual(attention(self.store)[0]["reason"], "blocked")
        queued = self.configured(fixtures.fixture("good"), enqueue=True)
        self.assertEqual(supervise(self.store, queued), "queue_owned")
        self.store.transition(queued, "orphan_fixture", after=["0123456789ab"])
        self.assertEqual(
            with_live_status(self.store, self.store.get(queued))[
                "missing_prerequisites"
            ],
            ["0123456789ab"],
        )
        self.assertEqual(len(self.cli("list")), 2)

    def test_fixed_event_windows_handoff_and_snapshot(self):
        run = self.configured(fixtures.fixture("good"))
        self.store.transition(
            run,
            "attempt_rejected",
            {"step": 1, "attempt": 1, "phase": "worker", "issue": "lost", "tree": None},
        )
        self.assertIn(
            "tree unknown", failure_brief(self.store, run, {"step": 0, "attempt": 1}, 3)
        )
        other = self.configured(fixtures.fixture("good"))
        with self.store.snapshot() as reader:
            first = reader.events(run, limit=None)
            self.store.transition(run, "report_test")
            self.assertEqual(reader.events(run, limit=None), first)
        newest = self.store.events(run, limit=1)[0]
        last = first[-1]["seq"]
        self.assertEqual(
            self.cli("events", run, "--before", str(newest["seq"]), "--limit", "1"),
            first[-1:],
        )
        self.assertEqual(self.cli("events", run, "--all", "--until", str(last)), first)
        self.assertEqual(self.cli("event", run, str(last)), first[-1])
        self.assertEqual(
            main(["--home", str(self.store.root), "event", other, str(last)]), 1
        )
        path = self.root / "handoff.md"
        self.cli("handoff", run, "--markdown", str(path))
        self.assertIn(self.head, path.read_text(encoding="utf-8"))
        compact = self.cli("list", "--compact", "--phase", "queued")
        self.assertEqual(len(compact), 2)
        self.assertEqual(compact[0]["total_milestones"], 1)
        self.assertNotIn("objective", compact[0]["spec"])
        self.assertNotIn("milestones", compact[0]["spec"])
        legacy = {
            "kind": "gate_finished",
            "details": {
                "name": "legacy",
                "exit": 0,
                "tree": "tree",
                "contract_sha256": "old",
                "verification_batch": "old",
            },
        }
        self.assertEqual(recovery_metrics([legacy])["gate_confirmation_rounds"], 0)

    def test_report_valid_later_receipt_survives_invalid_first_receipt(self):
        run = self.configured(fixtures.fixture("good"))
        root = self.store.directory(run) / "evidence"
        root.mkdir()
        png = root / "capture.png"
        png.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a90AAAAASUVORK5CYII="
            )
        )
        digest = hashlib.sha256(png.read_bytes()).hexdigest()
        for path, sha in [(png, "0" * 64), (png, digest), (root / "lost.png", digest)]:
            self.store.transition(
                run,
                "gate_finished",
                {
                    "name": "Plate check",
                    "exit": 0,
                    "artifacts": [
                        {"name": "capture", "path": str(path), "sha256": sha}
                    ],
                },
            )
        html = self.root / "report.html"
        run_report(self.store, run, html, True)
        page = html.read_text(encoding="utf-8")
        self.assertIn("1 content mismatch", page)
        self.assertIn("1 unreadable", page)
        self.assertEqual(page.count("data:image/png;base64,"), 1)

    @unittest.skipUnless(
        importlib.util.find_spec("playwright"), "Install the browser extra"
    )
    def test_offline_report_keyboard_filter_and_hidden_fragment(self):
        from playwright.sync_api import sync_playwright

        run = self.configured(fixtures.fixture("good"))
        self.store.transition(
            run, "attempt_rejected", {"issue": "Fixture mismatch <b>example</b>"}
        )
        seq = self.store.events(run, limit=1)[0]["seq"]
        html = self.root / "report.html"
        run_report(self.store, run, html)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel=os.getenv("NIGHTWEAVER_TEST_BROWSER_CHANNEL") or None
            )
            try:
                page = browser.new_page(
                    offline=True, viewport={"width": 320, "height": 650}
                )
                page.goto(html.as_uri())
                page.get_by_label("Event type").select_option("Problems")
                self.assertEqual(page.locator("details:visible").count(), 1)
                search = page.get_by_label("Search activity")
                search.fill("absent")
                self.assertIn("No matching", page.get_by_role("status").inner_text())
                page.get_by_role("button", name="Clear filters").focus()
                page.keyboard.press("Enter")
                self.assertTrue(search.evaluate("e=>e===document.activeElement"))
                search.fill("absent")
                page.locator(f'#checkpoint a[href="#event-{seq}"]').click()
                page.wait_for_function("document.querySelector('details:target')?.open")
                self.assertEqual(page.locator("details:visible").count(), 2)
                self.assertTrue(
                    page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                )
            finally:
                browser.close()
