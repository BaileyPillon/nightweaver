"""Parse the operator's explicit project and verification plan."""

import json
import math
from pathlib import Path, PurePosixPath
import re


IMPLEMENTERS = {"codex", "claude", "deepseek", "gemini", "command"}
REVIEWERS = IMPLEMENTERS
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def strict_json(data, *, parse_float=None):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"Duplicate JSON property: {key[:100]}")
            obj[key] = value
        return obj

    def invalid(value):
        raise ValueError(f"Nonstandard JSON number: {value}")

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            return invalid(value)
        return number

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=invalid, parse_float=parse_float or finite)
    except RecursionError as error:
        raise ValueError("JSON nesting exceeds the supported depth") from error


def read_json(path):
    return strict_json(Path(path).read_text(encoding="utf-8-sig"))


def scoped_milestones(raw):
    steps = raw.get("milestones", [])
    return bool(steps and any(isinstance(s, dict) and
                              {"paths", "gates", "worker", "worker_fallbacks", "retain_gates"} & s.keys() for s in steps))


def anchor_provider_paths(raw, folder):
    """A generated plan might be opened outside the template's directory."""
    pursue = raw.get("pursue")
    if isinstance(pursue, dict):
        if isinstance(pursue.get("references"), list):
            pursue["references"] = [str((folder / Path(item).expanduser()).resolve()) if isinstance(item, str) and item.strip() else item
                                    for item in pursue["references"]]
        explore, rescue = pursue.get("explore"), pursue.get("rescue")
        routes = [pursue.get("judge"), pursue.get("planner"), *array(pursue.get("judge_fallbacks", []), "judge_fallbacks", 5),
                  *(array(explore.get("workers", []), "explore workers", 4) if isinstance(explore, dict) else []),
                  *(array(rescue.get("workers", []), "rescue workers", 4) if isinstance(rescue, dict) else [])]
        for route in routes:
            if isinstance(route, dict):
                for key in ("profile_dir", "api_key_file", "oauth_token_file"):
                    if key in route:
                        route[key] = str((folder / Path(text(route, key)).expanduser()).resolve())
    for section in [raw, *array(raw.get("milestones", []), "milestones", 50)]:
        if not isinstance(section, dict):
            raise ValueError("A milestone must be an object")
        routes = [section.get("worker"), section.get("reviewer")]
        for key in ("worker_fallbacks", "reviewer_fallbacks"):
            routes.extend(array(section.get(key, []), key, 5))
        for route in routes:
            if isinstance(route, dict):
                for key in ("profile_dir", "api_key_file", "oauth_token_file"):
                    if key in route:
                        route[key] = str((folder / Path(text(route,key)).expanduser()).resolve())


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


