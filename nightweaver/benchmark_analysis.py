"""Read-only views of benchmark evidence, with denominators for every comparison."""

import random
from collections import defaultdict
from itertools import combinations
from math import ceil
from pathlib import Path
from statistics import fmean, mean, median

from . import context
from .benchmark import read_suite, scored, select_trials
from .engine import common_dir, git, git_bytes, verify_registered_repository
from .evaluation import matched_contract
from .safety import redact
from .spec import read_json


def grouped(rows, *keys):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in keys)].append(row)
    return groups


def fixed_inputs(trials):
    """Require a single known baseline, task contract and route within one contender."""
    return (
        bool(trials)
        and matched_contract(trials)
        and all(
            trials[0].get(k) and all(r.get(k) == trials[0][k] for r in trials)
            for k in ("mode", "route_sha256")
        )
    )


def task_intervals(rows, seed, samples=2000):
    """Percentile bootstrap over independent task units, retaining all repeats in each."""
    if not 200 <= samples <= 10000:
        raise ValueError("Choose 200 to 10000 bootstrap samples")
    groups = grouped(rows, "contender", "task")
    tasks = sorted({row["task"] for row in rows})
    pairs = []
    for left, right in combinations(sorted({r["contender"] for r in rows}), 2):
        gaps, excluded = [], []
        for task in tasks:
            a, b = groups[left, task], groups[right, task]
            sides = [{r["repeat"] for r in group} for group in (a, b)]
            if (
                not fixed_inputs(a)
                or not fixed_inputs(b)
                or sides[0] != sides[1]
                or not matched_contract([a[0], b[0]])
                or any(not scored(r) for r in a + b)
            ):
                excluded.append(task)
                continue
            gaps.append(
                100 * (mean(r["accepted"] for r in a) - mean(r["accepted"] for r in b))
            )
        interval = None
        if len(gaps) >= 2:
            rng = random.Random(seed)
            draws = sorted(
                fmean(rng.choices(gaps, k=len(gaps))) for _ in range(samples)
            )
            interval = [round(draws[ceil(samples * q) - 1], 3) for q in (0.025, 0.975)]
        pairs.append(
            {
                "left": left,
                "right": right,
                "eligible_tasks": len(gaps),
                "planned_tasks": len(tasks),
                "excluded_tasks": excluded,
                "macro_gap_left_minus_right_pp": round(mean(gaps), 3) if gaps else None,
                "task_bootstrap_95_pp": interval,
                "resamples": samples if interval else 0,
                "seed": seed,
            }
        )
    return pairs


def instability(rows):
    result = []
    for (name, task), trials in sorted(grouped(rows, "contender", "task").items()):
        valid = [r for r in trials if scored(r)]
        passes = sum(r["accepted"] for r in valid)
        status = "insufficient"
        if fixed_inputs(trials) and len(valid) >= 2:
            status = (
                "mixed"
                if 0 < passes < len(valid)
                else ("all_passed" if passes else "all_failed")
            )
        result.append(
            {
                "contender": name,
                "task": task,
                "pattern": status,
                "scored": len(valid),
                "excluded": len(trials) - len(valid),
                "accepted": passes,
                "passed_run_ids": [r["run_id"] for r in valid if r["accepted"]],
                "failed_run_ids": [r["run_id"] for r in valid if not r["accepted"]],
            }
        )
    return result


def efficiency(rows):
    result = []
    for (name,), trials in sorted(grouped(rows, "contender").items()):
        accepted = sum(r["accepted"] for r in trials)
        finished = sum(r["phase"] in ("done", "blocked") for r in trials)
        calls = sum(r["provider_calls"] for r in trials)
        measured = sum(
            r["provider_calls"] - r["usage_pending_calls"] - r["usage_unknown_calls"]
            for r in trials
        )
        tokens = sum(r["observed_tokens"] or 0 for r in trials)
        closed = finished == len(trials)
        result.append(
            {
                "contender": name,
                "planned": len(trials),
                "finished": finished,
                "accepted": accepted,
                "all_calls": calls,
                "calls_with_tokens": measured,
                "calls_per_acceptance": round(calls / accepted, 3)
                if closed and accepted
                else None,
                "tokens_per_acceptance": round(tokens / accepted, 3)
                if closed and accepted and calls == measured
                else None,
            }
        )
    return result


