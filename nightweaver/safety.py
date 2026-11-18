"""Bounded parsing, diagnostic redaction and crash-safe local publication."""

import json
import os
from pathlib import Path
import re
import tempfile


def redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"(?:api[_-]?key|auth[_-]?token|password|secret|authorization)$", key, re.I)
                      else redact(item)) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b", "[REDACTED]", value)
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
