"""Repeatable coding-workflow experiments with frozen baselines and all outcomes retained."""

from copy import deepcopy
from hashlib import sha256
from collections import Counter
from html import escape
import json
from math import comb, sqrt
from pathlib import Path
import random
from statistics import median
from itertools import combinations
from uuid import uuid4
import platform

from . import __version__

from .engine import create_run, directory_identity, git, source_info
from .reports import document, metrics
from .safety import atomic_write, redact, write_json
from .spec import array, fields, integer, provider, IMPLEMENTERS, read_plan, read_json


INCONCLUSIVE = {"provider_blocked", "execution_blocked", "interrupted", "verification_inconsistent"}


def config_digest(config):
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(serialized).hexdigest()


def create_suite(store, source, *, preview=False):
    source = Path(source).resolve(strict=True)
    raw = read_json(source)
    fields(raw, "name tasks contenders repetitions seed schedule", "benchmark")
    schedule = raw.get("schedule", "shuffle")
    if schedule not in ("shuffle", "balanced"):
        raise ValueError("Benchmark schedule must be shuffle or balanced")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Benchmark needs a name")
    repetitions = integer(raw.get("repetitions", 3), "repetitions", 1, 20)
    seed = integer(raw.get("seed", 42), "seed", 0, 2147483647)
    tasks = array(raw.get("tasks"), "benchmark tasks", 50)
    contenders = array(raw.get("contenders"), "contenders", 10)
    if not tasks or not contenders or len(tasks) * len(contenders) * repetitions > 500:
        raise ValueError("Benchmark needs tasks and contenders, with at most 500 total runs")
    prepared, task_names, contender_names = [], set(), set()
    for contender in contenders:
        fields(contender, "name worker reviewer worker_fallbacks mode", "contender")
        if not isinstance(contender.get("name"), str) or not contender["name"].strip() or contender["name"] in contender_names:
            raise ValueError("Contender names must be unique nonempty strings")
        contender_names.add(contender["name"])
        contender["worker"] = provider(contender.get("worker"), IMPLEMENTERS, "benchmark worker", source.parent)
        mode = contender.get("mode", "nightweaver")
        if mode not in ("nightweaver", "external", "standalone"):
            raise ValueError("Contender mode must be nightweaver, standalone or external")
        if contender.get("mode") == "external" and (contender["worker"]["kind"] != "command" or contender.get("reviewer") or contender.get("worker_fallbacks")):
            raise ValueError("External contenders require one command route and manage their own orchestration")
        if mode == "standalone" and (contender["worker"]["kind"] not in {"codex", "claude", "gemini", "antigravity", "deepseek"}
                                    or contender.get("reviewer") or contender.get("worker_fallbacks")):
            raise ValueError("Standalone contenders require one native model CLI without reviewer or fallback")
        if "reviewer" in contender:
            contender["reviewer"] = provider(contender["reviewer"], IMPLEMENTERS, "benchmark reviewer", source.parent)
        contender["worker_fallbacks"] = [provider(route, IMPLEMENTERS, "benchmark fallback", source.parent) for route in
                                          array(contender.get("worker_fallbacks", []), "benchmark fallbacks", 5)]
    for task in tasks:
        fields(task, "name plan tags", "benchmark task")
        tags = array(task.get("tags", []), "task tags", 32)
        if any(not isinstance(tag, str) or not tag.strip() or tag != tag.strip() for tag in tags) or len(set(tags)) != len(tags):
            raise ValueError("Task tags must be distinct nonempty strings without surrounding spaces")
        if not isinstance(task.get("name"), str) or not task["name"].strip() or task["name"] in task_names:
            raise ValueError("Task names must be unique nonempty strings")
        task_names.add(task["name"])
        spec = read_plan(source.parent / task["plan"], store)
        source_data = source_info(store, spec)
        try:
            for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                git(Path(spec["repository"]), "var", identity)
        except ValueError as error:
            raise ValueError("Benchmark checkout needs a Git author and committer identity to record accepted trials. "
                             "Set user.name and user.email locally in the benchmark repository.") from error
        spec["expected_base"] = source_data["base"]
        # Match the operator's full brief and checks before adapting it to each contender.
        contract = {k: v for k, v in spec.items() if k not in
                    {"repository", "project", "git_common_dir", "expected_base", "reviewer", "reviewer_fallbacks", "milestones"}}
        contract["milestones"] = [{k: v for k, v in step.items() if k not in {"worker", "worker_fallbacks"}}
                                  for step in spec["milestones"]]
        digest = config_digest(contract)
        spec["git_common_dir"] = source_data["git_common_dir"]
        spec["git_directory_identity"] = directory_identity(source_data["git_common_dir"])
        if not spec.get("budgets", {}).get("max_seconds"):
            raise ValueError("Benchmark task plans require a max_seconds budget")
        for contender in contenders:
            route = {k: v for k, v in contender.items() if k not in ("name", "mode")}
            route_hash = config_digest(route)
            for repeat in range(repetitions):
                plan = deepcopy(spec)
                plan.pop("reviewer", None)
                plan["reviewer_fallbacks"] = []
                if contender.get("reviewer"):
                    plan["reviewer"] = contender["reviewer"]
                for step in plan["milestones"]:
                    step["worker"] = contender["worker"]
                    step["worker_fallbacks"] = contender.get("worker_fallbacks", [])
                if contender.get("mode") in ("external", "standalone"):
                    checks = {}
                    for step in plan["milestones"]:
                        for gate in step["gates"]:
                            if gate["name"] in checks and gate != checks[gate["name"]]:
                                raise ValueError("Single-invocation benchmark has conflicting gate names")
                            checks[gate["name"]] = gate
                    # Preserve the complete job when removing the controller's milestone loop.
                    brief = "Complete all of the following work in this single invocation. " \
                            "Use your own tools to inspect, test and fix the result before returning.\n\n" + "\n\n".join(
                                f"{i}. {step['title']}\n{step['task']}\nAllowed paths: {step['paths'] or 'repository files needed for the task'}"
                                for i, step in enumerate(plan["milestones"], 1))
                    allowed = list(dict.fromkeys(path for step in plan["milestones"] for path in step["paths"])) \
                        if all(step["paths"] for step in plan["milestones"]) else []
                    plan["milestones"] = [{"title": task["name"], "task": brief, "worker": contender["worker"],
                                            "worker_fallbacks": [], "paths": allowed, "gates": list(checks.values())}]
                    plan["attempts"] = 1
                    plan["retry"] = {**plan.get("retry", {}), "transient_attempts": 0}
                    plan["budgets"] = {**plan["budgets"], "max_calls": 1}
                    plan.pop("advisory_ollama", None)
                prepared.append({"task": task["name"], "contract_sha256": digest, "route_sha256": route_hash,
                                 "contender": contender["name"], "repeat": repeat + 1,
                                 "mode": contender.get("mode", "nightweaver"), "tags": tags, "plan": plan})
    rng = random.Random(seed)
    if schedule == "shuffle":
        rng.shuffle(prepared)
    else:
        # Each task has a seeded first order. Rotate it each repetition so every
        # contender takes every position once per full cycle.
        orders = {}
        for task_name in sorted(task_names):
            order = sorted(contender_names)
            rng.shuffle(order)
            orders[task_name] = order
        blocks = sorted({(r["task"], r["repeat"]) for r in prepared})
        rng.shuffle(blocks)
        slot = {key: i for i, key in enumerate(blocks)}
        def position(row):
            order = orders[row["task"]]
            return slot[row["task"], row["repeat"]], (order.index(row["contender"]) - row["repeat"] + 1) % len(order)
        prepared.sort(key=position)
    coordinates = [{k: r[k] for k in ("task", "contender", "repeat", "mode", "tags", "contract_sha256", "route_sha256")}
                   | {"baseline": r["plan"]["expected_base"]} for r in prepared]
    matrix_digest = config_digest(redact(coordinates))
    if preview:
        rows = [{**{k: v for k, v in row.items() if k != "plan"}, "baseline": row["plan"]["expected_base"],
                 "max_seconds": row["plan"]["budgets"]["max_seconds"],
                 "max_calls": row["plan"]["budgets"].get("max_calls")} for row in prepared]
        budgets = []
        for label in sorted(contender_names):
            trials = [r for r in rows if r["contender"] == label]
            caps = [r["max_calls"] for r in trials if r["max_calls"] is not None]
            budgets.append({"contender": label, "trials": len(trials), "capped_call_trials": len(caps),
                            "max_calls": sum(caps) if len(caps) == len(trials) else None,
                            "max_seconds": sum(r["max_seconds"] for r in trials)})
        task_budgets = []
        for label in sorted(task_names):
            trials = [r for r in rows if r["task"] == label]
            task_budgets.append({"task": label, "trials": len(trials), "max_seconds": sum(r["max_seconds"] for r in trials),
                                 "max_calls": sum(r["max_calls"] for r in trials) if all(r["max_calls"] is not None for r in trials) else None})
        return {"name": name, "seed": seed, "schedule": schedule, "matrix_sha256": matrix_digest,
                "trials": len(rows), "runs": rows, "contender_budgets": budgets, "task_budgets": task_budgets,
                "configured_seconds_total": sum(row["max_seconds"] for row in rows),
                "configured_call_cap_total": (sum(row["max_calls"] for row in rows)
                                              if all(row["max_calls"] is not None for row in rows) else None)}
    suite_id = uuid4().hex[:12]
    manifest = {"id": suite_id, "name": name, "seed": seed, "repetitions": repetitions, "runs": [],
                "planned_trials": len(prepared), "matrix_sha256": matrix_digest, "schedule": schedule,
                "runtime": {"nightweaver": __version__, "python": platform.python_version(), "platform": platform.platform()},
                "contenders": contenders}
    target = store.root / "benchmarks" / f"{suite_id}.json"
    write_json(target, manifest)
    for row in prepared:
        plan = row.pop("plan")
        run_id = create_run(store, plan)
        store.transition(run_id, "benchmark_registered", {"suite": suite_id, "matrix_sha256": matrix_digest,
                                                         "schedule": schedule, "seed": seed, **row})
        manifest["runs"].append({**row, "run_id": run_id, "baseline": plan["expected_base"]})
        write_json(target, manifest)
    return {"benchmark_id": suite_id, "runs": len(prepared), "manifest": str(target), "started": False}


