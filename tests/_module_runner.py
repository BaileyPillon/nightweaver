"""Run one test module in its own process, for CI: python tests/_module_runner.py MODULE.py SECONDS

When the module is still running after SECONDS, every thread's stack goes to stderr, so a hang names the call it is stuck in. The
caller's own, later deadline is what ends the process."""

import faulthandler
import sys
import unittest
from pathlib import Path

# Running a script puts the script's own folder on sys.path. `python -m unittest` put the current folder there instead, and some tests
# import from the repository root (examples/ is not part of the installed package), so the root goes first, as it did.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    pattern, seconds = sys.argv[1], float(sys.argv[2])
    faulthandler.dump_traceback_later(seconds)
    unittest.main(module=None, argv=["unittest", "discover", "-s", str(ROOT / "tests"), "-p", pattern, "-v"])
