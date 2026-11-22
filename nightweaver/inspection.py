"""Read-only views for automation, CI and local operators."""

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
from html import escape
import json
import math
import time

from .checks import observed_pass
from .operations import audit_evidence
from .process import runner_active
from .safety import atomic_write, redact, write_json


def wait_for_run(store, run_id, timeout=None):
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("Wait timeout must be a finite, nonnegative number of seconds")
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        row = store.get(run_id)
        phase = row["state"]["phase"]
        if phase in ("done", "blocked"):
            return {"run_id": run_id, "phase": phase}, 0 if phase == "done" else 3
        if row["pause"]:
            return {"run_id": run_id, "phase": "paused"}, 2
        state = row["state"]
        if (state.get("guard_crashed") or state.get("guard_launch")) and state.get("guard_restarts", 0) >= state.get("guard_limit", 2):
            if not runner_active(store.directory(run_id) / "guard.lock") and not runner_active(store.directory(run_id) / "runner.lock"):
                return {"run_id": run_id, "phase": phase, "disposition": "limit_reached"}, 5
        if deadline is not None and time.monotonic() >= deadline:
            return {"run_id": run_id, "phase": phase, "timed_out": True}, 4
        time.sleep(
            min(0.25, max(0, deadline - time.monotonic()))
            if deadline is not None
            else 0.25
        )


def matching_runs(store, project=None, phase=None, *, compact=False):
    return [
        r
        for r in (store.run_summaries() if compact else store.runs())
        if (
            project is None
            or str(r["spec"].get("project", "")).casefold() == project.casefold()
        )
        and (phase is None or r["state"]["phase"] == phase)
    ]


def fleet(store, rows):
    groups = {}
    for row in rows:
        key = row["spec"].get("project") or row["spec"]["repository"]
        group = groups.setdefault(
            key,
            {
                "project": key,
                "runs": 0,
                "phases": Counter(),
                "active_runners": 0,
                "paused": 0,
                "queue_enrolled": 0,
            },
        )
        group["runs"] += 1
        group["phases"][row["state"]["phase"]] += 1
        group["active_runners"] += runner_active(
            store.directory(row["id"]) / "runner.lock"
        )
        group["paused"] += row["pause"]
        group["queue_enrolled"] += bool(
            row["state"].get("dispatch")
            and row["state"]["phase"] not in ("done", "blocked")
        )
    return redact({"runs": len(rows), "projects": [groups[k] for k in sorted(groups)]})


def evidence_manifest(store, run_id, destination):
    audit = audit_evidence(store, run_id)
    rows = [{k: v for k, v in r.items() if k != "reason"} for r in audit["files"]]
    result = {
        "schema": "nightweaver.evidence.v1",
        "run_id": run_id,
        "status": audit["status"],
        "files": rows,
    }
    write_json(destination, result)
    return {
        "run_id": run_id,
        "status": audit["status"],
        "files": len(rows),
        "output": str(destination.resolve()),
    }


def browser_coverage(store, run_id):
    spec = store.get(run_id)["spec"]
    groups = defaultdict(list)
    unavailable = []
    browser_names = {
        g["name"]
        for s in spec["milestones"]
        for g in s["gates"]
        if g.get("kind") == "browser"
    }
    for event in store.events(run_id, limit=None, kinds=["gate_finished"]):
        d = event["details"]
        step, index = d.get("step"), d.get("gate_index")
        if not (
            isinstance(step, int) and 1 <= step <= len(spec["milestones"])
        ) or not isinstance(index, int):
            if d.get("receipt") or d.get("name") in browser_names:
                unavailable.append(
                    {
                        "event": event["seq"],
                        "step": step,
                        "gate": d.get("name"),
                        "reason": "unknown_gate_location",
                    }
                )
            continue
        gates = spec["milestones"][step - 1]["gates"]
        if not (
            isinstance(index, int)
            and 1 <= index <= len(gates)
            and gates[index - 1].get("kind") == "browser"
        ):
            continue
        if not d.get("browser_coverage") or observed_pass(d) is None:
            unavailable.append(
                {
                    "event": event["seq"],
                    "step": step,
                    "gate": d["name"],
                    "reason": d.get("browser_issue") or "coverage_unavailable",
                }
            )
            continue
        for view in d["browser_coverage"]:
            groups[step, d["name"], view["viewport"], view.get("scenario","")].append(view)
    return {
        "run_id": run_id,
        "unmeasured_receipts": unavailable,
        "viewports": [
            {
                "step": step,
                "gate": gate,
                "viewport": name,
                **({"scenario": scenario} if scenario else {}),
                "samples": len(views),
                "passed_samples": sum(v["passed"] for v in views),
                "completed_steps": sum(v["completed_steps"] for v in views),
                "planned_steps": sum(v["planned_steps"] for v in views),
            }
            for (step, gate, name, scenario), views in sorted(groups.items())
        ],
    }


