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

from .engine import create_run, git, source_info
from .reports import document, metrics
from .safety import atomic_write, redact, write_json
from .spec import array, fields, integer, provider, IMPLEMENTERS, read_plan


INCONCLUSIVE = {"provider_blocked", "execution_blocked", "interrupted", "verification_inconsistent"}


def config_digest(config):
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(serialized).hexdigest()


def create_suite(store, source, *, preview=False):
    source = Path(source).resolve(strict=True)
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    fields(raw, "name tasks contenders repetitions seed", "benchmark")
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
        contender["worker"] = provider(contender.get("worker"), IMPLEMENTERS, "benchmark worker")
        mode = contender.get("mode", "nightweaver")
        if mode not in ("nightweaver", "external", "standalone"):
            raise ValueError("Contender mode must be nightweaver, standalone or external")
        if contender.get("mode") == "external" and (contender["worker"]["kind"] != "command" or contender.get("reviewer") or contender.get("worker_fallbacks")):
            raise ValueError("External contenders require one command route and manage their own orchestration")
        if mode == "standalone" and (contender["worker"]["kind"] not in {"codex", "claude", "gemini", "deepseek"}
                                    or contender.get("reviewer") or contender.get("worker_fallbacks")):
            raise ValueError("Standalone contenders require one native model CLI without reviewer or fallback")
        if contender.get("reviewer"):
            contender["reviewer"] = provider(contender["reviewer"], IMPLEMENTERS, "benchmark reviewer")
        contender["worker_fallbacks"] = [provider(route, IMPLEMENTERS, "benchmark fallback") for route in
                                          array(contender.get("worker_fallbacks", []), "benchmark fallbacks", 5)]
    for task in tasks:
        fields(task, "name plan", "benchmark task")
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
                    {"repository", "project", "expected_base", "reviewer", "reviewer_fallbacks", "milestones"}}
        contract["milestones"] = [{k: v for k, v in step.items() if k not in {"worker", "worker_fallbacks"}}
                                  for step in spec["milestones"]]
        digest = config_digest(contract)
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
                                 "mode": contender.get("mode", "nightweaver"), "plan": plan})
    random.Random(seed).shuffle(prepared)
    if preview:
        rows = [{**{k: v for k, v in row.items() if k != "plan"}, "baseline": row["plan"]["expected_base"],
                 "max_seconds": row["plan"]["budgets"]["max_seconds"],
                 "max_calls": row["plan"]["budgets"].get("max_calls")} for row in prepared]
        return {"name": name, "seed": seed, "trials": len(rows), "runs": rows,
                "configured_seconds_total": sum(row["max_seconds"] for row in rows),
                "configured_call_cap_total": (sum(row["max_calls"] for row in rows)
                                              if all(row["max_calls"] is not None for row in rows) else None)}
    suite_id = uuid4().hex[:12]
    manifest = {"id": suite_id, "name": name, "seed": seed, "repetitions": repetitions, "runs": [],
                "planned_trials": len(prepared),
                "runtime": {"nightweaver": __version__, "python": platform.python_version(), "platform": platform.platform()},
                "contenders": contenders}
    target = store.root / "benchmarks" / f"{suite_id}.json"
    write_json(target, manifest)
    for row in prepared:
        plan = row.pop("plan")
        run_id = create_run(store, plan)
        store.transition(run_id, "benchmark_registered", {"suite": suite_id, **row})
        manifest["runs"].append({**row, "run_id": run_id, "baseline": plan["expected_base"]})
        write_json(target, manifest)
    return {"benchmark_id": suite_id, "runs": len(prepared), "manifest": str(target), "started": False}


def read_suite(store, suite_id):
    store.directory(suite_id)  # validate identifier without accepting arbitrary paths
    suite = json.loads((store.root / "benchmarks" / f"{suite_id}.json").read_text(encoding="utf-8"))
    registered = len(suite["runs"])
    if registered != suite.get("planned_trials", registered):
        raise ValueError(f"Incomplete benchmark registration: {registered}/{suite['planned_trials']} trials")
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
        key = row["contender"], row["task"], row["baseline"], row.get("contract_sha256"), row.get("route_sha256")
        groups.setdefault(key, []).append(row)
    results = []
    for (name, task, base, contract, route), rows in groups.items():
        valid = [r for r in rows if scored(r)]
        n, c = len(valid), sum(r["accepted"] for r in valid)
        den = comb(n, k) if n >= k and contract and route else 0
        results.append({"contender":name, "task":task, "base":base, "contract_sha256":contract, "route_sha256":route,
                        "scored":n, "accepted":c, "excluded":len(rows)-n, "k":k,
                        "pass_at_k":1 - comb(n-c,k)/den if den else None, "pass_all_k":comb(c,k)/den if den else None})
    return results


def select_trials(rows, task=None, contender=None, repeat=None):
    selected = [r for r in rows if (task is None or r["task"] == task)
                and (contender is None or r["contender"] == contender)
                and (repeat is None or r["repeat"] == repeat)]
    if not selected:
        raise ValueError("No registered benchmark trials match the selection")
    return selected


