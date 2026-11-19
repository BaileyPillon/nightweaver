"""Parse the operator's explicit project and verification plan."""

import json
from pathlib import Path, PurePosixPath
import re


IMPLEMENTERS = {"codex", "claude", "deepseek", "gemini", "command"}
REVIEWERS = IMPLEMENTERS


def fields(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - set(allowed.split())
    if unknown:
        raise ValueError(f"Unknown {label} fields: {', '.join(sorted(unknown))}")


def array(value, label, maximum=100):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a list of at most {maximum} items")
    return value


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    return value


def command(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a nonempty executable argument array")
    if Path(value[0]).suffix.casefold() in {".cmd", ".bat"}:
        raise ValueError(f"{label} must name an executable. Invoke PowerShell explicitly for scripts.")
    return value


def text(obj, key):
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise ValueError(f"Missing {key}")
    return obj[key].strip()


def provider(value, choices, label):
    fields(value, "kind argv executable model api_key api_key_file profile_dir unavailable_exit_code", label)
    if not isinstance(value, dict) or value.get("kind") not in choices:
        raise ValueError(f"Invalid {label} adapter")
    out = {"kind": value["kind"]}
    if value["kind"] == "command":
        if set(value) - {"kind", "argv", "unavailable_exit_code"}:
            raise ValueError("Unsupported command provider field")
        out["argv"] = command(value.get("argv"), label)
        if "unavailable_exit_code" in value:
            out["unavailable_exit_code"] = integer(value["unavailable_exit_code"], "unavailable_exit_code", 1, 123)
    elif "unavailable_exit_code" in value:
        raise ValueError("Only command providers support unavailable_exit_code")
    elif "argv" in value:
        raise ValueError("Only command providers accept argv")
    elif value.get("executable"):
        out["executable"] = text(value, "executable")
    if value.get("model"):
        out["model"] = text(value, "model")
    if "api_key" in value:
        raise ValueError("Use api_key_file or DEEPSEEK_API_KEY for credentials")
    if value.get("api_key_file"):
        if value["kind"] != "deepseek":
            raise ValueError("api_key_file is supported by the DeepSeek adapter")
        out["api_key_file"] = str(Path(text(value, "api_key_file")).expanduser().resolve())
    if value.get("profile_dir"):
        profile = Path(text(value, "profile_dir")).expanduser().resolve()
        if value["kind"] == "command":
            raise ValueError("profile_dir is supported by model providers")
        out["profile_dir"] = str(profile)
    return out


def artifact(value):
    if not isinstance(value, str) or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Artifact paths use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or any(c in value for c in ':*?"<>|'):
        raise ValueError(f"Artifact must be relative to the worktree: {value}")
    for part in path.parts:
        if part.endswith((" ", ".")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ValueError(f"Invalid Windows path component: {part}")
    return path.as_posix()


def gate_spec(item):
    fields(item, "name argv timeout artifacts kind browser max_artifact_bytes pinned_inputs", "gate")
    timeout = item.get("timeout", 120)
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ValueError("Gate timeout must be between 1 and 7200 seconds")
    artifacts = [artifact(path) for path in array(item.get("artifacts", []), "artifacts", 32)]
    if len({p.casefold() for p in artifacts}) != len(artifacts):
        raise ValueError("Duplicate artifact paths")
    result = {"name": text(item, "name"), "timeout": timeout, "artifacts": artifacts,
              "max_artifact_bytes": integer(item.get("max_artifact_bytes", 32 * 1024 * 1024), "max_artifact_bytes", 1, 256 * 1024 * 1024)}
    kind = item.get("kind", "command")
    pins = {}
    for entry in array(item.get("pinned_inputs", []), "pinned inputs", 32):
        fields(entry, "path sha256", "pinned input")
        name = text(entry, "path")
        digest = text(entry, "sha256").lower()
        if not Path(name).is_absolute():
            name = artifact(name)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or name.casefold() in pins:
            raise ValueError("Pinned inputs require distinct file paths and SHA-256 digests")
        pins[name.casefold()] = {"path": name, "sha256": digest}
    if pins:
        result["pinned_inputs"] = list(pins.values())
    if kind == "browser":
        from .browser import validate_browser
        if "argv" in item or artifacts:
            raise ValueError("Browser gates manage their own command and evidence")
        result.update(kind="browser", browser=validate_browser(item.get("browser")))
    elif kind == "command" and "browser" not in item:
        result["argv"] = command(item.get("argv"), "Gate")
    else:
        raise ValueError("Gate kind must be command or browser")
    return result


def resolve_repository(raw, relative_to=None, store=None):
    if not isinstance(raw, dict):
        raise ValueError("A plan must be a JSON object")
    project = None
    if "project" in raw:
        if store is None:
            raise ValueError("A registered project requires a Nightweaver runtime")
        project = store.project(text(raw, "project"))
    value = project["repository"] if project and "repository" not in raw else text(raw, "repository")
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = (relative_to or Path.cwd()) / root
    repository = root.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("Repository must be a folder")
    if project and repository != Path(project["repository"]):
        raise ValueError(f"Project {project['name']!r} is bound to {project['repository']}, but the plan selects {repository}")
    return repository, project


def validate(raw, relative_to=None, store=None):
    fields(raw, "schema_version repository project title objective attempts agent_timeout gates final_gates milestones "
           "worker reviewer protected_paths acceptance expected_base advisory_ollama budgets retry worker_fallbacks "
           "reviewer_fallbacks context_files repository_map source_issue", "plan")
    if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
        raise ValueError("Unsupported plan schema_version; expected 1")
    repository, project = resolve_repository(raw, relative_to, store)
    timeout = raw.get("agent_timeout", 1800)
    if type(timeout) is not int or not 1 <= timeout <= 14400:
        raise ValueError("Agent timeout must be between 1 and 14400 seconds")
    retries = raw.get("attempts", 3)
    if type(retries) is not int or not 1 <= retries <= 10:
        raise ValueError("Attempts must be between 1 and 10")
    gates = [gate_spec(item) for item in array(raw.get("gates", []), "gates")]
    final_gates = [gate_spec(item) for item in array(raw.get("final_gates", []), "final_gates")]
    milestones = array(raw.get("milestones", []), "milestones", 50)
    steps = []
    for index, item in enumerate(milestones):
        fields(item, "title task gates paths worker worker_fallbacks", "milestone")
        current = gates + [gate_spec(g) for g in array(item.get("gates", []), "milestone gates")]
        if index == len(milestones) - 1:
            current += final_gates
        names = [g["name"] for g in current]
        if not names or len(names) != len(set(names)):
            raise ValueError("Each milestone needs uniquely named checks")
        paths = array(item.get("paths", []), "paths")
        steps.append({"title": text(item, "title"), "task": text(item, "task"), "gates": current,
                      "paths": [artifact(path) for path in paths],
                      "worker": provider(item.get("worker") or raw.get("worker"), IMPLEMENTERS, "worker"),
                      "worker_fallbacks": [provider(p, IMPLEMENTERS, "worker fallback") for p in
                                           array(item.get("worker_fallbacks", raw.get("worker_fallbacks", [])), "worker_fallbacks", 5)]})
    if not steps:
        raise ValueError("At least one milestone is required")
    out = {"schema_version": 1, "repository": str(repository), "title": text(raw, "title"), "objective": text(raw, "objective"),
           "attempts": retries, "agent_timeout": timeout, "milestones": steps,
           "protected_paths": [artifact(path) for path in array(raw.get("protected_paths", []), "protected_paths")]}
    out["reviewer_fallbacks"] = [provider(p, REVIEWERS, "reviewer fallback") for p in
                                 array(raw.get("reviewer_fallbacks", []), "reviewer_fallbacks", 5)]
    if out["reviewer_fallbacks"] and not raw.get("reviewer"):
        raise ValueError("reviewer_fallbacks require a reviewer")
    out["context_files"] = [artifact(p) for p in array(raw.get("context_files", []), "context_files", 20)]
    if type(raw.get("repository_map", False)) is not bool:
        raise ValueError("repository_map must be true or false")
    out["repository_map"] = raw.get("repository_map", False)
    budgets = raw.get("budgets", {})
    fields(budgets, "max_calls max_seconds max_observed_tokens idle_timeout max_log_bytes", "budgets")
    bounds = {"max_calls": (1, 10000), "max_seconds": (1, 604800), "max_observed_tokens": (1, 1000000000),
              "idle_timeout": (1, 14400), "max_log_bytes": (1024, 256 * 1024 * 1024)}
    out["budgets"] = {key: integer(value, key, *bounds[key]) for key, value in budgets.items()}
    retry = raw.get("retry", {})
    fields(retry, "initial_seconds max_seconds transient_attempts repeated_failure_limit", "retry")
    defaults = {"initial_seconds": 2, "max_seconds": 60, "transient_attempts": 2, "repeated_failure_limit": 3}
    out["retry"] = {key: integer(retry.get(key, value), key, 1 if key in ("initial_seconds", "max_seconds") else 0,
                                  3600 if key in ("initial_seconds", "max_seconds") else 20)
                    for key, value in defaults.items()}
    if out["retry"]["max_seconds"] < out["retry"]["initial_seconds"]:
        raise ValueError("retry max_seconds must be at least initial_seconds")
    if "source_issue" in raw:
        fields(raw["source_issue"], "url number repository", "source_issue")
        out["source_issue"] = {"url": text(raw["source_issue"], "url"), "repository": text(raw["source_issue"], "repository"),
                               "number": integer(raw["source_issue"].get("number"), "issue number", 1, 2147483647)}
    if project:
        out["project"] = project["name"]
        out["git_common_dir"] = project["git_common_dir"]
    criteria = raw.get("acceptance", [])
    if not isinstance(criteria, list) or any(not isinstance(item, str) or not item.strip() for item in criteria):
        raise ValueError("acceptance must be a list of nonempty requirements")
    out["acceptance"] = [item.strip() for item in criteria]
    if "expected_base" in raw:
        base = text(raw, "expected_base").lower()
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base):
            raise ValueError("expected_base must be a full Git commit hash")
        out["expected_base"] = base
    if raw.get("reviewer"):
        out["reviewer"] = provider(raw["reviewer"], REVIEWERS, "reviewer")
    if raw.get("advisory_ollama"):
        fields(raw["advisory_ollama"], "model port timeout max_tokens", "advisory_ollama")
        model = text(raw["advisory_ollama"], "model")
        if not re.fullmatch(r"[a-zA-Z0-9_.:/-]+", model):
            raise ValueError("Invalid Ollama model name")
        port = raw["advisory_ollama"].get("port", 11434)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid Ollama loopback port")
        local_timeout = raw["advisory_ollama"].get("timeout", 90)
        max_tokens = raw["advisory_ollama"].get("max_tokens", 100)
        if type(local_timeout) is not int or not 1 <= local_timeout <= 600:
            raise ValueError("Ollama timeout must be between 1 and 600 seconds")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1024:
            raise ValueError("Ollama max_tokens must be between 1 and 1024")
        out["advisory_ollama"] = {"model": model, "port": port, "timeout": local_timeout, "max_tokens": max_tokens}
    return out


def read_plan(path, store=None):
    path = Path(path).resolve(strict=True)
    return validate(json.loads(path.read_text(encoding="utf-8-sig")), path.parent, store)