def browser_matrix(store, run_id):
    """Track the latest observation even when a later verifier could not score it."""
    from .journeys import browser_cases

    cells = {}
    for number, milestone in enumerate(store.get(run_id)["spec"]["milestones"], 1):
        for index, gate in enumerate(milestone["gates"], 1):
            if gate.get("kind") != "browser":
                continue
            for scenario, viewport, steps in browser_cases(gate["browser"]):
                key = number, index, scenario.get("name"), viewport["name"]
                cells[key] = {
                    "milestone": number,
                    "gate": gate["name"],
                    "scenario": scenario.get("name"),
                    "viewport": viewport["name"],
                    "planned_steps": len(steps),
                    "status": "pending",
                    "samples": 0,
                    "passed_samples": 0,
                    "failed_samples": 0,
                    "unscored_samples": 0,
                }
    for event in store.events(run_id, limit=None, kinds=["gate_finished"]):
        detail = event["details"]
        lookup = {
            (v.get("scenario"), v["viewport"]): v
            for v in detail.get("browser_coverage", [])
        }
        for (number, index, scenario, viewport), cell in cells.items():
            if (number, index) != (detail.get("step"), detail.get("gate_index")):
                continue
            row = lookup.get((scenario, viewport))
            scored = row is not None and observed_pass(detail) is not None
            status = ("passed" if row["passed"] else "failed") if scored else "unscored"
            cell.update(
                status=status,
                event=event["seq"],
                attempt=detail.get("attempt"),
                tree=detail.get("tree"),
                completed_steps=row.get("completed_steps") if scored else None,
                runtime=detail.get("browser_runtime"),
                receipt=detail.get("receipt"),
                navigation_ms=row.get("navigation_ms") if scored else None,
                network=row.get("network_summary") if scored else None,
                checkpoints=row.get("checkpoint_layout") if scored else None,
            )
            cell["samples"] += 1
            cell[status + "_samples"] += 1
    return {"run_id": run_id, "cases": list(cells.values())}


def milestone_timeline(store, run_id):
    row = store.get(run_id)
    milestones = [
        {
            "step": i,
            "title": s["title"],
            "started": None,
            "accepted": None,
            "elapsed_seconds": None,
            "discarded": False,
            "worker_attempts": 0,
            "rejections": 0,
            "gate_executions": 0,
            "commit": None,
        }
        for i, s in enumerate(row["spec"]["milestones"], 1)
    ]
    cursor = 0
    gone = set(store.discarded_commits(run_id))
    for event in store.events(
        run_id,
        limit=None,
        kinds=[
            "worker_started",
            "attempt_rejected",
            "gate_finished",
            "milestone_accepted",
        ],
    ):
        if cursor >= len(milestones):
            break
        current = milestones[cursor]
        kind, d = event["kind"], event["details"]
        if kind == "milestone_accepted":
            current = milestones[d["step"] - 1]
            # A milestone of a wave a revert threw away was built but is not part of the result.
            current.update(commit=d["commit"], **({"discarded": True} if d["commit"] in gone else {"accepted": event["time"]}))
            if current["started"]:
                current["elapsed_seconds"] = round(
                    (
                        datetime.fromisoformat(event["time"])
                        - datetime.fromisoformat(current["started"])
                    ).total_seconds(),
                    3,
                )
            cursor = d["step"]
        elif kind == "worker_started":
            current["started"] = current["started"] or event["time"]
            current["worker_attempts"] += 1
        elif kind == "attempt_rejected":
            current["rejections"] += 1
        else:
            current["gate_executions"] += 1
    return redact({"run_id": run_id, "milestones": milestones})


