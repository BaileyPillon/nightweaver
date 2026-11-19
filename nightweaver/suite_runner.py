"""Native suite adapters used by repository OneShots. Invoked as a standalone script."""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree


def unittest_suite(folder):
    sys.path.insert(0, str(Path.cwd()))
    suite = unittest.defaultTestLoader.discover(folder)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    exercised = result.testsRun - len(result.skipped) - len(result.expectedFailures)
    if exercised < 1:
        print(
            "No normally executed tests in the discovered unittest suite.",
            file=sys.stderr,
        )
        return 1
    return int(not result.wasSuccessful())


def dotnet_suite(executable, project):
    with TemporaryDirectory(prefix="nw-trx-") as directory:
        result = subprocess.run(
            [
                executable,
                "test",
                project,
                "--nologo",
                "--artifacts-path",
                str(Path(directory) / "build"),
                "--results-directory",
                str(Path(directory) / "results"),
                "--logger",
                "trx",
            ],
            check=False,
        )
        if result.returncode:
            return result.returncode
        reports = list(Path(directory).rglob("*.trx"))
        passed = 0
        for file in reports:
            root = ElementTree.parse(file).getroot()
            counters = root.find(".//{*}ResultSummary/{*}Counters")
            if (
                counters is None
                or int(counters.attrib["executed"]) < 1
                or int(counters.attrib["passed"]) < 1
                or any(
                    int(counters.get(key, 0))
                    for key in ("failed", "error", "timeout", "aborted")
                )
            ):
                return 1
            passed += int(counters.attrib["passed"])
        print(f"TRX: {passed} passed tests across {len(reports)} reports")
        return int(passed == 0)


if __name__ == "__main__":
    if sys.argv[1] == "unittest":
        sys.exit(unittest_suite(sys.argv[2]))
    if sys.argv[1] == "dotnet":
        sys.exit(dotnet_suite(*sys.argv[2:]))
    sys.exit("Unknown suite adapter")
