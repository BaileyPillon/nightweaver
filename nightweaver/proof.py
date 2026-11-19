"""Model-free regression evidence on an immutable baseline and candidate."""

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from .checks import evidence_folder, observed_pass
from .junit import grade, selectors
from .safety import write_json
from .spec import fields, integer


def proof_contract(raw):
    fields(raw, "repairs preserves samples", "proof")
    fixes = selectors(raw.get("repairs", []), "repairs", symptom=True)
    controls = selectors(raw.get("preserves", []), "preserves")
    if not fixes:
        raise ValueError("A proof needs at least one named repair case")
    for fix in fixes:
        for control in controls:
            if (fix["classname"], fix["name"]) == (
                control["classname"],
                control["name"],
            ) and (
                "suite" not in fix
                or "suite" not in control
                or fix["suite"] == control["suite"]
            ):
                raise ValueError("A repair case cannot also be a preservation control")
    return {
        "repairs": fixes,
        "preserves": controls,
        "samples": integer(raw.get("samples", 1), "proof samples", 1, 5),
    }


def tracked(gate):
    policy = gate.get("proof", {})
    return [*policy.get("repairs", []), *policy.get("preserves", [])]


def issues_for(gate, report, *, before):
    cases = report["tracked"]
    required = tracked(gate)
    if len(cases) != len(required):
        raise ValueError("Proof selectors do not match the measured report")
    issues = []
    for index, (q, r) in enumerate(zip(required, cases)):
        expected = (
            "failed" if before and index < len(gate["proof"]["repairs"]) else "passed"
        )
        label = f"{q['classname']} / {q['name']}"
        if r["status"] != expected or r.get("flaky") or r.get("retry_failures", 0):
            issues.append(
                f"{label}: expected a clean {expected} observation, measured {r['status']} with {r.get('retry_failures', 0)} retry failures"
            )
        elif (
            before
            and expected == "failed"
            and "message_contains" in q
            and not r.get("symptom_matched")
        ):
            issues.append(
                f"{label}: baseline failure did not contain the requested symptom"
            )
    return issues


def isolated(gate, repo, commit, folder, process_options):
    from .checks import _single_gate
    from .engine import git

    with TemporaryDirectory(prefix="nw-proof-") as temp:
        copy = Path(temp) / "project"
        git(
            repo,
            "clone",
            "--local",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            str(repo),
            str(copy),
        )
        git(copy, "checkout", "--detach", commit)
        tree = git(copy, "rev-parse", "HEAD^{tree}")
        receipt = _single_gate(gate, copy, folder, tree, **process_options)
        git(copy, "add", "-A", "--", ".")
        if git(copy, "write-tree") != tree or git(copy, "rev-parse", "HEAD") != commit:
            receipt["source_error"] = "Verifier changed the isolated source tree"
    return receipt


def baseline_check(gate, repo, commit, folder, process_options):
    from .engine import git

    ref = git(repo, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}")
    samples, artifacts = [], []
    for i in range(1, gate["proof"]["samples"] + 1):
        sub = evidence_folder(folder, i)
        record = isolated(gate, repo, ref, sub, process_options)
        measured = record.get("junit")
        error = record.get("source_error") or record.get("artifact_error")
        if not error and (measured is None or record["exit"] not in (0, 1)):
            error = f"Baseline command did not finish a usable test run (exit {record['exit']})"
        issues = [] if error else issues_for(gate, measured, before=True)
        if (
            not error
            and not issues
            and samples
            and samples[0]["passed"]
            and [r.get("suite") for r in measured["tracked"]]
            != [r.get("suite") for r in samples[0]["tracked"]]
        ):
            issues.append(
                "Baseline samples selected different suite paths for the same contract"
            )
        samples.append(
            {
                "sample": i,
                "tree": record["tree"],
                "contract_sha256": record["contract_sha256"],
                "exit": record["exit"],
                "error": error,
                "issues": issues,
                "tracked": measured["tracked"] if measured else [],
                "passed": not error and not issues,
            }
        )
        for artifact in record["artifacts"]:
            artifacts.append({**artifact, "name": f"baseline-{i}/{artifact['name']}"})
        log = Path(record["log"])
        if log.is_file():
            from hashlib import file_digest

            with log.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
            artifacts.append(
                {"name": f"baseline-{i}/gate.log", "path": str(log), "sha256": digest}
            )
    passed = all(s["passed"] for s in samples)
    status = (
        "inconclusive"
        if any(s["error"] for s in samples)
        else "reproduced"
        if passed
        else "not_reproduced"
    )
    return {
        "commit": ref,
        "samples": samples,
        "passed": passed,
        "status": status,
        "pinned_inputs": gate.get("pinned_inputs", []),
    }, artifacts