def read_suite(store, suite_id):
    store.directory(suite_id)  # validate identifier without accepting arbitrary paths
    suite = read_json(store.root / "benchmarks" / f"{suite_id}.json")
    if suite.get("id") != suite_id:
        raise ValueError("Benchmark manifest ID differs from the selected suite")
    registered = len(suite["runs"])
    if registered != suite.get("planned_trials", registered):
        raise ValueError(f"Incomplete benchmark registration: {registered}/{suite['planned_trials']} trials")
    coordinates, ids = set(), set()
    for row in suite["runs"]:
        key = (row["task"], row["contender"], row["repeat"])
        if key in coordinates or row["run_id"] in ids:
            raise ValueError("Duplicate trial coordinate or run ID in benchmark manifest")
        coordinates.add(key)
        ids.add(row["run_id"])
    if not ids or len(ids) > 500:
        raise ValueError("Benchmark manifest must contain between 1 and 500 trials")
    # All historical suite registrations wrote this event, including suites
    # created before contract and route digests were introduced.
    with store.connect() as db:
        placeholders = ",".join("?" for _ in ids)
        registrations = db.execute(
            f"SELECT e.run_id,e.details,json_extract(r.state,'$.base') AS base FROM events e "
            f"JOIN runs r ON e.run_id=r.id WHERE e.kind='benchmark_registered' AND e.run_id IN ({placeholders})",
            tuple(ids)).fetchall()
    receipts = {r["run_id"]: (json.loads(r["details"]), r["base"]) for r in registrations}
    if len(registrations) != len(ids) or receipts.keys() != ids:
        raise ValueError("Benchmark trials lack unique registration receipts")
    for row in suite["runs"]:
        receipt, base = receipts[row["run_id"]]
        if (receipt.get("suite") != suite_id or type(row["repeat"]) is not int
                or any(redact(row[k]) != receipt.get(k) for k in ("task", "contender", "repeat"))
                or any(redact(row[k]) != receipt.get(k) for k in ("contract_sha256", "route_sha256", "mode") if k in row)
                or redact(row.get("tags")) != receipt.get("tags")
                or any(suite.get(k) != receipt[k] for k in ("schedule", "seed") if k in receipt)
                or row.get("baseline", base) != base or suite.get("matrix_sha256") != receipt.get("matrix_sha256")):
            raise ValueError("Benchmark trial differs from its registration receipt")
    if suite.get("matrix_sha256"):
        # Legacy exports can omit optional hashes. Use the original registration
        # to validate membership/order without granting missing fields a score.
        frozen = [{k: receipts[r["run_id"]][0].get(k) for k in
                   ("task", "contender", "repeat", "mode", "tags", "contract_sha256", "route_sha256")}
                  | {"baseline": receipts[r["run_id"]][1]} for r in suite["runs"]]
        if config_digest(frozen) != suite["matrix_sha256"]:
            raise ValueError("Benchmark matrix differs from its registered order or membership")
    return suite


