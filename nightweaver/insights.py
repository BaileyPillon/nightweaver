"""Read-only diagnostics over durable, recorded controller evidence."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
import re
from statistics import median

from .safety import atomic_write, redact
from .checks import observed_pass


def failure_excerpt(path, size=3600):
    """Reserve room for the first error even if later progress floods the log."""
    path = Path(path)
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        head = stream.read(16384)
        stream.seek(max(0, path.stat().st_size - 16384))
        tail = stream.read(16384)

    def clean(data):
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", data.decode("utf-8", "replace")).strip()

    ending = clean(tail)
    failures = [
        line
        for line in clean(head).splitlines()
        if re.search(r"\b(fail(?:ed|ure)?|error|exception|traceback|assertionerror)\b", line, re.I)
    ]
    first = "\n".join(failures[:4])[: size // 2]
    if first and first not in ending[-size:]:
        return (first + "\n…\n" + ending[-(size - len(first) - 3) :]).strip()
    return ending[-size:]


def budget_status(spec, state, now=None):
    limits = spec.get("budgets", {})
    elapsed = None
    if state.get("started_at"):
        elapsed = max(
            0,
            (
                (now or datetime.now(timezone.utc)) - datetime.fromisoformat(state["started_at"])
            ).total_seconds(),
        )
    used = {
        "max_calls": state.get("provider_calls", 0),
        "max_observed_tokens": state.get("observed_tokens", 0),
        "max_seconds": round(elapsed or 0, 3),
    }
    return {
        "used": used,
        "remaining": {
            key: max(0, round(limits[key] - value, 3)) for key, value in used.items() if key in limits
        },
        "limits": {key: limits[key] for key in used if key in limits},
        "unknown_usage_calls": state.get("usage_unknown_calls", 0),
        "call_in_progress": bool(state.get("pending_call")),
    }


def budget_report(store, run_id):
    from .reports import metrics

    row = store.get(run_id)
    stop = None
    if row["state"]["phase"] in ("done", "blocked"):
        measured = metrics(store, run_id)
        if measured["elapsed_seconds"] is not None and row["state"].get("started_at"):
            from datetime import timedelta

            stop = datetime.fromisoformat(row["state"]["started_at"]) + timedelta(
                seconds=measured["elapsed_seconds"]
            )
    result = budget_status(row["spec"], row["state"], stop)
    if row["state"]["phase"] in ("done", "blocked") and stop is None:
        result["used"]["max_seconds"] = None
        if "max_seconds" in result["remaining"]:
            result["remaining"]["max_seconds"] = None
    return {"run_id": run_id, **result}


def gate_stats(events):
    groups = defaultdict(list)
    for event in events:
        if event["kind"] == "gate_finished":
            detail = event["details"]
            groups[(detail["name"], detail.get("contract_sha256"))].append(detail)
    results = []
    for (name, digest), receipts in groups.items():
        outcomes = [observed_pass(r) for r in receipts]
        passes, failed = outcomes.count(True), outcomes.count(False)
        durations = sorted(
            item["duration_ms"] for item in receipts if isinstance(item.get("duration_ms"), int)
        )
        results.append(
            {
                "gate": name,
                "contract_sha256": digest,
                "executions": len(receipts),
                "failures": sum(observed_pass(r) is not True for r in receipts),
                "unscored": sum(observed_pass(r) is None for r in receipts),
                "passed": passes, "functional_failures": failed, "scored": passes + failed,
                "scored_pass_rate": passes / (passes + failed) if passes + failed else None,
                "timed": len(durations),
                "total_measured_ms": sum(durations) if durations else None,
                "median_ms": median(durations) if durations else None,
                "p95_ms": durations[ceil(0.95 * len(durations)) - 1] if durations else None,
            }
        )
    total = sum(r["total_measured_ms"] or 0 for r in results)
    for row in results:
        ms = row["total_measured_ms"]
        row["measured_time_share"] = ms / total if ms is not None and total else None
    return sorted(results, key=lambda r: (-r["failures"], r["gate"]))


def diagnosis(store, run_id):
    row = store.get(run_id)
    events = store.events(run_id, limit=None)
    kinds = {
        "attempt_rejected",
        "budget_exhausted",
        "provider_unavailable",
        "dispatcher_run_failed",
        "repeated_failure_stopped",
        "verification_inconsistent",
        "pursuit_stopped",
        "judge_invalid",
        "plan_invalid",
        "blocked",
        "model_mismatch",
    }
    failures = [e for e in events if e["kind"] in kinds]
    counts = Counter(e["details"].get("phase", e["kind"]) for e in failures if e["kind"] != "blocked")
    gate = next(
        (
            e
            for e in reversed(events)
            if e["kind"] == "gate_finished"
            and observed_pass(e["details"]) is not True
        ),
        None,
    )
    return redact(
        {
            "run_id": run_id,
            "phase": row["state"]["phase"],
            "recorded_failure_stages": dict(counts),
            "last_failure_event": failures[-1] if failures else None,
            "last_failed_gate": gate,
            "unresolved": row["state"]["phase"] == "blocked",
            "remaining_milestones": len(row["spec"]["milestones"]) - row["state"]["step"],
        }
    )


def export_trace(store, run_id, output, after=0, until=None):
    row = store.get(run_id)
    keep = {
        "step",
        "attempt",
        "role",
        "provider",
        "adapter",
        "model",
        "served_model",
        "exit",
        "tree",
        "commit",
        "contract_sha256",
        "duration_ms",
        "call",
        "observed_tokens",
        "gate_index",
        "verification_batch",
        "sample",
        "stability_runs",
        "seconds",
        "same_failure_count",
        "wave",
        "score",
        "verdict",
    }
    header = {"schema": "nightweaver.trace.v1", "run_id": run_id, "base": row["state"]["base"]}
    records = [header]
    cursor = after
    for e in store.events(run_id, limit=None, after=after, until=until):
        cursor = e["seq"]
        data = {
            k: v for k, v in e["details"].items() if k in keep and isinstance(v, (str, int, float, bool))
        }
        records.append({"seq": e["seq"], "time": e["time"], "kind": e["kind"], "data": redact(data)})
    atomic_write(output, "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records))
    return {"run_id": run_id, "events": len(records) - 1, "last_seq": cursor, "path": str(Path(output).resolve())}


def provider_totals(events):
    groups = {}
    for event in events:
        if event["kind"] != "provider_call_finished":
            continue
        d = event["details"]
        route = d.get("role"), d.get("provider"), d.get("model")
        group = groups.setdefault(
            route,
            {
                "role": route[0],
                "provider": route[1],
                "model": route[2],
                "calls": 0,
                "measured_calls": 0,
                "observed_tokens": None,
                "token_channels": {},
            },
        )
        group["calls"] += 1
        if isinstance(d.get("observed_tokens"), int):
            group["measured_calls"] += 1
            group["observed_tokens"] = (group["observed_tokens"] or 0) + d["observed_tokens"]
        for key, value in d.get("token_channels", {}).items():
            group["token_channels"][key] = group["token_channels"].get(key, 0) + value
    return list(groups.values())
