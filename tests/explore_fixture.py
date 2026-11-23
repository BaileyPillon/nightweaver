"""Scripted builders and judges for the design-direction tournament tests."""

import json
from pathlib import Path
import re
import sys


mode = sys.argv[1]
if mode == "build":
    # build LOG FAILING TAG: record the prompt, then write files that differ visibly per design direction.
    prompt = sys.stdin.read()
    log, failing, tag = Path(sys.argv[2]), sys.argv[3].split(","), sys.argv[4]
    with log.open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    title = re.search(r"^Milestone \d+/\d+: (.*)$", prompt, re.M).group(1)
    with Path("lineage.txt").open("a", encoding="utf-8", newline="\n") as lineage:
        lineage.write(title + "\n")
    direction = re.search(r"^Design direction (.*?): (.*?)\. Make this direction unmistakable", prompt, re.M | re.S)
    Path("result.txt").write_text("permanent defect" if direction and direction.group(1) in failing else "ready", encoding="utf-8")
    if direction:
        Path("design.txt").write_text(direction.group(1), encoding="utf-8")
        Path("index.html").write_text(f"<h1 data-direction=\"{direction.group(1)}\" data-built-by=\"{tag}\">{direction.group(2)}</h1>", encoding="utf-8")
    print(f"SUMMARY built by {tag}")
elif mode == "judge":
    # judge TABLE.json: a goal judgment, a pairwise comparison or a ranked comparison, decided by a table of design names.
    prompt = sys.stdin.read()
    table = Path(sys.argv[2])
    with table.with_suffix(".prompts").open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    plan = json.loads(table.read_text(encoding="utf-8"))
    names = dict(re.findall(r'^Candidate ([A-D]): design direction "(.*?)"$', prompt, re.M))
    order = plan.get("order", [])
    if prompt.startswith("Independently judge whether this software goal"):
        print(json.dumps(plan.get("goal", {"verdict": "MET", "score": 90, "reason": "Fine", "gaps": [],
                                           "criteria": [{"criterion": "Looks right", "met": True, "note": "Observed"}]})))
    elif "Rank all candidates" in prompt:
        print(json.dumps({"ranking": sorted(names, key=lambda label: order.index(names[label])), "reason": "By table"}))
    elif plan.get("always"):
        print(json.dumps({"winner": plan["always"], "reason": "Always the same position"}))
    else:
        a, b = order.index(names["A"]), order.index(names["B"])
        print(json.dumps({"winner": "A" if a < b else "B", "reason": f"{names['A']} against {names['B']}"}))
