"""Supervise one dedicated Windows runner and explain its durable recovery point."""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import notify
from .engine import pending_dependencies
from .process import run_lock, runner_active
from .safety import redact

GUARD_EVENTS = (
    "guard_enrolled",
    "guard_started",
    "guard_launch_admitted",
    "guard_unobserved_exit",
    "guard_child_started",
    "guard_child_contended",
    "guard_child_exited",
    "guard_restart_scheduled",
    "guard_stopped",
)


def restart_limit(value):
    if type(value) is not int or not 0 <= value <= 10:
        raise ValueError("Restart limit must be an integer from 0 to 10")
    return value


def checkpoint(row):
    state = row["state"]
    return {
        "phase": state["phase"],
        "accepted": state["accepted"],
        "completed": state["step"],
        "attempt": state["attempt"],
        "candidate": state.get("candidate"),
        "pending_role": (state.get("pending_call") or {}).get("role"),
    }


def disposition(store, row):
    state = row["state"]
    if state["phase"] in ("done", "blocked"):
        return state["phase"]
    if row["pause"]:
        return "paused"
    if state.get("dispatch"):
        return "queue_owned"
    if pending_dependencies(store, state):
        return "prerequisites"
    if runner_active(store.directory(row["id"]) / "runner.lock"):
        return "runner_active"
    return "eligible"


def recovery_status(store, run_id):
    row = store.get(run_id)
    state = row["state"]
    guard = runner_active(store.directory(run_id) / "guard.lock")
    decision = disposition(store, row)
    used = state.get("guard_restarts", 0)
    limit = state.get("guard_limit")
    if guard and decision == "eligible":
        decision = "supervised"
    elif (
        decision == "eligible"
        and (state.get("guard_crashed") or state.get("guard_launch"))
        and limit is not None
        and used >= limit
    ):
        decision = "limit_reached"
    return redact(
        {
            "run_id": run_id,
            "title": row["spec"]["title"],
            "disposition": decision,
            "guard_active": guard,
            "checkpoint": checkpoint(row),
            "restarts_used": used,
            "restart_limit": limit,
            "remaining": None if limit is None else max(0, limit - used),
            "retry_after": state.get("guard_retry_at"),
            "last_exit": state.get("guard_last_exit"),
            "unsettled_launch": state.get("guard_launch"),
            "last_error": state.get("last_error"),
            "waiting_for": pending_dependencies(store, state),
        }
    )


def launch_runner(store, run_id, launch_id, retry=False):
    command = [
        sys.executable,
        "-m",
        "nightweaver",
        "--home",
        str(store.root),
        "run",
        run_id,
        "--guard-child",
        launch_id,
    ]
    if retry:
        command.append("--retry")
    with (store.directory(run_id) / "runner.log").open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def supervise(store, run_id, limit=None, *, retry=False):
    root = store.directory(run_id)
    store.get(run_id)
    with run_lock(root / "guard.lock", "A supervisor already owns this run"):
        row = store.get(run_id)
        limit = restart_limit(
            limit if limit is not None else row["state"].get("guard_limit", 2)
        )

        def stop(reason):
            details = {"reason": reason}
            notify.announce(store, run_id, "guard_stopped", details, store.transition(run_id, "guard_stopped", details))
            return reason

        initial = disposition(store, row)
        if initial != "eligible" and not (
            initial == "blocked" and retry and not row["pause"]
        ):
            return initial
        if row["state"].get("dispatch"):
            return "queue_owned"
        store.transition(
            run_id,
            "guard_started",
            {"pid": os.getpid(), "limit": limit},
            guard_limit=limit,
        )
        if launch := row["state"].get("guard_launch"):
            # Both process locks are free but the old supervisor did not record
            # an exit. Preserve uncertainty; never invent a PID or exit code.
            store.transition(
                run_id,
                "guard_unobserved_exit",
                {"launch_id": launch, **checkpoint(row)},
                guard_launch=None,
                guard_crashed=True,
            )
        while True:
            row = store.get(run_id)
            state = row["state"]
            reason = disposition(store, row)
            if reason != "eligible" and not (
                reason == "blocked" and retry and not row["pause"]
            ):
                return stop(reason)
            if reason == "blocked":
                if state.get("dispatch"):
                    return stop("queue_owned")
                if pending_dependencies(store, state):
                    return stop("prerequisites")
            if state.get("guard_crashed"):
                used = state.get("guard_restarts", 0)
                if used >= limit:
                    return stop("limit_reached")
                deadline = state.get("guard_retry_at")
                if deadline:
                    delay = (
                        datetime.fromisoformat(deadline) - datetime.now(UTC)
                    ).total_seconds()
                    if delay > 0:
                        time.sleep(min(delay, 0.25))
                        continue
            # Only this supervisor debits the lifetime allowance. Persist admission
            # before spawning so a killed supervisor cannot issue a free duplicate.
            launch_id = uuid4().hex
            admission = {"launch_id": launch_id, **checkpoint(row)}
            if state.get("guard_crashed"):
                store.transition(
                    run_id,
                    "guard_restart_scheduled",
                    {"restart": used + 1, "limit": limit, **admission},
                    following_event=("guard_launch_admitted", admission),
                    guard_launch=launch_id,
                    guard_restarts=used + 1,
                    guard_crashed=False,
                    guard_retry_at=None,
                )
            else:
                store.transition(
                    run_id, "guard_launch_admitted", admission, guard_launch=launch_id
                )
            child = launch_runner(store, run_id, launch_id, retry)
            retry = False
            store.transition(
                run_id,
                "guard_child_started",
                {"pid": child.pid, "launch_id": launch_id, **checkpoint(row)},
            )
            code = child.wait()
            row = store.get(run_id)
            reason = disposition(store, row)
            receipt = {
                "pid": child.pid,
                "launch_id": launch_id,
                "exit": code,
                **checkpoint(row),
            }
            contention = False
            if code == 4:
                stamps = store.events(run_id, limit=1, kinds=["guard_child_contended"])
                contention = bool(
                    stamps
                    and stamps[0]["details"].get("pid") == child.pid
                    and stamps[0]["details"].get("launch_id") == launch_id
                )
            crashed = reason == "eligible" and not contention
            delay = min(30, 2 ** min(row["state"].get("guard_restarts", 0), 5))
            can_retry = crashed and row["state"].get("guard_restarts", 0) < limit
            deadline = (
                (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                if can_retry
                else None
            )
            store.transition(
                run_id,
                "guard_child_exited",
                {**receipt, "unexpected": crashed},
                guard_last_exit=receipt,
                guard_crashed=crashed,
                guard_retry_at=deadline,
                guard_launch=None,
            )
            if not crashed:
                return stop("contended" if reason == "eligible" else reason)


EXIT_CODES = {"done": 0, "paused": 2, "blocked": 3, "limit_reached": 5}
