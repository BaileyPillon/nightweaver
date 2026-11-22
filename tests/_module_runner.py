"""Run one test module in its own process, for CI: python tests/_module_runner.py MODULE.py SECONDS

When the module is still running after SECONDS, every thread's stack goes to stderr, so a hang names the call it is stuck in. The
caller's own, later deadline is what ends the process."""

import faulthandler
import sys
import unittest

pattern, seconds = sys.argv[1], float(sys.argv[2])
faulthandler.dump_traceback_later(seconds)
unittest.main(module=None, argv=["unittest", "discover", "-s", "tests", "-p", pattern, "-v"])
