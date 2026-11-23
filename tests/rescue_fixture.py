"""Scripted builders and judges for the plateau rescue tests."""

import json
from pathlib import Path
import re
import sys


def next_answer(table, key):
    """The next scripted answer under `key`; the last one repeats. A string is printed as written."""
    items = json.loads(table.read_text(encoding="utf-8")).get(key)
    if not items:
        return None
    position = table.with_suffix(f".{key}.pos")
    index = int(position.read_text()) if position.exists() else 0
    position.write_text(str(index + 1))
    return items[min(index, len(items) - 1)]


mode = sys.argv[1]
if mode == "build":
    # build LOG FAILING TAG: record the prompt, then make a change. A rescue strategy writes files that differ per strategy.
    prompt = sys.stdin.read()
    log, failing, tag = Path(sys.argv[2]), sys.argv[3].split(","), sys.argv[4]
    with log.open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    title = re.search(r"^Milestone \d+/\d+: (.*)$", prompt, re.M).group(1)
    with Path("lineage.txt").open("a", encoding="utf-8", newline="\n") as lineage:
        lineage.write(title + "\n")
    strategy = re.search(r"^Strategy (.*?): (.*)$", prompt, re.M)
    Path("result.txt").write_text("permanent defect" if strategy and strategy.group(1) in failing else "ready", encoding="utf-8")
    if strategy:
        Path("strategy.txt").write_text(strategy.group(1), encoding="utf-8")
        Path("index.html").write_text(f"<h1 data-strategy=\"{strategy.group(1)}\" data-built-by=\"{tag}\">{strategy.group(2)}</h1>", encoding="utf-8")
    print(f"SUMMARY built by {tag}")
elif mode == "judge":
    # judge TABLE.json: goal judgments, the rescue diagnosis and pairwise comparisons from one table.
    #   goal / diagnosis / compare  scripted answers in order (the last repeats); compare falls back to `order` when absent
    #   order                       candidate names, best first; the incumbent is "Current best build"
    #   always                      answer "A" or "B" to every comparison, whatever the order
    prompt = sys.stdin.read()
    table = Path(sys.argv[2])
    with table.with_suffix(".prompts").open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    plan = json.loads(table.read_text(encoding="utf-8"))
    if prompt.startswith("Independently judge whether this software goal"):
        key = "goal"
    elif "You diagnose an autonomous coding run" in prompt:
        key = "diagnosis"
    else:
        key = "compare"
    answer = next_answer(table, key)
    if answer is not None:
        print(answer if isinstance(answer, str) else json.dumps(answer))
    elif key == "compare":
        names = dict(re.findall(r'^Candidate ([A-D]): strategy "(.*?)"$', prompt, re.M))
        order = plan.get("order", [])
        if plan.get("always"):
            print(json.dumps({"winner": plan["always"], "reason": "Always the same position"}))
        else:
            a, b = order.index(names["A"]), order.index(names["B"])
            print(json.dumps({"winner": "A" if a < b else "B", "reason": f"{names['A']} against {names['B']}"}))
    else:
        raise SystemExit(f"no scripted {key} answer")
elif mode == "shot":
    # A check that leaves a screenshot in the evidence folder, as a browser gate would.
    import base64
    import os
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    Path(os.environ["NIGHTWEAVER_EVIDENCE"], "screen.png").write_bytes(png)
