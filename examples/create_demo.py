"""Create a disposable Git project and a two-milestone Rookline plan."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="A directory that does not exist yet")
    parser.add_argument("--live", action="store_true", help="Use Codex and Claude Code instead of deterministic workers")
    parser.add_argument("--claude-profile", type=Path, help="An existing authenticated Claude configuration directory")
    parser.add_argument("--ollama", help="Optional already-installed local Ollama model")
    parser.add_argument("--ollama-port", type=int, default=11434)
    args = parser.parse_args()
    folder = args.directory.resolve()
    folder.mkdir(parents=True)
    repo = folder / "project"
    repo.mkdir()
    (repo / "title.txt").write_text("Draft\n", encoding="utf-8")
    (repo / "check.py").write_text(
        'from pathlib import Path\nimport sys\n\n'
        'assert Path("title.txt").read_text(encoding="utf-8").strip() == "Field Notes", "expected Field Notes in title.txt"\n'
        'if "--final" in sys.argv:\n'
        '    assert Path("summary.txt").read_text(encoding="utf-8").strip() == "Field Notes is ready.", "expected Field Notes is ready. in summary.txt"\n',
        encoding="utf-8",
    )
    for command in (["init", "-b", "main"], ["config", "user.name", "Rookline Demo"],
                    ["config", "user.email", "demo@example.invalid"], ["add", "title.txt", "check.py"],
                    ["commit", "-m", "Start demo"]):
        subprocess.run(["git", "-C", str(repo), *command], check=True, stdout=subprocess.DEVNULL)

    worker = {
        "kind": "command", "argv": [sys.executable, "-c",
            'import sys; from pathlib import Path; feedback = sys.stdin.read(); '
            'Path("title.txt").write_text("Field Notes\\n" if "expected Field Notes" in feedback '
            'else "First pass\\n", encoding="utf-8")'],
    }
    followup = {
        "kind": "command", "argv": [sys.executable, "-c",
            'from pathlib import Path; Path("summary.txt").write_text("Field Notes is ready.\\n", encoding="utf-8")'],
    }
    plan = {
        "repository": "project",
        "title": "Field Notes",
        "objective": "Deliver a tiny field journal with a verified title and one-line summary.",
        "attempts": 3,
        "protected_paths": ["check.py"],
        "gates": [{"name": "Title contract", "argv": [sys.executable, "check.py"]}],
        "worker": {"kind": "codex"} if args.live else worker,
        "milestones": [
            {"title": "Name the journal", "task": "Set title.txt to Field Notes followed by a newline.", "paths": ["title.txt"]},
            {"title": "Write the summary", "task": "Add summary.txt containing Field Notes is ready. followed by a newline.",
             "paths": ["summary.txt"], "worker": {"kind": "claude"} if args.live else followup,
             "gates": [{"name": "Summary contract", "argv": [sys.executable, "check.py", "--final"]}]},
        ],
    }
    if args.ollama:
        plan["advisory_ollama"] = {"model": args.ollama, "port": args.ollama_port}
    if args.live and args.claude_profile:
        plan["milestones"][1]["worker"]["profile_dir"] = str(args.claude_profile.resolve(strict=True))
    (folder / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(folder / "plan.json")


if __name__ == "__main__":
    main()
