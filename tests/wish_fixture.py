"""Scripted brief writer, planner and judge used by the wish tests."""

import json
from pathlib import Path
import sys


mode = sys.argv[1]
if mode == "script":
    # Print the next scripted answer (the last one repeats) and save every prompt for assertions.
    sys.stdout.reconfigure(encoding="utf-8")
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
