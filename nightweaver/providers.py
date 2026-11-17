"""Use native authenticated CLIs and an optional loopback model."""

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

from .process import execute
from .store import default_home


class ProviderUnavailable(ValueError):
    """An account or service problem that another coding attempt cannot fix."""


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
            raise ProviderUnavailable("DeepSeek's configured key file could not be read. Configure an accessible key file.") from error
    if not key or not key.isascii() or any(char.isspace() for char in key):
        raise ProviderUnavailable("DeepSeek needs a valid key through configure deepseek --key-file, api_key_file, or DEEPSEEK_API_KEY.")
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
    exe = config.get("executable", "claude" if kind == "deepseek" else kind)
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
        env["CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"] = config["profile_dir"]
    if kind == "codex":
        sandbox = "workspace-write" if mode == "worker" else "read-only"
        argv = [exe, "--sandbox", sandbox, "--ask-for-approval", "never", "exec"]
        if session:
            argv += ["resume", session]
        argv += ["--json"]
        if config.get("model"):
            argv += ["--model", config["model"]]
        return argv + ["-"], env
    argv = [exe, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits" if mode == "worker" else "dontAsk"]
    if mode != "worker":
        argv += ["--tools", "Read,Grep,Glob" if mode == "reviewer" else ""]
    if mode == "planner":
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
    with path.open(encoding="utf-8", errors="replace") as stream:
        if kind == "command":
            return None, stream.read()[-10000:]
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                session = event.get("thread_id") or session
            if event.get("type") == "system":
                session = event.get("session_id") or session
            if event.get("type") == "result":
                last = event.get("result") or last
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                last = item.get("text") or last
    return session, last


def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, evidence=None):
    argv, env = adapter(config, mode, session)
    if evidence is not None:
        env["NIGHTWEAVER_EVIDENCE"] = str(evidence)
        env["ROOKLINE_EVIDENCE"] = str(evidence)
        if config["kind"] in {"claude", "deepseek"}:
            argv += ["--add-dir", str(evidence)]
    code = execute(argv, cwd, log, timeout, prompt=prompt, env=env, paused=paused)
    current, answer = metadata(log, config["kind"])
    if code and config["kind"] in {"claude", "codex", "deepseek"}:
        detail = answer.strip()
        if not detail:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8000))
                detail = stream.read().decode("utf-8", "replace")
        if config["kind"] == "deepseek" and re.search(
                r"invalid.*api.?key|authentication_error|authentication.fail|insufficient.balance|invalid.authentication", detail, re.I):
            raise ProviderUnavailable("DeepSeek reported an API credential or balance error. "
                                      "Check the configured key and API account, then resume with --retry.")
        if re.search(r"authentication_failed|failed to authenticate|not logged in|please log in|refresh token.*expired", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} authentication needs renewal for the selected profile. "
                                      "Sign in with that CLI, then resume this run with --retry.")
        if re.search(r"oauth_org_not_allowed|oauth_not_allowed_for_organization|can't use your Claude subscription", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reported a subscription or organization access error. "
                                      "Resolve access for the selected profile, then resume this run with --retry.")
    return code, current or session, answer


def json_response(answer):
    stripped = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return json.loads(fence.group(1) if fence else stripped)


def advisory(config, information):
    body = {"model": config["model"], "stream": False, "think": False,
            "messages": [{"role": "user", "content": information[:3000]}],
            "options": {"num_predict": config.get("max_tokens", 100), "num_ctx": 2048}}
    request = Request(f"http://127.0.0.1:{config['port']}/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=config.get("timeout", 90)) as response:
        return json.load(response).get("message", {}).get("content", "")[:1500]
