"""Command line interface shared by automation and the Windows desktop."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from .engine import Run, create_run, pending_dependencies
from .process import BusyRun, run_lock, runner_active
from .providers import configure_deepseek_key, invoke, json_response
from .spec import read_plan, validate
from .store import Store, timestamp


def output(data):
    print(json.dumps(data, indent=2, ensure_ascii=True))


def with_live_status(store, row):
    lock = store.directory(row["id"]) / "runner.lock"
    ref = row["state"].get("result_ref", f"refs/heads/rookline/{row['id']}")
    return {**row, "runner_active": runner_active(lock), "result_branch": ref.removeprefix("refs/heads/"),
            "waiting_for": pending_dependencies(store, row["state"])}


def dispatch(store, watch):
    if watch:
        record_dispatch(store, "Dispatcher started")
    with run_lock(store.root / "dispatcher.lock", "Another dispatcher is already serving this runtime"):
        while True:
            results = []
            for row in store.dispatch_runs():
                try:
                    phase = Run(store, row["id"]).run()
                except BusyRun as error:
                    if not watch:
                        results.append({"run_id": row["id"], "waiting": str(error)})
                    continue
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
                                 cwd=Path.cwd())
    return child.pid


def plan_goal(store, args):
    path = Path(args.template).resolve(strict=True)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    repository = Path(raw["repository"]).expanduser()
    if not repository.is_absolute():
        repository = (path.parent / repository).resolve()
    raw["repository"] = str(repository)
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
    config = {"kind": args.planner}
    if args.profile_dir:
        config["profile_dir"] = str(Path(args.profile_dir).resolve(strict=True))
    if args.model:
        config["model"] = args.model
    log = store.root / "planning" / f"{uuid4().hex[:12]}.jsonl"
    code, _, answer = invoke(config, "planner", repository, log, prompt, 240, lambda: False)
    if code:
        raise ValueError(f"Planner exited {code}. See {log}")
    response = json_response(answer)
    milestones = response.get("milestones", [])
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 5:
        raise ValueError(f"Planner did not return one to five milestones. See {log}")
    raw["milestones"] = [{"title": item["title"], "task": item["task"]} for item in milestones]
    spec = validate(raw)
    if args.start or args.enqueue:
        result = register(store, spec, args)
        return {**result, "milestones": raw["milestones"]}
    return raw


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
    goal.add_argument("--planner", choices=("claude", "codex", "deepseek"), default="claude")
    goal.add_argument("--profile-dir")
    goal.add_argument("--model", help="Model used by the planner")
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
    commands.add_parser("list")
    commands.add_parser("doctor")
    args = app.parse_args(argv)
    if getattr(args, "after", None) and not args.enqueue:
        app.error("--after requires --enqueue")
    if os.name != "nt":
        print("Nightweaver's native process runner requires Windows.", file=sys.stderr)
        return 1
    try:
        store = Store(args.home)
        os.environ["NIGHTWEAVER_HOME"] = str(store.root)
        name = args.command
        if name == "doctor":
            output({"data_directory": str(store.root),
                    "executables": {key: shutil.which(key) for key in ("git", "claude", "codex", "ollama")}})
        elif name == "list":
            output([with_live_status(store, row) for row in store.runs()])
        elif name == "configure":
            output(configure_deepseek_key(args.key_file))
        elif name == "add":
            output(register(store, read_plan(args.plan), args))
        elif name == "goal":
            output(plan_goal(store, args))
        elif name == "status":
            output(with_live_status(store, store.get(args.id)))
        elif name == "events":
            output(store.events(args.id))
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
            return {"done": 0, "paused": 2, "blocked": 3}[phase]
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
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as error:
        if args.command == "dispatch" and args.watch and "store" in locals():
            record_dispatch(store, str(error))
        print(f"Nightweaver: {error}", file=sys.stderr)
        return 1
