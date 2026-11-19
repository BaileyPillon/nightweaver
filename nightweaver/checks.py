"""Immutable evidence for one invocation of a trusted gate."""

from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp
import time

from .process import execute
from .safety import redact, write_json


def contract_hash(check):
    return sha256(json.dumps(check, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_folder(parent, index):
    parent.mkdir(parents=True, exist_ok=True)
    name = f"gate-{index:02d}"
    folder = parent / name
    try:
        folder.mkdir()
    except FileExistsError:
        folder = Path(mkdtemp(prefix=name + "-recheck-", dir=parent))
    return folder


class InputDrift(ValueError):
    pass


def observed_pass(receipt):
    """Only a completed, evidenced check on the named source can score a sample."""
    if type(receipt.get("exit")) is not int or receipt.get("artifact_error") or receipt.get("source_error"):
        return None
    return receipt["exit"] == 0


def verify_saved_sample(receipt, evidence):
    """Validate the stored bytes before reusing an interrupted verification sample."""
    files = [(receipt["log"], receipt["log_sha256"])] if receipt.get("log_sha256") else []
    files.extend((a["path"], a["sha256"]) for a in receipt.get("artifacts", []))
    for name, expected in files:
        path = Path(name).resolve(strict=True)
        if not path.is_relative_to(evidence.resolve()) or not path.is_file():
            raise ValueError(f"Invalid saved gate evidence: {name}")
        with path.open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Saved gate evidence changed: {name}")


def verify_inputs(check, cwd, base=None):
    for pin in check.get("pinned_inputs", []):
        name = pin["path"]
        path = cwd / name
        if base and not Path(name).is_absolute():
            from .engine import git, git_bytes

            entry = git(cwd, "--literal-pathspecs", "ls-tree", base, "--", name)
            if entry.split(" ", 1)[0] not in ("100644", "100755"):
                raise InputDrift(f"Missing or nonregular pinned check input at baseline: {name}")
            # The verifier runs checked-out bytes, including the pinned revision's EOL and smudge rules.
            digest = sha256(git_bytes(cwd, "cat-file", "--filters", f"{base}:{name}", attributes_from=base)).hexdigest()
        else:
            if not path.is_file():
                raise InputDrift(f"Missing pinned check input: {name}")
            with path.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
        if digest != pin["sha256"]:
            raise InputDrift(f"Digest mismatch for pinned check input: {name}")


def read_browser_evidence(config, out, receipt, max_bytes):
    receipt["receipt"] = str(out / "browser.json")
    nonempty_files = set()
    for file in sorted(out.iterdir()):
        if not file.is_file() or file.is_symlink():
            raise ValueError(f"Invalid browser evidence: {file.name}")
        size = file.stat().st_size
        if size > max_bytes:
            raise ValueError(f"Invalid or oversized browser evidence: {file.name}")
        with file.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        receipt["artifacts"].append({"name": file.name, "path": str(file), "sha256": digest})
        if size:
            nonempty_files.add(file.name)

    report = json.loads((out / "browser.json").read_text(encoding="utf-8"))
    if not isinstance(report, dict) or type(report.get("passed")) is not bool:
        raise ValueError("Invalid browser verdict")
    if receipt["exit"] not in (0, 1) or report["passed"] != (receipt["exit"] == 0):
        raise ValueError(f"Browser verdict disagrees with process exit {receipt['exit']}")
    if "error" in report:
        kind = report.get("failure_kind")
        if report["passed"] or "viewports" in report or not isinstance(report["error"], str):
            raise ValueError("Contradictory browser error receipt")
        issue = redact(report["error"])[:4000]
        if kind == "candidate_startup":
            receipt["browser_failures"] = [{"stage": {"action": "startup"}, "error": issue}]
            return
        receipt["browser_issue"] = kind if kind == "browser_environment" else "verification_error"
        raise ValueError(issue or "Browser verifier returned no diagnosis")

    views = report.get("viewports")
    expected = {v["name"] for v in config["viewports"]}
    if (not isinstance(views, list) or len(views) != len(expected)
            or any(not isinstance(v, dict) or not isinstance(v.get("viewport"), str) for v in views)
            or {v["viewport"] for v in views} != expected):
        raise ValueError("Browser receipt does not cover each configured viewport exactly once")
    planned = len(config["steps"])
    checkpoints = [(i, s["value"]) for i, s in enumerate(config["steps"], 1) if s["action"] == "checkpoint"]
    coverage, failures = [], []
    for view in views:
        passed, completed, name = view.get("passed"), view.get("completed_steps"), view["viewport"]
        if (type(passed) is not bool or type(completed) is not int or not 0 <= completed <= planned
                or (passed and completed != planned)):
            raise ValueError(f"Invalid browser step coverage for {name}: {completed}/{planned}")
        if not isinstance(view.get("errors"), list) or (passed and view["errors"]):
            raise ValueError(f"Invalid browser errors for {name}")
        required = {f"{name}.png", f"{name}.trace.zip"}
        required.update(f"checkpoint.{name}.{label}.png" for i, label in checkpoints if i <= completed)
        if missing := required - nonempty_files:
            raise ValueError(f"Missing or empty browser evidence for {name}: {', '.join(sorted(missing))}")
        coverage_row = {"viewport": name, "passed": passed, "completed_steps": completed, "planned_steps": planned}
        timings = view.get("step_durations_ms")
        if timings is not None:
            if (not isinstance(timings, list) or not completed <= len(timings) <= min(completed + 1, planned)
                    or any(type(v) is not int or v < 0 for v in timings)):
                raise ValueError(f"Invalid browser step durations for {name}")
            coverage_row["step_durations_ms"] = timings
        coverage.append(coverage_row)
        if not passed:
            failures.append({"viewport": name, "stage": view.get("failed_at"),
                             "error": str(next(iter(view["errors"]), ""))[:600],
                             "errors": [str(error)[:240] for error in dict.fromkeys(map(str, view["errors"]))][:4]})
    if report["passed"] == bool(failures):
        raise ValueError("Browser verdict disagrees with viewport results")
    receipt["browser_coverage"] = coverage
    if failures:
        receipt["browser_failures"] = failures


def run_gate(check, path, folder, tree, *, baseline=None, **process_options):
    from .engine import artifact_stamp, preserve_artifacts

    log = folder / "gate.log"
    env = {
        **os.environ,
        "NIGHTWEAVER_EVIDENCE": str(folder.parent),
        "NIGHTWEAVER_TREE": tree,
        "ROOKLINE_EVIDENCE": str(folder.parent),
        "ROOKLINE_TREE": tree,
    }
    started = time.monotonic()
    max_bytes = check.get("max_artifact_bytes", 32 * 1024 * 1024)
    receipt = {
        "name": check["name"],
        "tree": tree,
        "log": str(log),
        "contract_sha256": contract_hash(check),
        "artifacts": [],
        "exit": None,
    }
    try:
        verify_inputs(check, path)
        before = {name: artifact_stamp(path, name) for name in check["artifacts"]}
        browser = check.get("kind") == "browser"
        catalog = check.get("kind") == "catalog"
        if browser:
            config = folder / "config.json"
            out = folder / "browser"
            write_json(config, check["browser"])
            argv = [
                sys.executable,
                str(Path(__file__).with_name("browser.py")),
                "--config-file",
                str(config),
                "--output",
                str(out),
            ]
        elif catalog:
            config = folder / "contract.json"
            catalog_file = folder / "catalog.json"
            write_json(config, check["catalog"])
            argv = [sys.executable, str(Path(__file__).with_name("catalog.py")), "--contract", str(config),
                    "--base", baseline, "--candidate", tree, "--output", str(catalog_file)]
        else:
            argv = check["argv"]
        try:
            receipt["exit"] = execute(argv, path, log, check["timeout"], env=env, **process_options)
        except OSError as error:
            receipt["execution_error"] = True
            raise ValueError(f"Cannot start gate executable: {error}") from error
        verify_inputs(check, path)
        receipt["artifacts"] = preserve_artifacts(path, folder, check["artifacts"], before, max_bytes)
        if catalog:
            try:
                data = catalog_file.read_bytes()
                if len(data) > max_bytes:
                    raise ValueError("Catalog receipt exceeds the gate artifact limit")
                report = json.loads(data)
                if report.get("input_error"):
                    raise InputDrift(report["input_error"])
                if (report.get("schema") != "nightweaver.catalog.v1" or report.get("base") != baseline
                        or report.get("tree") != tree or type(report.get("passed")) is not bool
                        or receipt["exit"] != int(not report["passed"])):
                    raise ValueError("Catalog verdict does not match the expected objects and exit status")
                receipt["catalog"] = report
                receipt["artifacts"].append({"path": str(catalog_file), "name": catalog_file.name,
                                             "sha256": sha256(data).hexdigest()})
            except (ValueError, OSError) as error:
                receipt["execution_error"] = True
                raise ValueError(f"Catalog verification unavailable: {error}") from error
        if browser:
            try:
                read_browser_evidence(check["browser"], out, receipt, max_bytes)
            except (OSError, ValueError) as error:
                receipt["execution_error"] = True
                receipt.setdefault("browser_issue", "verification_error")
                raise ValueError(f"{check['name']} browser verification: {error}") from error
    except InputDrift as error:
        receipt.update(input_error=True, artifact_error=str(error))
    except (OSError, ValueError) as error:
        # BudgetExceeded inherits ValueError and must reach the runner's budget handler.
        from .engine import BudgetExceeded

        if isinstance(error, BudgetExceeded):
            raise
        receipt["artifact_error"] = str(error)
    receipt["duration_ms"] = round((time.monotonic() - started) * 1000)
    return receipt
