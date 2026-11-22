"""Bounded parsing, diagnostic redaction and crash-safe local publication."""

import json
import os
from pathlib import Path
import re
import tempfile


# Each vendor's credential variables. A child process gets its own vendor's and no other's.
CREDENTIALS = {"claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
               "deepseek": ("DEEPSEEK_API_KEY",), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
               "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
SECRET_VARIABLES = frozenset(name.upper() + suffix for names in CREDENTIALS.values() for name in names for suffix in ("", "_FILE"))
# Any other variable whose name says it holds a credential (*_TOKEN, *_API_KEY, *_SECRET, AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, a
# Bedrock bearer token, Claude Code's own session socket) is scrubbed too, with or without a *_FILE pointer.
SECRET_NAME = re.compile(r"(?:^|_)(?:TOKEN|API_?KEY|SECRET|SECRET_KEY|ACCESS_KEY|ACCESS_KEY_ID|PRIVATE_KEY|PASSWORD|PASSWD|PASSPHRASE"
                         r"|CREDENTIALS?)(?:_FILE)?$|^AWS_BEARER_TOKEN_|^CLAUDE_CODE_.*_SOCKET$")
NOT_SECRET = frozenset({"HF_HUB_DISABLE_IMPLICIT_TOKEN"})  # switches whose names merely look like credentials
# What each vendor's own CLI may keep besides its fixed names: credential-shaped variables with these prefixes. A Claude route on
# Bedrock or Vertex also keeps that cloud's credentials, found by the switch that turns it on.
FAMILIES = {"claude": ("ANTHROPIC_", "CLAUDE_CODE_OAUTH_", "AWS_BEARER_TOKEN_BEDROCK"), "codex": ("OPENAI_", "CODEX_", "AZURE_OPENAI_"),
            "gemini": ("GEMINI_", "GOOGLE_"), "deepseek": ("DEEPSEEK_",)}
CLOUDS = {"CLAUDE_CODE_USE_BEDROCK": "AWS_", "CLAUDE_CODE_USE_VERTEX": "GOOGLE_APPLICATION_"}


def is_secret(name):
    upper = name.upper()
    return upper not in NOT_SECRET and (upper in SECRET_VARIABLES or bool(SECRET_NAME.search(upper)))


def own_credentials(kind, base):
    """The names in base that a route of this kind may keep: its vendor's fixed credentials with their *_FILE pointers, and
    credential-shaped variables of its vendor's families. Every other vendor's, and every unrelated secret, is scrubbed."""
    prefixes = list(FAMILIES.get(kind, ()))
    if kind == "claude":
        prefixes += [prefix for switch, prefix in CLOUDS.items() if str(base.get(switch, "")).strip().lower() in ("1", "true", "yes", "on")]
    fixed = [name + suffix for name in CREDENTIALS.get(kind, ()) for suffix in ("", "_FILE")]
    return tuple(fixed) + tuple(name for name in base if is_secret(name) and name.upper().startswith(tuple(prefixes)))


def scrub_env(base, keep=()):
    """A copy of an environment without any credential-shaped variable (or *_FILE pointer to one) except the named variables.

    Names compare case-insensitively, as Windows treats them. Gates, git, suite adapters and command routes get this with
    no keep list; a model route keeps what own_credentials() names for its vendor."""
    kept = {name.upper() for name in keep}
    return {name: value for name, value in base.items() if not is_secret(name) or name.upper() in kept}


KEY_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b")


def mask_tokens(path):
    """Mask key-shaped tokens (sk-ant-..., ghp_...) in a log file in place. Only those: a gate log is evidence a reviewer reads,
    so the wider patterns of redact() (password: ...) stay out of it."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return
    masked = re.sub(KEY_TOKEN.pattern.encode(), b"[REDACTED]", data)
    if masked != data:
        atomic_write(path, masked)


def redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"(?:api[_-]?key|(?:auth|access|refresh|id)[_-]?token|password|secret|authorization)$", key, re.I)
                      else redact(item)) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)([?&](?:access_token|refresh_token|id_token|code|code_verifier)=)[^&#\s]+",
                   r"\1[REDACTED]", value)
    value = KEY_TOKEN.sub("[REDACTED]", value)
    value = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
    return re.sub(r"(?i)((?:api[_-]?key|auth[_-]?token|password|secret)\s*[=:]\s*)[^\s,;\"'}]+", r"\1[REDACTED]", value)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n")


def json_events(path, max_line=2 * 1024 * 1024):
    """Skip malformed, oversized and non-object records without loading an entire log."""
    if not Path(path).is_file():
        return
    with Path(path).open("rb") as stream:
        while line := stream.readline(max_line + 1):
            if len(line) > max_line:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(max_line + 1)
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                yield event
