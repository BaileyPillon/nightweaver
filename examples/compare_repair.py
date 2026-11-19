"""Run a matched, model-free repair experiment against a Nightweaver 0.16 checkout.
Usage: python examples/compare_repair.py --baseline PATH --output NEW_DIRECTORY
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CURRENT = HERE.parents[1]
CHECKS = ("search", "restore")


def worker(role):
    prompt = sys.stdin.read()
    text = Path("result.txt")
    if role == "primary":
        text.write_text("partial\n", encoding="utf-8")
    elif role == "delivery":
        if all(key in text.read_text().splitlines() for key in CHECKS):
            Path("delivery.txt").write_text("Ready to share\n", encoding="utf-8")
    else:
        if "partial" not in text.read_text().splitlines():
            raise RuntimeError("Partial edits were lost between workers")
        observed = prompt.partition("Last observed failure (if any)")[2]
        fixed = [key for key in CHECKS if f"missing_{key}_wiring" in observed]
        text.write_text("partial\n" + "\n".join(fixed) + "\n", encoding="utf-8")


def check(key):
    if key == "delivery":
        return int(not Path("delivery.txt").is_file())
    if key not in Path("result.txt").read_text().splitlines():
        print(f"missing_{key}_wiring")
        return 1
    return 0


def cmd(cwd, *argv, codes=(0,)):
    call = subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if call.returncode not in codes:
        raise RuntimeError(
            f"{argv[0]} exited {call.returncode}\n{call.stderr[-1500:]}\n{call.stdout[-1500:]}"
        )
    return call.stdout.strip()


def route(role):
    return {"kind": "command", "argv": [sys.executable, str(HERE), "worker", role]}


def gate(key):
    return {"name": key, "argv": [sys.executable, str(HERE), "check", key]}


def make_plan(repo):
    return {
        "title": "Restore the prompt reel",
        "objective": "Repair both interactions and deliver the reel",
        "repository": str(repo),
        "worker": route("primary"),
        "worker_fallbacks": [route("alternate")],
        "attempts": 2,
        "retry": {"initial_seconds": 2, "max_seconds": 2},
        "budgets": {"max_calls": 3, "max_seconds": 300},
        "gates": [gate(k) for k in CHECKS],
        "milestones": [
            {"title": "Repair", "task": "Restore search and restore actions"},
            {
                "title": "Deliver",
                "task": "Record the delivery",
                "worker": route("delivery"),
                "gates": [gate("delivery")],
            },
        ],
    }


def experiment(baseline, output):
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    repo.mkdir()
    cmd(repo, "git", "init", "-qb", "main")
    cmd(repo, "git", "config", "user.name", "Nightweaver comparison")
    cmd(repo, "git", "config", "user.email", "fixture@example.invalid")
    (repo / "result.txt").write_text("broken\n")
    cmd(repo, "git", "add", ".")
    cmd(repo, "git", "commit", "-qm", "Shared broken starting point")
    base = cmd(repo, "git", "rev-parse", "HEAD")
    report = []
    for label, code, collect, rescue in (
        ("0.16 default", baseline, False, False),
        ("0.17 collect only", CURRENT, True, False),
        ("0.17 switch only", CURRENT, False, True),
        ("0.17 collect and switch", CURRENT, True, True),
    ):
        home = output / label.replace(" ", "-")
        plan = make_plan(repo)
        plan["expected_base"] = base
        if code == CURRENT:
            plan["repair"] = {"collect_failures": collect, "switch_after": int(rescue)}
        path = output / (home.name + ".json")
        path.write_text(json.dumps(plan), encoding="utf-8")
        cli = [sys.executable, "-m", "nightweaver", "--home", str(home)]
        rid = json.loads(cmd(code, *cli, "add", str(path)))["run_id"]
        # Exit one controller immediately after its first rejection. Resume with another process.
        fragment = (
            "from nightweaver.store import Store; from nightweaver.engine import Run; "
            "from pathlib import Path; import sys; "
            "print(Run(Store(Path(sys.argv[1])),sys.argv[2]).run(defer_retries=True))"
        )
        first = cmd(code, sys.executable, "-c", fragment, str(home), rid)
        if first != "waiting":
            raise AssertionError(f"Expected a durable retry point, got {first}")
        cmd(code, *cli, "run", rid, codes=(0, 3))
        m = json.loads(cmd(code, *cli, "metrics", rid))
        row = {
            "case": label,
            "base": base,
            "run_id": rid,
            "phase": m["phase"],
            "calls": m["provider_calls"],
            "milestones": m["accepted_milestones"],
            "handoffs": m.get("repair_handoffs", 0),
            "restarted": True,
        }
        if row["phase"] == "done":
            row["replay"] = json.loads(
                cmd(code, *cli, "replay-checks", rid, "--through")
            )["passed"]
        report.append(row)
    (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return int(
        [r["phase"] for r in report] != ["blocked", "blocked", "blocked", "done"]
    )


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        worker(sys.argv[2])
    elif sys.argv[1:2] == ["check"]:
        sys.exit(check(sys.argv[2]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--baseline", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args()
        sys.exit(experiment(args.baseline.resolve(strict=True), args.output.resolve()))