def provider(value, choices, label, relative_to=None):
    fields(value, "kind argv executable model effort api_key api_key_file oauth_token_file profile_dir unavailable_exit_code inherit_credentials", label)
    if not isinstance(value, dict) or value.get("kind") not in choices:
        raise ValueError(f"Invalid {label} adapter")
    out = {"kind": value["kind"]}
    if value["kind"] == "command":
        if set(value) - {"kind", "argv", "unavailable_exit_code", "inherit_credentials"}:
            raise ValueError("Unsupported command provider field")
        out["argv"] = command(value.get("argv"), label)
        if "inherit_credentials" in value:
            if type(value["inherit_credentials"]) is not bool:
                raise ValueError("inherit_credentials must be true or false")
            if value["inherit_credentials"]:
                out["inherit_credentials"] = True
        if "unavailable_exit_code" in value:
            out["unavailable_exit_code"] = integer(value["unavailable_exit_code"], "unavailable_exit_code", 1, 123)
    elif "unavailable_exit_code" in value:
        raise ValueError("Only command providers support unavailable_exit_code")
    elif "inherit_credentials" in value:
        raise ValueError("Only command providers support inherit_credentials")
    elif "argv" in value:
        raise ValueError("Only command providers accept argv")
    elif value.get("executable"):
        out["executable"] = text(value, "executable")
    if value.get("model"):
        out["model"] = text(value, "model")
    if "effort" in value:
        levels = {"claude": ("low", "medium", "high", "xhigh", "max"),
                  "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
                  "deepseek": ("low", "high", "max")}
        if value["effort"] not in levels.get(value["kind"], ()):
            raise ValueError(f"Unsupported effort for {value['kind']}: {value['effort']}")
        out["effort"] = value["effort"]
    if "api_key" in value:
        raise ValueError("Use api_key_file or DEEPSEEK_API_KEY for credentials")
    if value.get("api_key_file"):
        if value["kind"] != "deepseek":
            raise ValueError("api_key_file is supported by the DeepSeek adapter")
        out["api_key_file"] = str(((relative_to or Path.cwd()) / Path(text(value, "api_key_file")).expanduser()).resolve())
    if value.get("oauth_token_file"):
        if value["kind"] != "claude":
            raise ValueError("oauth_token_file is supported by the Claude adapter")
        out["oauth_token_file"] = str(((relative_to or Path.cwd()) / Path(text(value, "oauth_token_file")).expanduser()).resolve())
    if value.get("profile_dir"):
        profile = ((relative_to or Path.cwd()) / Path(text(value, "profile_dir")).expanduser()).resolve()
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
    fields(item, "name argv timeout artifacts kind browser catalog junit proof max_artifact_bytes pinned_inputs stability_runs", "gate")
    timeout = item.get("timeout", 120)
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ValueError("Gate timeout must be between 1 and 7200 seconds")
    artifacts = [artifact(path) for path in array(item.get("artifacts", []), "artifacts", 32)]
    if len({p.casefold() for p in artifacts}) != len(artifacts):
        raise ValueError("Duplicate artifact paths")
    result = {"name": text(item, "name"), "timeout": timeout, "artifacts": artifacts,
              "max_artifact_bytes": integer(item.get("max_artifact_bytes", 32 * 1024 * 1024), "max_artifact_bytes", 1, 256 * 1024 * 1024)}
    kind = item.get("kind", "command")
    if "proof" in item:
        from .proof import proof_contract
        if kind != "command" or "junit" not in item:
            raise ValueError("A proof requires a JUnit command gate")
        result["proof"] = proof_contract(item["proof"])
    if "junit" in item:
        from .junit import contract
        if kind != "command":
            raise ValueError("JUnit settings require a command gate")
        result["junit"] = contract(item["junit"])
    samples = integer(item.get("stability_runs", 1), "stability_runs", 1, 5)
    if samples > 1:
        result["stability_runs"] = samples
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
    if kind == "catalog":
        from .catalog import validate_contract
        if "argv" in item or "browser" in item or artifacts:
            raise ValueError("Catalog checks manage their own inputs and evidence")
        result.update(kind="catalog", catalog=validate_contract(item.get("catalog")))
    elif "catalog" in item:
        raise ValueError("Only catalog gates accept catalog settings")
    elif kind == "browser":
        from .browser import validate_browser
        if "argv" in item or artifacts:
            raise ValueError("Browser gates manage their own command and evidence")
        result.update(kind="browser", browser=validate_browser(item.get("browser")))
    elif kind == "command" and "browser" not in item:
        result["argv"] = command(item.get("argv"), "Gate")
    else:
        raise ValueError("Gate kind must be command, browser or catalog")
    return result


def exploration(raw, relative_to):
    """The design-direction tournament that runs before milestone 1. Workers default to milestone 1's worker (filled in by validate)."""
    fields(raw, "directions workers brief comparisons", "pursue.explore")
    out = {"directions": integer(raw.get("directions", 3), "directions", 2, 4)}
    if "workers" in raw:
        workers = array(raw["workers"], "explore workers", 4)
        if not workers:
            raise ValueError("explore workers must list at least one worker")
        out["workers"] = [provider(item, IMPLEMENTERS, "explore worker", relative_to) for item in workers]
    if "brief" in raw:
        if not isinstance(raw["brief"], str) or not raw["brief"].strip() or len(raw["brief"]) > 4000:
            raise ValueError("explore brief must be 1 to 4000 characters of guidance on how the directions should differ")
        out["brief"] = raw["brief"].strip()
    if raw.get("comparisons", "pairwise") not in ("pairwise", "single"):
        raise ValueError("explore comparisons must be pairwise or single")
    out["comparisons"] = raw.get("comparisons", "pairwise")
    return out


