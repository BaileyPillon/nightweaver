"""Check the routes selected by a plan before starting a run."""

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from uuid import uuid4

from .engine import source_info
from .providers import ProviderUnavailable, check_provider, describe_provider, invoke


def check_route(store, config, cwd, live):
    result = describe_provider(config)
    try:
        result["executable"] = check_provider(config, cwd)
    except ProviderUnavailable as error:
        return {**result, "status": "blocked", "reason": error.kind, "action": str(error)}

    if config["kind"] == "command":
        return {**result, "status": "configured", "action": "Executable found. Custom commands are checked during the run."}
    if not live:
        return {**result, "status": "configured", "action": "Use --live to verify model access."}

    log = store.root / "diagnostics" / f"{uuid4().hex[:12]}.jsonl"
    try:
        with TemporaryDirectory(prefix="probe-", dir=store.root) as probe:
            probe_config = {**config, "executable": result["executable"]}
            code, _, answer = invoke(probe_config, "probe", Path(probe), log, "Reply with the single word READY.",
                                     45, lambda: False)
    except ProviderUnavailable as error:
        return {**result, "status": "blocked", "reason": error.kind, "action": str(error), "log": str(log)}
    except OSError:
        return {**result, "status": "blocked", "reason": "launch_failed", "action": "Could not start the provider process.",
                "log": str(log)}
    if code == 0 and answer.strip().casefold().rstrip(".") == "ready":
        return {**result, "status": "ready", "log": str(log)}
    return {**result, "status": "unverified", "reason": "timeout" if code == 124 else "unexpected_response",
            "action": "Inspect the local probe log and check the selected CLI profile.", "log": str(log)}


def ollama_readiness(config):
    endpoint = f"http://127.0.0.1:{config['port']}"
    result = {"provider": "ollama", "model": config["model"], "endpoint": endpoint, "roles": ["local advice"]}
    try:
        with urlopen(endpoint + "/api/tags", timeout=3) as response:
            models = [item["name"] for item in json.load(response)["models"]]
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "status": "unavailable", "action": "Start Ollama on the configured loopback port."}
    name = config["model"]
    if name in models or f"{name}:latest" in models:
        return {**result, "status": "available", "action": "The server lists this model. Inference is checked during the run."}
    return {**result, "status": "unavailable", "action": "Install the selected model or change advisory_ollama.model."}


def diagnose(store, spec=None, planner=None, live=False):
    report = {"data_directory": str(store.root),
              "executables": {key: shutil.which(key) for key in ("git", "claude", "codex", "ollama")}}
    if not spec and not planner:
        return report
    roles = []
    if spec:
        report["source"] = source_info(store, spec)
        if "project" in spec:
            report["source"]["project"] = spec["project"]
        roles = [(f"worker {index}: {step['title']}", step["worker"])
                 for index, step in enumerate(spec["milestones"], 1)]
        if "reviewer" in spec:
            roles.append(("reviewer", spec["reviewer"]))
    if planner:
        roles.insert(0, ("planner", planner))
    routes = {}
    cwd = Path(report.get("source", {}).get("repository", store.root))
    for role, config in roles:
        key = json.dumps(config, sort_keys=True)
        if key not in routes:
            routes[key] = {**check_route(store, config, cwd, live), "roles": []}
        routes[key]["roles"].append(role)
    report["providers"] = list(routes.values())
    if spec and "advisory_ollama" in spec:
        report["providers"].append(ollama_readiness(spec["advisory_ollama"]))
    return report
