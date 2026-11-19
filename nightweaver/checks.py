"""Immutable evidence for one invocation of a trusted gate."""

from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp
import time

from .process import execute
from .safety import write_json


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


def verify_inputs(check, cwd, base=None):
    for pin in check.get("pinned_inputs", []):
        name = pin["path"]
        path = cwd / name
        if base and not Path(name).is_absolute():
            from .engine import git, git_bytes

            entry = git(cwd, "--literal-pathspecs", "ls-tree", base, "--", name)
            if entry.split(" ", 1)[0] not in ("100644", "100755"):
                raise InputDrift(f"Missing or nonregular pinned check input at baseline: {name}")
            digest = sha256(git_bytes(cwd, "cat-file", "blob", f"{base}:{name}")).hexdigest()
        else:
            if not path.is_file():
                raise InputDrift(f"Missing pinned check input: {name}")
            with path.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
        if digest != pin["sha256"]:
            raise InputDrift(f"Digest mismatch for pinned check input: {name}")


def run_gate(check, path, folder, tree, **process_options):
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
        else:
            argv = check["argv"]
        try:
            receipt["exit"] = execute(argv, path, log, check["timeout"], env=env, **process_options)
        except OSError as error:
            receipt["execution_error"] = True
            raise ValueError(f"Cannot start gate executable: {error}") from error
        verify_inputs(check, path)
        receipt["artifacts"] = preserve_artifacts(path, folder, check["artifacts"], before, max_bytes)
        if browser:
            receipt["receipt"] = str(out / "browser.json")
            for file in sorted(out.iterdir()):
                if not file.is_file() or file.is_symlink() or file.stat().st_size > max_bytes:
                    raise ValueError(f"Invalid or oversized browser evidence: {file.name}")
                with file.open("rb") as stream:
                    receipt["artifacts"].append(
                        {
                            "name": file.name,
                            "path": str(file),
                            "sha256": file_digest(stream, "sha256").hexdigest(),
                        }
                    )
            report = json.loads((out / "browser.json").read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not isinstance(report.get("viewports"), list):
                raise ValueError("Browser returned an invalid receipt")
            if any(
                not isinstance(v, dict) or type(v.get("passed")) is not bool for v in report["viewports"]
            ):
                raise ValueError("Browser returned an invalid viewport receipt")
            failures = [
                {
                    "viewport": view.get("viewport"),
                    "stage": view.get("failed_at"),
                    "error": str(next(iter(view.get("errors") or []), ""))[:600],
                }
                for view in report["viewports"]
                if isinstance(view, dict) and not view.get("passed")
            ]
            if failures:
                receipt["browser_failures"] = failures
            if receipt["exit"] == 0 and (
                report.get("passed") is not True or failures or not report["viewports"]
            ):
                raise ValueError("Browser returned no passing receipt")
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
