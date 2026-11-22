"""Immutable evidence for one invocation of a trusted gate."""

from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp
import time

from .process import ProcessLimit, execute
from .journeys import browser_cases, case_prefix
from .safety import mask_tokens, redact, scrub_env, write_json
from .spec import read_json


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
    return receipt["exit"] == 0 and receipt.get("junit", {}).get("passed", True)


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
        receipt["artifacts"].append(
            {"name": file.name, "path": str(file), "sha256": digest}
        )
        if size:
            nonempty_files.add(file.name)

    report = read_json(out / "browser.json")
    if not isinstance(report, dict) or type(report.get("passed")) is not bool:
        raise ValueError("Invalid browser verdict")
    if receipt["exit"] not in (0, 1) or report["passed"] != (receipt["exit"] == 0):
        raise ValueError(
            f"Browser verdict disagrees with process exit {receipt['exit']}"
        )
    if "error" in report:
        kind = report.get("failure_kind")
        if (
            report["passed"]
            or "viewports" in report
            or not isinstance(report["error"], str)
        ):
            raise ValueError("Contradictory browser error receipt")
        issue = redact(report["error"])[:4000]
        if kind == "candidate_startup":
            receipt["browser_failures"] = [
                {"stage": {"action": "startup"}, "error": issue}
            ]
            return
        receipt["browser_issue"] = (
            kind if kind == "browser_environment" else "verification_error"
        )
        raise ValueError(issue or "Browser verifier returned no diagnosis")

    views = report.get("viewports")
    expected = {
        (s.get("name"), v["name"]): steps for s, v, steps in browser_cases(config)
    }
    if (
        not isinstance(views, list)
        or len(views) != len(expected)
        or any(
            not isinstance(v, dict)
            or not isinstance(v.get("viewport"), str)
            or not isinstance(v.get("scenario", ""), str)
            for v in views
        )
        or {(v.get("scenario"), v["viewport"]) for v in views} != expected.keys()
    ):
        raise ValueError(
            "Browser receipt does not cover each configured viewport exactly once"
        )
    schema = report.get("schema")
    modern = schema == "nightweaver.browser.v2"
    if (schema is not None and not modern) or ("scenarios" in config and not modern):
        raise ValueError("Unsupported browser evidence schema for this plan")
    if modern:
        runtime = report.get("runtime")
        if not isinstance(runtime, dict) or not all(
            isinstance(runtime.get(k), str) and runtime[k]
            for k in ("chromium", "playwright", "channel")
        ):
            raise ValueError("Invalid browser runtime receipt")
        receipt["browser_runtime"] = runtime
    coverage, failures = [], []
    for view in views:
        passed, completed, name = (
            view.get("passed"),
            view.get("completed_steps"),
            view["viewport"],
        )
        scenario = view.get("scenario")
        prefix = case_prefix(scenario, name)
        steps = expected[scenario, name]
        planned = len(steps)
        if (
            type(passed) is not bool
            or type(completed) is not int
            or not 0 <= completed <= planned
            or (passed and completed != planned)
        ):
            raise ValueError(
                f"Invalid browser step coverage for {name}: {completed}/{planned}"
            )
        if not isinstance(view.get("errors"), list) or (passed and view["errors"]):
            raise ValueError(f"Invalid browser errors for {name}")
        required = {f"{prefix}.png", f"{prefix}.trace.zip"}
        finished = list(enumerate(steps[:completed], 1))
        required.update(
            f"checkpoint.{prefix}.{s['value']}.png"
            for i, s in finished
            if s["action"] == "checkpoint"
        )
        required.update(
            f"{prefix}.{i:02d}.actual.png"
            for i, s in finished
            if s["action"] == "visual_match"
        )
        if missing := required - nonempty_files:
            raise ValueError(
                f"Missing or empty browser evidence for {name}: {', '.join(sorted(missing))}"
            )
        coverage_row = {
            "viewport": name,
            "passed": passed,
            "completed_steps": completed,
            "planned_steps": planned,
        }
        if scenario is not None:
            coverage_row["scenario"] = scenario
        overflow = view.get("overflow_px")
        if "overflow_px" in view or (modern and passed):
            if type(overflow) is not int or overflow < 0 or (passed and overflow > 1):
                raise ValueError(f"Invalid browser layout observation for {prefix}")
            coverage_row["overflow_px"] = overflow
        if "network" in config:
            count = view.get("http_requests")
            if (
                type(count) is not int
                or count < 0
                or (passed and count > config["network"].get("max_requests", count))
            ):
                raise ValueError(f"Invalid browser HTTP request count for {prefix}")
            coverage_row["http_requests"] = count
        expected_responses = [
            {"index": i, **s["value"]} for i, s in finished if s["action"] == "response"
        ]
        if view.get("response_matches", []) != expected_responses:
            raise ValueError(
                f"Browser HTTP observations differ from completed steps for {prefix}"
            )
        if expected_responses:
            coverage_row["response_matches"] = expected_responses
        if modern:
            for key in ("navigation_ms", "duration_ms"):
                if key == "navigation_ms" and key not in view and not passed:
                    continue
                if type(view.get(key)) is not int or view[key] < 0:
                    raise ValueError(f"Invalid {key} for {prefix}")
                coverage_row[key] = view[key]
            summary = view.get("network_summary")
            if not isinstance(summary, dict) or any(
                type(summary.get(k)) is not int or summary[k] < 0
                for k in ("requests", "failed_requests")
            ):
                raise ValueError(f"Invalid network summary for {prefix}")
            for key in ("by_type", "response_classes"):
                if not isinstance(summary.get(key), dict) or any(
                    type(n) is not int or n < 0 for n in summary[key].values()
                ):
                    raise ValueError(f"Invalid network counts for {prefix}")
            if (
                sum(summary["by_type"].values()) != summary["requests"]
                or (
                    "network" in config and view["http_requests"] != summary["requests"]
                )
                or (
                    passed
                    and config.get("network", {}).get("require_success")
                    and (
                        summary["failed_requests"]
                        or summary["response_classes"].get("4xx")
                        or summary["response_classes"].get("5xx")
                    )
                )
            ):
                raise ValueError(f"Inconsistent network summary for {prefix}")
            coverage_row["network_summary"] = summary
            checkpoints = view.get("checkpoints", [])
            expected_checkpoints = [
                (i, s["value"], f"checkpoint.{prefix}.{s['value']}.png")
                for i, s in finished
                if s["action"] == "checkpoint"
            ]
            if (
                not isinstance(checkpoints, list)
                or any(
                    not isinstance(c, dict)
                    or type(c.get("overflow_px")) is not int
                    or c["overflow_px"] < 0
                    for c in checkpoints
                )
                or [(c.get("step"), c.get("name"), c.get("file")) for c in checkpoints]
                != expected_checkpoints
            ):
                raise ValueError(f"Inconsistent checkpoint layout for {prefix}")
            coverage_row["checkpoint_layout"] = [
                {k: c[k] for k in ("step", "name", "overflow_px")} for c in checkpoints
            ]
        visuals = view.get("visual_matches", [])
        if not isinstance(visuals, list) or any(
            not isinstance(v, dict) for v in visuals
        ):
            raise ValueError(f"Invalid visual measurements for {prefix}")
        for i, s in finished:
            if s["action"] != "visual_match":
                continue
            matches = [v for v in visuals if v.get("step") == f"{prefix}.{i:02d}"]
            contract = s["value"]
            if len(matches) != 1:
                raise ValueError(f"Missing visual measurement for {prefix} step {i}")
            v = matches[0]
            total, changed = v.get("total_pixels"), v.get("changed_pixels")
            if (
                v.get("baseline_sha256") != contract["sha256"]
                or type(total) is not int
                or not 0 < total <= 16000000
                or type(changed) is not int
                or not 0 <= changed <= total
                or changed * 1000000 > total * contract.get("max_changed_ppm", 0)
            ):
                raise ValueError(
                    f"Visual measurement outside contract for {prefix} step {i}"
                )
        if visuals:
            coverage_row["visual_matches"] = visuals
        timings = view.get("step_durations_ms")
        if timings is not None:
            if (
                not isinstance(timings, list)
                or not completed <= len(timings) <= min(completed + 1, planned)
                or any(type(v) is not int or v < 0 for v in timings)
            ):
                raise ValueError(f"Invalid browser step durations for {name}")
            coverage_row["step_durations_ms"] = timings
        coverage.append(coverage_row)
        if not passed:
            failures.append(
                {
                    "viewport": name,
                    **({"scenario": scenario} if scenario is not None else {}),
                    "stage": view.get("failed_at"),
                    "error_counts": view.get("error_counts", {}),
                    "error": str(next(iter(view["errors"]), ""))[:600],
                    "errors": [
                        str(error)[:240]
                        for error in dict.fromkeys(map(str, view["errors"]))
                    ][:4],
                }
            )
    if report["passed"] == bool(failures):
        raise ValueError("Browser verdict disagrees with viewport results")
    receipt["browser_coverage"] = coverage
    if failures:
        receipt["browser_failures"] = failures