def plan_diff(before, after):
    changes = []

    def compare(field, a, b):
        if a != b:

            def digest(value):
                return sha256(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()

            changes.append(
                {"field": field, "before_sha256": digest(a), "after_sha256": digest(b)}
            )

    for key in sorted((before.keys() | after.keys()) - {"milestones"}):
        compare(key, before.get(key), after.get(key))
    left, right = before["milestones"], after["milestones"]
    compare("milestone_count", len(left), len(right))
    for i in range(max(len(left), len(right))):
        a, b = left[i] if i < len(left) else {}, right[i] if i < len(right) else {}
        for k in sorted(a.keys() | b.keys()):
            if k != "gates":
                compare(f"milestones[{i + 1}].{k}", a.get(k), b.get(k))
        checks = [{g["name"]: g for g in step.get("gates", [])} for step in (a, b)]
        compare(f"milestones[{i + 1}].gate_order", list(checks[0]), list(checks[1]))
        for name in sorted(checks[0].keys() | checks[1].keys()):
            compare(
                f"milestones[{i + 1}].gates.{name}",
                checks[0].get(name),
                checks[1].get(name),
            )
    return {"identical": not changes, "changes": changes}


def ci_summary(store, run_id, output):
    from .reports import metrics

    m = metrics(store, run_id)
    # Keep issue titles and other project text out of workflow markup.
    lines = [
        "## Nightweaver run",
        "",
        f"Run `{run_id}` · **{escape(m['outcome'])}**",
        "",
        "| Measurement | Value |",
        "| --- | ---: |",
        f"| Milestones accepted | {m['accepted_milestones']} / {m['total_milestones']} |",
        *([f"| Milestones discarded by a revert | {m['pursuit']['discarded_milestones']} |"] if m.get("pursuit", {}).get("discarded_milestones") else []),
        f"| Worker attempts | {m['worker_attempts']} |",
        f"| Provider calls | {m['provider_calls']} |",
        f"| Failed attempts | {m['failed_attempts']} |",
        f"| Unexpected runner exits | {m['unexpected_runner_exits']} |",
        f"| Unsettled launches found on recovery | {m['unobserved_runner_exits']} |",
        f"| Supervised restarts admitted | {m['supervised_restarts']} |",
        f"| Functional worker handoffs | {m['repair_handoffs']} |",
        f"| Milestones accepted after a handoff | {m['accepted_after_handoff']} |",
    ]
    coverage = browser_coverage(store, run_id)
    views = coverage["viewports"]
    if views or coverage["unmeasured_receipts"]:
        lines.extend(
            [
                f"| Browser steps completed | {sum(v['completed_steps'] for v in views)} / {sum(v['planned_steps'] for v in views)} |",
                f"| Browser receipts without measured coverage | {len(coverage['unmeasured_receipts'])} |",
            ]
        )
    scenario_views = [v for v in views if v.get("scenario")]
    if scenario_views:
        lines.append(f"| Named browser scenario samples passed | {sum(v['passed_samples'] for v in scenario_views)} / {sum(v['samples'] for v in scenario_views)} |")
    from .contracts import catalog_history
    from .junit import history
    tests = history(store, run_id)["observations"]
    if tests:
        proofs = [t["proof"] for t in tests if t.get("proof")]
        if proofs:
            controls = [sample for p in proofs for sample in p["baseline"]["samples"]]
            lines.append(
                f"| Baseline samples reproducing the contracted behavior | {sum(c['passed'] for c in controls)} / {len(controls)} |"
            )
            lines.append(f"| Matched proof observations passed | {sum(p['passed'] for p in proofs)} / {len(proofs)} |")
        counts = [t["report"]["counts"] for t in tests if t["report"] and t["passed"] is not None]
        lines.append(f"| JUnit test executions passed / measured across attempts | {sum(c['passed'] for c in counts)} / {sum(c['tests'] for c in counts)} |")
        lines.append(f"| JUnit test executions skipped | {sum(c['skipped'] for c in counts)} |")
        lines.append(f"| Unscored JUnit observations | {sum(t['passed'] is None for t in tests)} |")
    samples = catalog_history(store, run_id)["observations"]
    if samples:
        lines.append(f"| Catalog observations passed | {sum(s['passed'] is True for s in samples)} / {len(samples)} |")
        lines.append(f"| Unscored catalog observations | {sum(s['passed'] is None for s in samples)} |")
        issues = sum(sum(s["violations"].values()) for s in samples if s["passed"] is not None)
        lines.append(f"| Catalog rule violations across scored attempts | {issues} |")
        for sample in reversed(samples):
            if sample["candidate_records"] is not None:
                lines.append(f"| Last measured catalog, baseline / candidate records | {sample['baseline_records']} / {sample['candidate_records']} |")
                if sample["changed_records"] is not None:
                    lines.append(f"| Last measured catalog, modified / unchanged matched records | {sample['changed_records']} / {sample['unchanged_records']} |")
                break
    atomic_write(output, "\n".join(lines) + "\n")
    return {"run_id": run_id, "outcome": m["outcome"], "output": str(output.resolve())}
