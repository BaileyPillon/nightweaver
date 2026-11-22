"""Use native authenticated CLIs and an optional loopback model."""

from collections import deque
from contextlib import ExitStack
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import Request, urlopen

from .process import CONTROLLER_EXITS, execute
from .store import default_home
from .safety import json_events, own_credentials, redact, scrub_env


READ_ONLY = ("reviewer", "judge", "replanner", "brief", "directions", "comparison", "ranking")
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "score", "criteria", "gaps", "reason"],
                "properties": {"verdict": {"enum": ["MET", "NOT_MET"]}, "score": {"type": "integer", "minimum": 0, "maximum": 100},
                               "criteria": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": ["criterion", "met", "note"],
                                            "properties": {"criterion": {"type": "string"}, "met": {"type": "boolean"}, "note": {"type": "string"}}}},
                               "gaps": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                        "required": ["title", "detail"],
                                        "properties": {"title": {"type": "string"}, "detail": {"type": "string"}}}},
                               "reason": {"type": "string"}}}
WAVE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["milestones"],
               "properties": {"milestones": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False,
                              "required": ["title", "task"], "properties": {"title": {"type": "string"}, "task": {"type": "string"}}}},
                              "conflicts": {"type": "array", "items": {"type": "string"}}}}
DIRECTIONS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["directions"],
                     "properties": {"directions": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
                         "type": "object", "additionalProperties": False, "required": ["name", "brief"],
                         "properties": {"name": {"type": "string"}, "brief": {"type": "string"}}}}}}
COMPARISON_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["winner", "reason"],
                     "properties": {"winner": {"enum": ["A", "B", "tie"]}, "reason": {"type": "string"}}}
RANKING_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ranking", "reason"],
                  "properties": {"ranking": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string"}},
                                 "reason": {"type": "string"}}}
SCHEMAS = {"reviewer": {"type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
                        "properties": {"verdict": {"enum": ["PASS", "REVISE"]}, "reason": {"type": "string"}}},
           "judge": JUDGE_SCHEMA, "replanner": WAVE_SCHEMA, "directions": DIRECTIONS_SCHEMA, "comparison": COMPARISON_SCHEMA,
           "ranking": RANKING_SCHEMA}


class ProviderUnavailable(ValueError):
    """An account or service problem that another coding attempt cannot fix."""

    def __init__(self, message, kind="unavailable", reset_at=None):
        super().__init__(message)
        self.kind = kind
        self.reset_at = reset_at


MONTHS = {name: number for number, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
CUE = r"(?:reset\w*|retry\w*|try again|available|until)\W{0,4}(?:(?:at|on|after|by|time)\b\W{0,3})*"
CLOCK = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\b\.?)?(?:\s*\((?P<z>[A-Za-z][A-Za-z_+\-/0-9]{1,40})\))?"
ISO = r"(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s?(?:Z|[+-]\d{2}:?\d{2})?)(?:\s*\((?P<z>[A-Za-z][A-Za-z_+\-/0-9]{1,40})\))?"
DATED = (r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?,?\s*(?:at\s+)?"
         + CLOCK)