def rescuing(raw, relative_to):
    """The plateau rescue that runs when progress stalls: a diagnosis, N strategies forked from the best build and a tournament.

    Workers default to the last milestone's worker, chosen when a rescue starts. max_rescues 0 turns the rescue off."""
    fields(raw, "strategies max_rescues workers", "pursue.rescue")
    out = {"strategies": integer(raw.get("strategies", 3), "strategies", 2, 4), "max_rescues": integer(raw.get("max_rescues", 1), "max_rescues", 0, 3)}
    if "workers" in raw:
        workers = array(raw["workers"], "rescue workers", 4)
        if not workers:
            raise ValueError("rescue workers must list at least one worker")
        out["workers"] = [provider(item, IMPLEMENTERS, "rescue worker", relative_to) for item in workers]
    return out


def pursuit(raw, relative_to, acceptance):
    fields(raw, "judge judge_fallbacks planner max_waves wave_size target_score plateau regression_margin references criteria explore rescue", "pursue")
    if "judge" not in raw:
        raise ValueError("pursue requires a judge")
    judge = provider(raw["judge"], REVIEWERS, "judge", relative_to)
    references = []
    for item in array(raw.get("references", []), "references", 6):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("references must be paths of image files")
        path = (relative_to or Path.cwd()) / Path(item.strip()).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"Reference image not found: {item}") from error
        if not path.is_file() or path.suffix.casefold() not in IMAGE_SUFFIXES:
            raise ValueError(f"Reference images must be .png, .jpg, .jpeg or .webp files: {item}")
        if str(path) not in references:
            references.append(str(path))
    criteria = raw.get("criteria", acceptance)
    if not isinstance(criteria, list) or any(not isinstance(item, str) or not item.strip() for item in criteria):
        raise ValueError("pursue criteria must be a list of nonempty strings")
    return {"judge": judge, **({"explore": exploration(raw["explore"], relative_to)} if "explore" in raw else {}),
            "rescue": rescuing(raw.get("rescue", {}), relative_to),
            "judge_fallbacks": [provider(p, REVIEWERS, "judge fallback", relative_to) for p in array(raw.get("judge_fallbacks", []), "judge_fallbacks", 5)],
            "planner": provider(raw["planner"], IMPLEMENTERS, "pursuit planner", relative_to) if "planner" in raw else dict(judge),
            "max_waves": integer(raw.get("max_waves", 3), "max_waves", 1, 20),
            "wave_size": integer(raw.get("wave_size", 3), "wave_size", 1, 5),
            "target_score": integer(raw.get("target_score", 85), "target_score", 0, 100),
            "plateau": integer(raw.get("plateau", 2), "plateau", 1, 5),
            "regression_margin": integer(raw.get("regression_margin", 5), "regression_margin", 0, 50),
            "references": references, "criteria": [item.strip() for item in criteria]}


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
    if project and repository != Path(project["repository"]).resolve():
        raise ValueError(f"Project {project['name']!r} is bound to {project['repository']}, but the plan selects {repository}")
    return repository, project


