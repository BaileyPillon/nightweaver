"""Start the target only after the parent owns this process through its Job."""

import json
from pathlib import Path
import subprocess
import sys


def main():
    if sys.stdin.buffer.read(1) != b"\x01":
        return 125
    argv = json.loads(sys.argv[1])
    with Path(sys.argv[2]).open("rb") as source:
        return subprocess.run(argv, stdin=source).returncode


if __name__ == "__main__":
    raise SystemExit(main())
