"""Native process ownership and run locking."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from .safety import scrub_env

# The exit codes execute() returns when the controller, not the program, ended the call: time, idle and output limits.
CONTROLLER_EXITS = (124, 125, 126)
# execute() returns these: 124 when the call ran past its wall-clock timeout, 125 when its log did not grow for idle_timeout seconds (only
# when an idle watchdog was passed), 126 when its log grew past max_log_bytes.
DEFAULT_MAX_LOG = 16 * 1024 * 1024


def controller_exit_text(code, subject, wall, idle=None, log=None):
    """The words for a call the controller ended: exit 124 (time limit), 125 (idle watchdog) or 126 (output limit). None for any other exit
    code, so a caller keeps its own text for an exit the program chose.

    `subject` begins the sentence ("The planner", "the reviewer"). `wall`, `idle` and `log` are (value, setting) pairs: the limit the call was
    held to (seconds, seconds, bytes; None when there was none) and the words that name the setting that controls it. The sentence has no
    final stop, so a caller can go on."""
    def sized(value):
        return f"{value / 1048576:g} MB"

    if code == 124:
        return f"{subject} timed out after {wall[0]} s, its time limit ({wall[1]})"
    if code == 125:
        value, setting = idle or (None, "no idle watchdog was set for this call")
        return (f"{subject} was stopped by the idle watchdog" + ("" if value is None else f" after {value} s without output")
                + f", its idle timeout ({setting})")
    if code == 126:
        value, setting = log or (DEFAULT_MAX_LOG, "the log-size limit of this call")
        return f"{subject} was stopped at its output limit: its log grew past {sized(value)} ({setting})"
    return None


class BusyRun(ValueError):
    pass


class PausedRun(Exception):
    pass


def runner_active(path):
    if not path.is_file():
        return False
    try:
        with run_lock(path):
            return False
    except BusyRun:
        return True


@contextmanager
def run_lock(path, message="A runner already owns this run"):
    import msvcrt

    with path.open("a+b") as handle:
        # Windows permits locking a range beyond EOF, so there is no initialization race.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BusyRun(message) from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_time", ctypes.c_int64), ("per_job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("reads", "writes", "others", "bytes_read", "bytes_written", "bytes_other")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class WindowsJob:
    """Keep an operation's child tree tied to the engine process lifetime."""

    def __enter__(self):
        self.kernel = k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            k.CloseHandle(self.handle)
            raise error
        return self

    def attach(self, pid):
        handle = self.kernel.OpenProcess(0x100 | 0x1, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(handle)

    def __exit__(self, *_error):
        self.kernel.CloseHandle(self.handle)

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())


class ProcessLimit(RuntimeError):
    """The controller stopped the process before it could deliver a verdict."""


def execute(argv, cwd, log, timeout, *, prompt=None, env=None, paused=lambda: False,
            idle_timeout=None, max_log_bytes=DEFAULT_MAX_LOG, heartbeat=None, raise_on_limit=False):
    """Run an operator-approved program using files for I/O and a native Job for ownership.

    A caller that passes no env gets the process environment without provider credentials."""
    env = scrub_env(os.environ) if env is None else env
    log.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = log.with_suffix(".input.txt")
    if prompt is not None:
        prompt_path.write_text(prompt, encoding="utf-8")
    source = prompt_path if prompt is not None else os.devnull
    deadline = time.monotonic() + timeout
    changed_at = beat_at = time.monotonic()
    last_size = 0

    def limit(code, reason):
        if raise_on_limit:
            raise ProcessLimit(reason)
        return code

    shim = [sys.executable, str(Path(__file__).with_name("_child.py")), json.dumps(argv), str(source)]
    with TemporaryDirectory(prefix="nw-launch-") as temp, log.open("wb") as out, WindowsJob() as job:
        error_file = Path(temp) / "launch-error"
        shim.append(str(error_file))
        child = subprocess.Popen(shim, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                 env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        attached = False
        try:
            with child.stdin as control:
                job.attach(child.pid)
                attached = True
                control.write(b"\x01")
            while child.poll() is None:
                if paused():
                    raise PausedRun
                if time.monotonic() >= deadline:
                    return limit(124, "Gate exceeded its wall-clock timeout")
                now = time.monotonic()
                size = log.stat().st_size
                if size != last_size:
                    changed_at, last_size = now, size
                if size > max_log_bytes:
                    return limit(126, "Gate exceeded its log-size limit")
                if idle_timeout is not None and now - changed_at >= idle_timeout:
                    return limit(125, "Gate exceeded its idle timeout")
                if heartbeat and now - beat_at >= 10:
                    heartbeat({"pid": child.pid, "log_bytes": size})
                    beat_at = now
                time.sleep(.2)
            if error_file.is_file():
                raise OSError(error_file.read_text(encoding="utf-8"))
            if log.stat().st_size > max_log_bytes:
                return limit(126, "Gate exceeded its log-size limit")
            return child.returncode
        finally:
            if child.poll() is None:
                if attached:
                    job.terminate()
                else:
                    child.kill()  # the blocked shim has not been allowed to launch its child
                child.wait(timeout=10)