def latency(rows):
    result = []
    for (name,), trials in sorted(grouped(rows, "contender").items()):
        finished = [r for r in trials if r["phase"] in ("done", "blocked")]
        metrics = {}
        for key in ("elapsed_seconds", "queue_seconds", "seconds_to_first_acceptance"):
            values = sorted(r[key] for r in finished if r.get(key) is not None)
            metrics[key] = {
                "observed": len(values),
                "p50": median(values) if values else None,
                "p95_nearest_rank": values[ceil(0.95 * len(values)) - 1]
                if values
                else None,
            }
        result.append(
            {
                "contender": name,
                "planned": len(trials),
                "finished": len(finished),
                "metrics": metrics,
            }
        )
    return result


def qualify(rows, contender, floor, minimum_tasks=1):
    if not 0 <= floor <= 100 or minimum_tasks < 1:
        raise ValueError(
            "Require a percentage from 0 to 100 and a positive task minimum"
        )
    selected = select_trials(rows, contender=contender)
    tasks = grouped(selected, "task")
    rates = []
    for trials in tasks.values():
        if fixed_inputs(trials) and all(scored(r) for r in trials):
            rates.append(mean(r["accepted"] for r in trials))
    rate = 100 * mean(rates) if rates else None
    verdict = "inconclusive"
    if len(rates) == len(tasks) and len(rates) >= minimum_tasks:
        verdict = "pass" if rate >= floor else "below_floor"
    return {
        "contender": contender,
        "verdict": verdict,
        "minimum_rate_percent": floor,
        "minimum_tasks": minimum_tasks,
        "complete_tasks": len(rates),
        "planned_tasks": len(tasks),
        "macro_acceptance_percent": round(rate, 4) if rates else None,
    }


def footprint(store, rows):
    measurements = []
    for trial in rows:
        item = {k: trial[k] for k in ("run_id", "contender", "task", "repeat")}
        if not trial["accepted"]:
            measurements.append({**item, "status": "unaccepted"})
            continue
        row = store.get(trial["run_id"])
        repo = Path(row["spec"]["repository"])
        try:
            verify_registered_repository(row["spec"], common_dir(repo))
            data = git_bytes(
                repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--numstat",
                "-z",
                row["state"]["base"],
                row["state"]["accepted"],
                "--",
            )
            additions = deletions = binaries = files = 0
            for entry in data.split(b"\0"):
                if entry:
                    added, removed, _ = entry.split(b"\t", 2)
                    files += 1
                    if added == b"-":
                        binaries += 1
                    else:
                        additions += int(added)
                        deletions += int(removed)
            item.update(
                status="measured",
                files=files,
                binary_files=binaries,
                text_additions=additions,
                text_deletions=deletions,
                base=row["state"]["base"],
                tip=row["state"]["accepted"],
            )
        except (OSError, ValueError) as error:
            item.update(status="unavailable", reason=redact(str(error)))
        measurements.append(item)
    return measurements


def audit_inputs(store, suite_id):
    try:
        suite = read_suite(store, suite_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {
            "benchmark_id": suite_id,
            "verdict": "failed",
            "reason": redact(str(error)),
            "matrix_sha256": None,
            "trials": [],
            "model_calls": 0,
        }
    results = []
    for trial in suite["runs"]:
        row = store.get(trial["run_id"])
        spec, base = row["spec"], row["state"]["base"]
        repo = Path(spec["repository"])
        status, reason = "verified", None
        try:
            verify_registered_repository(spec, common_dir(repo))
            git(repo, "cat-file", "-e", base + "^{commit}")
            if not all(
                trial.get(k)
                for k in ("baseline", "mode", "route_sha256", "contract_sha256")
            ) or not spec.get("context_sha256"):
                status, reason = (
                    "unknown",
                    "historical registration lacks input digests",
                )
            else:
                pack = read_json(store.directory(trial["run_id"]) / "context.json")
                if (
                    pack.get("base") != base
                    or context.digest(pack) != spec["context_sha256"]
                ):
                    raise ValueError("Saved context differs from registered inputs")
        except (OSError, ValueError, AttributeError) as error:
            status, reason = "failed", redact(str(error))
        results.append({"run_id": trial["run_id"], "status": status, "reason": reason})
    verdict = (
        "failed"
        if any(r["status"] == "failed" for r in results)
        else (
            "inconclusive" if any(r["status"] == "unknown" for r in results) else "pass"
        )
    )
    return {
        "benchmark_id": suite_id,
        "verdict": verdict,
        "matrix_sha256": suite.get("matrix_sha256"),
        "trials": results,
        "model_calls": 0,
    }