def summarize(runs):
    summaries = []
    for name in sorted({row["contender"] for row in runs}):
        rows = [row for row in runs if row["contender"] == name]
        completed = [row for row in rows if row["phase"] in ("done", "blocked")]
        successes = sum(row["accepted"] for row in rows)
        times = [row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None]
        total_seconds = sum(times) if len(times) == len(rows) else None
        valid = [r for r in rows if scored(r)]
        by_task = {}
        for r in valid:
            by_task.setdefault(r["task"], []).append(r["accepted"])
        scored_passes = sum(r["accepted"] for r in valid)
        summaries.append({"contender": name, "planned": len(rows), "finished": len(completed), "accepted": successes,
                          "scored_trials": len(valid), "scored_tasks": len(by_task),
                          "macro_task_acceptance": (sum(sum(v)/len(v) for v in by_task.values())/len(by_task) if by_task else None),
                          "scored_wilson_95": wilson_interval(scored_passes, len(valid)),
                          "outcomes": dict(Counter(row["outcome"] for row in rows)),
                          "blocked": sum(row["phase"] == "blocked" for row in rows),
                          "unfinished": len(rows) - len(completed),
                          "acceptance_rate": successes / len(rows), "unassisted": sum(row["unassisted_acceptance"] for row in rows),
                          "finished_acceptance_rate": successes / len(completed) if completed else None,
                          "calls_all_attempts": sum(row["provider_calls"] for row in rows),
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
        sides = [{(row["task"], row["repeat"], row["baseline"]): row for row in runs if row["contender"] == name}
                 for name in (left, right)]
        counts = dict(both_accepted=0, left_only_accepted=0, right_only_accepted=0, neither_accepted=0,
                      unfinished_pairs=0, inconclusive_pairs=0)
        time_ratios = []
        for key in sides[0].keys() | sides[1].keys():
            a, b = sides[0].get(key), sides[1].get(key)
            if not a or not b or any(row["phase"] not in ("done", "blocked") for row in (a, b)):
                counts["unfinished_pairs"] += 1
            elif (not a.get("contract_sha256") or a["contract_sha256"] != b.get("contract_sha256")
                  or any(row.get("outcome") in INCONCLUSIVE for row in (a, b))):
                counts["inconclusive_pairs"] += 1
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
        pairs.append({"left": left, "right": right, **counts, "scored_pairs": scored,
                      "discordant_pairs": discordant, "exact_two_sided_p": p_value,
                      "acceptance_gap_left_minus_right_pp": round(100 * difference / scored, 2) if scored else None,
                      "timed_success_pairs": len(time_ratios),
                      "median_right_seconds_over_left": round(median(time_ratios), 3) if time_ratios else None})
    return pairs


def suite_report(store, suite_id, output=None):
    suite = read_suite(store, suite_id)
    runs = [{**item, **metrics(store, item["run_id"])} for item in suite["runs"]]
    summaries = summarize(runs)
    result = {"benchmark_id": suite_id, "name": suite["name"], "summaries": summaries, "runs": runs,
              "per_task": [{"task": task, "summaries": summarize([row for row in runs if row["task"] == task])}
                           for task in sorted({row["task"] for row in runs})],
              "paired_outcomes": paired_outcomes(runs), "runtime": suite.get("runtime", {}),
              "interpretation": "Exploratory paired task runs; no automatic winner or statistical significance claim. "
              "Unfinished runs are not failures. acceptance_rate uses all planned runs; finished_acceptance_rate uses only done/blocked runs. "
              "These are operational acceptance rates, including account/environment blockers; matched pairs with those blockers are inconclusive. "
              "Macro task acceptance weights each scored task equally. The descriptive Wilson interval assumes independent trials. "
              "The exploratory exact sign test assumes independent discordant pairs. Repeated tasks may be correlated. "
              "Calls count CLI invocations, not equal model compute. Inspect modes, model versions and usage coverage."}
    result = redact(result)
    if output:
        body = f'<header><h1>{escape(result["name"])}</h1><p>{escape(result["interpretation"])}</p></header><h2>Contenders</h2><div class="table"><table><thead><tr><th>Setup</th><th>Finished / planned</th><th>Accepted / finished</th><th>Outcomes</th><th>No intervention</th><th>Total calls</th><th>Seconds / acceptance</th></tr></thead><tbody>'
        for row in result["summaries"]:
            body += '<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in [row["contender"], f'{row["finished"]}/{row["planned"]}', f'{row["accepted"]}/{row["finished"]}', ", ".join(f"{key.replace('_', ' ')}: {count}" for key, count in row["outcomes"].items()), row["unassisted"], row["calls_all_attempts"], row["seconds_per_acceptance"] if row["seconds_per_acceptance"] is not None else "Unavailable"]) + '</tr>'
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
            body += f'<details><summary>{escape(item["task"])}</summary><pre>{escape(json.dumps(item["summaries"], indent=2))}</pre></details>'
        body += '<h2>Every task and attempt</h2>'
        for row in result["runs"]:
            body += f'<details><summary>{escape(row["task"])} · {escape(row["contender"])} · repeat {row["repeat"]} · {escape(row["phase"])}</summary><pre>{escape(json.dumps(row, indent=2))}</pre></details>'
        atomic_write(output, document(result["name"], body))
        result["report"] = str(Path(output).resolve())
    return result
