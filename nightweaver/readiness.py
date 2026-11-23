"""Check the routes selected by a plan before starting a run."""

from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from uuid import uuid4

from .engine import identity_error, source_info
from .providers import ProviderUnavailable, advisory, check_provider, claude_auth, describe_provider, invoke


def check_route(store, config, cwd, live):
    result = describe_provider(config)
    if config["kind"] == "claude":
        try:
            result["auth"] = claude_auth(config)[1]  # the source only; the token itself is never reported
        except ProviderUnavailable:
            result["auth"] = "oauth_token_file"
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


def ollama_readiness(config, live=False):
    endpoint = f"http://127.0.0.1:{config['port']}"
    result = {"provider": "ollama", "model": config["model"], "endpoint": endpoint, "roles": ["local advice"]}
    if "think" in config:
        result["think"] = config["think"]
    try:
        with urlopen(endpoint + "/api/tags", timeout=3) as response:
            models = [item["name"] for item in json.load(response)["models"]]
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "status": "unverified" if live else "unavailable", "action": "Start Ollama on the configured loopback port."}
    name = config["model"]
    if name in models or f"{name}:latest" in models:
        if live:
            receipt = {}
            try:
                answer = advisory(config, "Reply with the single word READY.", receipt=receipt)
            except (OSError, ValueError) as error:
                return {**result, "status": "unverified", "action": str(error), "inference": receipt}
            passed = answer.strip().casefold().rstrip(".") == "ready" and receipt["model"] in (name, name + ":latest")
            return {**result, "status": "ready" if passed else "unverified", "inference": receipt}
        return {**result, "status": "available", "action": "The server lists this model. Inference is checked during the run."}
    return {**result, "status": "unverified" if live else "unavailable", "action": "Install the selected model or change advisory_ollama.model."}


INSTALL_CHROMIUM = "python -m playwright install chromium"
# channel -> (Windows path below a Program Files folder, macOS app name, Linux path, Linux command)
BROWSER_INSTALLS = {
    "msedge": ("Microsoft/Edge/Application/msedge.exe", "Microsoft Edge", "/opt/microsoft/msedge/msedge", "microsoft-edge"),
    "chrome": ("Google/Chrome/Application/chrome.exe", "Google Chrome", "/opt/google/chrome/chrome", "google-chrome"),
}


