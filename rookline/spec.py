"""Parse the operator's explicit project and verification plan."""

import json
from pathlib import Path, PurePosixPath
import re


IMPLEMENTERS = {"codex", "claude", "command"}
REVIEWERS = {"codex", "claude", "command"}


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
    if value.get("profile_dir"):
        profile = Path(text(value, "profile_dir")).expanduser().resolve(strict=True)
        if not profile.is_dir() or value["kind"] == "command":
            raise ValueError("profile_dir must be an existing Claude or Codex configuration directory")
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


def validate(raw, relative_to=None):
    if not isinstance(raw, dict):
        raise ValueError("A plan must be a JSON object")
    root = Path(text(raw, "repository")).expanduser()
    if not root.is_absolute():
        root = (relative_to or Path.cwd()) / root
    repository = root.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("Repository must be a folder")
    timeout = raw.get("agent_timeout", 1800)
    if type(timeout) is not int or not 1 <= timeout <= 14400:
        raise ValueError("Agent timeout must be between 1 and 14400 seconds")
    retries = raw.get("attempts", 3)
    if type(retries) is not int or not 1 <= retries <= 10:
        raise ValueError("Attempts must be between 1 and 10")
    gates = [gate_spec(item) for item in raw.get("gates", [])]
    steps = []
    for item in raw.get("milestones", []):
        current = gates + [gate_spec(g) for g in item.get("gates", [])]
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


def read_plan(path):
    path = Path(path).resolve(strict=True)
    return validate(json.loads(path.read_text(encoding="utf-8-sig")), path.parent)
