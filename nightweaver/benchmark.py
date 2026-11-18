"""Repeatable coding-workflow experiments with frozen baselines and all outcomes retained."""

from copy import deepcopy
from html import escape
import json
from pathlib import Path
import random
from uuid import uuid4
import platform

from . import __version__

from .engine import create_run, source_info
from .reports import document, metrics
from .safety import atomic_write, write_json
from .spec import array, fields, integer, provider, IMPLEMENTERS, read_plan


def create_suite(store, source):
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
        if contender.get("mode", "nightweaver") not in ("nightweaver", "external"):
            raise ValueError("Contender mode must be nightweaver or external")
        if contender.get("mode") == "external" and (contender["worker"]["kind"] != "command" or contender.get("reviewer") or contender.get("worker_fallbacks")):
            raise ValueError("External contenders require one command route and manage their own orchestration")
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
        spec["expected_base"] = source_data["base"]
        if not spec.get("budgets", {}).get("max_seconds"):
            raise ValueError("Benchmark task plans require a max_seconds budget")
        for contender in contenders:
            for repeat in range(repetitions):
                plan = deepcopy(spec)
                plan.pop("reviewer", None)
                plan["reviewer_fallbacks"] = []
                if contender.get("reviewer"):
                    plan["reviewer"] = contender["reviewer"]
                for step in plan["milestones"]:
                    step["worker"] = contender["worker"]
                    step["worker_fallbacks"] = contender.get("worker_fallbacks", [])
                if contender.get("mode") == "external":
                    checks = {}
                    for step in plan["milestones"]:
                        for gate in step["gates"]:
                            if gate["name"] in checks and gate != checks[gate["name"]]:
                                raise ValueError("External benchmark has conflicting gate names")
                            checks[gate["name"]] = gate
                    plan["milestones"] = [{"title": task["name"], "task": plan["objective"], "worker": contender["worker"],
                                            "worker_fallbacks": [], "paths": [], "gates": list(checks.values())}]
                    plan["attempts"] = 1
                    plan["retry"] = {**plan.get("retry", {}), "transient_attempts": 0}
                prepared.append({"task": task["name"], "contender": contender["name"], "repeat": repeat + 1,
                                 "mode": contender.get("mode", "nightweaver"), "plan": plan})
    random.Random(seed).shuffle(prepared)
    suite_id = uuid4().hex[:12]
    manifest = {"id": suite_id, "name": name, "seed": seed, "repetitions": repetitions, "runs": [],
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
    return json.loads((store.root / "benchmarks" / f"{suite_id}.json").read_text(encoding="utf-8"))


def suite_report(store, suite_id, output=None):
    suite = read_suite(store, suite_id)
    runs = [{**item, **metrics(store, item["run_id"])} for item in suite["runs"]]
    summaries = []
    for name in sorted({row["contender"] for row in runs}):
        rows = [row for row in runs if row["contender"] == name]
        completed = [row for row in rows if row["phase"] in ("done", "blocked")]
        successes = sum(row["accepted"] for row in rows)
        total_seconds = sum(row["elapsed_seconds"] or 0 for row in rows)
        summaries.append({"contender": name, "planned": len(rows), "finished": len(completed), "accepted": successes,
                          "acceptance_rate": successes / len(rows), "unassisted": sum(row["unassisted_acceptance"] for row in rows),
                          "calls_all_attempts": sum(row["provider_calls"] for row in rows),
                          "seconds_all_attempts": round(total_seconds, 3),
                          "seconds_per_acceptance": round(total_seconds / successes, 3) if successes else None})
    result = {"benchmark_id": suite_id, "name": suite["name"], "summaries": summaries, "runs": runs,
              "interpretation": "Exploratory paired task runs; no automatic winner or statistical significance claim. Inspect unfinished runs, modes, model versions and usage coverage."}
    if output:
        body = f'<header><h1>{escape(suite["name"])}</h1><p>{escape(result["interpretation"])}</p></header><h2>Contenders</h2><div class="table"><table><thead><tr><th>Setup</th><th>Finished</th><th>Accepted</th><th>No intervention</th><th>Total calls</th><th>Seconds / acceptance</th></tr></thead><tbody>'
        for row in summaries:
            body += '<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in [row["contender"], f'{row["finished"]}/{row["planned"]}', f'{row["accepted"]}/{row["planned"]}', row["unassisted"], row["calls_all_attempts"], row["seconds_per_acceptance"] or "Unavailable"]) + '</tr>'
        body += '</tbody></table></div><h2>Every task and attempt</h2>'
        for row in runs:
            body += f'<details><summary>{escape(row["task"])} · {escape(row["contender"])} · repeat {row["repeat"]} · {escape(row["phase"])}</summary><pre>{escape(json.dumps(row, indent=2))}</pre></details>'
        atomic_write(output, document(suite["name"], body))
        result["report"] = str(Path(output).resolve())
    return result