def apply_proof(record, gate, baseline, artifacts):
    record["artifacts"].extend(artifacts)
    record["proof"] = {"baseline": baseline, "passed": False}
    if not baseline["passed"]:
        bad = next(s for s in baseline["samples"] if not s["passed"])
        reason = bad["error"] or "; ".join(bad["issues"][:3])
        record.update(
            execution_error=True,
            artifact_error=f"Baseline did not establish the requested proof: {reason}",
        )
        return
    if measured := record.get("junit"):
        issues = issues_for(gate, measured, before=False)
        for old, new in zip(baseline["samples"][0]["tracked"], measured["tracked"]):
            if new["status"] == "passed" and tuple(new["suite"]) != tuple(old["suite"]):
                issues.append(
                    f"{new['classname']} / {new['name']}: suite identity changed between revisions"
                )
        if issues:
            measured["passed"] = False
            combined = issues + measured["issues"]
            measured["issues"] = combined[:12]
            measured["omitted_issues"] += max(0, len(combined) - 12)
    record["proof"]["passed"] = observed_pass(record) is True


def select_gate(spec, milestone, name):
    if not 1 <= milestone <= len(spec["milestones"]):
        raise ValueError("Choose a milestone in this plan")
    for gate in spec["milestones"][milestone - 1]["gates"]:
        if gate["name"] == name and "proof" in gate:
            return gate
    raise ValueError("Choose a named gate with a proof contract in this milestone")


def compare(store, spec, milestone, name, before, after=None):
    from .engine import git

    gate = select_gate(spec, milestone, name)
    repo = Path(spec["repository"])
    base = git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        (before or spec.get("expected_base", "HEAD")) + "^{commit}",
    )
    candidate = (
        git(repo, "rev-parse", "--verify", "--end-of-options", after + "^{commit}")
        if after is not None
        else None
    )
    folder = store.root / "comparisons" / uuid4().hex
    folder.mkdir(parents=True)
    opts = {
        "max_log_bytes": spec.get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024)
    }
    start = monotonic()
    baseline, artifacts = baseline_check(gate, repo, base, folder / "baseline", opts)
    result = {
        "baseline": baseline,
        "artifacts": artifacts,
        "passed": baseline["passed"],
        "model_calls": 0,
        "gate": name,
        "evidence": str(folder),
        "candidate": candidate,
        "samples": [],
    }
    if candidate:
        for i in range(1, gate.get("stability_runs", 1) + 1):
            record = isolated(
                gate, repo, candidate, evidence_folder(folder / "candidate", i), opts
            )
            apply_proof(record, gate, baseline, [])
            result["samples"].append(record)
        result["passed"] = all(observed_pass(r) is True for r in result["samples"])
    if not baseline["passed"] or not candidate:
        result["status"] = baseline["status"]
    elif any(observed_pass(r) is None for r in result["samples"]):
        result["status"] = "inconclusive"
    else:
        results = {observed_pass(r) for r in result["samples"]}
        result["status"] = (
            "mixed"
            if len(results) > 1
            else "verified"
            if result["passed"]
            else "candidate_failed"
        )
    result["duration_ms"] = round((monotonic() - start) * 1000)
    write_json(folder / "comparison.json", result)
    return result


def case_history(store, run_id, query):
    from .junit import selector

    query = selector(query)
    root = (store.directory(run_id) / "evidence").resolve()
    spec = store.get(run_id)["spec"]
    rows = []
    for ev in store.events(run_id, limit=None, kinds=["gate_finished"]):
        d = ev["details"]
        step, idx = d.get("step"), d.get("gate_index")
        if (
            not isinstance(step, int)
            or not isinstance(idx, int)
            or not (1 <= step <= len(spec["milestones"]))
        ):
            continue
        gates = spec["milestones"][step - 1]["gates"]
        if not 1 <= idx <= len(gates) or "junit" not in gates[idx - 1]:
            continue
        gate = gates[idx - 1]
        row = {
            "event": ev["seq"],
            "step": step,
            "attempt": d.get("attempt"),
            "sample": d.get("sample", 1),
            "gate": d["name"],
            "tree": d["tree"],
        }
        try:
            a = next(
                (a for a in d.get("artifacts", []) if a["name"] == "junit.xml"), None
            )
            if not a:
                raise ValueError("JUnit artifact was not recorded")
            p = Path(a["path"]).resolve(strict=True)
            if (
                not p.is_relative_to(root)
                or not p.is_file()
                or p.stat().st_size > gate["max_artifact_bytes"]
            ):
                raise ValueError("Invalid stored JUnit artifact")
            with p.open("rb") as stream:
                data = stream.read(gate["max_artifact_bytes"] + 1)
            if sha256(data).hexdigest() != a["sha256"]:
                raise ValueError("Stored JUnit digest mismatch")
            row["case"] = grade(data, {"min_passed": 1}, [query])["tracked"][0]
            row["gate_passed"] = observed_pass(d)
        except (OSError, ValueError) as err:
            row["error"] = str(err)
        rows.append(row)
    from .safety import redact

    return redact(
        {"run_id": run_id, "selector": query, "observations": rows, "model_calls": 0}
    )
