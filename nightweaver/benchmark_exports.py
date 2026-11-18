"""Matched benchmark history and CI output built from the same run measurements."""

import re
from collections import Counter
from statistics import median
from xml.etree import ElementTree as ET

from .benchmark import INCONCLUSIVE, suite_report
from .safety import atomic_write


def compare_suites(store, before_id, after_id):
    if before_id == after_id:
        raise ValueError("Select two different benchmarks")

    def indexed(suite_id):
        runs = suite_report(store, suite_id)["runs"]
        return {(row["task"], row["contender"], row["repeat"]): row for row in runs}

    before, after = indexed(before_id), indexed(after_id)
    rows, deltas = [], []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key), after.get(key)
        delta = None
        if not left or not right:
            status = "unmatched"
        elif not left.get("contract_sha256") or not right.get("contract_sha256"):
            status = "unknown_contract"
        elif any(
            left.get(field) != right.get(field)
            for field in ("baseline", "contract_sha256", "mode", "route_sha256")
        ):
            status = "changed_inputs"
        elif any(
            r["outcome"] in INCONCLUSIVE or r["phase"] not in ("done", "blocked") for r in (left, right)
        ):
            status = "inconclusive"
        elif left["accepted"] and right["accepted"]:
            status = "both_accepted"
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
            }
        )
    return {
        "before": before_id,
        "after": after_id,
        "counts": dict(Counter(r["status"] for r in rows)),
        "timed_pairs": len(deltas),
        "median_elapsed_delta_seconds": median(deltas) if deltas else None,
        "pairs": rows,
    }


def xml_text(value):
    return re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]", "�", str(value))


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
