"""Command line interface shared by automation and the Windows desktop."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from .engine import Run, create_run
from .process import runner_active
from .providers import invoke, json_response
from .spec import read_plan, validate
from .store import Store


def output(data):
    print(json.dumps(data, indent=2, ensure_ascii=True))


def with_live_status(store, row):
    lock = store.directory(row["id"]) / "runner.lock"
    return {**row, "runner_active": runner_active(lock)}


def background(store, run_id, retry):
    root = store.directory(run_id)
    command = [sys.executable, "-m", "rookline", "--home", str(store.root), "run", run_id]
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
    gates = [check["name"] for check in raw.get("gates", [])]
    if not gates:
        raise ValueError("A OneShot template needs operator-authored gates")
    prompt = (
        "Break the following software goal into one to five ordered milestones. "
        "The runner executes them in isolated Git worktrees. Give each a short title and a detailed, "
        "self-contained task with a testable accepted result. Preserve user choices and state assumptions. "
        'Return only JSON {"milestones":[{"title":"...","task":"..."}]}. '
        "These checks were chosen by the operator, keep them as provided: "
        + ", ".join(gates) + "\n\nGoal:\n" + args.objective
    )
    config = {"kind": args.planner}
    if args.profile_dir:
        config["profile_dir"] = str(Path(args.profile_dir).resolve(strict=True))
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
    if args.start:
        run_id = create_run(store, spec)
        return {"run_id": run_id, "runner_pid": background(store, run_id, False), "milestones": raw["milestones"]}
    return raw


def main(argv=None):
    app = argparse.ArgumentParser(prog="rookline")
    app.add_argument("--home", type=Path, help="Override private runtime data directory")
    commands = app.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register an explicit plan")
    add.add_argument("plan")
    add.add_argument("--start", action="store_true")
    goal = commands.add_parser("goal", help="Turn one goal into an ordered plan using an authenticated CLI")
    goal.add_argument("template")
    goal.add_argument("objective")
    goal.add_argument("--planner", choices=("claude", "codex"), default="claude")
    goal.add_argument("--profile-dir")
    goal.add_argument("--start", action="store_true")
    for name in ("run", "start", "pause", "resume", "status", "events"):
        parser = commands.add_parser(name)
        parser.add_argument("id")
        if name in ("run", "start", "resume"):
            parser.add_argument("--retry", action="store_true", help="Try a blocked run again")
    commands.add_parser("list")
    commands.add_parser("doctor")
    args = app.parse_args(argv)
    if os.name != "nt":
        print("Rookline's native process runner requires Windows.", file=sys.stderr)
        return 1
    try:
        store = Store(args.home)
        name = args.command
        if name == "doctor":
            output({"data_directory": str(store.root),
                    "executables": {key: shutil.which(key) for key in ("git", "claude", "codex", "ollama")}})
        elif name == "list":
            output([with_live_status(store, row) for row in store.runs()])
        elif name == "add":
            run_id = create_run(store, read_plan(args.plan))
            result = {"run_id": run_id}
            if args.start:
                result["runner_pid"] = background(store, run_id, False)
            output(result)
        elif name == "goal":
            output(plan_goal(store, args))
        elif name == "status":
            output(with_live_status(store, store.get(args.id)))
        elif name == "events":
            output(store.events(args.id))
        elif name == "pause":
            store.pause(args.id, True)
            output({"run_id": args.id, "pause_requested": True})
        elif name == "run":
            phase = Run(store, args.id).run(retry=args.retry)
            output({"run_id": args.id, "phase": phase})
            return {"done": 0, "paused": 2, "blocked": 3}[phase]
        else:
            store.get(args.id)
            if name == "resume":
                store.pause(args.id, False)
            output({"run_id": args.id, "runner_pid": background(store, args.id, args.retry)})
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as error:
        print(f"Rookline: {error}", file=sys.stderr)
        return 1