def scored(row):
    return row["phase"] in ("done", "blocked") and row["outcome"] not in INCONCLUSIVE


def wilson_interval(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    scale = 1 + z*z / total
    midpoint = (p + z*z/(2*total)) / scale
    margin = z * sqrt(p*(1-p)/total + z*z/(4*total*total)) / scale
    return [max(0, midpoint-margin), min(1, midpoint+margin)]


def reliability(runs, k):
    if type(k) is not int or not 1 <= k <= 20:
        raise ValueError("k must be between 1 and 20")
    groups = {}
    for row in runs:
        key = row["contender"], row["task"], row.get("baseline"), row.get("contract_sha256"), row.get("route_sha256"), row.get("mode")
        groups.setdefault(key, []).append(row)
    results = []
    for (name, task, base, contract, route, mode), rows in groups.items():
        valid = [r for r in rows if scored(r)]
        n, c = len(valid), sum(r["accepted"] for r in valid)
        den = comb(n, k) if n >= k and base and contract and route and mode else 0
        results.append({"contender":name, "task":task, "base":base, "contract_sha256":contract, "route_sha256":route, "mode":mode,
                        "scored":n, "accepted":c, "excluded":len(rows)-n, "k":k,
                        "unavailable_reason": (None if den else "missing_identity" if not (base and contract and route and mode) else "insufficient_scored_repeats"),
                        "pass_at_k":1 - comb(n-c,k)/den if den else None, "pass_all_k":comb(c,k)/den if den else None})
    return results


def select_trials(rows, task=None, contender=None, repeat=None):
    selected = [r for r in rows if (task is None or r["task"] == task)
                and (contender is None or r["contender"] == contender)
                and (repeat is None or r["repeat"] == repeat)]
    if not selected:
        raise ValueError("No registered benchmark trials match the selection")
    return selected


def filter_tag(rows, tag):
    if tag is None:
        return rows
    selected = [row for row in rows if tag in row.get("tags", [])]
    if not selected:
        raise ValueError(f"No registered tasks have tag {tag!r}")
    return selected


def summarize(runs):
    summaries = []
    for name in sorted({row["contender"] for row in runs}):
        rows = [row for row in runs if row["contender"] == name]
        completed = [row for row in rows if row["phase"] in ("done", "blocked")]
        successes = sum(row["accepted"] for row in rows)
        times = [row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None]
        total_seconds = sum(times) if len(times) == len(completed) == len(rows) else None
        valid = [r for r in rows if scored(r)]
        by_task = {}
        for r in valid:
            by_task.setdefault(r["task"], []).append(r["accepted"])
        scored_passes = sum(r["accepted"] for r in valid)
        started_calls = sum(r["provider_calls"] for r in rows)
        unknown_calls = sum(r["usage_unknown_calls"] + r["usage_pending_calls"] for r in rows)
        first_pass = sum(r["accepted"] and r.get("total_milestones", 0) > 0
                         and r.get("first_pass_milestones") == r["total_milestones"] for r in rows)
        repaired = sum(r["accepted"] and r.get("repaired_milestones", 0) > 0 for r in rows)
        summaries.append({"contender": name, "planned": len(rows), "finished": len(completed), "accepted": successes,
                          "scored_trials": len(valid), "scored_tasks": len(by_task),
                          "macro_task_acceptance": (sum(sum(v)/len(v) for v in by_task.values())/len(by_task) if by_task else None),
                          "scored_wilson_95": wilson_interval(scored_passes, len(valid)),
                          "outcomes": dict(Counter(row["outcome"] for row in rows)),
                          "blocked": sum(row["phase"] == "blocked" for row in rows),
                          "unfinished": len(rows) - len(completed),
                          "acceptance_rate": successes / len(rows), "unassisted": sum(row["unassisted_acceptance"] for row in rows),
                          "first_pass_acceptances": first_pass, "accepted_after_repair": repaired,
                          "unclassified_acceptances": successes - first_pass - repaired,
                          "finished_acceptance_rate": successes / len(completed) if completed else None,
                          "calls_all_attempts": started_calls,
                          "calls_with_token_usage": started_calls - unknown_calls,
                          "token_coverage_fraction": (started_calls - unknown_calls) / started_calls if started_calls else None,
                          "observed_tokens": sum(row["observed_tokens"] or 0 for row in rows) if any(
                              row["observed_tokens"] is not None for row in rows) else None,
                          "usage_unknown_calls": sum(row["usage_unknown_calls"] for row in rows),
                          "usage_pending_calls": sum(row["usage_pending_calls"] for row in rows),
                          "timed_trials": len(times), "known_elapsed_seconds": round(sum(times), 3) if times else None,
                          "seconds_all_attempts": round(total_seconds, 3) if total_seconds is not None else None,
                          "seconds_per_acceptance": round(total_seconds / successes, 3) if successes and total_seconds is not None else None})
    return summaries


def paired_outcomes(runs):
    pairs = []
    for left, right in combinations(sorted({row["contender"] for row in runs}), 2):
        sides = [{(row["task"], row["repeat"]): row for row in runs if row["contender"] == name}
                 for name in (left, right)]
        counts = dict(both_accepted=0, left_only_accepted=0, right_only_accepted=0, neither_accepted=0,
                      unfinished_pairs=0, inconclusive_pairs=0)
        time_ratios = []
        exclusions = Counter()
        for key in sides[0].keys() | sides[1].keys():
            a, b = sides[0].get(key), sides[1].get(key)
            if not a or not b or any(row["phase"] not in ("done", "blocked") for row in (a, b)):
                counts["unfinished_pairs"] += 1
                exclusions["missing_side" if not a or not b else "unfinished"] += 1
            elif (not a.get("baseline") or a["baseline"] != b.get("baseline") or not a.get("contract_sha256")
                  or a["contract_sha256"] != b.get("contract_sha256") or any(row.get("outcome") in INCONCLUSIVE for row in (a, b))):
                counts["inconclusive_pairs"] += 1
                if not all(row.get("baseline") and row.get("contract_sha256") for row in (a,b)):
                    exclusions["unknown_inputs"] += 1
                elif a["baseline"] != b["baseline"] or a["contract_sha256"] != b["contract_sha256"]:
                    exclusions["changed_inputs"] += 1
                else:
                    exclusions["operational_outcome"] += 1
            else:
                label = ("both_accepted" if b["accepted"] else "left_only_accepted") if a["accepted"] else (
                    "right_only_accepted" if b["accepted"] else "neither_accepted")
                counts[label] += 1
                if a["accepted"] and b["accepted"] and a.get("elapsed_seconds") and b.get("elapsed_seconds"):
                    time_ratios.append(b["elapsed_seconds"] / a["elapsed_seconds"])
        scored = sum(counts[k] for k in ("both_accepted", "left_only_accepted", "right_only_accepted", "neither_accepted"))
        difference = counts["left_only_accepted"] - counts["right_only_accepted"]
        wins, losses = counts["left_only_accepted"], counts["right_only_accepted"]
        discordant = wins + losses
        p_value = min(1.0, 2 * sum(comb(discordant, k) for k in range(min(wins, losses) + 1))
                      / 2 ** discordant) if discordant else None
        pairs.append({"left": left, "right": right, **counts, "scored_pairs": scored, "exclusion_reasons": dict(exclusions),
                      "discordant_pairs": discordant, "exact_two_sided_p": p_value,
                      "acceptance_gap_left_minus_right_pp": round(100 * difference / scored, 2) if scored else None,
                      "timed_success_pairs": len(time_ratios),
                      "median_right_seconds_over_left": round(median(time_ratios), 3) if time_ratios else None})
    return pairs


def suite_report(store, suite_id, output=None, *, tag=None):
    suite = read_suite(store, suite_id)
    items = filter_tag(suite["runs"], tag)
    runs = [{**item, **metrics(store, item["run_id"]), "baseline": item.get("baseline")} for item in items]
    summaries = summarize(runs)
    per_task = []
    for task in sorted({r["task"] for r in runs}):
        group = [r for r in runs if r["task"] == task]
        per_task.append({"task": task, "summaries": summarize(group), "paired_outcomes": paired_outcomes(group)})
    result = {"benchmark_id": suite_id, "name": suite["name"], "summaries": summaries, "runs": runs,
              "per_task": per_task, "seed": suite.get("seed"), "schedule": suite.get("schedule", "shuffle"),
              "matrix_sha256": suite.get("matrix_sha256"), "tag": tag,
              "paired_outcomes": paired_outcomes(runs), "runtime": suite.get("runtime", {}),
              "interpretation": "Exploratory paired task runs; no automatic winner or statistical significance claim. "
              "Unfinished runs are not failures. acceptance_rate uses all planned runs; finished_acceptance_rate uses only done/blocked runs. "
              "These are operational acceptance rates, including account/environment blockers; matched pairs with those blockers are inconclusive. "
              "Macro task acceptance weights each scored task equally. The descriptive Wilson interval assumes independent trials. "
              "The exploratory exact sign test assumes independent discordant pairs. Repeated tasks may be correlated. "
              "Calls count CLI invocations, not equal model compute. Inspect modes, model versions and usage coverage."}
    result = redact(result)
    if output:
        body = f'<header><h1>{escape(result["name"])}</h1><p>{escape(result["interpretation"])}</p></header><h2>Contenders</h2><div class="table"><table><thead><tr><th>Setup</th><th>Finished / planned</th><th>Accepted / finished</th><th>Scored / planned</th><th>Outcomes</th><th>No intervention</th><th>Total calls</th><th>Seconds / acceptance</th></tr></thead><tbody>'
        for row in result["summaries"]:
            body += '<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in [row["contender"], f'{row["finished"]}/{row["planned"]}', f'{row["accepted"]}/{row["finished"]}', f'{row["scored_trials"]}/{row["planned"]}', ", ".join(f"{key.replace('_', ' ')}: {count}" for key, count in row["outcomes"].items()), row["unassisted"], row["calls_all_attempts"], row["seconds_per_acceptance"] if row["seconds_per_acceptance"] is not None else "Unavailable"]) + '</tr>'
        body += ('</tbody></table></div><h2>Matched outcomes</h2><p>Acceptance gap is left minus right in percentage points. '
                 'Time ratio divides right elapsed time by left elapsed time on pairs where both passed. '
                 'A ratio above 1 means the left workflow was faster.</p><div class="table"><table><thead><tr>'
                 '<th>Left</th><th>Right</th><th>Scored pairs</th><th>Acceptance gap</th><th>Median time ratio</th>'
                 '<th>Timed pairs</th><th>Inconclusive</th><th>Unfinished</th><th>Discordant pairs</th><th>Exact two-sided p</th></tr></thead><tbody>')
        for pair in result["paired_outcomes"]:
            cells = [pair[key] for key in ("left", "right", "scored_pairs", "acceptance_gap_left_minus_right_pp",
                                           "median_right_seconds_over_left", "timed_success_pairs", "inconclusive_pairs", "unfinished_pairs",
                                           "discordant_pairs", "exact_two_sided_p")]
            body += '<tr>' + ''.join(f'<td>{escape(str(value)) if value is not None else "—"}</td>' for value in cells) + '</tr>'
        body += '</tbody></table></div>'
        body += '<h2>Results by task</h2>'
        for item in result["per_task"]:
            detail = {"summaries": item["summaries"], "paired_outcomes": item["paired_outcomes"]}
            body += f'<details><summary>{escape(item["task"])}</summary><pre>{escape(json.dumps(detail, indent=2))}</pre></details>'
        body += '<h2>Every task and attempt</h2>'
        for row in result["runs"]:
            body += f'<details><summary>{escape(row["task"])} · {escape(row["contender"])} · repeat {row["repeat"]} · {escape(row["phase"])}</summary><pre>{escape(json.dumps(row, indent=2))}</pre></details>'
        atomic_write(output, document(result["name"], body))
        result["report"] = str(Path(output).resolve())
    return result