def installed_channel(channel):
    """Find a system browser at its standard install location without starting it."""
    windows, app, linux, command = BROWSER_INSTALLS[channel]
    if sys.platform == "win32":
        bases = [os.environ.get(key) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [Path(base, windows) for base in bases if base]
    elif sys.platform == "darwin":
        candidates = [Path(f"/Applications/{app}.app/Contents/MacOS/{app}")]
    else:
        candidates = [Path(linux)]
    found = next((str(path) for path in candidates if path.is_file()), None)
    return found or (shutil.which(command) if sys.platform.startswith("linux") else None)


def playwright_cache(package):
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        return package / "driver/package/.local-browsers"
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"


def browser_readiness():
    """Report whether browser gates can run, from the install folders alone. No browser is started."""
    result = {"playwright": {"installed": False}, "bundled": {"present": False}, "channels": {}, "optional": True}
    spec = None
    try:
        spec = find_spec("playwright")
    except (ImportError, ValueError):
        pass
    if spec is None or not spec.submodule_search_locations:
        return {**result, "status": "unavailable", "action": "Optional. Browser gates need Playwright: python -m pip install playwright, then " + INSTALL_CHROMIUM}
    try:
        result["playwright"] = {"installed": True, "version": version("playwright")}
    except PackageNotFoundError:
        result["playwright"] = {"installed": True, "version": None}
    # The default launch is headless, so it needs the headless shell build that this Playwright release pins.
    revisions, package = {}, Path(next(iter(spec.submodule_search_locations)))
    try:
        manifest = package / "driver/package/browsers.json"
        revisions = {b["name"]: b["revision"] for b in json.loads(manifest.read_text(encoding="utf-8"))["browsers"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    cache = playwright_cache(package)
    name, pattern, folder = (("chromium-headless-shell", "*/chrome-headless-shell*", "chromium_headless_shell")
                             if "chromium-headless-shell" in revisions else ("chromium", "*/chrome*", "chromium"))
    wanted = revisions.get(name)
    try:
        found = sorted(path.name.rpartition("-")[2] for path in cache.glob(folder + "-*") if next(path.glob(pattern), None))
    except OSError:
        found = []
    bundled = {"present": bool(found) if wanted is None else wanted in found, "expected_revision": wanted,
               "installed_revisions": found, "folder": str(cache)}
    result["bundled"] = bundled
    result["channels"] = {channel: installed_channel(channel) for channel in BROWSER_INSTALLS}
    system = next((channel for channel, path in result["channels"].items() if path), None)
    if bundled["present"]:
        return {**result, "status": "ready", "channel": "bundled"}
    hint = INSTALL_CHROMIUM + (f" (this Playwright expects revision {wanted}, found {', '.join(found) or 'none'})" if wanted else "")
    if system and os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):  # The same rule as browser.fallback_allowed.
        return {**result, "status": "unavailable", "action": f"PLAYWRIGHT_BROWSERS_PATH is set, so the installed {system} is not used as a fallback. "
                f"Install Chromium there with {hint}, or unset PLAYWRIGHT_BROWSERS_PATH."}
    if system:
        return {**result, "status": "ready_fallback", "channel": system,
                "action": f"Bundled Chromium is missing, so browser gates use the installed {system}, except gates with visual_match "
                f"baselines, which fail closed because pixels differ between browsers. To use Chromium instead: {hint}"}
    return {**result, "status": "unavailable", "action": f"Install a browser: {hint}, or install Microsoft Edge or Google Chrome"}


def identity_readiness(repository):
    """Whether git can author the commits Nightweaver makes in the repository: promotion stops with 'Author identity unknown' otherwise."""
    problem = identity_error(repository)
    return {"status": "blocked", "action": problem} if problem else {"status": "ready"}


def diagnose(store, spec=None, planner=None, live=False):
    report = {"data_directory": str(store.root),
              "executables": {key: shutil.which(key) for key in ("git", "claude", "codex", "gemini", "ollama")},
              "browser": browser_readiness()}
    if not spec and not planner:
        return report
    roles = []
    if spec:
        report["source"] = source_info(store, spec)
        report["identity"] = identity_readiness(report["source"]["repository"])
        if "project" in spec:
            report["source"]["project"] = spec["project"]
        roles = [(f"worker {index}: {step['title']}", step["worker"])
                 for index, step in enumerate(spec["milestones"], 1)]
        if "reviewer" in spec:
            roles.append(("reviewer", spec["reviewer"]))
        for index, step in enumerate(spec["milestones"], 1):
            roles.extend((f"worker {index} fallback {number}", route) for number, route in enumerate(step.get("worker_fallbacks", []), 1))
        roles.extend((f"reviewer fallback {number}", route) for number, route in enumerate(spec.get("reviewer_fallbacks", []), 1))
        if pursue := spec.get("pursue"):
            roles.append(("pursuit judge", pursue["judge"]))
            roles.extend((f"pursuit judge fallback {number}", route) for number, route in enumerate(pursue.get("judge_fallbacks", []), 1))
            if pursue.get("confirm", True) and pursue.get("confirm_judge"):
                roles.append(("pursuit confirmation judge", pursue["confirm_judge"]))
            roles.append(("pursuit planner", pursue["planner"]))
            roles.extend((f"exploration worker {number}", route) for number, route in enumerate(pursue.get("explore", {}).get("workers", []), 1))
            roles.extend((f"rescue worker {number}", route) for number, route in enumerate(pursue.get("rescue", {}).get("workers", []), 1))
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
    claude = next((p for p in report["providers"] if p.get("provider") == "claude"), None)
    report["claude_auth"] = claude["auth"] if claude else claude_auth()[1]
    if spec and "advisory_ollama" in spec:
        report["providers"].append(ollama_readiness(spec["advisory_ollama"], live))
    return report
