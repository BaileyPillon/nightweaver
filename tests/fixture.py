"""Harmless native-process demo used by the integration tests."""

import json
from pathlib import Path
import subprocess
import sys
import time


mode = sys.argv[1]
if mode == "implement":
    feedback = sys.stdin.read()
    value = "ready" if "expected ready" in feedback else "first draft"
    Path("result.txt").write_text(value, encoding="utf-8")
elif mode == "continue":
    handoff = sys.stdin.read()
    assert "Prepare fixture (" in handoff
    assert "The second milestone creates delivery.txt." in handoff
    assert Path("result.txt").read_text(encoding="utf-8") == "ready"
    Path("delivery.txt").write_text("second milestone", encoding="utf-8")
elif mode == "final_delivery":
    assert Path("delivery.txt").read_text(encoding="utf-8") == "second milestone"
elif mode == "gate":
    if Path("result.txt").read_text(encoding="utf-8") != "ready":
        print("expected ready")
        raise SystemExit(2)
elif mode == "review":
    assert "Checks:" in sys.stdin.read()
    print(json.dumps({"verdict": "PASS", "reason": "The fixture observed the independent check receipt."}))
elif mode == "interrupt":
    feedback = sys.stdin.read()
    if "prior process stopped" not in feedback:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(sys.argv[2]).write_text(str(child.pid), encoding="utf-8")
        time.sleep(120)
    Path("result.txt").write_text("ready", encoding="utf-8")
elif mode == "drift":
    Path("result.txt").write_text("changed by check", encoding="utf-8")
elif mode == "emit":
    Path("receipt.txt").write_text("evidence", encoding="utf-8")
elif mode == "no_evidence":
    assert Path("receipt.txt").exists()
elif mode == "good":
    sys.stdin.read()
    Path("result.txt").write_text("ready", encoding="utf-8")
elif mode == "bad":
    sys.stdin.read()
    Path("result.txt").write_text("permanent defect", encoding="utf-8")
elif mode == "sleep":
    time.sleep(20)
elif mode == "flood":
    print("x" * 10000)
elif mode == "large_artifact":
    Path("receipt.txt").write_text("x" * 10000, encoding="utf-8")
elif mode == "browser_build":
    feedback = sys.stdin.read()
    width = "100%" if "Horizontal overflow" in feedback else "1400px"
    Path("index.html").write_text(f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
    <style>body{{margin:0;width:{width};background:#161a19;color:#ecf1eb;font:20px Segoe UI}}main{{padding:24px;box-sizing:border-box}}</style>
    <main><h1>Atlas fixture</h1><label>Search <input id="search"></label><button id="apply">Apply</button><p id="result">Waiting</p></main>
    <script>document.querySelector('#apply').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#search').value;</script></html>''', encoding="utf-8")
