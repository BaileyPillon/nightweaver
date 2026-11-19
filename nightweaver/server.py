"""Owned, bounded local app servers for browser gates on Windows."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
from threading import Event, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler

from .process import execute, PausedRun
from .safety import redact


class CandidateStartupFailure(ValueError):
    """The configured application failed to provide its test page."""


def local_path(value, label):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or "\\" in value:
        raise ValueError(f"{label} must be an absolute URL path on the owned loopback server")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {label}")
    return value


def validate_server(raw):
    from .spec import command, fields, integer
    fields(raw, "argv path ready_path startup_timeout max_log_bytes", "browser server")
    return {
        "argv": command(raw.get("argv"), "Browser server"),
        "path": local_path(raw.get("path", "/"), "server path"),
        "ready_path": local_path(raw.get("ready_path", "/"), "server ready_path"),
        "startup_timeout": integer(raw.get("startup_timeout", 30), "server startup_timeout", 1, 300),
        "max_log_bytes": integer(raw.get("max_log_bytes", 1024 * 1024), "server max_log_bytes", 1024, 16 * 1024 * 1024),
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


@contextmanager
def managed_server(config, root, output):
    """The existing native process runner owns the tree, including on parent death."""
    config = validate_server(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    log = output / "server.log"
    # Do not reuse a user's development server or choose a fixed well-known port.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    argv = [part.replace("{port}", str(port)) for part in config["argv"]]
    env = {**os.environ, "NIGHTWEAVER_PORT": str(port), "NIGHTWEAVER_HOST": "127.0.0.1"}
    stopping, finished = Event(), Event()
    outcome = {}

    def run():
        try:
            outcome["exit"] = execute(argv, root, log, 7200, env=env, paused=stopping.is_set,
                                      max_log_bytes=config["max_log_bytes"])
        except PausedRun:
            outcome["stopped"] = True
        except Exception as error:
            outcome["error"] = str(error)
        finally:
            finished.set()

    def failure(reason):
        detail = ""
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 3000))
                detail = stream.read().decode("utf-8", "replace")
        error_type = RuntimeError if "error" in outcome else CandidateStartupFailure
        return error_type(redact(f"{reason}; {outcome}\n{detail}"))

    def check():
        if finished.is_set():
            raise failure("Owned browser server stopped before the journey finished")
        if log.is_file() and log.stat().st_size > config["max_log_bytes"]:
            raise failure("Owned browser server exceeded its output limit")

    thread = Thread(target=run, name="nightweaver-browser-server", daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + config["startup_timeout"]
    try:
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure("Owned browser server readiness timed out")
            try:
                with opener.open(origin + config["ready_path"], timeout=min(.5, remaining)) as response:
                    ready = 200 <= response.status < 300
                if ready:
                    check()
                    break
            except HTTPError as error:
                error.close()
            except (OSError, URLError):
                pass
            finished.wait(.1)
        yield origin + config["path"], check
        check()
    finally:
        stopping.set()
        thread.join(timeout=12)
        if thread.is_alive():
            raise RuntimeError("Owned browser server did not stop within its cleanup deadline")
