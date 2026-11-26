"""Use native authenticated CLIs and an optional loopback model."""

from collections import deque
from contextlib import ExitStack
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile
from time import monotonic
from urllib.request import Request, urlopen

from .process import CONTROLLER_EXITS, execute
from .store import default_home
from .safety import atomic_write, json_events, own_credentials, redact, scrub_env


READ_ONLY = ("reviewer", "judge", "replanner", "brief", "directions", "comparison", "ranking", "diagnosis")
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
DIAGNOSIS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["diagnosis", "strategies"],
                    "properties": {"diagnosis": {"type": "string"}, "strategies": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
                        "type": "object", "additionalProperties": False, "required": ["name", "approach"],
                        "properties": {"name": {"type": "string"}, "approach": {"type": "string"}}}}}}
SCHEMAS = {"reviewer": {"type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
                        "properties": {"verdict": {"enum": ["PASS", "REVISE"]}, "reason": {"type": "string"}}},
           "judge": JUDGE_SCHEMA, "replanner": WAVE_SCHEMA, "directions": DIRECTIONS_SCHEMA, "comparison": COMPARISON_SCHEMA,
           "ranking": RANKING_SCHEMA, "diagnosis": DIAGNOSIS_SCHEMA}
# What agy is started with (--settings) for every role that must not change anything. write_file and command are the two rule classes
# live runs proved on agy 1.5.17: edit_file and write_file steps ended "Matches user-configured deny rule", and so did run_command. The
# three write_file patterns are the set those runs used. mcp(*) is the documented class for MCP tools and has not been run live. agy
# keeps a rule whatever its class is called (a bogus_tool(*) rule is loaded), so a misspelt class would fail open; antigravity_guard asks
# agy which rules it loaded, and agy_stream reports any write it completed anyway.
AGY_DENY = ("write_file(*)", "write_file(/)", "write_file(.)", "command(*)", "mcp(*)")
# Put in front of the prompt of every read-only agy call. The role prompts say only "remain read-only", so a model that sees gate scripts in the
# repository may try to run them, be refused under the deny rules, and answer anyway; invoke() then discards that answer (read_only_refused).
AGY_READ_ONLY_NOTE = ("This call is read-only. Every file write or edit and every shell or terminal command is denied, and an answer given "
                      "after a denied attempt is discarded. Use only read tools: read files, list folders, search. Everything you need "
                      "(the task, diffs, check results, logs) is in this prompt or in files you can read.")
# The tool steps a read-only role must never complete. Every name below is in the tool list agy 1.5.17 announces in its init event, except
# write_file and edit_file, which the streams show and that list does not: AGY_FILE_WRITERS change a file (Nightweaver can see which one),
# AGY_RUNNERS run something (a shell command, a notebook cell, an MCP tool, a workflow) or change state elsewhere, so their effect is never
# read from a path. AGY_WRITER_NAME catches a writing tool a later agy adds. Only write_file, edit_file and run_command are proven to be
# stopped by the rules; the rest are the stream check's job.
AGY_FILE_WRITERS = ("write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit")
AGY_RUNNERS = ("run_command", "send_command_input", "notebook_execution", "run_workflow", "call_mcp_tool", "generate_image", "delete_knowledge")
AGY_WRITERS = AGY_FILE_WRITERS + AGY_RUNNERS
AGY_WRITER_NAME = re.compile(r"(?:^|_)(?:write|edit|replace|sed|delete|remove|patch|insert|rename|create|append|save|run|exec)(?:_|$)")
AGY_TREE_LIMIT = 20000  # files agy_tree will list before it gives up
# A step of this kind starts a subagent. agy 1.5.17 writes it as one step (step_type "subagent", ACTIVE then DONE, with subagent_info) and none
# of the subagent's own steps, so what a subagent did can only be read from the tree (agy_tree).
AGY_DELEGATES = ("invoke_subagent", "browser_subagent")
AGY_PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "FilePath", "File")  # where a file tool names its file (write_file and edit_file: Path)
AGY_TALK = ("agent_response", "user_input", "system_message", "finish")  # steps that are not tool calls
AGY_PLAN = "implementation_plan.md"
AGY_USAGE = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")


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
    seconds = re.search(r"(?:retry[_-]?after(?:_seconds)?|retry_?delay|reset[s_]?_?in(?:_seconds)?|reset_?after(?:_seconds)?)\W{0,4}(\d+(?:\.\d+)?)(?![\d:])", message, re.I)
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
    if kind == "antigravity":
        return config.get("executable") or antigravity_executable()
    return config.get("executable", "claude" if kind == "deepseek" else kind)


def antigravity_executable():
    """The Antigravity CLI where its installer puts it (%LOCALAPPDATA%/agy/bin, which is not added to PATH), else whatever agy is on PATH."""
    installed = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "agy" / "bin" / "agy.exe"
    return str(installed) if installed.is_file() else "agy"


def find_executable(config, cwd):
    name = provider_executable(config)
    path = Path(name)
    if not path.is_absolute() and (path.parent != Path(".") or name.startswith((".", "/", "\\"))):
        name = str(cwd / path)
    return shutil.which(name)


def profile_override(config):
    kind = config["kind"]
    if kind == "antigravity":
        return None  # agy keeps its sign-in in the operating system's credential store, not in a directory a route can choose
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
                   Path.home() if kind == "gemini" else Path.home() / ".gemini" / "antigravity-cli" if kind == "antigravity"
                   else Path.home() / f".{kind}")
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
    if config["kind"] == "antigravity":
        antigravity_login(executable)
    return executable


