"""Operator views over durable runs and accepted Git objects."""

import hashlib
import time
from pathlib import Path

from .engine import common_dir, git, pending_dependencies
from .process import runner_active
from .safety import redact


def attention(store):
    """Classify actionable runs without starting, pausing or retrying them."""
    result = []
    dispatcher_running = runner_active(store.root / "dispatcher.lock")
    runs = {row["id"]: row for row in store.runs()}
    for row in runs.values():
        state, run_id = row["state"], row["id"]
        phase = state["phase"]
        if phase == "done":
            continue
        waiting = [rid for rid in state.get("after", []) if runs[rid]["state"]["phase"] != "done"]
        failed = [rid for rid in waiting if runs[rid]["state"]["phase"] == "blocked"]
        paused = [rid for rid in waiting if runs[rid]["pause"]]
        if row["pause"]:
            reason, action = "paused", ["resume", run_id]
        elif phase == "blocked":
            reason, action = "blocked", ["handoff", run_id]
        elif failed:
            reason, action = "blocked_prerequisite", ["handoff", failed[0]]
        elif paused:
            reason, action = "paused_prerequisite", ["resume", paused[0]]
        elif runner_active(store.directory(run_id) / "runner.lock"):
            continue
        elif state.get("dispatch") and not dispatcher_running:
            reason, action = "dispatcher_stopped", ["dispatch", "--watch"]
        elif waiting:
            continue
        elif state.get("dispatch"):
            # An eligible queued run or a retry backoff belongs to the dispatcher.
            continue
        elif phase == "queued":
            reason, action = "awaiting_start", ["start", run_id]
        else:
            reason, action = "runner_stopped", ["resume", run_id]
        result.append(
            {
                "run_id": run_id,
                "title": row["spec"]["title"],
                "reason": reason,
                "issue": state.get("last_error"),
                "prerequisites": waiting,
                "next_command": action,
            }
        )
    return redact(result)


def handoff(store, run_id):
    from .insights import budget_report

    row = store.get(run_id)
    state, spec = row["state"], row["spec"]
    index = state["step"]
    step = spec["milestones"][index] if index < len(spec["milestones"]) else None
    accepted = store.accepted_milestones(run_id)
    return redact(
        {
            "run_id": run_id,
            "objective": spec["objective"],
            "phase": state["phase"],
            "paused": row["pause"],
            "base": state["base"],
            "accepted_commit": state["accepted"],
            "completed": [
                {
                    "title": spec["milestones"][entry["step"] - 1]["title"],
                    "commit": entry["commit"],
                    "verified_checks": entry.get("verified_checks"),
                }
                for entry in accepted
                if entry["step"] <= index
            ],
            "current": {
                "title": step["title"],
                "task": step["task"],
                "paths": step["paths"],
                "checks": [gate["name"] for gate in step["gates"]],
            }
            if step
            else None,
            "acceptance": spec.get("acceptance", []),
            "provider_issue": state.get("provider_issue"),
            "last_error": state.get("last_error"),
            "operator_feedback": store.feedback_text(run_id),
            "budget": budget_report(store, run_id),
            "waiting_for": pending_dependencies(store, state),
        }
    )


def changes(store, run_id, milestone=None, include_patch=False):
    row = store.get(run_id)
    state, spec = row["state"], row["spec"]
    base, tip = state["base"], state["accepted"]
    if milestone is not None:
        entries = store.accepted_milestones(run_id)
        if not 1 <= milestone <= len(entries):
            raise ValueError("Select an accepted milestone numbered from 1")
        base = entries[milestone - 2]["commit"] if milestone > 1 else base
        tip = entries[milestone - 1]["commit"]
    repo = Path(spec["repository"])
    gitdir = common_dir(repo)
    if gitdir != Path(spec.get("git_common_dir", gitdir)):
        raise ValueError("The registered checkout now belongs to another repository")
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
    names = git(repo, *args, "--name-only", "-z", base, tip, "--")
    out = {
        "run_id": run_id,
        "base": base,
        "tip": tip,
        "files": [n for n in names.split("\0") if n],
        "stat": git(repo, *args, "--stat", base, tip, "--"),
    }
    if include_patch:
        out["patch"] = git(repo, *args, "--patch", base, tip, "--")
    return redact(out)


def audit_evidence(store, run_id):
    store.get(run_id)
    root = (store.directory(run_id) / "evidence").resolve()
    files = []
    for event in store.events(run_id, limit=None):
        if event["kind"] != "gate_finished":
            continue
        for artifact in event["details"].get("artifacts", []):
            record = {"event": event["seq"], "gate": event["details"].get("name"), "name": artifact["name"]}
            try:
                path = Path(artifact["path"]).resolve(strict=True)
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("Outside evidence directory or not a regular file")
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                record["status"] = "verified" if digest == artifact["sha256"] else "changed"
            except (OSError, RuntimeError, ValueError) as error:
                record.update(status="unavailable", reason=str(error))
            files.append(record)
    return redact(
        {
            "run_id": run_id,
            "status": (
                "no_artifacts"
                if not files
                else "verified"
                if all(f["status"] == "verified" for f in files)
                else "failed"
            ),
            "files": files,
        }
    )


def follow_events(store, run_id, after=0, page_size=80):
    """Yield NDJSON-ready records, including the complete last page of a finished run."""
    while True:
        # Read phase before events so a terminal transition cannot go unobserved.
        row = store.get(run_id)
        batch = store.events(run_id, after=after, limit=page_size)
        for event in batch:
            after = event["seq"]
            yield event
        if len(batch) == page_size:
            continue
        if row["state"]["phase"] in ("done", "blocked") or row["pause"]:
            return
        time.sleep(0.5)
