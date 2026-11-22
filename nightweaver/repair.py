"""Bounded, evidence-based worker handoffs and their durable history."""

import hashlib

from .spec import fields, integer

FAMILIES = {"codex": "openai", "claude": "anthropic", "deepseek": "deepseek", "gemini": "google", "command": "custom"}


def policy(raw):
    fields(raw, "collect_failures switch_after fresh_session_after history", "repair")
    collect = raw.get("collect_failures", False)
    if type(collect) is not bool:
        raise ValueError("repair.collect_failures must be boolean")
    return {
        "collect_failures": collect,
        "switch_after": integer(raw.get("switch_after", 0), "switch_after", 0, 10),
        "fresh_session_after": integer(
            raw.get("fresh_session_after", 0), "fresh_session_after", 0, 10
        ),
        "history": integer(raw.get("history", 3), "repair history", 0, 10),
    }


def next_worker(step, index):
    routes = [step["worker"], *step.get("worker_fallbacks", [])]
    return next(
        (
            i
            for i in range(index + 1, len(routes))
            if routes[i] not in routes[: index + 1]
        ),
        None,
    )


def describe(route):
    return {key: route[key] for key in ("kind", "model") if key in route}


def repair_history(store, run_id):
    events = store.events(
        run_id,
        limit=None,
        kinds=[
            "worker_started",
            "attempt_rejected",
            "milestone_accepted",
            "repair_handoff",
        ],
    )
    step = 1
    attempt = None
    rows, handoffs = [], []
    for event in events:
        data = event["details"]
        if event["kind"] == "worker_started":
            attempt = data.get("attempt")
        elif event["kind"] == "milestone_accepted":
            step = data["step"] + 1
            attempt = None
        elif event["kind"] == "repair_handoff":
            handoffs.append({"event": event["seq"], **data})
        else:
            rows.append(
                {"event": event["seq"], "step": step, "attempt": attempt, **data}
            )
    return {
        "run_id": run_id,
        "rejections": rows,
        "handoffs": handoffs,
        "model_calls": 0,
    }


def failure_brief(store, run_id, state, limit):
    if not limit or not state.get("attempt"):
        return ""
    # Query the tail only. Older event formats did not carry a milestone number.
    recent = store.events(run_id, limit=limit, kinds=["attempt_rejected"])
    rows = [
        e["details"] for e in recent if e["details"].get("step") == state["step"] + 1
    ]
    if not rows:
        return ""
    lines = ["Earlier attempts on this milestone (controller observations)"]
    for row in rows:
        gates = ", ".join(row.get("failed_gates", []))
        paths = ", ".join(row.get("changed_paths", [])[:6])
        lines.append(
            f"Attempt {row['attempt']} · {row['phase']} · tree {(row.get('tree') or 'unknown')[:12]}: "
            f"{gates[:180]} {row['issue'][:480]}"
            + (f" · Changed files: {paths[:220]}" if paths else "")
        )
    return "\n".join(lines) + "\n\n"


def combined_failure(failures):
    size = max(100, 4400 // len(failures))
    return "\n\n".join(
        f"{item['gate'][:160]} {item['diagnostic'][:size]}" for item in failures
    )


def family(route):
    return FAMILIES.get(route.get("kind"), route.get("kind"))


def fingerprint(phase, signature, failed_gates=(), exit_code=None, reason="", feedback=0):
    """Identify a failure by what failed on which build. Free text never counts: a paraphrase is the same failure."""
    parts = [phase, ",".join(sorted(set(failed_gates))), signature]
    if phase == "worker":
        parts.append(str(exit_code))
    if phase == "feedback":
        parts.append(str(feedback))
    if signature == "unknown":
        parts.append(reason)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def cause_line(phase, failed_gates, count, used, attempts, repeated):
    if phase == "no_progress":
        return "Stopped: no change after review feedback"
    if repeated and phase == "check" and failed_gates:
        names = ", ".join(failed_gates)[:160]
        return f"Stopped: gate{'s' * (len(failed_gates) > 1)} {names} failed {count} times on the same build"
    if repeated and phase == "review":
        return f"Stopped: the reviewer asked for changes {count} times on the same build"
    if repeated:
        return f"Stopped: the same {phase} failure happened {count} times on the same build"
    return f"Stopped: {used} of {attempts} attempts used"


def blocked_message(cause, reason, summary):
    """What an operator reads first: the cause, the last failure, then the worker's own account."""
    parts = [cause, reason[:3600]]
    if summary:
        parts.append("Worker's last explanation:\n" + summary)
    return "\n\n".join(parts)


def prior_findings(store, run_id, state, count=3, size=700):
    """The reviewer's own recent REVISE findings for the milestone in progress."""
    rows = [
        e["details"]
        for e in store.events(run_id, limit=60, kinds=["attempt_rejected"])
        if e["details"].get("phase") == "review"
        and e["details"].get("step") == state["step"] + 1
        and e["details"].get("finding")
    ]
    return [f"- Attempt {row.get('attempt')}: {row['finding'][:size]}" for row in rows[-count:]]


def conflict_brief(conflicts):
    if not conflicts:
        return ""
    return (
        "Planner notes\nThe planner flagged these requirements as contradictory or not satisfiable as stated. "
        "Choose the most reasonable reading that serves the goal, and say in your summary how you resolved each:\n"
        + "\n".join(f"- {item}" for item in conflicts)
    )
