"""Operator views derived from structured events, without exposing model prompts."""

from collections import defaultdict
from statistics import median

from .checks import observed_pass


def call_ledger(store, run_id):
    calls = {}
    legacy_events = 0
    for event in store.events(run_id, limit=None, kinds=["provider_call_started", "provider_call_finished"]):
        data = event["details"]
        number = data.get("call")
        if type(number) is not int:
            legacy_events += 1
            continue
        row = calls.setdefault(
            number,
            {
                "call": number,
                "started": None,
                "finished": None,
                "status": "pending",
                "seconds": None,
                "observed_tokens": None,
            },
        )
        for key in ("step", "attempt", "role", "provider", "model"):
            if key in data:
                row[key] = data[key]
        if event["kind"] == "provider_call_started":
            row["started"] = data.get("started") or event["time"]
        else:
            recovered = data.get("recovered", False)
            row.update(
                finished=event["time"],
                status="recovered" if recovered else "finished",
                seconds=None if recovered else data.get("seconds"),
                observed_tokens=data.get("observed_tokens"),
            )
    return {
        "run_id": run_id,
        "calls": [calls[n] for n in sorted(calls)],
        "unattributed_events": legacy_events,
    }


def browser_hotspots(store, run_id):
    spec = store.get(run_id)["spec"]
    groups = defaultdict(list)
    failures = defaultdict(int)
    unmeasured = unlocated = 0
    for event in store.events(run_id, limit=None, kinds=["gate_finished"]):
        d = event["details"]
        step, gate = d.get("step"), d.get("gate_index")
        if not (type(step) is int and 1 <= step <= len(spec["milestones"])):
            if d.get("receipt") or d.get("browser_coverage"):
                unmeasured += 1
            continue
        gates = spec["milestones"][step - 1]["gates"]
        if not (type(gate) is int and 1 <= gate <= len(gates)):
            if d.get("receipt") or d.get("browser_coverage"):
                unmeasured += 1
            continue
        check = gates[gate - 1]
        if check.get("kind") != "browser":
            continue
        config = check["browser"]
        actions_by_case = {scenario.get("name",""):config["steps"] + scenario.get("steps",[])
                           for scenario in config.get("scenarios",[{}])}
        if observed_pass(d) is None or not d.get("browser_coverage"):
            unmeasured += 1
            unlocated += len(d.get("browser_failures", []))
            continue
        for view in d["browser_coverage"]:
            scenario = view.get("scenario","")
            actions = actions_by_case.get(scenario,[])
            durations = view.get("step_durations_ms")
            if durations is None:
                unmeasured += 1
            else:
                for index, duration in enumerate(durations, 1):
                    if index > len(actions):
                        unmeasured += 1
                        continue
                    key = (step, check["name"], view["viewport"], index, actions[index - 1]["action"], scenario)
                    groups[key].append(duration)
        for fail in d.get("browser_failures", []):
            scenario = fail.get("scenario","")
            actions = actions_by_case.get(scenario,[])
            index = (fail.get("stage") or {}).get("index")
            if type(index) is int and 1 <= index <= len(actions) and fail.get("viewport"):
                key = (step, check["name"], fail["viewport"], index, actions[index - 1]["action"], scenario)
                failures[key] += 1
            else:
                unlocated += 1
    rows = []
    for key in sorted(groups.keys() | failures.keys()):
        durations = groups[key]
        rows.append(
            {
                "step": key[0],
                "gate": key[1],
                "viewport": key[2],
                "action_index": key[3],
                "action": key[4],
                **({"scenario":key[5]} if key[5] else {}),
                "timed_observations": len(durations),
                "median_ms": median(durations) if durations else None,
                "total_ms": sum(durations) if durations else None,
                "failed_observations": failures[key],
            }
        )
    rows.sort(key=lambda r: (-r["failed_observations"], -(r["total_ms"] or 0)))
    return {
        "run_id": run_id,
        "actions": rows,
        "missing_timing_records": unmeasured,
        "failures_outside_steps": unlocated,
    }
