"""Use native authenticated CLIs and an optional loopback model."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import Request, urlopen

from .process import execute
from .store import default_home
from .safety import json_events


class ProviderUnavailable(ValueError):
    """An account or service problem that another coding attempt cannot fix."""

    def __init__(self, message, kind="unavailable"):
        super().__init__(message)
        self.kind = kind


def provider_executable(config):
    kind = config["kind"]
    if kind == "command":
        return config["argv"][0]
    return config.get("executable", "claude" if kind == "deepseek" else kind)


def find_executable(config, cwd):
    name = provider_executable(config)
    path = Path(name)
    if not path.is_absolute() and (path.parent != Path(".") or name.startswith((".", "/", "\\"))):
        name = str(cwd / path)
    return shutil.which(name)


def profile_override(config):
    kind = config["kind"]
    variable = {"codex": "CODEX_HOME", "gemini": "GEMINI_CLI_HOME"}.get(kind, "CLAUDE_CONFIG_DIR")
    inherited = None if kind == "deepseek" else os.environ.get(variable)
    return config.get("profile_dir") or inherited


def describe_provider(config):
    kind = config["kind"]
    if kind == "command":
        return {"provider": kind, "executable": provider_executable(config)}
    profile = profile_override(config)
    if not profile:
        profile = (default_home() / "profiles" / "deepseek" if kind == "deepseek" else
                   Path.home() if kind == "gemini" else Path.home() / f".{kind}")
    return {"provider": kind, "executable": provider_executable(config),
            "profile_dir": str(Path(profile).expanduser().resolve()),
            "model": config.get("model") or ("deepseek-flash" if kind == "deepseek" else "CLI default")}


def check_provider(config, cwd):
    executable = find_executable(config, cwd)
    if not executable:
        raise ProviderUnavailable(f"Cannot find {provider_executable(config)}. Install it or set the provider executable.",
                                  "missing_executable")
    if config["kind"] == "command":
        return executable
    profile = profile_override(config)
    if profile and not Path(profile).expanduser().is_dir():
        raise ProviderUnavailable("The selected provider profile directory is missing. Check profile_dir and the CLI environment.",
                                  "missing_profile")
    if config["kind"] == "deepseek":
        deepseek_key(config)
    return executable


def deepseek_key(config):
    source = config.get("api_key_file")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not source and not key:
        source = os.environ.get("DEEPSEEK_API_KEY_FILE")
        settings = default_home() / "providers.json"
        if not source and settings.is_file():
            source = json.loads(settings.read_text(encoding="utf-8")).get("deepseek", {}).get("api_key_file")
    if source:
        try:
            key = Path(source).expanduser().read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as error:
            raise ProviderUnavailable("DeepSeek's configured key file could not be read. Configure an accessible key file.",
                                      "missing_credentials") from error
    if not key or not key.isascii() or any(char.isspace() for char in key):
        raise ProviderUnavailable("DeepSeek needs a valid key through configure deepseek --key-file, api_key_file, or DEEPSEEK_API_KEY.",
                                  "missing_credentials")
    return key


def configure_deepseek_key(path):
    path = path.expanduser().resolve(strict=True)
    deepseek_key({"api_key_file": str(path)})
    target = default_home() / "providers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    settings["deepseek"] = {"api_key_file": str(path)}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as temp:
        json.dump(settings, temp, indent=2)
        staged = Path(temp.name)
    try:
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return {"provider": "deepseek", "configured": True, "api_key_file": str(path)}


def adapter(config, mode, session=None):
    kind = config["kind"]
    env = os.environ.copy()
    if kind == "command":
        return config["argv"], env
    exe = provider_executable(config)
    model = config.get("model")
    if kind == "deepseek":
        key = deepseek_key(config)
        model = model or "deepseek-flash"
        profile = Path(config.get("profile_dir") or default_home() / "profiles" / "deepseek")
        profile.mkdir(parents=True, exist_ok=True)
        env.update({"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "CLAUDE_CODE_SUBAGENT_MODEL": model,
                    "CLAUDE_CONFIG_DIR": str(profile), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        for variable in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                         "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env.pop(variable, None)
    if config.get("profile_dir"):
        env[{"codex": "CODEX_HOME", "gemini": "GEMINI_CLI_HOME"}.get(kind, "CLAUDE_CONFIG_DIR")] = config["profile_dir"]
    if kind == "gemini":
        argv = [exe, "--output-format", "stream-json", "--approval-mode", "auto_edit" if mode == "worker" else "plan",
                "--prompt", "Follow the task supplied on standard input."]
        if model:
            argv += ["--model", model]
        if session:
            argv += ["--resume", session]
        return argv, env
    if kind == "codex":
        sandbox = "workspace-write" if mode == "worker" else "read-only"
        argv = [exe, "--sandbox", sandbox, "--ask-for-approval", "never", "exec"]
        if session:
            argv += ["resume", session]
        argv += ["--json"]
        if config.get("model"):
            argv += ["--model", config["model"]]
        if mode == "probe":
            argv += ["--skip-git-repo-check", "--ephemeral"]
        return argv + ["-"], env
    argv = [exe, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits" if mode == "worker" else "dontAsk"]
    if mode != "worker":
        argv += ["--tools", "Read,Grep,Glob" if mode == "reviewer" else ""]
    if mode == "probe":
        argv += ["--no-session-persistence", "--strict-mcp-config",
                 "--system-prompt", "This is a connection test. Reply READY and use no tools."]
    elif mode == "planner":
        argv += ["--system-prompt", "You are a software milestone planner. Use only the supplied goal and constraints. "
                 "You have no tools and must not request tools or inspect files. Return only the JSON requested by the user, "
                 "without commentary or tool-call markup. Each milestone must be self-contained and testable."]
    if model:
        argv += ["--model", model]
    if session:
        argv += ["--resume", session]
    return argv, env


def metadata(path, kind):
    session, last = None, ""
    if not path.exists():
        return session, last
    if kind == "command":
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 10000))
            return None, stream.read().decode("utf-8", "replace")
    for event in json_events(path):
        if kind == "gemini":
            if event.get("type") == "init":
                session = event.get("session_id") or session
            if event.get("type") == "message" and event.get("role") == "assistant" and isinstance(event.get("content"), str):
                last = (last + event["content"] if event.get("delta") else event["content"])[-100000:]
            if isinstance(event.get("response"), str):
                last = event["response"]
        else:
            if event.get("type") == "thread.started":
                session = event.get("thread_id") or session
            if event.get("type") == "system":
                session = event.get("session_id") or session
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                last = event["result"] or last
            item = event.get("item", {})
            if isinstance(item, dict) and event.get("type") == "item.completed" and item.get("type") == "agent_message":
                last = item.get("text") or last
    return session if isinstance(session, str) else None, last if isinstance(last, str) else ""


def usage(path, kind):
    """Reported usage only. Missing telemetry stays unknown, never zero cost."""
    total = 0
    seen = False
    for event in json_events(path):
        values = None
        if kind == "codex" and event.get("type") == "turn.completed":
            values = event.get("usage")
        elif kind in ("claude", "deepseek") and event.get("type") == "result":
            values = event.get("usage")
        elif kind == "gemini" and event.get("type") == "result":
            stats = event.get("stats", {})
            if isinstance(stats, dict):
                values = stats
        if not isinstance(values, dict):
            continue
        if type(values.get("total_tokens")) is int:
            count = values["total_tokens"]
        else:
            keys = ("input_tokens", "output_tokens")
            if kind in ("claude", "deepseek"):
                keys += ("cache_creation_input_tokens", "cache_read_input_tokens")
            available = [values[k] for k in keys if type(values.get(k)) is int]
            if not available:
                continue
            count = sum(available)
        if count >= 0:
            total += count
            seen = True
    return {"observed_tokens": total if seen else None, "billing": "not_measured"}


def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, evidence=None, health=None):
    executable = check_provider(config, cwd)
    argv, env = adapter(config, mode, session)
    argv = [executable, *argv[1:]]
    if Path(executable).suffix.casefold() in (".cmd", ".bat"):
        # npm's Windows launcher is a shell script. Resolve the known package without executing a shell.
        entry = Path(executable).parent / "node_modules/@google/gemini-cli/dist/index.js"
        node = shutil.which("node")
        if config["kind"] != "gemini" or not entry.is_file() or not node:
            raise ProviderUnavailable("Use a native executable; Gemini npm installs also require node and their package entrypoint.", "missing_executable")
        argv = [node, str(entry), *argv[1:]]
    if evidence is not None:
        env["NIGHTWEAVER_EVIDENCE"] = str(evidence)
        env["ROOKLINE_EVIDENCE"] = str(evidence)
        if config["kind"] in {"claude", "deepseek"}:
            argv += ["--add-dir", str(evidence)]
        elif config["kind"] == "gemini":
            argv += ["--include-directories", str(evidence)]
    code = execute(argv, cwd, log, timeout, prompt=prompt, env=env, paused=paused, **(health or {}))
    current, answer = metadata(log, config["kind"])
    if config["kind"] == "command" and code == config.get("unavailable_exit_code"):
        raise ProviderUnavailable(answer.strip()[-1500:] or "External workflow unavailable", "external_environment")
    if code == 0 and config["kind"] != "command":
        for event in json_events(log):
            if (event.get("type") == "result" and (event.get("is_error") is True or event.get("status") == "error")) or event.get("type") == "turn.failed":
                code = 1
    if code and config["kind"] in {"claude", "codex", "deepseek", "gemini"}:
        detail = answer.strip()
        if not detail:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8000))
                detail = stream.read().decode("utf-8", "replace")
        if config["kind"] == "deepseek" and re.search(
                r"invalid.*api.?key|authentication_error|authentication.fail|insufficient.balance|invalid.authentication", detail, re.I):
            raise ProviderUnavailable("DeepSeek reported an API credential or balance error. "
                                      "Check the configured key and API account, then resume with --retry.", "credentials_or_balance")
        if re.search(r"authentication_failed|failed to authenticate|not logged in|please (?:log|sign) in|refresh token.*expired|token.*(expired|revoked)", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} authentication needs renewal for the selected profile. "
                                      "Sign in with that CLI, then resume this run with --retry.", "authentication")
        if config["kind"] == "claude" and re.search(r"can.t use your Claude subscription", detail, re.I):
            raise ProviderUnavailable("Claude rejected the selected profile's subscription. Compare its "
                                      "claude auth status with the intended browser account at "
                                      "https://claude.ai/settings/billing. Verify access with doctor --live "
                                      "before retrying.", "subscription")
        if re.search(r"oauth_org_not_allowed|oauth_not_allowed_for_organization", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reported a subscription or organization access error. "
                                      "Resolve access for the selected profile, then resume this run with --retry.", "subscription")
        if re.search(r"credit balance.*(?:low|insufficient)|billing.hard.limit|insufficient credits", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reported a billing or credit balance problem. Check the selected account.",
                                      "billing")
        if re.search(r"usage limit|rate.limit|quota exceeded|too many requests|(?:HTTP|status|API error)[: ]+429\b", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reached a rate or usage limit. Retry after the limit resets.",
                                      "usage_limit")
        if re.search(r"ECONNRESET|ETIMEDOUT|temporarily unavailable|service unavailable|(?:HTTP|status|API error)[: ]+50[234]\b", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reported a temporary network or service failure.", "transient")
        if re.search(r"model_not_found|unsupported model|model .*?(?:not found|does not exist|not supported|not available)", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} could not use the selected model. Check model access for this profile.",
                                      "model_access")
    return code, current or session, answer


def json_response(answer):
    stripped = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if len(stripped) > 1000000:
        raise ValueError("Structured response exceeds 1MB")
    result = json.loads(fence.group(1) if fence else stripped)
    if not isinstance(result, dict):
        raise ValueError("Structured response must be a JSON object")
    return result


def advisory(config, information):
    body = {"model": config["model"], "stream": False, "think": False,
            "messages": [{"role": "user", "content": information[:3000]}],
            "options": {"num_predict": config.get("max_tokens", 100), "num_ctx": 2048}}
    request = Request(f"http://127.0.0.1:{config['port']}/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=config.get("timeout", 90)) as response:
        return json.load(response).get("message", {}).get("content", "")[:1500]
