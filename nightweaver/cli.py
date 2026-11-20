"""Command line interface shared by automation and the Windows desktop."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .engine import Run, checkout, create_run, directory_identity, fork_run, pending_dependencies, source_info
from .process import BusyRun, run_lock, runner_active
from .providers import ProviderUnavailable, configure_deepseek_key, invoke, json_response
from .readiness import diagnose
from .spec import IMPLEMENTERS, anchor_provider_paths, fields, provider, read_json, read_plan, resolve_repository, scoped_milestones, text, validate
from .store import Store, default_home, timestamp
from .safety import redact


def output(data):
    print(json.dumps(data, indent=2, ensure_ascii=True))


def launch_exit(result):
    phase = result.get("phase")
    if phase is None or phase == "done":
        return 0
    from .recovery import EXIT_CODES
    return EXIT_CODES.get(phase, 4)


def with_live_status(store, row, phases=None):
    lock = store.directory(row["id"]) / "runner.lock"
    ref = row["state"].get("result_ref", f"refs/heads/rookline/{row['id']}")
    if phases is None:
        phases = store.dependency_phases(row["state"].get("after", []))
    waiting = [p for p in row["state"].get("after", []) if phases.get(p) != "done"]
    return redact({**row, "runner_active": runner_active(lock), "result_branch": ref.removeprefix("refs/heads/"),
            "guard_active": runner_active(lock.with_name("guard.lock")), "waiting_for": waiting,
            "missing_prerequisites": [item for item in waiting if item not in phases],
            "blocked_prerequisites": [item for item in waiting if phases.get(item) == "blocked"]})


def dispatch(store, watch, recover_guards=False):
    if watch:
        record_dispatch(store, "Dispatcher started")
    with run_lock(store.root / "dispatcher.lock", "Another dispatcher is already serving this runtime"):
        while True:
            results = []
            if recover_guards:
                from .recovery import recovery_status
                for row in store.guarded_runs():
                    status = recovery_status(store, row["id"])
                    if status["disposition"] == "eligible":
                        pid = background(store, row["id"], False, status["restart_limit"])
                        results.append({"run_id": row["id"], "supervisor_pid": pid})
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


def background(store, run_id, retry, restarts=None):
    root = store.directory(run_id)
    command = [sys.executable, "-m", "nightweaver", "--home", str(store.root),
               "guard" if restarts is not None else "run", run_id]
    if restarts is not None:
        command.extend(["--max-restarts", str(restarts)])
    if retry:
        command.append("--retry")
    with (root / "runner.log").open("ab") as out:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 close_fds=True, creationflags=subprocess.DETACHED_PROCESS,
                                 cwd=Path(__file__).resolve().parent.parent)
    # A long-lived dispatcher must reap its detached child handles. A one-shot
    # client can exit immediately; Windows keeps the detached process alive.
    threading.Thread(target=child.wait, name=f"reap-{child.pid}", daemon=True).start()
    return child.pid


def plan_goal(store, args, template=None):
    if template is None:
        path = Path(args.template).resolve(strict=True)
        raw, folder = read_json(path), path.parent
    else:
        raw, folder = template, Path(template["repository"])
    anchor_provider_paths(raw, folder)
    repository, project = resolve_repository(raw, folder, store)
    raw["repository"] = str(repository)
    if project:
        raw["project"] = project["name"]
    raw["objective"] = args.objective
    gates = raw.get("gates", [])
    final_gates = raw.get("final_gates", [])
    fixed = scoped_milestones(raw)
    if not gates and not final_gates and not fixed:
        raise ValueError("A OneShot template needs operator-authored gates")
    template_steps = raw.get("milestones") or [{"title": "Plan", "task": "Prepare checked milestones"}]
    prepared = validate({**raw, "milestones": template_steps}, folder, store)
    if fixed and len(template_steps) > 5:
        raise ValueError("OneShot planning supports up to five fixed milestones")
    for prerequisite in getattr(args, "after", []):
        store.directory(prerequisite)
        store.get(prerequisite)
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
    prompt += "\nRun limits: " + json.dumps({"budgets":prepared["budgets"], "attempts_per_milestone":prepared["attempts"],
                                            "agent_timeout_seconds":prepared["agent_timeout"]})
    if fixed:
        boundaries = [{k:s[k] for k in ("title","task","paths","gates")} for s in prepared["milestones"]]
        prompt += ("\nUse exactly one entry per fixed milestone in its original order. Return only title and task. "
                   "The controller attaches the following operator-owned paths and checks.\n" + json.dumps(boundaries, ensure_ascii=False))
    pin_source = args.start or args.enqueue or project or "expected_base" in raw or prepared["context_files"] or prepared["repository_map"]
    if pin_source:
        source = source_info(store, raw)
        identity = directory_identity(source["git_common_dir"])
        raw["expected_base"] = source["base"]
        if prepared["context_files"] or prepared["repository_map"]:
            from . import context
            prompt += "\n" + context.format_brief(context.capture(prepared, source["base"]))
    if getattr(args, "prompt_only", False):
        return {"prompt": prompt, "baseline": raw.get("expected_base"), "fixed_milestones": fixed, "model_calls": 0}
    if args.start:
        from .diagnostics import probe_browser_channels
        checks = [g for step in prepared["milestones"] for g in step["gates"]]
        errors = probe_browser_channels(g["browser"].get("channel", "bundled") for g in checks if g.get("kind") == "browser")
        if errors:
            raise ValueError("OneShot browser check failed before planning:\n" + "\n".join(errors))
    if getattr(args, "plan_from", None):
        if args.plan_from.stat().st_size > 1000000:
            raise ValueError("Saved planner response exceeds 1MB")
        response = read_json(args.plan_from)
    else:
        config = planner_route(args)
        log = store.root / "planning" / f"{uuid4().hex[:12]}.jsonl"
        try:
            code, _, answer = invoke(config, "planner", repository, log, prompt, 240, lambda: False)
        except ProviderUnavailable as error:
            if error.kind != "authentication":
                raise
            profile = config.get("profile_dir", "the default profile")
            raise ValueError(
                f"{config['kind']} planner authentication failed for {profile} before run creation. "
                "Rerun the goal with --profile-dir pointing to a signed-in CLI profile, "
                "or sign in to the selected profile and rerun the goal."
            ) from error
        if code:
            raise ValueError(f"Planner exited {code}. See {log}")
        response = json_response(answer)
    fields(response, "milestones", "planner response")
    milestones = response.get("milestones", [])
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 5:
        raise ValueError("Planner did not return one to five milestones")
    if fixed and len(milestones) != len(template_steps):
        raise ValueError("Planner response does not cover the fixed milestone count")
    for index, item in enumerate(milestones):
        fields(
            item,
            "title task paths gates" if fixed else "title task",
            "planned milestone",
        )
        if fixed:
            for key in ("paths", "gates"):
                if key in item and json.dumps(item[key], sort_keys=True) != json.dumps(
                    boundaries[index][key], sort_keys=True
                ):
                    raise ValueError(
                        f"Planner changed fixed milestone {index + 1} {key}"
                    )
        text(item, "title")
        text(item, "task")
    if fixed:
        raw["milestones"] = [{**original, "task": original["task"] + "\n\nPlan detail\n" + proposed["task"]}
                             for original,proposed in zip(template_steps, milestones)]
    elif not gates and len(milestones) > 1:
        raw["milestones"] = [{"title": raw["title"], "task": "\n\n".join(
            f"{s['title']}\n{s['task']}" for s in milestones)}]
    else:
        raw["milestones"] = milestones
    spec = validate(raw, store=store)
    if pin_source:
        current = source_info(store, spec)
        if current["git_common_dir"] != source["git_common_dir"] or directory_identity(current["git_common_dir"]) != identity:
            raise ValueError("The Git repository was replaced during planning")
        spec.update(git_directory_identity=identity, git_common_dir=source["git_common_dir"])
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
    if getattr(args, "effort", None):
        config["effort"] = args.effort
        return provider(config, IMPLEMENTERS, "planner")
    return config


def register(store, spec, args):
    run_id = create_run(store, spec, enqueue=args.enqueue, after=args.after)
    result = {"run_id": run_id}
    if args.start:
        limit = getattr(args, "guard_restarts", None)
        if limit is not None:
            from .recovery import supervise
            store.transition(run_id, "guard_enrolled", {"limit": limit}, guard_limit=limit)
            if args.wait:
                result["phase"] = supervise(store, run_id, limit)
            else:
                result["supervisor_pid"] = background(store, run_id, False, limit)
        elif getattr(args, "wait", False):
            result["phase"] = Run(store, run_id).run()
        else:
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
    one = commands.add_parser("oneshot", help="Build and verify a goal from a repository's committed tests")
    one.add_argument("repository", type=Path)
    one.add_argument("objective")
    one.add_argument("--worker", choices=("codex", "claude", "gemini", "deepseek"), default="codex")
    one.add_argument("--model", help="Model for the primary worker and planner")
    one.add_argument("--effort", help="Reasoning effort for the primary worker and planner")
    one.add_argument("--python", type=Path, help="Python executable with the target repository's test dependencies")
    one.add_argument("--profile-dir", help="Signed-in CLI profile for the primary worker and planner")
    one.add_argument("--expected-base", help="Require this exact previewed Git commit before launching")
    one.add_argument("--rescue", choices=("codex", "claude", "gemini", "deepseek"), help="Alternate worker after a rejected attempt")
    one.add_argument("--rescue-profile", help="Signed-in CLI profile for the alternate")
    one.add_argument("--direct", action="store_true", help="Use the objective as one milestone and skip the planner call")
    launch_one = one.add_mutually_exclusive_group()
    launch_one.add_argument("--preview", action="store_true", help="Print the reusable plan and its checks without calling a model")
    launch_one.add_argument("--wait", action="store_true", help="Return the final run outcome")
    launch_one.add_argument("--enqueue", action="store_true", help="Use the persistent queue for the worker run")
    commands.add_parser("repair-history", help="Trace failed candidates and functional worker handoffs").add_argument("id")
    guard = commands.add_parser("guard", help="Run with bounded recovery after a Windows runner exits unexpectedly")
    guard.add_argument("id")
    guard.add_argument("--max-restarts", type=int, choices=range(11), metavar="0..10")
    guard.add_argument("--retry", action="store_true", help="Explicitly retry an initially blocked run once")
    for command, help_text in (("recovery", "Explain the saved checkpoint and live recovery disposition"),
                               ("restarts", "Read the supervisor process and checkpoint history")):
        commands.add_parser(command, help=help_text).add_argument("id")
    commands.add_parser("recovery-queue", help="List interrupted and exhausted dedicated runs")
    single = commands.add_parser("event", help="Inspect one exact evidence event within a run")
    single.add_argument("id")
    single.add_argument("seq", type=int)
    for parser in (one, add, goal):
        parser.add_argument("--guard-restarts", type=int, choices=range(11), metavar="0..10",
                            help="Launch under a dedicated crash supervisor with this lifetime restart allowance")
    mode = goal.add_mutually_exclusive_group()
    mode.add_argument("--prompt-only", action="store_true", help="Print the actual planner input without calling a model")
    mode.add_argument("--plan-from", type=Path, help="Use a saved planner response with the same validation")
    doctor = commands.add_parser("doctor", help="Check repository and provider readiness")
    doctor.add_argument("--plan", help="Check the exact provider routes in a plan")
    doctor.add_argument("--live", action="store_true", help="Make a small model request per distinct provider route")
    for parser in (goal, doctor):
        parser.add_argument("--planner", choices=("claude", "codex", "deepseek", "gemini"), default="claude" if parser is goal else None)
        parser.add_argument("--profile-dir")
        parser.add_argument("--model", help="Model used by the planner")
        parser.add_argument("--effort", help="Reasoning effort used by the planner")
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
        parser.add_argument("--wait", action="store_true", help="With --start, run through verification and return the outcome")
        launch.add_argument("--enqueue", action="store_true", help="Let the persistent dispatcher start this run")
        parser.add_argument("--after", action="append", default=[], metavar="RUN_ID",
                            help="Wait for this prerequisite run to complete. Requires --enqueue.")
    dispatcher = commands.add_parser("dispatch", help="Start eligible queued runs in order")
    dispatcher.add_argument("--recover-guards", action="store_true",
                            help="Also relaunch eligible dedicated runs enrolled in crash supervision")
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
        if name in ("start", "resume"):
            parser.add_argument("--wait", action="store_true")
        if name == "events":
            parser.add_argument("--until", type=int, help="Include events up to this inclusive sequence number")
            parser.add_argument("--before", type=int, help="Read the previous page before this exclusive sequence")
            parser.add_argument("--after", type=int)
            parser.add_argument("--limit", type=int, default=80)
            parser.add_argument("--all", action="store_true")
            parser.add_argument("--follow", action="store_true", help="Stream new events as JSON lines until the run stops")
            parser.add_argument("--kind", action="append", default=[], help="Only return this event kind (repeatable)")
        if name == "run":
            parser.add_argument("--guard-child", help=argparse.SUPPRESS)
    wait = commands.add_parser("wait", help="Observe a run until done, blocked, paused or timed out")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, help="Wall-clock seconds to wait")
    pd = commands.add_parser("plan-diff", help="Compare normalized plan contracts without calling a model")
    pd.add_argument("before")
    pd.add_argument("after")
    scope = commands.add_parser("scope", help="Explain which milestones may change the named paths")
    scope.add_argument("plan")
    scope.add_argument("paths", nargs="+")
    lint = commands.add_parser("plan-lint", help="Find impossible call budgets, scopes and repeated routes")
    lint.add_argument("plan")
    lint.add_argument("--strict", action="store_true", help="Fail on warnings as well as errors")
    catalog = commands.add_parser("catalog-check", help="Check a committed catalog against the plan baseline")
    catalog.add_argument("plan")
    catalog.add_argument("--ref", default="HEAD")
    catalog.add_argument("--before", help="Override the baseline commit for this comparison")
    catalog.add_argument("--milestone", type=int, default=1)
    catalog.add_argument("--gate")
    profile = commands.add_parser("catalog-profile", help="Measure field coverage and diversity on a Git revision")
    profile.add_argument("plan")
    profile.add_argument("--ref", default="HEAD")
    profile.add_argument("--milestone", type=int, default=1)
    profile.add_argument("--gate")
    profile.add_argument("--field", action="append", required=True, help="JSON Pointer to profile (repeatable)")
    commands.add_parser("catalog-history", help="Trace catalog losses and repairs across attempts").add_argument("id")
    commands.add_parser("check-schedule", help="Preview effective checks, retention and nominal timeouts").add_argument("plan")
    commands.add_parser("test-history", help="Inspect measured JUnit results across attempts").add_argument("id")
    for action in ("check-baseline", "compare-revisions"):
        probe = commands.add_parser(
            action, help="Verify named regression cases in isolated Git revisions"
        )
        probe.add_argument("plan")
        probe.add_argument("--milestone", type=int, default=1)
        probe.add_argument("--gate", required=True)
        probe.add_argument(
            "--before",
            help="Baseline Git revision, defaulting to the plan baseline or HEAD",
        )
        if action == "compare-revisions":
            probe.add_argument("--after", required=True, help="Candidate Git revision")
    cases = commands.add_parser(
        "case-history", help="Read one case from hash-verified archived JUnit reports"
    )
    cases.add_argument("id")
    cases.add_argument("--classname", required=True)
    cases.add_argument("--name", required=True)
    cases.add_argument(
        "--suite", action="append", help="Repeat for each component of the suite path"
    )
    for name, description in (
        ("dependencies", "Show upstream and downstream prerequisite edges"),
        ("timeline", "Show milestone attempts, timing and acceptance"),
        ("call-ledger", "Inspect individual provider calls, usage and interruptions"),
        (
            "browser-matrix",
            "Inspect the latest outcome and evidence per scenario and viewport",
        ),
        ("browser-hotspots", "Locate failing or slow actions per browser viewport"),
        ("browser-coverage", "Measure steps completed per gate and viewport"),
        (
            "evidence-manifest",
            "Export portable evidence hashes and verification results",
        ),
        ("ci-summary", "Write a Markdown run summary for CI"),
    ):
        p = commands.add_parser(name, help=description)
        p.add_argument("id")
        if name in ("ci-summary","evidence-manifest"):
            p.add_argument("--output", type=Path, required=True)
    pre = commands.add_parser("preflight", help="Validate committed context, routes, identity and browser runtime")
    pre.add_argument("plan")
    for action in ("replay-browser", "replay-checks"):
        replay = commands.add_parser(action, help="Recheck an accepted milestone in a private checkout")
        replay.add_argument("id")
        replay.add_argument("--milestone", type=int)
        replay.add_argument("--gate")
        if action == "replay-checks":
            replay.add_argument("--through", action="store_true", help="Run distinct checks from all milestones up to this one on its accepted commit")
            replay.add_argument("--repeat", type=int, help="Override the gate's configured sample count")
    for action, help_text in (("diagnose-run", "Locate recorded failures and their evidence"),
                              ("gate-stats", "Measure repeated checks by contract"),
                              ("budget", "Show remaining time, calls and observed-token allowance"),
                              ("trace", "Export a metadata-only JSONL trajectory")):
        inspect = commands.add_parser(action, help=help_text)
        inspect.add_argument("id")
        if action == "trace":
            inspect.add_argument("--output", type=Path, required=True)
            inspect.add_argument("--after", type=int, default=0)
            inspect.add_argument("--until", type=int)
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
        if command == "handoff":
            parser.add_argument("--markdown", type=Path, metavar="FILE", help="Save a portable session brief")
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
    for action in ("run", "report", "retry-blocked", "shard"):
        bench_parser = bench_sub.add_parser(action)
        bench_parser.add_argument("id")
        bench_parser.add_argument("--tag", help="Select tasks with this exact suite tag")
        if action == "report":
            bench_parser.add_argument("--output", type=Path)
        else:
            bench_parser.add_argument("--task")
            bench_parser.add_argument("--contender")
            bench_parser.add_argument("--repeat", type=int)
            bench_parser.add_argument("--shard", required=action == "shard", metavar="INDEX/TOTAL")
    for action, help_text in (("sensitivity", "Measure how omitting one task changes a paired acceptance gap"),
                              ("frontier", "Compare accepted tasks against observed provider calls"),
                              ("failures", "Group unsuccessful trials by outcome and rejection stage"),
                              ("calibrate", "Test the grader against matched positive and negative controls")):
        analysis = bench_sub.add_parser(action, help=help_text)
        analysis.add_argument("id")
        if action == "calibrate":
            analysis.add_argument("--positive", required=True)
            analysis.add_argument("--negative", required=True)
    reliability_cmd = bench_sub.add_parser("reliability", help="Estimate any-pass and all-pass rates over k trials")
    reliability_cmd.add_argument("id")
    reliability_cmd.add_argument("--k", type=int, default=2)
    csv_cmd = bench_sub.add_parser("csv", help="Export one spreadsheet row per trial")
    csv_cmd.add_argument("id")
    csv_cmd.add_argument("--output", type=Path, required=True)
    for action, description in (
        ("interval", "Bootstrap a task-weighted difference on fully comparable tasks"),
        ("instability", "Find repeated tasks with both passes and failures"),
        ("efficiency", "Count all attempts per accepted trial and report token coverage"),
        ("latency", "Measure elapsed, queue and first-acceptance latency distributions"),
        ("footprint", "Measure accepted Git changes, including text and binary files"),
        ("audit", "Verify registered checkout, baseline and saved context for every trial"),
        ("qualify", "Check a contender against a minimum task-weighted acceptance rate"),
        ("jsonl", "Export nested evidence as one JSON object per trial"),
    ):
        analysis = bench_sub.add_parser(action, help=description)
        analysis.add_argument("id")
        if action == "interval":
            analysis.add_argument("--samples", type=int, default=2000)
            analysis.add_argument("--seed", type=int, default=42)
        elif action == "qualify":
            analysis.add_argument("--contender", required=True)
            analysis.add_argument("--minimum-percent", type=float, required=True)
            analysis.add_argument("--minimum-tasks", type=int, default=1)
        elif action == "jsonl":
            analysis.add_argument("--output", required=True, type=Path)
    compare = bench_sub.add_parser("compare", help="Compare matching trials from two saved suites")
    compare.add_argument("before")
    compare.add_argument("after")
    verdict = bench_sub.add_parser("verdict", help="Check two suites for matched regressions with CI exit status")
    verdict.add_argument("before")
    verdict.add_argument("after")
    verdict.add_argument("--minimum-pairs", type=int, default=1)
    verdict.add_argument("--minimum-tasks", type=int, default=1)
    verdict.add_argument("--allow-regressions", type=int, default=0)
    junit = bench_sub.add_parser("junit", help="Export suite results for CI test dashboards")
    junit.add_argument("id")
    junit.add_argument("--output", type=Path, required=True)
    for action in ("list","fleet"):
        p = commands.add_parser(action)
        if action == "list":
            p.add_argument("--compact", action="store_true", help="Return sidebar summaries without prompts and gate definitions")
        p.add_argument("--project")
        p.add_argument("--phase", choices=("queued","ready","working","checking","reviewing","promoting","done","blocked"))
    args = app.parse_args(argv)
    if args.command == "goal" and args.prompt_only and (args.start or args.enqueue):
        app.error("--prompt-only previews a goal and cannot launch it")
    if args.command in ("add", "goal") and args.after and not args.enqueue:
        app.error("--after requires --enqueue")
    if args.command in ("add", "goal") and args.wait and not args.start:
        app.error("--wait requires --start")
    if getattr(args, "guard_restarts", None) is not None and (
            getattr(args, "preview", False) or getattr(args, "enqueue", False)
            or (args.command in ("goal", "add") and not args.start)):
        app.error("--guard-restarts requires a dedicated launch")
    if args.command == "events":
        if args.before is not None and (args.after is not None or args.until is not None or args.before < 1):
            app.error("--before requires a positive exclusive cursor and cannot combine with --after or --until")
        if args.follow and (args.until is not None or args.before is not None or args.all):
            app.error("--follow cannot combine with a bounded end cursor or --all")
    if args.command == "doctor":
        if args.live and not (args.plan or args.planner):
            app.error("--live requires --plan or --planner")
        if (args.profile_dir or args.model or args.effort) and not args.planner:
            app.error("--profile-dir, --model and --effort require --planner")
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
        if name in ("guard", "recovery", "restarts", "recovery-queue"):
            from .recovery import EXIT_CODES, GUARD_EVENTS, recovery_status, supervise
            if name == "guard":
                phase = supervise(store, args.id, args.max_restarts, retry=args.retry)
                output({"run_id": args.id, "phase": phase})
                return EXIT_CODES.get(phase, 4)
            if name == "recovery":
                output(recovery_status(store, args.id))
            elif name == "restarts":
                output(store.events(args.id, limit=None, kinds=GUARD_EVENTS))
            else:
                items = (recovery_status(store, r["id"]) for r in store.runs()
                         if (r["state"].get("started_at") or "guard_limit" in r["state"]) and r["state"]["phase"] not in ("done", "blocked"))
                output([r for r in items if r["disposition"] in ("eligible", "limit_reached")])
            return 0
        if name == "event":
            if args.seq < 1:
                raise ValueError("Event sequence must be positive")
            page = store.events(args.id, after=args.seq-1, until=args.seq, limit=1)
            if not page:
                raise ValueError("This run has no event with that sequence")
            output(page[0])
            return 0
        if name == "repair-history":
            from .repair import repair_history
            output(repair_history(store, args.id))
            return 0
        if name == "oneshot":
            from .discovery import discover, verify_tools
            if args.rescue_profile and not args.rescue:
                raise ValueError("--rescue-profile requires --rescue")
            args.planner = args.worker
            worker = planner_route(args)
            alternate = {"kind": args.rescue} if args.rescue else None
            if args.rescue_profile:
                alternate["profile_dir"] = str(Path(args.rescue_profile).expanduser().resolve())
            if worker == alternate:
                raise ValueError("Choose a distinct rescue provider or profile")
            raw = discover(args.repository, args.objective, worker, alternate, args.python)
            if args.expected_base and raw["expected_base"] != args.expected_base.lower():
                raise ValueError("The repository moved since preview. Preview its checks again.")
            spec = validate(raw, store=store)
            if args.preview:
                output(raw)
                return 0
            verify_tools(raw["gates"])
            args.start, args.after = not args.enqueue, []
            result = register(store, spec, args) if args.direct else plan_goal(store, args, raw)
            output(result)
            return launch_exit(result)
        if name in ("preflight", "replay-browser", "replay-checks"):
            from .diagnostics import preflight, replay_checks
            report = (preflight(store, read_plan(args.plan, store)) if name == "preflight"
                      else replay_checks(store, args.id, args.milestone, args.gate, getattr(args, "repeat", None),
                                         browser_only=name == "replay-browser", through=getattr(args, "through", False)))
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
        elif name in ("list","fleet"):
            from .inspection import matching_runs, fleet
            rows = matching_runs(store, args.project, args.phase, compact=getattr(args, "compact", False))
            if name == "fleet":
                output(fleet(store, rows))
            else:
                phases = store.dependency_phases({p for r in rows for p in r["state"].get("after", [])})
                output([with_live_status(store, row, phases) for row in rows])
        elif name == "configure":
            output(configure_deepseek_key(args.key_file))
        elif name in ("add","goal"):
            result = register(store, read_plan(args.plan, store), args) if name == "add" else plan_goal(store, args)
            output(result)
            return launch_exit(result)
        elif name == "wait":
            from .inspection import wait_for_run
            result, code = wait_for_run(store, args.id, args.timeout)
            output(result)
            return code
        elif name == "plan-diff":
            from .inspection import plan_diff
            output(plan_diff(read_plan(args.before,store), read_plan(args.after,store)))
        elif name == "check-schedule":
            from .contracts import check_schedule
            output(check_schedule(read_plan(args.plan, store)))
        elif name == "test-history":
            from .junit import history
            output(history(store, args.id))
        elif name in ("check-baseline", "compare-revisions"):
            from .proof import compare

            result = compare(
                store,
                read_plan(args.plan, store),
                args.milestone,
                args.gate,
                args.before,
                getattr(args, "after", None),
            )
            output(redact(result))
            return 0 if result["passed"] else 3
        elif name == "case-history":
            from .proof import case_history

            output(
                case_history(
                    store,
                    args.id,
                    {
                        "name": args.name,
                        "classname": args.classname,
                        **({"suite": args.suite} if args.suite is not None else {}),
                    },
                )
            )
        elif name in ("scope", "plan-lint", "catalog-check", "catalog-history", "catalog-profile"):
            from .contracts import catalog_history, inspect_catalog, lint_plan, scope_report
            if name == "catalog-history":
                output(catalog_history(store, args.id))
            else:
                contract = read_plan(args.plan, store)
                if name == "scope":
                    output(scope_report(contract,args.paths))
                elif name == "plan-lint":
                    report = lint_plan(contract)
                    output(report)
                    return 3 if not report["feasible"] or (args.strict and report["findings"]) else 0
                else:
                    if name == "catalog-profile":
                        output(inspect_catalog(contract, args.milestone, args.gate, args.ref, profile_fields=args.field))
                        return 0
                    report = inspect_catalog(contract,args.milestone,args.gate,args.ref,args.before)
                    output(report)
                    if any(item.get("input_error") for item in report["checks"]):
                        return 4
                    return 0 if report["passed"] else 3
        elif name in ("dependencies","timeline","browser-coverage","ci-summary","evidence-manifest"):
            from .inspection import browser_coverage, ci_summary, evidence_manifest, milestone_timeline
            from .operations import dependencies
            if name in ("ci-summary","evidence-manifest"):
                result = (ci_summary if name == "ci-summary" else evidence_manifest)(store,args.id,args.output)
                output(result)
                return 3 if name == "evidence-manifest" and result["status"] != "verified" else 0
            output({"dependencies":dependencies, "timeline":milestone_timeline,
                    "browser-coverage":browser_coverage}[name](store,args.id))
        elif name == "browser-matrix":
            from .inspection import browser_matrix
            output(browser_matrix(store,args.id))
        elif name in ("call-ledger", "browser-hotspots"):
            from .trajectory import call_ledger, browser_hotspots
            output((call_ledger if name == "call-ledger" else browser_hotspots)(store, args.id))
        elif name == "status":
            output(with_live_status(store, store.get(args.id)))
        elif name == "events":
            if args.follow:
                from .operations import follow_events
                for event in follow_events(store, args.id, args.after or 0, args.limit, args.kind):
                    print(json.dumps(event, ensure_ascii=True), flush=True)
            else:
                until = args.before - 1 if args.before is not None else args.until
                output(store.events(args.id, limit=None if args.all else args.limit, after=args.after, kinds=args.kind, until=until))
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
                output(export_trace(store, args.id, args.output, args.after, args.until))
        elif name == "handoff":
            from .operations import handoff, handoff_markdown
            with store.snapshot() as reader:
                data = handoff(reader, args.id)
            if args.markdown:
                from .safety import atomic_write
                atomic_write(args.markdown, handoff_markdown(data))
                output({"run_id": args.id, "handoff": str(args.markdown.resolve())})
            else:
                output(data)
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
                output(suite_report(store, args.id, args.output, tag=args.tag))
            elif args.benchmark_action == "audit":
                from .benchmark_analysis import audit_inputs
                result = audit_inputs(store, args.id)
                output(result)
                return {"pass": 0, "failed": 3, "inconclusive": 4}[result["verdict"]]
            elif args.benchmark_action in ("interval", "instability", "efficiency", "latency", "qualify", "footprint"):
                from .benchmark_analysis import task_intervals, instability, efficiency, latency, qualify, footprint
                rows = suite_report(store, args.id)["runs"]
                if args.benchmark_action == "qualify":
                    result = qualify(rows, args.contender, args.minimum_percent, args.minimum_tasks)
                    output(result)
                    return {"pass": 0, "below_floor": 3, "inconclusive": 4}[result["verdict"]]
                if args.benchmark_action == "interval":
                    output(task_intervals(rows, args.seed, args.samples))
                elif args.benchmark_action == "footprint":
                    output(footprint(store, rows))
                else:
                    output({"instability": instability, "efficiency": efficiency, "latency": latency}[args.benchmark_action](rows))
            elif args.benchmark_action == "jsonl":
                from .benchmark_exports import export_jsonl
                output(export_jsonl(store, args.id, args.output))
            elif args.benchmark_action == "reliability":
                from .benchmark import reliability
                output(reliability(suite_report(store, args.id)["runs"], args.k))
            elif args.benchmark_action in ("calibrate", "frontier", "sensitivity", "failures"):
                from .evaluation import calibration, call_frontier, task_sensitivity, failure_groups
                report = suite_report(store, args.id)
                if args.benchmark_action == "calibrate":
                    result = calibration(report["runs"], args.positive, args.negative)
                    output(result)
                    return {"pass": 0, "failed": 3, "inconclusive": 4}[result["verdict"]]
                if args.benchmark_action == "sensitivity":
                    output(task_sensitivity(report))
                else:
                    analyze = call_frontier if args.benchmark_action == "frontier" else failure_groups
                    output(analyze(report["runs"]))
            elif args.benchmark_action == "csv":
                from .benchmark_exports import export_csv
                output(export_csv(store, args.id, args.output))
            elif args.benchmark_action in ("compare","verdict"):
                from .benchmark_exports import compare_suites, regression_verdict
                comparison = compare_suites(store, args.before, args.after)
                if args.benchmark_action == "verdict":
                    result = regression_verdict(comparison,args.minimum_pairs,args.allow_regressions,args.minimum_tasks)
                    output(result)
                    return {"pass":0,"regression":3,"inconclusive":4}[result["verdict"]]
                output(comparison)
            elif args.benchmark_action == "junit":
                from .benchmark_exports import export_junit
                output(export_junit(store, args.id, args.output))
            else:
                results = []
                from .benchmark import filter_tag, select_trials
                from .evaluation import shard_trials
                rows = filter_tag(select_trials(read_suite(store, args.id)["runs"], args.task, args.contender, args.repeat), args.tag)
                rows = shard_trials(rows, args.shard)
                if args.benchmark_action == "shard":
                    output({"shard": args.shard, "trials": len(rows), "runs": rows})
                    return 0
                if not rows:
                    raise ValueError("Selected benchmark shard contains no trials to execute")
                for row in rows:
                    try:
                        retry = args.benchmark_action == "retry-blocked"
                        phase = Run(store, row["run_id"]).run(retry=retry, only_operational=retry)
                        results.append({"run_id": row["run_id"], "phase": phase})
                    except BusyRun as error:
                        results.append({"run_id": row["run_id"], "waiting": str(error)})
                output(results)
                return 0 if all(row.get("phase") in ("done","skipped") for row in results) else 3
        elif name == "dispatch":
            if args.tool_path:
                os.environ["PATH"] = args.tool_path + os.pathsep + os.environ.get("PATH", "")
            output(dispatch(store, args.watch, args.recover_guards))
        elif name == "pause":
            changed = store.pause(args.id, True)
            output({"run_id": args.id, "pause_requested": store.get(args.id)["pause"], "changed": changed})
        elif name == "run":
            try:
                phase = Run(store, args.id).run(retry=args.retry)
            except BusyRun as error:
                if not args.guard_child:
                    raise
                store.transition(args.id, "guard_child_contended", {"pid": os.getpid(),
                                 "launch_id": args.guard_child, "reason": str(error)})
                return 4
            output({"run_id": args.id, "phase": phase})
            return {"done": 0, "paused": 2, "blocked": 3, "waiting": 4}[phase]
        else:
            row = store.get(args.id)
            phase = row["state"]["phase"]
            if phase == "done" or (phase == "blocked" and not args.retry) or (name == "start" and row["pause"]):
                stopped = "paused" if phase not in ("done","blocked") else phase
                output({"run_id": args.id, "phase": stopped, "launched": False})
                return {"done":0,"paused":2,"blocked":3}[stopped]
            waiting = pending_dependencies(store, row["state"])
            if waiting and name != "resume":
                raise ValueError("Waiting for prerequisite runs: " + ", ".join(waiting))
            if name == "resume":
                store.pause(args.id, False)
            if waiting:
                output({"run_id": args.id, "waiting_for": waiting})
                return 4 if args.wait else 0
            elif any(runner_active(store.directory(args.id) / lock) for lock in ("runner.lock", "guard.lock")):
                if args.wait:
                    from .inspection import wait_for_run
                    result, code = wait_for_run(store, args.id)
                    output(result)
                    return code
                output({"run_id": args.id, "runner_active": True, "launched": False})
            elif "guard_limit" in row["state"]:
                if args.wait:
                    from .recovery import supervise
                    result = {"run_id": args.id, "phase": supervise(store, args.id, retry=args.retry)}
                    output(result)
                    return launch_exit(result)
                output({"run_id": args.id, "supervisor_pid": background(store, args.id, args.retry, row["state"]["guard_limit"])})
            elif args.wait:
                phase = Run(store,args.id).run(retry=args.retry)
                output({"run_id":args.id, "phase":phase})
                return {"done":0,"paused":2,"blocked":3,"waiting":4}[phase]
            else:
                output({"run_id": args.id, "runner_pid": background(store, args.id, args.retry)})
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as error:
        if args.command == "dispatch" and args.watch and "store" in locals():
            record_dispatch(store, str(error))
        print(f"Nightweaver: {redact(str(error))}", file=sys.stderr)
        return 1
