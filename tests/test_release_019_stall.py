"""Nightweaver 0.19: a deadline for a browser call that is never answered, and the CI module runner that keeps one test module from
holding a whole job."""

import _thread
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nightweaver import stall


def wait_in_a_loop(seconds):
    """Block the main thread the way a sync Playwright call does: inside an event loop, waiting for an answer from outside. (time.sleep is
    not that: Windows does not wake it for a simulated signal, the loop's own wake-up is what delivers one.)"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(seconds))
    finally:
        loop.close()


class WatchdogTests(unittest.TestCase):
    def test_a_call_that_outlasts_its_deadline_is_interrupted_and_the_watchdog_stays_usable(self):
        watch = stall.Watchdog()
        started = time.monotonic()
        with self.assertRaises(stall.Stall):
            with watch.limit(0.4):
                wait_in_a_loop(30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(watch.stalled)
        watch.clear()
        with watch.limit(5):
            pass  # The next call is watched as before.
        self.assertFalse(watch.stalled)

    def test_a_call_that_returns_in_time_is_left_alone_and_nothing_arrives_after_it(self):
        watch = stall.Watchdog()
        with watch.limit(0.3):
            pass
        time.sleep(0.9)  # Well past the deadline it had.
        self.assertFalse(watch.stalled)

    def test_a_call_inside_a_watched_call_is_part_of_it(self):
        watch = stall.Watchdog()
        with self.assertRaises(stall.Stall):
            with watch.limit(0.4):
                with watch.limit(60):  # The outer deadline still applies.
                    wait_in_a_loop(30)

    def test_a_real_interrupt_outside_a_watched_call_is_still_a_keyboard_interrupt(self):
        watch = stall.Watchdog()
        with watch.limit(5):
            pass  # Installs the handler, which stays.
        with self.assertRaises(KeyboardInterrupt):
            _thread.interrupt_main()
            wait_in_a_loop(5)

    def test_when_the_interrupt_cannot_free_the_call_the_driver_is_stopped(self):
        watch, killed = stall.Watchdog(), []
        watch.bind(lambda: killed.append(1))
        with patch.object(stall._thread, "interrupt_main", lambda *args: None), patch.object(stall, "KILL_AFTER", 0.3):
            with watch.limit(0.3):
                time.sleep(2)
        self.assertEqual(killed, [1])

    def test_a_call_in_a_thread_that_cannot_be_interrupted_goes_straight_to_stopping_the_driver(self):
        watch, freed, problems = stall.Watchdog(), threading.Event(), []
        watch.bind(freed.set)  # Stopping the driver is what makes the blocked call fail.

        def call():
            try:
                with watch.limit(0.3):
                    freed.wait(30)
            except BaseException as error:
                problems.append(error)

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertTrue(freed.is_set())
        self.assertEqual(problems, [])


class ModuleRunnerTests(unittest.TestCase):
    """CI runs `python tests/_module_runner.py MODULE SECONDS`. A script's own folder goes on sys.path, where `python -m unittest` put the
    current one, so the runner has to supply the repository root that some modules import from (examples/ is not in the install)."""

    runner = Path(__file__).resolve().parent / "_module_runner.py"

    def environment(self):
        return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    def test_the_repository_root_is_importable_wherever_the_runner_is_started(self):
        code = (f"import runpy, sys; runpy.run_path({str(self.runner)!r}, run_name='imported'); "
                "import examples.agtx_contender; print(sys.path[0])")
        with tempfile.TemporaryDirectory() as elsewhere:
            done = subprocess.run([sys.executable, "-c", code], cwd=elsewhere, env=self.environment(), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(done.stdout.strip()), self.runner.parent.parent)

    def test_a_module_that_imports_from_the_repository_root_runs_through_the_runner(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            done = subprocess.run([sys.executable, str(self.runner), "test_head_to_head.py", "120"], cwd=elsewhere, env=self.environment(),
                                  capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertRegex(done.stderr, r"Ran [1-9]\d* tests?")


if __name__ == "__main__":
    unittest.main()
