"""Use native authenticated CLIs and an optional loopback model."""

import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

from .process import execute


class ProviderUnavailable(ValueError):
    """An account or service problem that another coding attempt cannot fix."""


def adapter(config, mode, session=None):
    kind = config["kind"]
    env = os.environ.copy()
    if kind == "command":
        return config["argv"], env
    exe = config.get("executable", kind)
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
    if config.get("model"):
        argv += ["--model", config["model"]]
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
        env["ROOKLINE_EVIDENCE"] = str(evidence)
        if config["kind"] == "claude":
            argv += ["--add-dir", str(evidence)]
    code = execute(argv, cwd, log, timeout, prompt=prompt, env=env, paused=paused)
    current, answer = metadata(log, config["kind"])
    if code and config["kind"] in {"claude", "codex"}:
        detail = answer.strip()
        if not detail:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8000))
                detail = stream.read().decode("utf-8", "replace")
        if re.search(r"authentication_failed|failed to authenticate|not logged in|please log in|refresh token.*expired", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} authentication needs renewal for the selected profile. "
                                      "Sign in with that CLI, then resume this run with --retry.")
    return code, current or session, answer


def json_response(answer):
    stripped = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return json.loads(fence.group(1) if fence else stripped)


def advisory(config, information):
    body = {"model": config["model"], "stream": False, "think": False,
            "messages": [{"role": "user", "content": information[:3000]}],
            "options": {"num_predict": 100, "num_ctx": 2048}}
    request = Request(f"http://127.0.0.1:{config['port']}/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=90) as response:
        return json.load(response).get("message", {}).get("content", "")[:1500]
