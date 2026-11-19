"""Control trials and descriptive comparisons over the registered task matrix."""

from collections import Counter, defaultdict

from .benchmark import scored


def control_outcome(row):
    if row["phase"] == "done" and row["accepted"]:
        return "accepted"
    if (
        row["phase"] == "blocked"
        and row["outcome"] == "rejected"
        and row.get("last_rejection_stage") == "check"
    ):
        return "failed_check"
    return None


def matched_contract(rows):
    return all(
        rows[0].get(key) and all(r.get(key) == rows[0][key] for r in rows)
        for key in ("baseline", "contract_sha256")
    )


def calibration(runs, positive, negative):
    if positive == negative or not {positive, negative} <= {r["contender"] for r in runs}:
        raise ValueError("Choose two distinct registered control contenders")
    sides = [
        {(r["task"], r["repeat"]): r for r in runs if r["contender"] == name} for name in (positive, negative)
    ]
    cells = []
    for key in sorted(sides[0].keys() | sides[1].keys()):
        good, bad = (side.get(key) for side in sides)
        verdict = "inconclusive"
        if good and bad and matched_contract([good, bad]):
            pos, neg = control_outcome(good), control_outcome(bad)
            if pos == "failed_check" or neg == "accepted":
                verdict = "failed"
            elif pos == "accepted" and neg == "failed_check":
                verdict = "pass"
        cells.append(
            {
                "task": key[0],
                "repeat": key[1],
                "verdict": verdict,
                "positive_run": good and good["run_id"],
                "negative_run": bad and bad["run_id"],
            }
        )
    counts = Counter(cell["verdict"] for cell in cells)
    verdict = "pass"
    if counts["failed"]:
        verdict = "failed"
    elif counts["inconclusive"] or not cells:
        verdict = "inconclusive"
    return {
        "verdict": verdict,
        "positive": positive,
        "negative": negative,
        "counts": dict(counts),
        "controls": cells,
    }


def task_sensitivity(report):
    pairs = defaultdict(list)
    for entry in report["per_task"]:
        for p in entry["paired_outcomes"]:
            if p["scored_pairs"]:
                gap = 100 * (p["left_only_accepted"] - p["right_only_accepted"]) / p["scored_pairs"]
                pairs[p["left"], p["right"]].append((entry["task"], gap))
    result = []
    for pair in report["paired_outcomes"]:
        tasks = pairs[pair["left"], pair["right"]]
        total = sum(gap for _, gap in tasks)
        omitted = (
            [{"omitted": task, "gap_pp": round((total - gap) / (len(tasks) - 1), 3)} for task, gap in tasks]
            if len(tasks) > 1
            else []
        )
        result.append(
            {
                "left": pair["left"],
                "right": pair["right"],
                "scored_tasks": len(tasks),
                "planned_tasks": len(report["per_task"]),
                "scored_pairs": pair["scored_pairs"],
                "macro_gap_pp": round(total / len(tasks), 3) if tasks else None,
                "leave_one_task_out": omitted,
            }
        )
    return result


def call_frontier(runs):
    groups = defaultdict(dict)
    for r in runs:
        groups[r["contender"]][r["task"], r["repeat"]] = r
    common = []
    keys = sorted(set().union(*(group.keys() for group in groups.values())))
    for key in keys:
        rows = [g.get(key) for g in groups.values()]
        if any(r is None or not scored(r) or r.get("provider_calls") is None for r in rows):
            continue
        if matched_contract(rows):
            common.append(key)
    points = []
    for name, group in sorted(groups.items()):
        points.append(
            {
                "contender": name,
                "accepted": sum(group[k]["accepted"] for k in common),
                "calls": sum(group[k]["provider_calls"] for k in common),
            }
        )
    for point in points:
        point["dominated_by"] = [
            p["contender"]
            for p in points
            if p["accepted"] >= point["accepted"]
            and p["calls"] <= point["calls"]
            and (p["accepted"] > point["accepted"] or p["calls"] < point["calls"])
        ]
        point["on_frontier"] = not point["dominated_by"] if common else None
    return {
        "matched_trials_per_contender": len(common),
        "excluded_coordinates": len(keys) - len(common),
        "unit": "observed harness provider invocations",
        "points": points,
    }


def failure_groups(runs):
    groups = defaultdict(list)
    for row in runs:
        if row["accepted"] or row["phase"] not in ("done", "blocked"):
            continue
        stage = row.get("last_rejection_stage") if row["outcome"] == "rejected" else None
        groups[row["outcome"], stage or ""].append(row)
    return [
        {
            "outcome": outcome,
            "rejection_stage": stage or None,
            "trials": len(rows),
            "tasks": sorted({r["task"] for r in rows}),
            "run_ids": [r["run_id"] for r in rows],
        }
        for (outcome, stage), rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def shard_trials(rows, shard):
    if shard is None:
        return rows
    try:
        index, count = map(int, shard.split("/"))
    except ValueError as error:
        raise ValueError("Shard must be INDEX/TOTAL, for example 1/3") from error
    if not 1 <= index <= count <= 500:
        raise ValueError("Require 1 <= shard index <= total <= 500")
    ordered = sorted(rows, key=lambda r: (r["task"], r["contender"], r["repeat"]))
    selected = {r["run_id"] for r in ordered[index - 1 :: count]}
    return [r for r in rows if r["run_id"] in selected]
