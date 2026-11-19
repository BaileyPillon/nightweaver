"""Matched benchmark history and CI output built from the same run measurements."""

import re
import csv
from io import StringIO
from collections import Counter
from statistics import median
from xml.etree import ElementTree as ET

from .benchmark import INCONCLUSIVE, suite_report
from .safety import atomic_write


def compare_suites(store, before_id, after_id):
    if before_id == after_id:
        raise ValueError("Select two different benchmarks")

    def indexed(suite_id):
        report = suite_report(store, suite_id)
        return {(row["task"], row["contender"], row["repeat"]): row for row in report["runs"]}, report.get("runtime", {})

    (before, old_runtime), (after, new_runtime) = indexed(before_id), indexed(after_id)
    rows, deltas, call_deltas, token_deltas = [], [], [], []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key), after.get(key)
        delta = calls = tokens = None
        fields = ("baseline", "contract_sha256", "mode", "route_sha256")
        changed = [k for k in fields if left.get(k) != right.get(k)] if left and right else []
        if not left or not right:
            status = "unmatched"
        elif not left.get("contract_sha256") or not right.get("contract_sha256"):
            status = "unknown_contract"
        elif any(not row.get(k) for row in (left, right) for k in ("baseline", "mode", "route_sha256")):
            status = "unknown_inputs"
        elif any(
            left.get(field) != right.get(field) for field in fields
        ):
            status = "changed_inputs"
        elif any(
            r["outcome"] in INCONCLUSIVE or r["phase"] not in ("done", "blocked") for r in (left, right)
        ):
            status = "inconclusive"
        elif left["accepted"] and right["accepted"]:
            status = "both_accepted"
            if all(isinstance(r.get("provider_calls"), int) for r in (left, right)):
                calls = right["provider_calls"] - left["provider_calls"]
                call_deltas.append(calls)
            if all(r.get("observed_tokens") is not None and r.get("usage_unknown_calls") == 0
                   and r.get("usage_pending_calls") == 0 for r in (left, right)):
                tokens = right["observed_tokens"] - left["observed_tokens"]
                token_deltas.append(tokens)
            if left["elapsed_seconds"] is not None and right["elapsed_seconds"] is not None:
                delta = round(right["elapsed_seconds"] - left["elapsed_seconds"], 3)
                deltas.append(delta)
        elif left["accepted"]:
            status = "regression"
        elif right["accepted"]:
            status = "improvement"
        else:
            status = "neither_accepted"
        rows.append(
            {
                "task": key[0],
                "contender": key[1],
                "repeat": key[2],
                "status": status,
                "before": left["run_id"] if left else None,
                "after": right["run_id"] if right else None,
                "elapsed_delta_seconds": delta,
                "changed_fields": changed, "provider_call_delta": calls, "observed_token_delta": tokens,
            }
        )
    return {
        "before": before_id,
        "after": after_id,
        "runtime_changes": {k: {"before": old_runtime.get(k), "after": new_runtime.get(k)}
                            for k in sorted(old_runtime.keys() | new_runtime.keys())
                            if old_runtime.get(k) != new_runtime.get(k)},
        "runtime_recorded_both": bool(old_runtime and new_runtime),
        "counts": dict(Counter(r["status"] for r in rows)),
        "timed_pairs": len(deltas),
        "median_elapsed_delta_seconds": median(deltas) if deltas else None,
        "call_pairs": len(call_deltas), "median_provider_call_delta": median(call_deltas) if call_deltas else None,
        "token_pairs": len(token_deltas), "median_observed_token_delta": median(token_deltas) if token_deltas else None,
        "pairs": rows,
    }


def xml_text(value):
    return re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]", "�", str(value))


def regression_verdict(comparison, minimum, allowed, minimum_tasks=1):
    if minimum < 1 or minimum_tasks < 1 or allowed < 0:
        raise ValueError("Minimum matched pairs and tasks must be positive and allowed regressions nonnegative")
    counts = comparison["counts"]
    matched = sum(counts.get(k, 0) for k in ("regression", "improvement", "both_accepted", "neither_accepted"))
    excluded = len(comparison["pairs"]) - matched
    regressions = counts.get("regression", 0)
    scored_statuses = {"regression", "improvement", "both_accepted", "neither_accepted"}
    tasks = {row["task"] for row in comparison["pairs"] if row["status"] in scored_statuses}
    if regressions > allowed:
        verdict = "regression"
    elif matched < minimum or len(tasks) < minimum_tasks or excluded:
        verdict = "inconclusive"
    else:
        verdict = "pass"
    return {"verdict": verdict, "matched": matched, "excluded": excluded, "regressions": regressions,
            "allowed_regressions": allowed, "minimum_pairs": minimum, "counts": counts,
            "matched_tasks": len(tasks), "minimum_tasks": minimum_tasks,
            "before": comparison["before"], "after": comparison["after"]}


def export_csv(store, suite_id, output):
    report = suite_report(store, suite_id)
    keys = ("run_id", "task", "contender", "repeat", "baseline", "contract_sha256", "route_sha256", "mode",
            "phase", "outcome", "accepted", "provider_calls", "observed_tokens", "usage_unknown_calls", "elapsed_seconds",
            "usage_pending_calls", "interventions", "worker_attempts", "queue_seconds", "seconds_to_first_acceptance")
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(keys)
    for trial in report["runs"]:
        values = [trial.get(k) for k in keys]
        writer.writerow(["'" + v if isinstance(v, str) and
                         (v.startswith(("\t", "\r", "\n")) or v.lstrip().startswith(("=", "+", "-", "@"))) else v for v in values])
    atomic_write(output, stream.getvalue().encode("utf-8-sig"))
    return {"benchmark_id": suite_id, "file": str(output.resolve()), "trials": len(report["runs"])}


def export_junit(store, suite_id, output):
    report = suite_report(store, suite_id)
    runs = report["runs"]
    suite = ET.Element("testsuite", name=xml_text(report["name"]), tests=str(len(runs)))
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    for row in runs:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=xml_text(row["contender"]),
            name=xml_text(f"{row['task']} [{row['repeat']}]"),
        )
        if row["elapsed_seconds"] is not None:
            case.set("time", str(row["elapsed_seconds"]))
        if row["accepted"]:
            continue
        if row["outcome"] in INCONCLUSIVE:
            tag, key = "error", "errors"
        elif row["phase"] not in ("done", "blocked"):
            tag, key = "skipped", "skipped"
        else:
            tag, key = "failure", "failures"
        counts[key] += 1
        ET.SubElement(
            case, tag, type=row["outcome"], message=xml_text(row.get("block_reason") or row["outcome"])
        )
    suite.attrib.update({k: str(v) for k, v in counts.items()})
    atomic_write(output, ET.tostring(suite, encoding="utf-8", xml_declaration=True))
    return {"benchmark_id": suite_id, "file": str(output.resolve()), "tests": len(runs), **counts}