def run_gate(check, path, folder, tree, *, baseline=None, **process_options):
    started = time.monotonic()
    receipt = _single_gate(check, path, folder, tree, baseline=baseline, **process_options)
    if "proof" in check and observed_pass(receipt) is not None:
        from .proof import apply_proof, baseline_check
        from .engine import BudgetExceeded
        try:
            if not baseline:
                raise ValueError("Proof gate requires the recorded baseline commit")
            before, artifacts = baseline_check(check, path, baseline, folder / "baseline", process_options)
            apply_proof(receipt, check, before, artifacts)
        except (OSError, ValueError) as error:
            if isinstance(error, BudgetExceeded):
                raise
            receipt.update(execution_error=True, artifact_error=f"Cannot verify baseline: {error}")
        receipt["duration_ms"] = round((time.monotonic() - started) * 1000)
    return receipt


def _single_gate(check, path, folder, tree, *, baseline=None, **process_options):
    from .engine import artifact_stamp, preserve_artifacts

    log = folder / "gate.log"
    env = {
        # Gate code is written or changed by the worker. It never sees a provider credential.
        **scrub_env(os.environ),
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
        before = {name: artifact_stamp(path, name, max_bytes) for name in check["artifacts"]}
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
            if "junit" in check:
                report_path = (folder / "junit.xml").absolute()
                if report_path.exists() or report_path.is_symlink():
                    raise ValueError("JUnit evidence path is already occupied")
                env["NIGHTWEAVER_JUNIT"] = str(report_path)
                argv = [arg.replace("{junit}", str(report_path)) for arg in argv]
        try:
            receipt["exit"] = execute(argv, path, log, check["timeout"], env=env, raise_on_limit=True, **process_options)
        except ProcessLimit as error:
            receipt["execution_error"] = True
            raise ValueError(str(error)) from error
        except OSError as error:
            receipt["execution_error"] = True
            raise ValueError(f"Cannot start gate executable: {error}") from error
        finally:
            mask_tokens(log)  # a token the gate printed from anywhere never stays in evidence that reviewers and workers read
        verify_inputs(check, path)
        receipt["artifacts"] = preserve_artifacts(path, folder, check["artifacts"], before, max_bytes)
        if "junit" in check:
            from .junit import read_report
            try:
                from .proof import tracked
                read_report(report_path, receipt, max_bytes, check["junit"], tracked(check))
            except (OSError, ValueError) as error:
                receipt["execution_error"] = True
                raise ValueError(f"JUnit verification unavailable: {error}") from error
        if catalog:
            try:
                if catalog_file.is_symlink() or not catalog_file.is_file() or catalog_file.resolve().parent != folder.resolve():
                    raise ValueError("Missing regular catalog receipt in this gate's evidence folder")
                if catalog_file.stat().st_size > max_bytes:
                    raise ValueError("Catalog receipt exceeds the gate artifact limit")
                data = catalog_file.read_bytes()
                if len(data) > max_bytes:
                    raise ValueError("Catalog receipt exceeds the gate artifact limit")
                from .spec import strict_json
                from .catalog import validate_receipt
                report = strict_json(data)
                validate_receipt(report, baseline, tree, receipt["exit"])
                receipt["catalog"] = report
                receipt["artifacts"].append({"path": str(catalog_file), "name": catalog_file.name,
                                             "sha256": sha256(data).hexdigest()})
                if report.get("input_error"):
                    raise InputDrift(report["input_error"])
            except InputDrift:
                raise
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
