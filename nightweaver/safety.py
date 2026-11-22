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


def scrub_env(base, keep=()):
    """A copy of an environment without any provider credential (or *_FILE pointer to one) except the named variables.

    Names compare case-insensitively, as Windows treats them. Gates, git, suite adapters and command routes get this with
    no keep list; a model route keeps only its own vendor's variables."""
    kept = {name.upper() for name in keep}
    return {name: value for name, value in base.items() if name.upper() not in SECRET_VARIABLES or name.upper() in kept}


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
