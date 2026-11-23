"""Scripted workers and gates for the repair-loop tests."""

from pathlib import Path
import sys


mode = sys.argv[1]
if mode in ("write", "grow", "big"):
    # Save the prompt, make a change and print a recognizable explanation.
    prompt = sys.stdin.read()
    with Path(sys.argv[2]).open("a", encoding="utf-8") as record:
        record.write(prompt + "\n=====\n")
    if mode == "write":
        Path("result.txt").write_text(sys.argv[3], encoding="utf-8")
    elif mode == "grow":
        notes = Path("notes.txt")
        lines = notes.read_text(encoding="utf-8").splitlines() if notes.exists() else []
        notes.write_text("\n".join([*lines, f"revision {len(lines) + 1}"]) + "\n", encoding="utf-8")
        Path("result.txt").write_text("ready", encoding="utf-8")
    else:
        Path("big.txt").write_text(("x" * 99 + "\n") * 5000, encoding="utf-8")  # about 488 KB, over the review diff cap
        Path("result.txt").write_text("ready", encoding="utf-8")
    print("SUMMARY-MARK I changed the build and believe it is right.")
elif mode == "vary":
    # A gate that always fails but words the failure differently every time.
    counter = Path(sys.argv[2])
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    print(f"variant {count} of the failure: expected ready")
    raise SystemExit(2)
elif mode == "flaky":
    # Fails the first time it runs and passes afterwards, whatever the build.
    counter = Path(sys.argv[2])
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count == 1:
        print("flaky failure")
        raise SystemExit(2)