def validate(raw, relative_to=None, store=None):
    fields(raw, "schema_version repository project title objective attempts agent_timeout gates final_gates milestones "
           "worker reviewer protected_paths acceptance expected_base advisory_ollama budgets retry worker_fallbacks "
           "reviewer_fallbacks context_files repository_map source_issue repair pursue git_timeout", "plan")
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
    retained = []
    for index, item in enumerate(milestones):
        fields(item, "title task gates paths worker worker_fallbacks retain_gates", "milestone")
        own = [gate_spec(g) for g in array(item.get("gates", []), "milestone gates")]
        retention = item.get("retain_gates", "local")
        if retention not in ("local", "final", "subsequent") or (retention != "local" and not own):
            raise ValueError("retain_gates must be local, final or subsequent and retention needs milestone gates")
        current = gates + own
        if index == len(milestones) - 1:
            current += final_gates
        names = [g["name"] for g in current]
        if len(names) != len(set(names)):
            raise ValueError("Each milestone needs uniquely named checks")
        by_name = {g["name"]: g for g in current}
        inherited = {}
        for gate, policy, source in retained:
            if policy == "final" and index != len(milestones) - 1:
                continue
            name = gate["name"]
            if name in by_name and gate != by_name[name]:
                raise ValueError(f"Conflicting retained gate {name!r} at milestone {index + 1}")
            if name not in by_name:
                current.append(gate)
                by_name[name] = gate
            inherited.setdefault(name, []).append(source)
        if not current:
            raise ValueError("Each milestone needs uniquely named checks")
        paths = array(item.get("paths", []), "paths")
        steps.append({"title": text(item, "title"), "task": text(item, "task"), "gates": current,
                      "paths": [artifact(path) for path in paths],
                      "worker": provider(item.get("worker", raw.get("worker")), IMPLEMENTERS, "worker", relative_to),
                      "worker_fallbacks": [provider(p, IMPLEMENTERS, "worker fallback", relative_to) for p in
                                           array(item.get("worker_fallbacks", raw.get("worker_fallbacks", [])), "worker_fallbacks", 5)]})
        if inherited:
            steps[-1]["retained_from"] = inherited
        if retention != "local":
            retained.extend((g, retention, steps[-1]["title"]) for g in own)
    if not steps:
        raise ValueError("At least one milestone is required")
    out = {"schema_version": 1, "repository": str(repository), "title": text(raw, "title"), "objective": text(raw, "objective"),
           "attempts": retries, "agent_timeout": timeout, "milestones": steps,
           "protected_paths": [artifact(path) for path in array(raw.get("protected_paths", []), "protected_paths", 4096)]}
    out["reviewer_fallbacks"] = [provider(p, REVIEWERS, "reviewer fallback", relative_to) for p in
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
    out["idle_default"] = True  # written by 0.19 only: the engine applies its idle watchdog to plans that carry this, never to saved older ones
    if "git_timeout" in raw:
        out["git_timeout"] = integer(raw["git_timeout"], "git_timeout", 30, 3600)
    retry = raw.get("retry", {})
    fields(retry, "initial_seconds max_seconds transient_attempts repeated_failure_limit max_parks max_park_seconds", "retry")
    defaults = {"initial_seconds": 2, "max_seconds": 60, "transient_attempts": 2, "repeated_failure_limit": 3,
                "max_parks": 8, "max_park_seconds": 21600}
    limits = {"initial_seconds": (1, 3600), "max_seconds": (1, 3600), "max_parks": (0, 50), "max_park_seconds": (60, 604800)}
    out["retry"] = {key: integer(retry.get(key, value), key, *limits.get(key, (0, 20))) for key, value in defaults.items()}
    if "repair" in raw:
        from .repair import policy
        out["repair"] = policy(raw["repair"])
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
    if "reviewer" in raw:
        out["reviewer"] = provider(raw["reviewer"], REVIEWERS, "reviewer", relative_to)
    if "pursue" in raw:
        out["pursue"] = pursuit(raw["pursue"], relative_to, out["acceptance"])
        if "explore" in out["pursue"]:
            out["pursue"]["explore"].setdefault("workers", [dict(steps[0]["worker"])])
    if "advisory_ollama" in raw:
        fields(raw["advisory_ollama"], "model port timeout max_tokens think required", "advisory_ollama")
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
        if type(max_tokens) is not int or not 1 <= max_tokens <= 16384:
            raise ValueError("Ollama max_tokens must be between 1 and 16384")
        out["advisory_ollama"] = {"model": model, "port": port, "timeout": local_timeout, "max_tokens": max_tokens}
        if "required" in raw["advisory_ollama"]:
            required = raw["advisory_ollama"]["required"]
            if type(required) is not bool:
                raise ValueError("Ollama required must be true or false")
            out["advisory_ollama"]["required"] = required
        if "think" in raw["advisory_ollama"]:
            think = raw["advisory_ollama"]["think"]
            if type(think) is not bool and think not in ("low", "medium", "high", "xhigh", "max"):
                raise ValueError("Invalid Ollama thinking setting")
            out["advisory_ollama"]["think"] = think
    return out


def read_plan(path, store=None):
    path = Path(path).resolve(strict=True)
    return validate(read_json(path), path.parent, store)
