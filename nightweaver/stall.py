"""A deadline for a Playwright call that the browser may never answer.

Every sync Playwright call can be given a timeout, but the driver and the browser behind it are what keep it. A renderer that is wedged
(a page that ran out of memory and never finishes dying, say) leaves the call waiting for an answer that does not come, and the sync
wrapper has nothing of its own to stop it: the thread sits in the event loop for ever. `Watchdog.limit(seconds)` is that something.
A thread that never calls Playwright watches the clock. Past the deadline it interrupts the main thread, which Playwright's sync API
turns into an exception in the call that is waiting (Playwright's own answer to Ctrl+C and to test-runner timeouts). If the call
still has not left five seconds later it stops the driver, which fails every call that waits for it."""

import _thread
import signal
import threading
from contextlib import contextmanager
from time import monotonic

KILL_AFTER = 5  # Seconds between the interrupt and stopping the driver.


class Stall(Exception):
    """A call did not return within its deadline and was abandoned."""


class Arm:
    def __init__(self, seconds):
        self.seconds, self.due, self.stage = seconds, monotonic() + seconds, 0
        self.main = threading.current_thread() is threading.main_thread()  # Only the main thread can be interrupted.


class Watchdog:
    def __init__(self):
        self.cond = threading.Condition()
        self.armed = self.live = self.fired = self.previous = self.thread = self.kill = None
        self.stalled, self.hooked, self.depth = False, False, 0

    def bind(self, kill):
        """`kill` stops the browser driver. It is the last resort when the interrupt does not free the call."""
        self.kill, self.stalled = kill, False

    def unbind(self):
        self.kill = None

    def clear(self):
        self.stalled = False

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.watch, name="nightweaver-watchdog", daemon=True)
            self.thread.start()
        if not self.hooked and threading.current_thread() is threading.main_thread():
            # Installed once and kept: a signal that is still on its way when a call returns must find this handler, not the default.
            self.previous, self.hooked = signal.signal(signal.SIGINT, self.on_signal), True

    def on_signal(self, signum, frame):
        arm = self.live
        if arm is not None and arm is self.fired:
            raise Stall(f"No answer within {arm.seconds:g} seconds")
        previous = self.previous  # A real Ctrl+C, or one that arrived after the call had returned: whoever had the signal first gets it.
        if callable(previous):
            return previous(signum, frame)
        if previous != signal.SIG_IGN:
            raise KeyboardInterrupt

    def watch(self):
        while True:
            with self.cond:
                while self.armed is None:
                    self.cond.wait()
                arm = self.armed
                remaining = arm.due + (KILL_AFTER if arm.stage else 0) - monotonic()
                if remaining > 0:
                    self.cond.wait(remaining)
                    continue
                if self.armed is not arm:
                    continue
                self.stalled = True
                if arm.stage == 0 and arm.main and self.hooked and signal.getsignal(signal.SIGINT) == self.on_signal:
                    arm.stage, self.fired = 1, arm
                    interrupt = True
                else:
                    self.armed, interrupt = None, False
            if interrupt:
                _thread.interrupt_main(signal.SIGINT)
            elif self.kill is not None:
                try:
                    self.kill()
                except Exception:
                    pass  # Already gone.

    @contextmanager
    def limit(self, seconds):
        """Raise Stall in the calling thread when the body has not finished after `seconds`. A call made inside a limited call is part of it."""
        if self.depth:
            yield
            return
        self.start()
        arm = Arm(seconds)
        with self.cond:
            self.armed = arm
            self.cond.notify()
        self.depth, self.live = 1, arm
        try:
            yield
        finally:
            self.live, self.depth = None, 0
            with self.cond:
                if self.armed is arm:
                    self.armed = None


watch = Watchdog()