def zone_named(name):
    if not name:
        return None
    if name.upper() in ("UTC", "GMT", "Z", "ETC/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (ImportError, KeyError, ValueError, OSError):
        return None


def wall_clock(hour, minute, meridiem):
    """24-hour (hour, minute) from a clock reading, or None when it is not a time of day."""
    minute = int(minute or 0)
    hour = int(hour)
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem[0].lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def in_zone(naive, zone):
    return (naive.replace(tzinfo=zone) if zone else naive.astimezone()).astimezone(timezone.utc)


def reset_after(message, now=None):
    """The UTC moment a usage limit lifts, read from a provider's message, or None when it names none.

    Understands epoch seconds in JSON (resetsAt, reset_at), ISO-8601 stamps, dated and plain clock times
    ("resets 3:30pm (America/New_York)", "try again at Nov 22nd, 2026 3:42 PM") and relative waits
    ("try again in 2 hours 13 minutes", "in 47m"). A clock time uses its named IANA zone when this system
    knows it, otherwise local time, and rolls to tomorrow when today's has passed."""
    if not isinstance(message, str):
        return None
    now = now or datetime.now(timezone.utc)
    now = now.astimezone() if now.tzinfo is None else now
    epochs = re.findall(r"(?:reset\w*_?at|reset_?time|limit reached\|)\W{0,4}(\d{9,13})(?![\d:])", message, re.I)
    if epochs:
        value = int(epochs[-1])
        return datetime.fromtimestamp(value / 1000 if value > 10 ** 11 else value, timezone.utc)
    seconds = re.search(r"(?:retry[_-]?after(?:_seconds)?|reset[s_]?_?in(?:_seconds)?|reset_?after(?:_seconds)?)\W{0,4}(\d+(?:\.\d+)?)(?![\d:])", message, re.I)
    if seconds:
        return now + timedelta(seconds=float(seconds.group(1)))
    for found in re.finditer(CUE + ISO, message, re.I):
        text = re.sub(r"^(\d{4}-\d{2}-\d{2}) ", lambda day: day.group(1) + "T", found.group("iso").strip()).replace(" ", "")
        text = re.sub(r"Z$", "+00:00", text, flags=re.I)
        text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            continue
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else in_zone(stamp, zone_named(found.group("z")))
    relative = re.search(r"\b(?:in|after)\s+((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])[\s,]*(?:and\s+)?)+)",
                         message, re.I)
    if relative:
        total = sum(float(n) * UNITS[unit[0].lower()] for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", relative.group(1), re.I))
        return now + timedelta(seconds=total)
    for found in re.finditer(CUE + DATED, message, re.I):
        clock = wall_clock(found.group("h"), found.group("m"), found.group("ap"))
        zone = zone_named(found.group("z"))
        if clock is None or (found.group("m") is None and not found.group("ap")):
            continue
        today = now.astimezone(zone).date() if zone else now.astimezone().date()
        year = int(found.group("year") or today.year)
        try:
            when = in_zone(datetime(year, MONTHS[found.group("mon").lower()], int(found.group("day")), *clock), zone)
            if found.group("year") is None and when < now - timedelta(days=1):
                when = in_zone(datetime(year + 1, when.month, when.day, *clock), zone)
        except (ValueError, OverflowError):
            continue
        return when
    for found in re.finditer(CUE + CLOCK, message, re.I):
        clock = wall_clock(found.group("h"), found.group("m"), found.group("ap"))
        if clock is None or (found.group("m") is None and not found.group("ap")):
            continue
        zone = zone_named(found.group("z"))
        today = now.astimezone(zone).date() if zone else now.astimezone().date()
        when = in_zone(datetime.combine(today, time(*clock)), zone)
        if when <= now:
            when = in_zone(datetime.combine(today + timedelta(days=1), time(*clock)), zone)
        return when
    return None


OAUTH_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"


def user_environment(name):
    """A user-level environment variable straight from the registry, for processes started before it was set."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value = winreg.QueryValueEx(key, name)[0]
    except (ImportError, OSError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def token_from_file(path):
    try:
        token = Path(path).expanduser().read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as error:
        raise ProviderUnavailable("Claude's configured oauth_token_file could not be read. Fix the file or remove oauth_token_file.",
                                  "missing_credentials") from error
    if not token or not token.isascii() or any(char.isspace() for char in token):
        raise ProviderUnavailable("Claude's oauth_token_file must hold one token with no spaces. "
                                  "Create it with claude setup-token.", "missing_credentials")
    return token


def claude_auth(config=None):
    """(long-lived token or None, source) for a Claude route. The token is never logged or returned to a report.

    Precedence: the route's oauth_token_file; then the user environment variable. An explicit profile_dir means the operator
    chose that profile's own login, so neither variable applies to it. A token in the process environment is only a snapshot:
    a dispatcher or guard started before `claude setup-token` was repeated keeps the old one for good, while the registry
    value is current. When the two differ the registry wins, so a rotated token reaches Claude children without restarting
    long-lived processes. A route that must use one specific token sets oauth_token_file."""
    config = config or {}
    if config.get("oauth_token_file"):
        return token_from_file(config["oauth_token_file"]), "oauth_token_file"
    if not config.get("profile_dir"):
        inherited = os.environ.get(OAUTH_VARIABLE, "").strip()
        current = user_environment(OAUTH_VARIABLE)
        if current and current != inherited:
            return current, "oauth_token_user_env"
        if inherited:
            return inherited, "oauth_token_env"
    profile = Path(profile_override({**config, "kind": "claude"}) or default_claude()).expanduser()
    return None, "cli_login" if (profile / ".credentials.json").is_file() else "none"


def isolated_claude(config):
    """Nightweaver's own Claude config dir, used with a long-lived token unless the operator chose a profile."""
    if config.get("profile_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("NIGHTWEAVER_CLAUDE_SHARED_CONFIG") == "1":
        return None
    return default_home() / "profiles" / "claude"


def config_tag(directory):
    """A short stable name for a Claude config directory, kept beside a session id so a resume never crosses directories."""
    return hashlib.sha256(os.path.normcase(str(Path(directory).expanduser().resolve())).encode("utf-8")).hexdigest()[:12]


def default_claude():
    try:
        return Path.home() / ".claude"
    except RuntimeError:  # no home directory is known, so there is no shared profile to confuse with
        return Path(".claude")


def claude_directory(env):
    return env.get("CLAUDE_CONFIG_DIR") or default_claude()


def tag_file(log):
    return log.with_name(log.name + ".config")


def resumable(config, env, session):
    """The Claude session id to resume, or None when the session lives under another CLAUDE_CONFIG_DIR than this call uses.

    Claude Code keeps conversations under its config directory and cannot find them in another one. metadata() stores the
    directory a session was created under as id@tag. An untagged id comes from before this was recorded (0.18) and is taken
    to live where that version looked: the route's profile_dir, the CLAUDE_CONFIG_DIR of this process, or ~/.claude."""
    if not session:
        return None
    name, _, made = session.partition("@")
    made = made or config_tag(profile_override({**config, "kind": "claude"}) or default_claude())
    return name if name and made == config_tag(claude_directory(env)) else None


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
    if not profile and kind == "claude" and isolated_claude(config):
        try:
            profile = isolated_claude(config) if claude_auth(config)[0] else None
        except ProviderUnavailable:
            profile = None
    if not profile:
        profile = (default_home() / "profiles" / "deepseek" if kind == "deepseek" else
                   Path.home() if kind == "gemini" else Path.home() / f".{kind}")
    return {"provider": kind, "executable": provider_executable(config),
            "profile_dir": str(Path(profile).expanduser().resolve()),
            **({"effort": config["effort"]} if "effort" in config else {}),
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
    if config["kind"] == "claude":
        claude_auth(config)
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


def renewal(config):
    """What to do about an expired Claude login, for the route that hit it. A route with its own profile_dir never reads the
    token variable, so the advice for it is that profile's login or a token file."""
    if config.get("oauth_token_file"):
        return f"Run claude setup-token and write the new token to this route's oauth_token_file ({config['oauth_token_file']})."
    if config.get("profile_dir"):
        return (f"This route uses the profile {config['profile_dir']}, which ignores {OAUTH_VARIABLE}: sign in again with claude "
                "(CLAUDE_CONFIG_DIR set to that directory), or run claude setup-token and point this route's oauth_token_file at a file holding it.")
    return (f"Run claude setup-token, set the user environment variable {OAUTH_VARIABLE} to the token (or point this route's "
            "oauth_token_file at a file holding it).")


def adapter(config, mode, session=None, images=()):
    kind = config["kind"]
    if kind == "command":
        # Operator-approved programs get no credential unless the route says inherit_credentials.
        return config["argv"], dict(os.environ) if config.get("inherit_credentials") else scrub_env(os.environ)
    env = scrub_env(os.environ, own_credentials(kind, os.environ))
    exe = provider_executable(config)
    model = config.get("model")
    if kind == "claude":
        env.pop(OAUTH_VARIABLE, None)  # an inherited token never overrides a profile or token file the route chose
        token, _ = claude_auth(config)
        if token:
            env[OAUTH_VARIABLE] = token
            if isolated_claude(config):
                # The operator's interactive CLAUDE.md, skills and settings (e.g. "delegate to background
                # subagents") must not steer an unattended run; the token authenticates in any config dir.
                isolated_claude(config).mkdir(parents=True, exist_ok=True)
                env["CLAUDE_CONFIG_DIR"] = str(isolated_claude(config))
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
        for variable in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
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
        argv = [exe, "--sandbox", sandbox, "--ask-for-approval", "never"]
        if "effort" in config:
            argv += ["-c", "model_reasoning_effort=" + json.dumps(config["effort"])]
        argv.append("exec")
        if session:
            argv += ["resume", session]
        for image in images:
            # --image takes values greedily, so a following flag must end it before the stdin marker.
            argv += ["--image", str(image)]
        argv += ["--json"]
        if config.get("model"):
            argv += ["--model", config["model"]]
        if mode == "probe":
            argv += ["--skip-git-repo-check", "--ephemeral"]
        return argv + ["-"], env
    argv = [exe, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits" if mode == "worker" else "dontAsk"]
    if "effort" in config:
        argv += ["--effort", config["effort"]]
        env["CLAUDE_CODE_EFFORT_LEVEL"] = config["effort"]
    if mode != "worker":
        argv += ["--tools", "Read,Grep,Glob" if mode in READ_ONLY else ""]
    if mode in SCHEMAS:
        argv += ["--json-schema", json.dumps(SCHEMAS[mode])]
    if mode == "probe":
        argv += ["--no-session-persistence", "--strict-mcp-config",
                 "--system-prompt", "This is a connection test. Reply READY and use no tools."]
    elif mode == "planner":
        argv += ["--system-prompt", "You are a software milestone planner. Use only the supplied goal and constraints. "
                 "You have no tools and must not request tools or inspect files. Return only the JSON requested by the user, "
                 "without commentary or tool-call markup. Each milestone must be self-contained and testable."]
    if model:
        argv += ["--model", model]
    if kind == "claude":
        session = resumable(config, env, session)
    if session:
        argv += ["--resume", session]
    return argv, env


def metadata(path, kind, mode=None):
    """(session id, last answer) of one provider call's log. A Claude id carries its config directory as id@tag; see resumable()."""
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
            if mode in SCHEMAS and event.get("type") == "result" and isinstance(event.get("structured_output"), dict):
                last = json.dumps(event["structured_output"])
            item = event.get("item", {})
            if isinstance(item, dict) and event.get("type") == "item.completed" and item.get("type") == "agent_message":
                last = item.get("text") or last
    if kind == "claude" and isinstance(session, str) and session:
        try:
            tag = tag_file(path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            tag = ""  # an untagged id is taken to live in the directory an older version used
        session = f"{session}@{tag}" if tag else session
    return session if isinstance(session, str) else None, last if isinstance(last, str) else ""


def usage(path, kind):
    """Reported usage only. Missing telemetry stays unknown, never zero cost."""
    total = 0
    seen = False
    channels = {}
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
        names = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                 "cached_input_tokens", "cached", "input", "output")
        reported = {k: values[k] for k in names if type(values.get(k)) is int}
        if any(n < 0 for n in reported.values()):
            continue
        if type(values.get("total_tokens")) is int:
            count = values["total_tokens"]
        else:
            keys = ("input_tokens", "output_tokens")
            if kind in ("claude", "deepseek"):
                keys += ("cache_creation_input_tokens", "cache_read_input_tokens")
            if kind == "gemini" and not any(k in reported for k in keys):
                keys = ("input", "output")
            available = [reported[k] for k in keys if k in reported]
            if not available:
                continue
            count = sum(available)
        if count >= 0:
            total += count
            seen = True
            for key, value in reported.items():
                channels[key] = channels.get(key, 0) + value
    return {"observed_tokens": total if seen else None, "token_channels": channels, "billing": "not_measured"}


SYNTHETIC = "<synthetic>"  # Claude Code's own placeholder for messages it wrote itself, such as API errors
FAMILIES = ("opus", "sonnet", "haiku")
UNVERIFIABLE = ("", "default", "best", "opusplan", "auto")
GEMINI_ALIASES = ("pro", "flash", "flash-lite")
DATE_SUFFIX = re.compile(r"[-@](?:\d{8}|\d{4}-\d{2}-\d{2}|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))$")
ANNOUNCES = ("thread.started", "turn.started", "turn.completed", "session.configured", "session_configured")


def canonical_model(name):
    """A model id without the spellings that differ between routes: case, vendor prefix, context tag, dated snapshot, version tag."""
    name = re.sub(r"\[[^\]]*\]$", "", str(name).strip().lower()).rsplit("/", 1)[-1]
    name = re.sub(r"^(?:[a-z]{2}\.)?(?:anthropic|openai|deepseek|google|meta|amazon)\.", "", name)
    name = DATE_SUFFIX.sub("", re.sub(r"[-:@]latest$", "", re.sub(r"-v\d+:\d+$", "", name)))
    return re.sub(r"[._\s]+", "-", name).strip("-")


def gemini_alias(name):
    """The family a Gemini CLI alias (pro, flash, flash-lite) or a canonical Gemini id stands for, else None."""
    name = canonical_model(name)
    if name in GEMINI_ALIASES:
        return name
    found = re.fullmatch(r"gemini-(?:\d+(?:-\d+)?-)?(.+)", name)
    return re.sub(r"-(?:preview|exp|experimental|latest|\d{3})(?:-.*)?$", "", found.group(1)) if found else None


def same_model(requested, served):
    """Whether a reported model id is the one a route asked for. A bare family alias such as sonnet matches claude-sonnet-5-5,
    and a Gemini alias such as pro matches gemini-2.5-pro. auto and auto-gemini-* routes name no model to check."""
    asked, got = canonical_model(requested), canonical_model(served)
    if asked == got or asked in UNVERIFIABLE or asked.startswith("auto-") or (asked in FAMILIES and asked in got.split("-")):
        return True
    return asked in GEMINI_ALIASES and gemini_alias(got) == asked


def codex_rollout_model(thread, profile_dir=None):
    """The model Codex recorded for a thread's last turn. exec --json never prints it, but the session rollout does."""
    from uuid import UUID
    try:
        identity = UUID(thread)
        if identity.version != 7:
            return None
        thread = str(identity)
        made = datetime.fromtimestamp(int(identity.hex[:12], 16) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    sessions = Path(profile_dir or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"
    days = {(stamp + timedelta(days=shift)).date() for stamp in (made, made.astimezone()) for shift in (-1, 0, 1)}
    model = None
    try:
        for day in sorted(days):
            for rollout in (sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}").glob(f"rollout-*-{thread}.jsonl"):
                if rollout.stat().st_size > 512 * 1024 * 1024:
                    continue
                with rollout.open("rb") as stream:
                    for line in stream:
                        if b'"turn_context"' not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        payload = event.get("payload") if isinstance(event, dict) and event.get("type") == "turn_context" else None
                        found = payload.get("model") if isinstance(payload, dict) else None
                        model = found if isinstance(found, str) and found.strip() else model
    except OSError:
        return None
    return model


def main_model(models):
    """The model that did a Gemini call's main work, from the per-model stats of its result event, or None when that cannot be told.

    The CLI lists its helper and router calls (flash-lite and the like) beside the main model, so a model counts only when it is
    the sole one listed or carries more tokens than all the others together. Names that are aliases or routing modes never count."""
    weights = {}
    for name, entry in models.items():
        if not isinstance(name, str) or not name.strip() or canonical_model(name) in GEMINI_ALIASES or canonical_model(name).startswith("auto"):
            continue
        entry = entry if isinstance(entry, dict) else {}
        tokens = entry.get("total_tokens", (entry.get("tokens") or {}).get("total") if isinstance(entry.get("tokens"), dict) else None)
        weights[name.strip()] = tokens if type(tokens) is int and tokens >= 0 else None
    if len(weights) == 1:
        return next(iter(weights))
    if not weights or None in weights.values():
        return None
    top = max(weights, key=weights.get)
    return top if weights[top] > sum(weights.values()) - weights[top] else None


def served_models(config, log):
    """The model ids a provider reported for one call, most used first, and where they came from.

    Claude and DeepSeek name the model on every main-thread assistant message (a field of the API response; subagents and the
    CLI's synthetic messages are skipped), and the CLI repeats its own choice in the init event, which is only a fallback.
    Gemini names it at init, and its result stats name the model that carried the call (see main_model). Codex's --json stream
    names none, so the model it recorded for the thread comes from its session rollout.
    Returns {"models": [...], "source": "response" | "init" | "stream" | "codex_rollout" | None}."""
    kind = config.get("kind")
    replies, announced, thread, main = {}, {}, None, None

    def note(into, name):
        if isinstance(name, str) and name.strip() and name.strip() != SYNTHETIC:
            if kind == "gemini" and (canonical_model(name) in GEMINI_ALIASES or canonical_model(name).startswith("auto")):
                return  # an alias or a routing mode names no model
            into[name.strip()] = into.get(name.strip(), 0) + 1

    if kind in ("claude", "deepseek", "gemini", "codex"):
        for event in json_events(log):
            label = event.get("type")
            if kind in ("claude", "deepseek"):
                if label == "assistant" and isinstance(event.get("message"), dict) and not event.get("parent_tool_use_id"):
                    note(replies, event["message"].get("model"))
                elif label == "system" and event.get("subtype") == "init":
                    note(announced, event.get("model"))
            elif kind == "gemini":
                if label == "init":
                    note(announced, event.get("model"))
                elif label == "result" and isinstance(event.get("stats"), dict) and isinstance(event["stats"].get("models"), dict):
                    main = main_model(event["stats"]["models"]) or main
            else:
                thread = event.get("thread_id") or thread if label == "thread.started" else thread
                for part in (event, event.get("msg")):
                    if isinstance(part, dict) and part.get("type") in ANNOUNCES:
                        note(announced, part.get("model"))
    if main:
        announced = {}
        note(announced, main)
    if replies:
        return {"models": sorted(replies, key=lambda name: -replies[name]), "source": "response"}
    if announced:
        return {"models": sorted(announced, key=lambda name: -announced[name]), "source": "init" if kind != "codex" else "stream"}
    if kind == "codex" and isinstance(thread, str):
        model = codex_rollout_model(thread, profile_override(config))
        if model:
            return {"models": [model], "source": "codex_rollout"}
    return {"models": [], "source": None}


LIMIT = re.compile(r"usage limit|hit your limit|rate.limit|quota exceeded|too many requests|(?:HTTP|status|API error)[: ]+429\b", re.I)


def texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return texts(value.get("message")) if isinstance(value, dict) else []


def failure_parts(event):
    """The words in one stream event that a provider uses to report how a call ended or failed, or nothing for ordinary events."""
    label = event.get("type")
    if label == "result":  # the call's verdict; only a result that says it failed is the provider's error, otherwise it is the worker's answer
        failed = event.get("is_error") is True or event.get("status") == "error"
        return [*(texts(event.get("result")) if failed else ()), *texts(event.get("errors")), *texts(event.get("error"))]
    if label in ("error", "turn.failed", "thread.error"):
        return [*texts(event.get("message")), *texts(event.get("error"))]
    if label == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict) and event["rate_limit_info"].get("status") == "rejected":
        return ["rate limit rejected " + json.dumps(event["rate_limit_info"])]
    if label == "assistant" and event.get("error"):  # Claude Code flags the messages it writes about its own failures
        content = event.get("message", {}).get("content") if isinstance(event.get("message"), dict) else None
        return [str(event["error"]), *(block["text"] for block in content or () if isinstance(block, dict) and isinstance(block.get("text"), str))]
    return []


def provider_errors(log, limit=12000):
    """What a provider reported about a failed call, oldest first: its error, failed-turn and result events, a rejected rate limit,
    assistant messages the CLI flagged as errors, and the CLI's own non-JSON lines (stderr shares the log). The assistant's prose,
    its last message, tool output and file contents never count, so text that merely mentions a limit cannot classify a call."""
    found = deque(maxlen=400)
    with Path(log).open("rb") as stream:
        while raw := stream.readline(2 * 1024 * 1024 + 1):
            if len(raw) > 2 * 1024 * 1024:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(2 * 1024 * 1024 + 1)
                continue
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    found.append(line[:2000])
                continue
            if isinstance(event, dict):
                found.extend(failure_parts(event))
    return "\n".join(found)[-limit:]


def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, evidence=None, health=None, images=()):
    executable = check_provider(config, cwd)
    argv, env = adapter(config, mode, session, images)
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
    folders = [] if evidence is None else [Path(evidence)]
    for image in images:
        folder = Path(image).parent
        if folder not in folders and not any(folder.is_relative_to(known) for known in folders):
            folders.append(folder)
    for folder in folders:
        if config["kind"] in {"claude", "deepseek"}:
            argv += ["--add-dir", str(folder)]
        elif config["kind"] == "gemini":
            argv += ["--include-directories", str(folder)]
    with ExitStack() as resources:
        if os.name == "nt" and config["kind"] == "codex" and mode == "worker":
            # Windows sandbox setup grants write access to the child process's temp root.
            # Keep that operation off the user's shared Temp directory.
            scratch_root = default_home() / "provider-temp"
            scratch_root.mkdir(parents=True, exist_ok=True)
            scratch = resources.enter_context(tempfile.TemporaryDirectory(dir=scratch_root, prefix="codex-",
                                                                         ignore_cleanup_errors=True))
            env.update(TEMP=scratch, TMP=scratch, TMPDIR=scratch)
        if config["kind"] == "claude":
            # Written before the call so that a runner killed mid-call still leaves the directory its session lives in.
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                tag_file(log).unlink(missing_ok=True)
                tag_file(log).write_text(config_tag(claude_directory(env)), encoding="ascii")
            except OSError:
                pass
        code = execute(argv, cwd, log, timeout, prompt=prompt, env=env, paused=paused, **(health or {}))
    current, answer = metadata(log, config["kind"], mode)
    if config["kind"] == "command" and code == config.get("unavailable_exit_code"):
        raise ProviderUnavailable(answer.strip()[-1500:] or "External workflow unavailable", "external_environment")
    if code == 0 and config["kind"] != "command":
        for event in json_events(log):
            if (event.get("type") == "result" and (event.get("is_error") is True or event.get("status") == "error")) or event.get("type") == "turn.failed":
                code = 1
    if code and config["kind"] in {"claude", "codex", "deepseek", "gemini"} and code not in CONTROLLER_EXITS:
        # Only what the provider itself reported as a failure is classified, never the worker's own prose or its tool output.
        # A call the controller ended (time, idle or output limit) was not a provider failure at all.
        detail = provider_errors(log)
        if config["kind"] == "deepseek" and re.search(
                r"invalid.*api.?key|authentication_error|authentication.fail|insufficient.balance|invalid.authentication", detail, re.I):
            raise ProviderUnavailable("DeepSeek reported an API credential or balance error. "
                                      "Check the configured key and API account, then resume with --retry.", "credentials_or_balance")
        if config["kind"] == "claude" and re.search(r"reached its time limit|cannot be renewed|setup-token", detail, re.I):
            raise ProviderUnavailable(f"The Claude login on this machine expired and cannot be renewed. {renewal(config)} "
                                      "Then resume this run with --retry.", "authentication")
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
        if LIMIT.search(detail):
            now = datetime.now(timezone.utc)
            quote = re.search(r"[^\n\"{}]{0,120}(?:usage limit|hit your limit|rate.?limit|quota exceeded|too many requests|\b429\b)[^\n\"{}]{0,200}",
                              detail, re.I)
            note = f" Provider said: {redact(' '.join(quote.group(0).split()))}" if quote else ""
            raise ProviderUnavailable(f"{config['kind']} reached a rate or usage limit. Retry after the limit resets.{note}",
                                      "usage_limit", reset_after(detail, now))
        if re.search(r"ECONNRESET|ETIMEDOUT|temporarily unavailable|service unavailable|(?:HTTP|status|API error)[: ]+50[234]\b", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} reported a temporary network or service failure.", "transient")
        if re.search(r"model_not_found|unsupported model|model .*?(?:not found|does not exist|not supported|not available)", detail, re.I):
            raise ProviderUnavailable(f"{config['kind']} could not use the selected model. Check model access for this profile.",
                                      "model_access")
    if config["kind"] == "claude" and not resumable(config, env, session):
        session = None  # a session this call could not resume is not one it continued
    return code, current or session, answer


def json_response(answer):
    from .spec import strict_json
    if len(answer.encode("utf-8")) > 1000000:
        raise ValueError("Structured response exceeds 1MB")
    stripped = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    result = strict_json(fence.group(1) if fence else stripped)
    if not isinstance(result, dict):
        raise ValueError("Structured response must be a JSON object")
    level = [result]
    for _ in range(64):
        level = [child for container in level
                 for child in (container.values() if isinstance(container, dict) else container)
                 if isinstance(child, (dict, list))]
        if not level:
            return result
    raise ValueError("Structured response nesting exceeds 64 levels")


def advisory(config, information, *, receipt=None):
    output_limit = config.get("max_tokens", 100)
    endpoint = f"http://127.0.0.1:{config['port']}"
    headers = {"Content-Type": "application/json"}
    if "think" in config:
        show = Request(endpoint + "/api/show", data=json.dumps({"model": config["model"]}).encode(), headers=headers)
        with urlopen(show, timeout=min(15, config.get("timeout", 90))) as response:
            supported = (json.load(response).get("thinking") or {}).get("values")
        if supported is not None and config["think"] not in supported:
            raise ValueError(f"Ollama model {config['model']} supports thinking settings {supported}, requested {config['think']}")
    body = {"model": config["model"], "stream": False, "think": config.get("think", False),
            "messages": [{"role": "user", "content": information[:3000]}],
            "options": {"num_predict": output_limit, "num_ctx": max(2048, output_limit + 1024)}}
    request = Request(endpoint + "/api/chat", data=json.dumps(body).encode(), headers=headers)
    with urlopen(request, timeout=config.get("timeout", 90)) as response:
        result = json.load(response)
        if result.get("model") not in (config["model"], config["model"] + ":latest"):
            raise ValueError("Ollama returned a different model than requested")
        if receipt is not None:
            receipt.update({key: result.get(key) for key in ("model", "done_reason", "eval_count", "total_duration")})
            receipt["thinking_observed"] = bool(result.get("message", {}).get("thinking"))
        note = result.get("message", {}).get("content", "").strip()
        if not note:
            raise ValueError("Ollama returned no final answer")
        return note[:1500]
