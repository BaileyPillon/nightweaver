"""Inspect the constraints the Windows runner actually enforces."""

import json

from .checks import observed_pass
from .spec import artifact


def under(path, roots):
    path = path.casefold()
    return any(path == root.casefold() or path.startswith(root.casefold() + "/") for root in roots)


def scope_report(spec, paths):
    rows = []
    for raw in paths:
        name = artifact(raw)
        protected = under(name, spec["protected_paths"])
        permitted = [
            i
            for i, step in enumerate(spec["milestones"], 1)
            if not protected and (not step["paths"] or under(name, step["paths"]))
        ]
        rows.append({"path": name, "protected": protected, "permitted_milestones": permitted})
    return {"paths": rows, "model_calls": 0}


def lint_plan(spec):
    findings = []
    steps = spec["milestones"]
    minimum_calls = len(steps) * (
        1 + int(bool(spec.get("reviewer"))) + int(bool(spec.get("advisory_ollama")))
    )
    if (limit := spec["budgets"].get("max_calls")) is not None and limit < minimum_calls:
        findings.append(
            {"severity": "error", "code": "insufficient_calls", "minimum": minimum_calls, "configured": limit}
        )
    for i, step in enumerate(steps, 1):
        if step["paths"] and all(under(p, spec["protected_paths"]) for p in step["paths"]):
            findings.append({"severity": "error", "code": "entire_scope_protected", "step": i})
        for role, routes in (
            ("worker", [step["worker"], *step["worker_fallbacks"]]),
            ("reviewer", [spec.get("reviewer"), *spec["reviewer_fallbacks"]]),
        ):
            keys = [json.dumps(r, sort_keys=True) for r in routes if r]
            if len(set(keys)) != len(keys):
                findings.append({"severity": "warning", "code": "repeated_route", "role": role, "step": i})
        configured = sum(g.get("stability_runs", 1) * g["timeout"] for g in step["gates"])
        if (wall := spec["budgets"].get("max_seconds")) and configured > wall:
            findings.append(
                {
                    "severity": "warning",
                    "code": "gate_timeouts_exceed_run_budget",
                    "step": i,
                    "configured_seconds": configured,
                    "run_seconds": wall,
                }
            )
    return {
        "minimum_calls_for_acceptance": minimum_calls,
        "milestones": len(steps),
        "findings": findings,
        "feasible": not any(f["severity"] == "error" for f in findings),
        "model_calls": 0,
    }


def catalog_history(store, run_id):
    spec = store.get(run_id)["spec"]
    controls = {
        (i, j)
        for i, s in enumerate(spec["milestones"], 1)
        for j, g in enumerate(s["gates"], 1)
        if g.get("kind") == "catalog"
    }
    observations = []
    for event in store.events(run_id, limit=None, kinds=["gate_finished"]):
        detail = event["details"]
        if (detail.get("step"), detail.get("gate_index")) not in controls:
            continue
        result = detail.get("catalog", {})
        observations.append(
            {
                "event": event["seq"],
                "step": detail["step"],
                "attempt": detail.get("attempt"),
                "sample": detail.get("sample", 1),
                "gate": detail["name"],
                "tree": detail.get("tree"),
                "passed": observed_pass(detail),
                "baseline_records": result.get("baseline_records"),
                "candidate_records": result.get("candidate_records"),
                "added_ids": result.get("added_ids"),
                "removed_ids": result.get("removed_ids"),
                "violations": result.get("violations", {}),
            }
        )
    return {"run_id": run_id, "observations": observations}


def inspect_catalog(spec, milestone, name, revision):
    from .catalog import check_catalog
    from .engine import git

    if not 1 <= milestone <= len(spec["milestones"]):
        raise ValueError("Choose a milestone in this plan")
    gates = [
        g
        for g in spec["milestones"][milestone - 1]["gates"]
        if g.get("kind") == "catalog" and (name is None or g["name"] == name)
    ]
    if not gates:
        raise ValueError("No matching catalog checks in the selected milestone")
    repo = spec["repository"]
    base = git(
        repo, "rev-parse", "--verify", "--end-of-options", spec.get("expected_base", "HEAD") + "^{commit}"
    )
    tree = git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{tree}")
    results = [{"gate": g["name"], **check_catalog(repo, base, tree, g["catalog"])} for g in gates]
    return {
        "base": base,
        "tree": tree,
        "checks": results,
        "passed": all(r["passed"] for r in results),
        "model_calls": 0,
    }