NPM_PACKAGE = "node_modules/@google/gemini-cli"
GEMINI_ENTRIES = ("bundle/gemini.js", "dist/index.js")  # 0.70 ships the first; older packages the second


def gemini_entrypoint(launcher):
    """The JavaScript file behind npm's gemini.cmd, so it can start under node without a shell: the package's own bin entry,
    then bundle/gemini.js and dist/index.js. None when the package is not installed beside the launcher."""
    package = Path(launcher).parent / NPM_PACKAGE
    try:
        declared = json.loads((package / "package.json").read_text(encoding="utf-8-sig")).get("bin")
    except (OSError, ValueError, AttributeError):
        declared = None
    names = [declared] if isinstance(declared, str) else [declared.get("gemini")] if isinstance(declared, dict) else []
    for name in [*names, *GEMINI_ENTRIES]:
        entry = package / name if isinstance(name, str) and name else None
        if entry and entry.is_file() and entry.resolve().is_relative_to(package.resolve()):
            return entry
    return None


def gemini_auth(config):
    """How a Gemini CLI route signs in, from the environment and the profile alone: an API key, Google Cloud or saved login. Never a secret."""
    env = scrub_env(os.environ, own_credentials("gemini", os.environ))
    given = {name.upper(): str(value).strip() for name, value in env.items()}
    if given.get("GEMINI_API_KEY") or given.get("GOOGLE_API_KEY"):
        return "api_key"
    if any(given.get(name, "").lower() in ("1", "true", "yes") for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA")):
        return "google_cloud"
    home = Path(profile_override(config) or Path.home()).expanduser()
    return "oauth_login" if (home / ".gemini" / "oauth_creds.json").is_file() else "none_found"


SIGNED_OUT = re.compile(r"sign in|signed in|log in|login required|authentication required|not authenticated", re.I)
AGY_SIGNED_IN = {}  # agy path -> when `agy models` last succeeded


def agy_run(executable, env, *args, timeout=30):
    """(exit code, output) of one short agy command, or (None, "") when it could not run in time."""
    try:
        done = subprocess.run([executable, *args], capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=timeout, text=True,
                              encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout + done.stderr


def antigravity_settings():
    """agy's own settings.json as a dict ({} when it is absent or unreadable). Only its modelProvider is ever looked at."""
    try:
        found = json.loads((Path.home() / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def antigravity_key(env):
    return any(str(env.get(name) or "").strip() for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"))


def antigravity_source(env):
    """How an agy route signs in. A key in the environment does nothing by itself: agy uses it only when its settings.json says
    "modelProvider": "gemini" (observed with agy 1.5.15: with the variable alone it prints a sign-in link and waits a minute for a code).
    Otherwise it is Application Default Credentials (AGY_ADC_AUTH) or the CLI's own browser login."""
    if antigravity_settings().get("modelProvider") == "gemini":
        return "api_key"
    given = {name.upper(): str(value).strip() for name, value in env.items()}
    return "adc" if given.get("AGY_ADC_AUTH", "").lower() in ("1", "true", "yes", "on") else "cli_login"


def antigravity_login(executable):
    """Stop a call that would sign in on its own. Signed out, agy prints an OAuth link and waits a minute for a code that a
    headless run can never give, so `agy models` (exit 1, "Please sign in") is asked first, also when only GEMINI_API_KEY is set,
    because the key counts only with "modelProvider": "gemini" in settings.json. A route that selects the Gemini API there is
    checked for its key instead, and an ADC route is left to the call, which fails fast with "authentication required"."""
    env = scrub_env(os.environ, own_credentials("antigravity", os.environ))
    source = antigravity_source(env)
    if source == "api_key" and not antigravity_key(env):
        raise ProviderUnavailable("agy's settings.json selects the Gemini API (\"modelProvider\": \"gemini\"), but neither GEMINI_API_KEY "
                                  "nor GOOGLE_API_KEY is set in this process. Set one in the environment Nightweaver runs in, or remove "
                                  "modelProvider to use agy's own login. Then resume this run with --retry.", "authentication")
    if source != "cli_login" or monotonic() - AGY_SIGNED_IN.get(executable, -1e9) < 300:
        return
    code, output = agy_run(executable, env, "models")
    if code == 0:
        AGY_SIGNED_IN[executable] = monotonic()
    elif code is not None and SIGNED_OUT.search(output):
        ignored = (" GEMINI_API_KEY is set, but agy ignores it unless settings.json has \"modelProvider\": \"gemini\"; without that it "
                   "waits for a browser sign-in." if antigravity_key(env) else "")
        raise ProviderUnavailable("The Antigravity CLI is not signed in." + ignored + " Run agy once and sign in with the Google account "
                                  "that has the model (Gemini 4 Neon needs Google AI Pro or Ultra), or use an API key: set GEMINI_API_KEY "
                                  "and \"modelProvider\": \"gemini\" in ~/.gemini/antigravity-cli/settings.json. Then resume this run "
                                  "with --retry.", "authentication")


def agy_rules(output):
    """The deny rules agy's /permissions command listed, from its structured command_result event (every scope), or None when the
    output holds no such event. /permissions is a free command: no model turn, no tokens."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(event, dict) and event.get("event") == "command_result" and isinstance(event.get("command"), dict)):
            continue
        command = event["command"]
        scopes = command["data"].get("permissions") if command.get("name") == "permissions" and isinstance(command.get("data"), dict) else None
        if isinstance(scopes, list):
            return {rule for scope in scopes if isinstance(scope, dict) and isinstance(scope.get("deny"), list)
                    for rule in scope["deny"] if isinstance(rule, str)}
    return None


AGY_READ_ONLY = {}  # agy path -> when its read-only rules were last seen loaded


def antigravity_rules(executable, env):
    """"enforced" when agy reports every read-only deny rule as loaded from the --settings file Nightweaver passes, "missing" when it
    reports the rules without them, None when it could not say (it did not run in time, or printed no command_result). It asks
    `agy -p /permissions --settings FILE`, which agy answers without a model turn (checked on agy 1.5.17). A misspelt rule class would
    still load, so this proves the file reached agy and was applied, not that every class name means something."""
    code, output = agy_run(executable, env, "-p", "/permissions", "--output-format", "stream-json", "--settings", str(settings_file()))
    loaded = agy_rules(output) if code == 0 else None
    state = None if loaded is None else "enforced" if set(AGY_DENY) <= loaded else "missing"
    if state == "enforced":
        AGY_READ_ONLY[executable] = monotonic()
    return state


def antigravity_guard(executable):
    """Stop a read-only call that agy would run without the deny rules. The barrier of every read-only role is that file, so a call is
    not made when agy reports the rules did not load (a changed settings format, a policy that overrides them). A cleared check is
    remembered for five minutes. Where agy cannot say, the call goes on: its own stream is checked afterwards (agy_violation). The
    check is skipped when agy's settings.json turns slash commands off, because /permissions would then be a model turn."""
    if monotonic() - AGY_READ_ONLY.get(executable, -1e9) < 300 or antigravity_settings().get("disableSlashCommands"):
        return
    if antigravity_rules(executable, scrub_env(os.environ, own_credentials("antigravity", os.environ))) == "missing":
        raise ProviderUnavailable("agy did not load the read-only permission rules that Nightweaver passes with --settings "
                                  f"({', '.join(AGY_DENY)}), so a reviewer, judge or planner would run with permission to edit files "
                                  "and the call was not made. Run agy -p /permissions --settings <file> to see what it loaded, update "
                                  "agy or remove the setting or policy that overrides it, then resume this run with --retry.",
                                  "configuration")


def antigravity_report(config, cwd):
    """What doctor shows for an agy route besides its executable: version, how it signs in, whether it is signed in (None when agy
    could not say), whether a key in the environment is being ignored, whether agy loads the read-only rules (read_only_rules) and,
    when signed in and the route names a model, whether `agy models` lists it. Never a secret."""
    executable = find_executable(config, cwd)
    if not executable:
        return {}
    env = scrub_env(os.environ, own_credentials("antigravity", os.environ))
    _, version = agy_run(executable, env, "--version", timeout=15)
    code, listing = agy_run(executable, env, "models")
    report = {"auth": antigravity_source(env), "version": (version.strip().splitlines() or [None])[0],
              "signed_in": True if code == 0 else False if code is not None and SIGNED_OUT.search(listing) else None,
              "read_only_rules": None if antigravity_settings().get("disableSlashCommands") else antigravity_rules(executable, env)}
    if report["auth"] != "api_key" and antigravity_key(env):
        report["api_key_ignored"] = True  # GEMINI_API_KEY is set, but settings.json does not select the Gemini API
    if report["signed_in"] and config.get("model"):
        report["model_listed"] = config["model"].strip().lower() in listing.lower()
    return report


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


def provider_file(name, body):
    """A small file in Nightweaver's own home that a provider reads by path (agy's --json-schema and --settings). The content is fixed,
    so concurrent calls write the same bytes and a stale copy is rewritten."""
    target = default_home() / "provider-temp" / name
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        current = None
    if current != body:
        try:
            atomic_write(target, body)
        except OSError:
            if current is None:  # another call holds the file open on Windows; a readable copy is good enough
                raise
    return target


def schema_file(mode):
    """The JSON schema of a structured mode, for agy's --json-schema."""
    return provider_file(f"agy-schema-{mode}.json", json.dumps(SCHEMAS[mode], indent=2) + "\n")


def settings_file():
    """The read-only rules, for agy's --settings: a JSON object of settings.json keys, here only permissions.deny."""
    return provider_file("agy-settings-readonly.json", json.dumps({"permissions": {"deny": list(AGY_DENY)}}, indent=2) + "\n")


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
        # A Nightweaver worktree is never in the CLI's trusted-folders list, and a headless run in an untrusted folder exits at
        # startup. The variable is honoured by 0.70 and ignored by older versions; --skip-trust would fail on those.
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        argv = [exe, "--output-format", "stream-json", "--approval-mode", "auto_edit" if mode == "worker" else "plan",
                "--prompt", "Follow the task supplied on standard input."]
        if model:
            argv += ["--model", model]
        if session:
            argv += ["--resume", session]
        return argv, env
    if kind == "antigravity":
        # The task goes in on stdin as one stream-json user message. --print takes its prompt as a flag value and then ignores stdin
        # (and --input-format stream-json rejects a flag prompt), so a task of any length can only travel this way.
        # Every role runs --mode accept-edits, so the machine's own agentMode (auto, by default) never decides. The worker may edit; a
        # command that needs review is soft-denied in a headless run. Every other role is read-only by agy's permission rules
        # (--settings, see AGY_DENY), which live runs proved is the one barrier that stops edit and write tools and shell commands.
        # Plan mode is not used: it is only the model's obedience (no write was ever attempted under it), and for a task-like prompt
        # agy answers with an implementation_plan.md instead of the requested JSON. --sandbox restricts terminal commands only (an
        # edit went through under it), so it is no barrier either. The worker keeps --disable-slash-commands, which stops a task that
        # starts with "/" from running as a command; invoke() guards a read-only prompt itself.
        readonly = mode != "worker"
        argv = [exe, "--input-format", "stream-json", "--output-format", "stream-json",
                *([] if readonly else ["--disable-slash-commands"]), "--mode", "accept-edits"]
        if config.get("effort"):
            argv += ["--effort", config["effort"]]
        if model:
            argv += ["--model", model]
        if readonly:
            argv += ["--settings", str(settings_file())]
        if mode in SCHEMAS:
            argv += ["--json-schema", str(schema_file(mode))]
        if session:
            argv += ["--conversation", session]
        if os.environ.get("NIGHTWEAVER_ANTIGRAVITY_SANDBOX") == "1":
            argv.append("--sandbox")  # opt-in: on Windows the CLI's sandbox needs Windows 11 24H2 or later
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


AGY_FAILED_REASON = re.compile(r"error|time.?out|interrupt|cancel|abort|fail", re.I)
AGY_DENIED = re.compile(r"deny rule", re.I)  # "... Matches user-configured deny rule." ends a tool step in state ERROR, and the run goes on


def agy_failed(body):
    """Whether an Antigravity result ends a failed call: a status other than SUCCESS, or a finished_reason that names an error, a
    timeout or an interruption. A success carries no finished_reason; the real error result has status ERROR and finished_reason "error"."""
    status, reason = str(body.get("status") or "").upper(), body.get("finished_reason")
    return bool((status and status != "SUCCESS") or (reason and AGY_FAILED_REASON.search(str(reason))))


def agy_where(path):
    """A path as one comparable string: forward slashes, `.` and `..` resolved, case folded (agy runs on Windows, where both are the same)."""
    return posixpath.normpath(str(path).replace("\\", "/")).casefold()


def agy_scratch(path):
    """Whether a file path is inside agy's own conversation folder, <home>/.gemini/antigravity-cli/brain, where it keeps plans and notes.
    The path is resolved first and compared with that folder under this process's home, so a path that only contains the folder's name
    (a repository with such a subfolder, another home) or climbs out of it with `..` is not agy's own."""
    return bool(str(path).strip()) and agy_where(path).startswith(agy_where(Path.home() / ".gemini" / "antigravity-cli" / "brain") + "/")


def agy_writes(name):
    """Whether a tool step by this name changes files or runs something."""
    return name in AGY_WRITERS or bool(AGY_WRITER_NAME.search(name))


def agy_target(name, params):
    """(text for a message, whether the step is only agy writing in its own folder). Only a file tool is exempt, and only when every path it
    names is agy's own: a command line is never a path, and a step that names no path gives no proof."""
    paths = [str(params[key]) for key in AGY_PATH_KEYS if params.get(key)]
    own = name not in AGY_RUNNERS and bool(paths) and all(agy_scratch(path) for path in paths)
    return (paths or [str(params.get("CommandLine") or "")])[0], own


def agy_tree(folder, skip=()):
    """{relative path: (size, mtime_ns)} of every file under folder except .git and node_modules (and the files named in skip), or None when
    it cannot be read or holds more than AGY_TREE_LIMIT files. Nightweaver's own check of what a subagent did, which the stream does not show."""
    found, skip = {}, {os.path.normcase(os.path.abspath(path)) for path in skip}
    try:
        for base, folders, files in os.walk(folder):
            folders[:] = [name for name in folders if name not in (".git", "node_modules")]
            for name in files:
                path = os.path.join(base, name)
                if os.path.normcase(os.path.abspath(path)) not in skip:
                    info = os.stat(path)
                    found[os.path.relpath(path, folder)] = (info.st_size, info.st_mtime_ns)
            if len(found) > AGY_TREE_LIMIT:
                return None
    except OSError:
        return None
    return found


def agy_changes(before, after):
    """The files that differ between two agy_tree results, sorted ([] when either is unknown)."""
    if before is None or after is None:
        return []
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def agy_answer(body, mode, said):
    """(answer, where it came from) of one result event. A schema mode takes structured_output: agy re-asks until the model gives one that
    fits, and `response` joins every agent message of the run (narration included, the JSON twice after a re-ask), so it is no answer for
    those. Any other mode takes `response`. When that is blank too the answer is the last agent message with text; a final message with
    none, as after a tool call, does not hide an earlier one."""
    structured = body.get("structured_output")
    if mode in SCHEMAS and isinstance(structured, dict):
        return json.dumps(structured), "structured_output"
    if mode in SCHEMAS and isinstance(structured, str) and structured.strip():
        return structured, "structured_output"
    if isinstance(body.get("response"), str) and body["response"].strip():
        return body["response"], "response"
    return (said[-1], "agent_response") if said else ("", None)


def agy_stream(path, mode=None):
    """What an Antigravity `--output-format stream-json` log says, from the shape agy 1.5.17 wrote in nine signed-in runs.

    Events are {"event": "init" | "step_update" | "result", "<event>": {...}} (fields beside "event" are accepted too). init names the
    conversation_id and model. step_update names conversation_id, step_index, state (ACTIVE, DONE, ERROR) and step_type (user_input,
    agent_response, tool, system_message, finish). An agent_response streams text_delta chunks (ACTIVE) and its DONE event holds the last
    chunk, or the whole message when none was streamed, with that step's usage; a tool step carries tool_name and tool_info (parameters,
    output, or error when agy refused it). result names conversation_id, status, response, structured_output (with --json-schema),
    num_turns and usage, cumulative over a resumed conversation; finished_reason and error only appear on a failure. A subagent shows as
    one step (step_type subagent, tool_name invoke_subagent, ACTIVE then DONE, subagent_info naming its prompt, conversation and workspace)
    and none of its own steps are in this stream (checked on 1.5.17), so what it did is not known here. A step that did name another
    conversation is tolerated: only init and result name this one, and another conversation's words are not this call's answer.

    Returns {session, answer, source, usage (the result's), call_usage (this call's: the sum of its steps' own usage, which a resumed
    conversation's cumulative result cannot say), model, failed, status, reason, error, messages (agent messages with text), denied
    (tool steps agy refused under a deny rule), refused (the denied ones outside agy's own folder: a read-only role tried to change
    something), wrote (tool steps that change files or run something and completed, see agy_writes, outside agy's own folder),
    delegated (subagent steps, whose own steps are not in the stream) and plan_file (the implementation_plan.md agy wrote, named in the
    stream and never read)}."""
    found = {"session": None, "answer": "", "source": None, "usage": None, "call_usage": None, "model": None, "failed": False,
             "status": None, "reason": None, "error": None, "messages": 0, "denied": [], "refused": [], "wrote": [], "delegated": [],
             "plan_file": None}
    texts, spent, result = {}, {}, None
    for event in json_events(path):
        label = event.get("event")
        body = event.get(label) if isinstance(event.get(label), dict) else event
        for holder in (event, body):  # a step may belong to a subagent's conversation, so only init and result name this one
            if isinstance(holder.get("conversation_id"), str) and holder["conversation_id"].strip() and (
                    label in ("init", "result") or not found["session"]):
                found["session"] = holder["conversation_id"].strip()
        if label == "init" and isinstance(body.get("model"), str) and body["model"].strip():
            found["model"] = body["model"].strip()
        elif label == "step_update":
            conversation, index = body.get("conversation_id"), body.get("step_index")
            ours = not isinstance(conversation, str) or not found["session"] or conversation.strip() == found["session"]
            if body.get("step_type") == "agent_response":
                if ours and isinstance(body.get("text_delta"), str):
                    texts[index] = (texts.get(index, "") + body["text_delta"])[-100000:]
                if body.get("state") == "DONE" and isinstance(body.get("usage"), dict):
                    spent[(conversation, index)] = body["usage"]
            elif body.get("step_type") not in AGY_TALK and (body.get("tool_name") or isinstance(body.get("tool_info"), dict)
                                                            or isinstance(body.get("subagent_info"), dict) or body.get("step_type") == "subagent"):
                info = body.get("tool_info") if isinstance(body.get("tool_info"), dict) else {}
                name = str(body.get("tool_name") or info.get("name") or "")
                params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
                target, own = agy_target(name, params)
                refusal = info.get("error", {}).get("message") if isinstance(info.get("error"), dict) else None
                if name in AGY_DELEGATES or body.get("step_type") == "subagent" or isinstance(body.get("subagent_info"), dict):
                    if (name or "subagent") not in found["delegated"]:
                        found["delegated"].append(name or "subagent")
                elif body.get("state") == "ERROR" and isinstance(refusal, str) and AGY_DENIED.search(refusal):
                    found["denied"].append(f"{name} {target}"[:300])
                    if not own:
                        found["refused"].append(f"{name} {target}"[:300])
                elif body.get("state") == "DONE" and agy_writes(name) and not own:
                    found["wrote"].append(f"{name} {target}"[:300])
                if name == "write_file" and body.get("state") != "ERROR" and re.split(r"[\\/]", str(params.get("Path") or ""))[-1].lower() == AGY_PLAN:
                    found["plan_file"] = str(params["Path"])
        elif label == "result":
            result = body
            found.update(status=str(body.get("status") or "").upper() or None, failed=agy_failed(body), reason=body.get("finished_reason"),
                         usage=body.get("usage") if isinstance(body.get("usage"), dict) else found["usage"],
                         error=body.get("error") if body.get("error") else found["error"])
    said = [text.strip() for _, text in sorted(texts.items(), key=lambda item: (item[0] is None, item[0] if isinstance(item[0], int) else 0))
            if text.strip()]
    found["messages"] = len(said)
    found["answer"], found["source"] = agy_answer(result or {}, mode, said)
    steps = [{key: value[key] for key in AGY_USAGE if type(value.get(key)) is int} for value in spent.values()]
    found["call_usage"] = {key: sum(part.get(key, 0) for part in steps) for key in AGY_USAGE if any(key in part for part in steps)} \
        if any(steps) else found["usage"]
    return found


def agy_silence(found, mode, log):
    """Why an Antigravity call that finished with status SUCCESS gave no answer, in words an operator can act on."""
    said = (f"{found['messages']} agent message{'s' if found['messages'] != 1 else ''} with text" if found["messages"] else
            "no agent message with text") + (", no structured output" if mode in SCHEMAS else "")
    notes = []
    if found["denied"]:
        notes.append(f"agy refused {len(found['denied'])} tool action{'s' if len(found['denied']) != 1 else ''} under the read-only rules "
                     f"({'; '.join(found['denied'][:3])}).")
    if found["plan_file"]:
        notes.append(f"It wrote its implementation plan to {found['plan_file']} instead of answering. Nightweaver does not read that file "
                     "as the answer: it is a plan to make edits, not what this role returns.")
    return " ".join([f"The Antigravity CLI finished the {mode} call (status {found['status'] or 'SUCCESS'}) but returned no answer: {said}.",
                     *notes, f"See {log}. A run retries this as a transient failure before it blocks; resume with --retry once agy answers."])


def agy_violation(found, mode, log):
    """The message for a read-only call that completed a write, an edit or a command anyway."""
    return (f"The Antigravity {mode} call is read-only, but agy completed {len(found['wrote'])} action{'s' if len(found['wrote']) != 1 else ''} "
            f"that change files ({'; '.join(found['wrote'][:3])}), so its permission rules did not hold and its answer is discarded. "
            f"Check the worktree for edits and run doctor --live. See {log}.")


def agy_refusal(found, mode, log):
    """The message for a read-only call that tried to change something, was refused under the deny rules and answered anyway."""
    count = len(found["refused"])
    return (f"The Antigravity {mode} call is read-only, but agy tried {count} action{'s' if count != 1 else ''} that change files or run commands "
            f"and was refused under the permission rules ({'; '.join(found['refused'][:3])}). It answered anyway, and an answer written after refused "
            f"actions can describe the refusals instead of the work, so it is not taken as a {mode} result. See {log}. A run retries this as a "
            "transient failure before it blocks; resume with --retry once the role stays read-only.")


def metadata(path, kind, mode=None):
    """(session id, last answer) of one provider call's log. A Claude id carries its config directory as id@tag; see resumable()."""
    session, last = None, ""
    if not path.exists():
        return session, last
    if kind == "command":
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 10000))
            return None, stream.read().decode("utf-8", "replace")
    if kind == "antigravity":
        found = agy_stream(path, mode)
        return found["session"], found["answer"][-100000:]
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
    # agy's result.usage is cumulative over a resumed conversation (--conversation), so a call counts what its own steps reported
    for event in ([{"usage": agy_stream(path)["call_usage"]}] if kind == "antigravity" else json_events(path)):
        values = None
        if kind == "codex" and event.get("type") == "turn.completed":
            values = event.get("usage")
        elif kind in ("claude", "deepseek") and event.get("type") == "result":
            values = event.get("usage")
        elif kind == "gemini" and event.get("type") == "result":
            stats = event.get("stats", {})
            if isinstance(stats, dict):
                values = stats
        elif kind == "antigravity":
            values = event["usage"]
        if not isinstance(values, dict):
            continue
        names = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                 "cached_input_tokens", "cached", "input", "output", "thinking_tokens", "cache_read_tokens")
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


def effortless(name):
    """A Gemini 4 id without the reasoning-level suffix agy appends: gemini-4-neon-xhigh is gemini-4-neon at xhigh."""
    return re.sub(r"^(gemini-4-[a-z]+)-(?:low|medium|high|xhigh|max)$", r"\1", name)


def same_model(requested, served):
    """Whether a reported model id is the one a route asked for. A bare family alias such as sonnet matches claude-sonnet-5-5,
    and a Gemini alias such as pro matches gemini-2.5-pro. auto and auto-gemini-* routes name no model to check. A Gemini 4 id
    matches its slug with a level (gemini-4-neon, gemini-4-neon-xhigh) unless both name a level and the levels differ."""
    asked, got = canonical_model(requested), canonical_model(served)
    if asked == got or asked in UNVERIFIABLE or asked.startswith("auto-") or (asked in FAMILIES and asked in got.split("-")):
        return True
    if effortless(asked) == effortless(got) and (asked == effortless(asked) or got == effortless(got)):
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
    served_here = agy_stream(log)["model"] if kind == "antigravity" else None  # agy names the model in its init event, when asked with --model

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
    note(announced, served_here)
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
    if event.get("event") == "result" and "type" not in event:  # Antigravity: a result that is not SUCCESS (see agy_stream) carries the error
        body = event["result"] if isinstance(event.get("result"), dict) else event
        if not agy_failed(body):
            return []
        error = body.get("error")
        return [*texts(error), *([json.dumps(error)] if isinstance(error, dict) else []), *texts(body.get("finished_reason"))]
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


GOOGLE_KINDS = ("gemini", "antigravity")


def http_code(*codes):
    """How Google's failures state an HTTP status: the Gemini API's JSON "code": 429, agy's AGY_ERROR "error_code": 429 and the
    "Error 429, Message: ..." text agy prints."""
    return r"(?:\"(?:error_)?code\"\s*:\s*|\bError\s+)(?:" + "|".join(map(str, codes)) + r")\b"


GOOGLE_AUTH = re.compile(r"please set an auth method|specify one of the following environment variables|authentication required|"
                         r"authentication (?:failed|timed out)|not signed in|please sign in|sign in once with|api key (?:not valid|expired|invalid)|"
                         r"API_KEY_INVALID|invalid api key|UNAUTHENTICATED|modelProvider is set to", re.I)
GOOGLE_TRUST = re.compile(r"not running in a trusted directory|untrusted (?:workspace|folder|directory)|FatalUntrustedWorkspaceError", re.I)
# Gemini CLI's own capacity test is /exhausted your capacity|capacity exceeded|MODEL_CAPACITY_EXHAUSTED/ and it ends a 429 with
# "Please wait and try again later. To increase your limits, request a quota increase through AI Studio".
GOOGLE_LIMIT = re.compile(r"exhausted your (?:daily )?(?:quota|capacity)|RESOURCE_EXHAUSTED|MODEL_CAPACITY_(?:EXHAUSTED|EXCEEDED)|capacity exceeded|"
                          r"no capacity available for model|quota will reset after|request a quota increase|exceeded your (?:current )?quota|"
                          r"daily quota|spend cap|prepaid credits|rate limit exceeded|" + http_code(429), re.I)
GOOGLE_BUSY = re.compile(r"model is overloaded|high demand|\"status\"\s*:\s*\"UNAVAILABLE\"|status:\s*UNAVAILABLE|" + http_code(502, 503, 504), re.I)
# A key or account the service refuses: Gemini 4 Neon is paid-tier only, so a key without it gets a 403.
GOOGLE_DENIED = re.compile(r"(?-i:PERMISSION_DENIED)|" + http_code(403), re.I)
GOOGLE_MODEL = re.compile(r"requested entity was not found|is not found for API version|model not found|no model configuration is available|"
                          r"\"status\"\s*:\s*\"NOT_FOUND\"|status:\s*NOT_FOUND|" + http_code(404), re.I)
# agy checks --model and --effort before it calls anything and exits 1 with, for example, 'error: invalid model selection (--model "x"
# --effort ""): model x is not recognized as a known model or custom model in settings' or '... gemini-3.1-pro has no "xhigh" effort
# (available: low, high)' or '--model gemini-4-neon-xhigh conflicts with --effort=high'. No retry can fix them.
AGY_SELECTION = re.compile(r"invalid model selection[^\n]*", re.I)
LIMIT_WORDS = r"usage limit|hit your limit|rate.?limit|quota exceeded|too many requests|\b429\b"
GOOGLE_LIMIT_WORDS = LIMIT_WORDS + "|" + GOOGLE_LIMIT.pattern


def usage_limit(kind, detail):
    now = datetime.now(timezone.utc)
    quote = re.search(r"[^\n\"{}]{0,120}(?:" + (GOOGLE_LIMIT_WORDS if kind in GOOGLE_KINDS else LIMIT_WORDS) + r")[^\n\"{}]{0,200}", detail, re.I)
    note = f" Provider said: {redact(' '.join(quote.group(0).split()))}" if quote else ""
    return ProviderUnavailable(f"{kind} reached a rate or usage limit. Retry after the limit resets.{note}", "usage_limit", reset_after(detail, now))


def google_problem(config, code, detail):
    """The ProviderUnavailable a Gemini CLI or Antigravity failure stands for, or None for an ordinary failed attempt.

    A missing sign-in or key, an untrusted folder, an exhausted quota or capacity, a model the account cannot reach and a busy model
    are account or service problems that another coding attempt cannot fix, so they park or block the run instead of using up attempts.
    Gemini's own exit codes count too: 41 is no usable authentication, 52 a configuration error and 55 an untrusted folder."""
    kind = config["kind"]
    name = "Gemini CLI" if kind == "gemini" else "Antigravity CLI"
    if (kind == "gemini" and code == 41) or GOOGLE_AUTH.search(detail):
        how = ("Set GEMINI_API_KEY (a paid AI Studio key; the CLI's Google-account login is Enterprise-only now), or use Vertex with "
               "GOOGLE_API_KEY and GOOGLE_GENAI_USE_VERTEXAI=true, or use the antigravity kind." if kind == "gemini" else
               "Run agy once and sign in, or set GEMINI_API_KEY with \"modelProvider\": \"gemini\" in ~/.gemini/antigravity-cli/settings.json.")
        return ProviderUnavailable(f"{name} has no usable sign-in or API key. {how} Then resume this run with --retry.", "authentication")
    if GOOGLE_TRUST.search(detail) or (kind == "gemini" and code == 55):
        return ProviderUnavailable(f"{name} refused to run in an untrusted folder even though Nightweaver sets GEMINI_CLI_TRUST_WORKSPACE=true. "
                                   "Remove GEMINI_RESTRICTED_MODE and any trusted-folders override, then resume this run with --retry.", "configuration")
    if kind == "gemini" and code == 52:
        return ProviderUnavailable("Gemini CLI reported a configuration error (exit 52). Check its settings.json, then resume this run "
                                   "with --retry.", "configuration")
    if picked := AGY_SELECTION.search(detail):
        quote = redact(" ".join(picked.group(0).split()))[:300]
        return ProviderUnavailable(f"{name} rejected the route's model or effort ({quote}). Fix the route's model or effort (agy models lists "
                                   "what this sign-in offers), then resume this run with --retry.",
                                   "model_access" if re.search(r"not recognized as a known model", quote, re.I) else "configuration")
    if GOOGLE_LIMIT.search(detail):
        return usage_limit(kind, detail)
    if GOOGLE_DENIED.search(detail):
        return ProviderUnavailable(f"{name} was refused access (403 PERMISSION_DENIED). This key or account cannot use the selected model (Gemini 4 "
                                   "Neon needs a paid-tier API key or a Google AI Pro or Ultra plan) or the API is not enabled for its project. "
                                   "Fix the access, then resume this run with --retry.", "model_access")
    if GOOGLE_MODEL.search(detail):
        return ProviderUnavailable(f"{name} could not use the selected model. Check the model id and that this account or key can use it.", "model_access")
    if GOOGLE_BUSY.search(detail):
        return ProviderUnavailable(f"{name} reported that the model is overloaded or unavailable.", "transient")
    return None


def invoke(config, mode, cwd, log, prompt, timeout, paused, session=None, evidence=None, health=None, images=()):
    executable = check_provider(config, cwd)
    argv, env = adapter(config, mode, session, images)
    argv = [executable, *argv[1:]]
    if config["kind"] == "antigravity" and "--settings" in argv:
        antigravity_guard(executable)  # a read-only role is not started when agy did not load its deny rules
    if Path(executable).suffix.casefold() in (".cmd", ".bat"):
        # npm's Windows launcher is a shell script. Resolve the package's own JavaScript entrypoint without executing a shell.
        entry = gemini_entrypoint(executable) if config["kind"] == "gemini" else None
        node = Path(executable).with_name("node.exe")
        node = str(node) if node.is_file() else shutil.which("node")
        if config["kind"] != "gemini":
            raise ProviderUnavailable("Use a native executable; npm launchers are only resolved for the Gemini CLI.", "missing_executable")
        if not entry or not node:
            raise ProviderUnavailable(f"Cannot start the Gemini CLI from {executable}: " + (
                "node is not installed or not on PATH." if entry else
                "its npm package (node_modules/@google/gemini-cli with bundle/gemini.js or dist/index.js) is not beside the launcher. "
                "Reinstall it with npm install -g @google/gemini-cli."), "missing_executable")
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
        # agy has no image attachment: an image reaches it only as a path in the prompt, which its read tool opens from a folder added here.
        if config["kind"] in {"claude", "deepseek", "antigravity"}:
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
        if config["kind"] == "antigravity" and "--settings" in argv:
            prompt = AGY_READ_ONLY_NOTE + "\n\n" + prompt  # a read-only role is told what is denied; the worker's prompt is never touched
        if config["kind"] == "antigravity" and "--disable-slash-commands" not in argv and prompt.lstrip().startswith("/"):
            # A read-only role's prompt must not run as a slash command (the worker's flag does that). The note now comes first, so this no
            # longer fires; it stays as a guard in case the note is ever empty or moved.
            prompt = "Task:\n" + prompt
        message = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n" if config["kind"] == "antigravity" else prompt
        # A subagent's steps are not in agy's stream, so what a read-only call's subagent did is read from the files (only when it ran one).
        before = agy_tree(cwd, [log]) if config["kind"] == "antigravity" and mode != "worker" else None
        code = execute(argv, cwd, log, timeout, prompt=message, env=env, paused=paused, **(health or {}))
    current, answer = metadata(log, config["kind"], mode)
    found = agy_stream(log, mode) if config["kind"] == "antigravity" else None
    if found and found["delegated"] and mode != "worker" and (changed := agy_changes(before, agy_tree(cwd, [log]))):
        found["wrote"].append(f"{found['delegated'][0]} (its steps are not in the stream; the files changed: {', '.join(changed[:3])})"[:300])
    if found and found["wrote"] and mode != "worker":
        # The deny rules are the barrier, this reads what agy completed, and Nightweaver's snapshot checks are the net behind both.
        raise ProviderUnavailable(agy_violation(found, mode, log), "read_only_violation")
    if config["kind"] == "command" and code == config.get("unavailable_exit_code"):
        raise ProviderUnavailable(answer.strip()[-1500:] or "External workflow unavailable", "external_environment")
    if code == 0 and config["kind"] != "command":
        for event in json_events(log):
            if (event.get("type") == "result" and (event.get("is_error") is True or event.get("status") == "error")) or event.get("type") == "turn.failed":
                code = 1
        if found and found["failed"]:
            code = 1  # a stream-json session can end on a failed turn and still exit 0
    if found and code == 0 and mode != "worker" and not answer.strip():
        # A read-only role exists to answer. An exit 0 with nothing to read is a failure that says so (a worker's prose is not its product).
        raise ProviderUnavailable(agy_silence(found, mode, log), "no_answer")
    if found and code == 0 and mode != "worker" and found["refused"]:
        # The rules held, but the role was not doing its job: it went for an edit or a command and answered afterwards.
        raise ProviderUnavailable(agy_refusal(found, mode, log), "read_only_refused")
    if code and config["kind"] in {"claude", "codex", "deepseek", "gemini", "antigravity"} and code not in CONTROLLER_EXITS:
        # Only what the provider itself reported as a failure is classified, never the worker's own prose or its tool output.
        # A call the controller ended (time, idle or output limit) was not a provider failure at all.
        detail = provider_errors(log)
        if config["kind"] in GOOGLE_KINDS and (problem := google_problem(config, code, detail)):
            raise problem
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
            raise usage_limit(config["kind"], detail)
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
