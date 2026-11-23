"""Scripted worker, judge and planner used by the goal pursuit tests."""

import json
from pathlib import Path
import re
import sys


mode = sys.argv[1]
if mode == "build":
    # Append the milestone title so every milestone makes a distinct tracked change.
    prompt = sys.stdin.read()
    with Path(sys.argv[2]).open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    title = re.search(r"^Milestone \d+/\d+: (.*)$", prompt, re.M).group(1)
    with Path("lineage.txt").open("a", encoding="utf-8", newline="\n") as lineage:
        lineage.write(title + "\n")
    Path("result.txt").write_text("ready", encoding="utf-8")
elif mode == "script":
    # Print the next scripted answer; the last one repeats. Save every prompt for assertions.
    sequence = Path(sys.argv[2])
    position = sequence.with_suffix(".pos")
    prompt = sys.stdin.read()
    with sequence.with_suffix(".prompts").open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    items = json.loads(sequence.read_text(encoding="utf-8"))
    index = int(position.read_text()) if position.exists() else 0
    position.write_text(str(index + 1))
    item = items[min(index, len(items) - 1)]
    print(item if isinstance(item, str) else json.dumps(item))
elif mode == "never_judge":
    # A check that holds in milestone worktrees but fails on the judge's checkout of the finished build.
    cwd = Path.cwd()
    lines = cwd.joinpath("lineage.txt").read_text(encoding="utf-8").splitlines()
    raise SystemExit(1 if cwd.name.startswith("judge-") and len(lines) < int(sys.argv[2]) else 0)
elif mode == "artifact_unless_judge":
    # Promised evidence that the judge's checkout never produces: an environment error, not a verdict.
    if not Path.cwd().name.startswith("judge-"):
        Path("receipt.txt").write_text("evidence", encoding="utf-8")
elif mode == "shot":
    # A check that leaves a screenshot in the evidence folder, as a browser gate would.
    import base64
    import os
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    Path(os.environ["NIGHTWEAVER_EVIDENCE"], "screen.png").write_bytes(png)
