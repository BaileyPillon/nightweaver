"""Command line interface shared by automation and the Windows desktop."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from .engine import Run, checkout, create_run, fork_run, pending_dependencies, source_info
from .process import BusyRun, run_lock, runner_active
from .providers import configure_deepseek_key, invoke, json_response
from .readiness import diagnose
from .spec import read_plan, resolve_repository, validate
from .store import Store, default_home, timestamp
from .safety import redact


def output(data):
    print(json.dumps(data, indent=2, ensure_ascii=True))


def with_live_status(store, row):
    lock = store.directory(row["id"]) / "runner.lock"
    ref = row["state"].get("result_ref", f"refs/heads/rookline/{row['id']}")
    waiting = pending_dependencies(store, row["state"])
    return redact({**row, "runner_active": runner_active(lock), "result_branch": ref.removeprefix("refs/heads/"),
            "waiting_for": waiting,
            "blocked_prerequisites": [item for item in waiting if store.get(item)["state"]["phase"] == "blocked"]})


def dispatch(store, watch):
    if watch:
        record_dispatch(store, "Dispatcher started")
    with run_lock(store.root / "dispatcher.lock", "Another dispatcher is already serving this runtime"):
        while True:
            results = []
            for row in store.dispatch_runs():
                try:
                    phase = Run(store, row["id"]).run(defer_retries=watch)
                except BusyRun as error:
                    if not watch:
                        results.append({"run_id": row["id"], "waiting": str(error)})
                    continue
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                    store.transition(row["id"], "dispatcher_run_failed", {"issue": str(error)},
                                     phase="blocked", last_error=redact(str(error)))
                    phase = "blocked"
                results.append({"run_id": row["id"], "phase": phase})
            if not watch:
                return results
            if results:
                record_dispatch(store, json.dumps(results))
            time.sleep(10)


def record_dispatch(store, message):
    with (store.root / "dispatcher.log").open("a", encoding="utf-8") as log:
        print(timestamp(), message, file=log)


def background(store, run_id, retry):
    root = store.directory(run_id)
    command = [sys.executable, "-m", "nightweaver", "--home", str(store.root), "run", run_id]
    if retry:
        command.append("--retry")
    with (root / "runner.log").open("ab") as out:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 close_fds=True, creationflags=subprocess.DETACHED_PROCESS,
                                 cwd=Path(__file__).resolve().parent.parent)
    return child.pid


def plan_goal(store, args):
    path = Path(args.template).resolve(strict=True)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    repository, project = resolve_repository(raw, path.parent, store)
    raw["repository"] = str(repository)
    if project:
        raw["project"] = project["name"]
    raw["objective"] = args.objective
    gates = raw.get("gates", [])
    final_gates = raw.get("final_gates", [])
    if not gates and not final_gates:
        raise ValueError("A OneShot template needs operator-authored gates")
    prompt = (
        "Break the following software goal into one to five ordered milestones. "
        "The runner executes them in isolated Git worktrees. Give each a short title and a detailed, "
        "self-contained task with a testable accepted result. This runs natively on Windows. "
        "Preserve user choices and do not invent repository facts or require files to be new unless explicitly requested. "
        "Every milestone must make a useful tracked change and pass the per-milestone checks. "
        "The final milestone must also pass the final checks. If there are no per-milestone checks, return one milestone. "
        "If there is no way to split the work while passing the per-milestone checks, return one milestone. "
        'Return only JSON {"milestones":[{"title":"...","task":"..."}]}. '
        "These checks were chosen by the operator, keep them as provided: "
        + json.dumps(gates) + "\nFinal checks: " + json.dumps(final_gates)
        + "\nAcceptance requirements: " + json.dumps(raw.get("acceptance", []))
        + "\nProtected paths: " + json.dumps(raw.get("protected_paths", []))
        + "\n\nGoal:\n" + args.objective
    )
    if project or "expected_base" in raw:
        source_info(store, raw)
    config = planner_route(args)
    log = store.root / "planning" / f"{uuid4().hex[:12]}.jsonl"
    code, _, answer = invoke(config, "planner", repository, log, prompt, 240, lambda: False)
    if code:
        raise ValueError(f"Planner exited {code}. See {log}")
    response = json_response(answer)
    milestones = response.get("milestones", [])
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 5:
        raise ValueError(f"Planner did not return one to five milestones. See {log}")
    raw["milestones"] = [{"title": item["title"], "task": item["task"]} for item in milestones]
    spec = validate(raw, store=store)
    if args.start or args.enqueue:
        result = register(store, spec, args)
        return {**result, "milestones": raw["milestones"]}
    return raw


def planner_route(args):
    config = {"kind": args.planner}
    if args.profile_dir:
        config["profile_dir"] = str(Path(args.profile_dir).expanduser().resolve())
    if args.model:
        config["model"] = args.model
    return config


def register(store, spec, args):
    run_id = create_run(store, spec, enqueue=args.enqueue, after=args.after)
    result = {"run_id": run_id}
    if args.start:
        result["runner_pid"] = background(store, run_id, False)
    return result


def main(argv=None):
    app = argparse.ArgumentParser(prog="nightweaver")
    app.add_argument("--home", type=Path, help="Override private runtime data directory")
    commands = app.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register an explicit plan")
    add.add_argument("plan")
    goal = commands.add_parser("goal", help="Turn one goal into an ordered plan using an authenticated CLI")
    goal.add_argument("template")
    goal.add_argument("objective")
    doctor = commands.add_parser("doctor", help="Check repository and provider readiness")
    doctor.add_argument("--plan", help="Check the exact provider routes in a plan")
    doctor.add_argument("--live", action="store_true", help="Make a small model request per distinct provider route")
    for parser in (goal, doctor):
        parser.add_argument("--planner", choices=("claude", "codex", "deepseek", "gemini"), default="claude" if parser is goal else None)
        parser.add_argument("--profile-dir")
        parser.add_argument("--model", help="Model used by the planner")
    project = commands.add_parser("project", help="Manage canonical project checkouts")
    project_commands = project.add_subparsers(dest="project_action", required=True)
    project_commands.add_parser("list")
    bind = project_commands.add_parser("bind")
    bind.add_argument("project_name")
    bind.add_argument("repository", type=Path)
    bind.add_argument("--replace", action="store_true", help="Change an existing project's binding for future runs")
    for parser in (add, goal):
        launch = parser.add_mutually_exclusive_group()
        launch.add_argument("--start", action="store_true", help="Start a dedicated runner now")
        launch.add_argument("--enqueue", action="store_true", help="Let the persistent dispatcher start this run")
        parser.add_argument("--after", action="append", default=[], metavar="RUN_ID",
                            help="Wait for this prerequisite run to complete. Requires --enqueue.")
    dispatcher = commands.add_parser("dispatch", help="Start eligible queued runs in order")
    dispatcher.add_argument("--watch", action="store_true", help="Keep serving the queue")
    dispatcher.add_argument("--tool-path", help="Add executable directories to the dispatcher's PATH")
    configure = commands.add_parser("configure", help="Configure a provider using an existing local key file")
    configure.add_argument("provider", choices=("deepseek",))
    configure.add_argument("--key-file", type=Path, required=True)
    for name in ("run", "start", "pause", "resume", "status", "events"):
        parser = commands.add_parser(name)
        parser.add_argument("id")
        if name in ("run", "start", "resume"):
            parser.add_argument("--retry", action="store_true", help="Try a blocked run again")
        if name == "events":
            parser.add_argument("--after", type=int)
            parser.add_argument("--limit", type=int, default=80)
            parser.add_argument("--all", action="store_true")
            parser.add_argument("--follow", action="store_true", help="Stream new events as JSON lines until the run stops")
    pre = commands.add_parser("preflight", help="Validate committed context, routes, identity and browser runtime")
    pre.add_argument("plan")
    for action in ("replay-browser", "replay-checks"):
        replay = commands.add_parser(action, help="Recheck an accepted milestone in a private checkout")
        replay.add_argument("id")
        replay.add_argument("--milestone", type=int)
        replay.add_argument("--gate")
        if action == "replay-checks":
            replay.add_argument("--repeat", type=int, default=1)
    for action, help_text in (("diagnose-run", "Locate recorded failures and their evidence"),
                              ("gate-stats", "Measure repeated checks by contract"),
                              ("budget", "Show remaining time, calls and observed-token allowance"),
                              ("trace", "Export a metadata-only JSONL trajectory")):
        inspect = commands.add_parser(action, help=help_text)
        inspect.add_argument("id")
        if action == "trace":
            inspect.add_argument("--output", type=Path, required=True)
    backup = commands.add_parser("db-backup", help="Snapshot the runtime database to a new file")
    backup.add_argument("output", type=Path)
    integrity = commands.add_parser("db-check", help="Verify the live runtime database or a backup")
    integrity.add_argument("--file", type=Path)
    commands.add_parser("attention", help="List runs requiring an operator decision")
    for command, help_text in (("handoff", "Summarize durable context for a fresh session"),
                               ("changes", "Review accepted changes from immutable Git commits"),
                               ("audit-evidence", "Recheck the hashes of recorded gate artifacts")):
        parser = commands.add_parser(command, help=help_text)
        parser.add_argument("id")
        if command == "changes":
            parser.add_argument("--milestone", type=int)
            parser.add_argument("--patch", action="store_true", help="Include a redacted patch for inspection")
    metrics_parser = commands.add_parser("metrics", help="Measure a run using its complete history")
    metrics_parser.add_argument("id")
    report_parser = commands.add_parser("report", help="Export a standalone HTML evidence report")
    report_parser.add_argument("id")
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--include-images", action="store_true", help="Embed verified screenshots (may contain private project data)")
    feedback = commands.add_parser("feedback", help="Add durable instructions for subsequent worker/reviewer calls")
    feedback.add_argument("id")
    feedback.add_argument("message")
    fork = commands.add_parser("fork", help="Start a new run from an accepted checkpoint")
    fork.add_argument("id")
    fork.add_argument("--milestone", type=int, required=True)
    fork.add_argument("--worker", choices=("codex", "claude", "deepseek", "gemini"))
    issue = commands.add_parser("issue", help="Import one GitHub issue into a local plan")
    issue.add_argument("template")
    issue.add_argument("--repo", required=True)
    issue.add_argument("--number", type=int, required=True)
    issue.add_argument("--output", required=True, type=Path)
    benchmark = commands.add_parser("benchmark", help="Create, run or report reproducible comparisons")
    bench_sub = benchmark.add_subparsers(dest="benchmark_action", required=True)
    bench_create = bench_sub.add_parser("create")
    bench_create.add_argument("suite")
    bench_sub.add_parser("preview", help="Resolve the complete matrix and budgets without registering runs").add_argument("suite")
    for action in ("run", "report"):
        bench_parser = bench_sub.add_parser(action)
        bench_parser.add_argument("id")
        if action == "report":
            bench_parser.add_argument("--output", type=Path)
        else:
            bench_parser.add_argument("--task")
            bench_parser.add_argument("--contender")
            bench_parser.add_argument("--repeat", type=int)
    reliability_cmd = bench_sub.add_parser("reliability", help="Estimate any-pass and all-pass rates over k trials")
    reliability_cmd.add_argument("id")
    reliability_cmd.add_argument("--k", type=int, default=2)
    csv_cmd = bench_sub.add_parser("csv", help="Export one spreadsheet row per trial")
    csv_cmd.add_argument("id")
    csv_cmd.add_argument("--output", type=Path, required=True)
    compare = bench_sub.add_parser("compare", help="Compare matching trials from two saved suites")
    compare.add_argument("before")
    compare.add_argument("after")
    junit = bench_sub.add_parser("junit", help="Export suite results for CI test dashboards")
    junit.add_argument("id")
    junit.add_argument("--output", type=Path, required=True)
    commands.add_parser("list")
    args = app.parse_args(argv)
    if args.command in ("add", "goal") and args.after and not args.enqueue:
        app.error("--after requires --enqueue")
    if args.command == "doctor":
        if args.live and not (args.plan or args.planner):
            app.error("--live requires --plan or --planner")
        if (args.profile_dir or args.model) and not args.planner:
            app.error("--profile-dir and --model require --planner")
    if os.name != "nt":
        print("Nightweaver's native process runner requires Windows.", file=sys.stderr)
        return 1
    try:
        if args.command in ("db-backup", "db-check"):
            from .diagnostics import database_backup, database_check
            database = Path(args.home or default_home()) / "runs.sqlite3"
            report = (database_backup(database, args.output) if args.command == "db-backup"
                      else database_check(args.file or database))
            output(report)
            return 0 if report["ok"] else 3
        store = Store(args.home)
        os.environ["NIGHTWEAVER_HOME"] = str(store.root)
        name = args.command
        if name in ("preflight", "replay-browser", "replay-checks"):
            from .diagnostics import preflight, replay_checks
            report = (preflight(store, read_plan(args.plan, store)) if name == "preflight"
                      else replay_checks(store, args.id, args.milestone, args.gate, getattr(args, "repeat", 1),
                                         browser_only=name == "replay-browser"))
            output(report)
            return 0 if report.get("ready", report.get("passed")) else 3
        if name == "doctor":
            report = diagnose(store, read_plan(args.plan, store) if args.plan else None,
                              planner_route(args) if args.planner else None, args.live)
            output(report)
            return 3 if any(route["status"] in {"blocked", "unverified"} for route in report.get("providers", [])) else 0
        elif name == "project":
            if args.project_action == "list":
                output(store.projects())
            else:
                repository, gitdir = checkout(args.repository.expanduser().resolve(strict=True))
                output(store.bind_project(args.project_name, repository, gitdir, args.replace))
        elif name == "list":
            output([with_live_status(store, row) for row in store.runs()])
        elif name == "configure":
            output(configure_deepseek_key(args.key_file))
        elif name == "add":
            output(register(store, read_plan(args.plan, store), args))
        elif name == "goal":
            output(plan_goal(store, args))
        elif name == "status":
            output(with_live_status(store, store.get(args.id)))
        elif name == "events":
            if args.follow:
                from .operations import follow_events
                for event in follow_events(store, args.id, args.after or 0, args.limit):
                    print(json.dumps(event, ensure_ascii=True), flush=True)
            else:
                output(store.events(args.id, limit=None if args.all else args.limit, after=args.after))
        elif name == "attention":
            from .operations import attention
            output(attention(store))
        elif name in ("diagnose-run", "gate-stats", "budget", "trace"):
            from .insights import budget_report, diagnosis, export_trace, gate_stats
            if name == "diagnose-run":
                output(diagnosis(store, args.id))
            elif name == "gate-stats":
                output(gate_stats(store.events(args.id, limit=None)))
            elif name == "budget":
                output(budget_report(store, args.id))
            else:
                output(export_trace(store, args.id, args.output))
        elif name == "handoff":
            from .operations import handoff
            output(handoff(store, args.id))
        elif name == "changes":
            from .operations import changes
            output(changes(store, args.id, args.milestone, args.patch))
        elif name == "audit-evidence":
            from .operations import audit_evidence
            audit = audit_evidence(store, args.id)
            output(audit)
            return 0 if audit["status"] == "verified" else 3
        elif name == "metrics":
            from .reports import metrics
            output(metrics(store, args.id))
        elif name == "report":
            from .reports import run_report
            output(run_report(store, args.id, args.output or store.directory(args.id) / "report.html", args.include_images))
        elif name == "feedback":
            store.feedback(args.id, args.message)
            output({"run_id": args.id, "feedback_saved": True})
        elif name == "fork":
            output({"run_id": fork_run(store, args.id, args.milestone, {"kind": args.worker} if args.worker else None)})
        elif name == "issue":
            from .intake import issue_plan
            output(issue_plan(store, args.template, args.repo, args.number, args.output))
        elif name == "benchmark":
            from .benchmark import create_suite, read_suite, suite_report
            if args.benchmark_action in ("preview", "create"):
                output(create_suite(store, args.suite, preview=args.benchmark_action == "preview"))
            elif args.benchmark_action == "report":
                output(suite_report(store, args.id, args.output))
            elif args.benchmark_action == "reliability":
                from .benchmark import reliability
                output(reliability(suite_report(store, args.id)["runs"], args.k))
            elif args.benchmark_action == "csv":
                from .benchmark_exports import export_csv
                output(export_csv(store, args.id, args.output))
            elif args.benchmark_action == "compare":
                from .benchmark_exports import compare_suites
                output(compare_suites(store, args.before, args.after))
            elif args.benchmark_action == "junit":
                from .benchmark_exports import export_junit
                output(export_junit(store, args.id, args.output))
            else:
                results = []
                from .benchmark import select_trials
                for row in select_trials(read_suite(store, args.id)["runs"], args.task, args.contender, args.repeat):
                    try:
                        phase = Run(store, row["run_id"]).run()
                        results.append({"run_id": row["run_id"], "phase": phase})
                    except BusyRun as error:
                        results.append({"run_id": row["run_id"], "waiting": str(error)})
                output(results)
                return 0 if all(row.get("phase") == "done" for row in results) else 3
        elif name == "dispatch":
            if args.tool_path:
                os.environ["PATH"] = args.tool_path + os.pathsep + os.environ.get("PATH", "")
            output(dispatch(store, args.watch))
        elif name == "pause":
            store.pause(args.id, True)
            output({"run_id": args.id, "pause_requested": True})
        elif name == "run":
            phase = Run(store, args.id).run(retry=args.retry)
            output({"run_id": args.id, "phase": phase})
            return {"done": 0, "paused": 2, "blocked": 3, "waiting": 4}[phase]
        else:
            row = store.get(args.id)
            waiting = pending_dependencies(store, row["state"])
            if waiting and name != "resume":
                raise ValueError("Waiting for prerequisite runs: " + ", ".join(waiting))
            if name == "resume":
                store.pause(args.id, False)
            if waiting:
                output({"run_id": args.id, "waiting_for": waiting})
            else:
                output({"run_id": args.id, "runner_pid": background(store, args.id, args.retry)})
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as error:
        if args.command == "dispatch" and args.watch and "store" in locals():
            record_dispatch(store, str(error))
        print(f"Nightweaver: {redact(str(error))}", file=sys.stderr)
        return 1
