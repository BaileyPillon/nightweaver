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
