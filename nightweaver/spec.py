"""Parse the operator's explicit project and verification plan."""

import json
from pathlib import Path, PurePosixPath
import re


IMPLEMENTERS = {"codex", "claude", "deepseek", "command"}
REVIEWERS = {"codex", "claude", "deepseek", "command"}


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
    if not isinstance(value, dict) or value.get("kind") not in choices:
        raise ValueError(f"Invalid {label} adapter")
    out = {"kind": value["kind"]}
    if value["kind"] == "command":
        out["argv"] = command(value.get("argv"), label)
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
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Artifact paths use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in value:
        raise ValueError(f"Artifact must be relative to the worktree: {value}")
    return path.as_posix()


def gate_spec(item):
    timeout = item.get("timeout", 120)
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ValueError("Gate timeout must be between 1 and 7200 seconds")
    return {"name": text(item, "name"), "argv": command(item.get("argv"), "Gate"),
            "timeout": timeout, "artifacts": [artifact(path) for path in item.get("artifacts", [])]}


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
    repository, project = resolve_repository(raw, relative_to, store)
    timeout = raw.get("agent_timeout", 1800)
    if type(timeout) is not int or not 1 <= timeout <= 14400:
        raise ValueError("Agent timeout must be between 1 and 14400 seconds")
    retries = raw.get("attempts", 3)
    if type(retries) is not int or not 1 <= retries <= 10:
        raise ValueError("Attempts must be between 1 and 10")
    gates = [gate_spec(item) for item in raw.get("gates", [])]
    final_gates = [gate_spec(item) for item in raw.get("final_gates", [])]
    milestones = raw.get("milestones", [])
    steps = []
    for index, item in enumerate(milestones):
        current = gates + [gate_spec(g) for g in item.get("gates", [])]
        if index == len(milestones) - 1:
            current += final_gates
        names = [g["name"] for g in current]
        if not names or len(names) != len(set(names)):
            raise ValueError("Each milestone needs uniquely named checks")
        paths = item.get("paths", [])
        steps.append({"title": text(item, "title"), "task": text(item, "task"), "gates": current,
                      "paths": [artifact(path) for path in paths],
                      "worker": provider(item.get("worker") or raw.get("worker"), IMPLEMENTERS, "worker")})
    if not steps:
        raise ValueError("At least one milestone is required")
    out = {"repository": str(repository), "title": text(raw, "title"), "objective": text(raw, "objective"),
           "attempts": retries, "agent_timeout": timeout, "milestones": steps,
           "protected_paths": [artifact(path) for path in raw.get("protected_paths", [])]}
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
