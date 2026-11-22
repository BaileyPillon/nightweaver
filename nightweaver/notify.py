"""Opt-in phone notifications and a decision inbox over the ntfy protocol.

ntfy takes a plain HTTP POST or PUT to <server>/<topic> and lets a phone app subscribe to that topic. Replies are read by
polling <server>/<reply_topic>/json. Nothing is ever sent unless providers.json holds a notify.ntfy block, which only
`nightweaver notify setup` writes. Topics are random because anyone who knows a topic name can read it."""

import atexit
import base64
from datetime import datetime, timedelta, timezone
import json
import mimetypes
import platform
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .safety import redact, write_json
from .store import timestamp

TIMEOUT = 5
DEFAULT_SERVER = "https://ntfy.sh"
DEFAULT_EVENTS = ("goal_met", "run_completed", "pursuit_stopped", "blocked", "provider_unavailable", "provider_parked",
                  "model_mismatch")
OPTIONAL_EVENTS = ("wave_completed", "milestone_accepted", "budget_exhausted", "repeated_failure_stopped",
                   "verification_inconsistent", "judge_invalid", "plan_invalid", "local_advice_required", "independence_degraded")
MAX_IMAGES, MAX_IMAGE_BYTES, MAX_BODY, MAX_ACTIONS, KEEP_HELD = 2, 5 * 1024 * 1024, 3800, 3, 200
PRIORITY = {"blocked": 4, "provider_unavailable": 4, "pursuit_stopped": 4, "model_mismatch": 4, "provider_parked": 2}
TAGS = {"goal_met": "white_check_mark", "run_completed": "white_check_mark", "pursuit_stopped": "octagonal_sign",
        "blocked": "warning", "provider_unavailable": "no_entry", "provider_parked": "hourglass_flowing_sand", "model_mismatch": "mag"}
STOPS = {"wave_limit": "the wave limit was reached", "plateau": "scores stopped improving", "milestone_limit": "the milestone limit was reached"}
NOTE = ("Anyone who knows a topic name can read it, because ntfy topics have no password. These names are random: keep them "
        "private and do not paste them into chats, issues or screenshots.")

_cache, _lock, _threads, _exits = {}, threading.Lock(), [], []


class _Straight(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would turn the POST into a GET that publishes nothing, so it counts as a failure


_opener = build_opener(_Straight)


def clock():
    return datetime.now().astimezone()


def forget():
    _cache.clear()


# ---- configuration -------------------------------------------------------------------------------------------------

def settings_path(store):
    return Path(store.root) / "providers.json"


def read_settings(store):
    path = settings_path(store)
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("providers.json must hold a JSON object")
    return data


def quiet_window(text):
    """(start, end) minutes after midnight from "23:00-07:00", or None for no quiet hours."""
    if not text:
        return None
    found = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not found or int(found[1]) > 23 or int(found[3]) > 23 or int(found[2]) > 59 or int(found[4]) > 59:
        raise ValueError("quiet_hours must look like 23:00-07:00 (24-hour local time)")
    start, end = int(found[1]) * 60 + int(found[2]), int(found[3]) * 60 + int(found[4])
    return None if start == end else (start, end)


def valid(block):
    """Normalize a notify.ntfy block or raise ValueError saying what is wrong with it."""
    if not isinstance(block, dict):
        raise ValueError("notify.ntfy must be an object")
    server = str(block.get("server") or DEFAULT_SERVER).strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/@?#]+(?:/[^\s?#]*)?", server):
        raise ValueError("notify.ntfy.server must be an http(s) URL without credentials")
    names = {}
    for field in ("topic", "reply_topic"):
        names[field] = block.get(field) or None
        if names[field] is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(names[field])):
            raise ValueError(f"notify.ntfy.{field} may hold only letters, digits, - and _ (at most 64)")
    if not names["topic"]:
        raise ValueError("notify.ntfy.topic is missing")
    events = block.get("events", DEFAULT_EVENTS)
    if not isinstance(events, (list, tuple)) or not all(isinstance(item, str) for item in events):
        raise ValueError("notify.ntfy.events must be a list of event names")
    limit = block.get("max_per_night", 3)
    if type(limit) is not int or limit < 0:
        raise ValueError("notify.ntfy.max_per_night must be a whole number, 0 or more")
    return {"server": server, **names, "events": tuple(events), "quiet": quiet_window(block.get("quiet_hours")),
            "quiet_text": block.get("quiet_hours") or None, "max_per_night": limit, "images": bool(block.get("images", True))}


def _config(store):
    """The validated configuration, or None. Re-read only when providers.json changes, so an unconfigured hook costs one stat."""
    path = settings_path(store)
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_mtime_ns, info.st_size)
    if _cache.get("entry", (None, None))[0] != key:
        try:
            block = (read_settings(store).get("notify") or {}).get("ntfy")
            config = valid(block) if block else None
        except (OSError, ValueError, AttributeError):
            config = None
        _cache["entry"] = (key, config)
    return _cache["entry"][1]


def configured(store) -> bool:
    return _config(store) is not None


def mask(name):
    name = str(name or "")
    return name if len(name) <= 8 else f"{name[:4]}...{name[-2:]} ({len(name)} characters)"


def setup(store, *, server=None, events=None, quiet_hours=None, max_per_night=None, images=None, rotate=False):
    """Write the notify.ntfy block with fresh random topics (kept on a re-run unless rotate). Sends nothing."""
    settings = read_settings(store)
    notify = settings.get("notify") if isinstance(settings.get("notify"), dict) else {}
    old = notify.get("ntfy") if isinstance(notify.get("ntfy"), dict) else {}
    keep = bool(old.get("topic")) and not rotate
    allowed = {*DEFAULT_EVENTS, *OPTIONAL_EVENTS}
    chosen = list(dict.fromkeys(events)) if events else list(old.get("events") or DEFAULT_EVENTS)
    unknown = [item for item in chosen if item not in allowed]
    if unknown:
        raise ValueError(f"Unknown notification events: {', '.join(unknown)}. Choose from: {', '.join(sorted(allowed))}")
    block = {"server": server or old.get("server") or DEFAULT_SERVER,
             "topic": old["topic"] if keep else secrets.token_urlsafe(24),
             "reply_topic": old["reply_topic"] if keep and old.get("reply_topic") else secrets.token_urlsafe(24),
             "events": chosen, "quiet_hours": quiet_hours if quiet_hours is not None else old.get("quiet_hours", "23:00-07:00"),
             "max_per_night": max_per_night if max_per_night is not None else old.get("max_per_night", 3),
             "images": images if images is not None else old.get("images", True)}
    block["server"] = str(block["server"]).strip().rstrip("/")
    valid(block)
    write_json(settings_path(store), {**settings, "notify": {**notify, "ntfy": block}})
    forget()
    return {**block, "new_topics": not keep}


def setup_text(result):
    address = f"{result['server']}/{result['topic']}"
    lines = ["Nightweaver notifications are set up. Nothing was sent.", "",
             "1. Install the ntfy app on your phone (Android, iOS or the web app at " + result["server"] + ").",
             "2. In the app choose Subscribe to topic" + ("" if result["server"] == DEFAULT_SERVER else " (use your own server)") + ":",
             f"     topic   {result['topic']}", f"     server  {result['server']}", f"   or open {address}", "",
             "Decision buttons answer on a separate private reply topic that Nightweaver reads. You do not subscribe to it.", "",
             NOTE, "Run `nightweaver notify setup --rotate` for new names if one leaks.", "",
             "Sends " + ", ".join(result["events"]) + ".", f"Quiet hours {result['quiet_hours'] or 'off'}: at most "
             f"{result['max_per_night']} messages a night, the rest wait for `nightweaver notify digest`.",
             "Run `nightweaver notify test` to send one test message."]
    if not result["server"].startswith("https://"):
        lines.insert(-1, "This server is not https, so messages cross the network unencrypted.")
    return "\n".join(lines)


def off(store):
    settings = read_settings(store)
    notify = settings.get("notify") if isinstance(settings.get("notify"), dict) else {}
    removed = notify.pop("ntfy", None) is not None
    if removed:
        if notify:
            settings["notify"] = notify
        else:
            settings.pop("notify", None)
        write_json(settings_path(store), settings)
        forget()
    return {"configured": False, "removed": removed}


def status(store):
    try:
        block = (read_settings(store).get("notify") or {}).get("ntfy")
    except (OSError, ValueError, AttributeError) as error:
        return {"configured": False, "problem": str(error)}
    if not block:
        return {"configured": False, "hint": "Run nightweaver notify setup"}
    try:
        config = valid(block)
    except ValueError as error:
        return {"configured": False, "problem": str(error)}
    data, night = _state(store), _night(config["quiet"], clock())
    return {"configured": True, "server": config["server"], "topic": mask(config["topic"]), "reply_topic": mask(config["reply_topic"]),
            "events": list(config["events"]), "quiet_hours": config["quiet_text"], "max_per_night": config["max_per_night"],
            "images": config["images"], "quiet_now": night is not None,
            "sent_tonight": data.get("nights", {}).get(night, 0) if night else None, "held_for_digest": len(data.get("held", []))}


# ---- ntfy requests -------------------------------------------------------------------------------------------------

def _header(value):
    """A header value ntfy can read: one line, RFC 2047 encoded when it holds anything but ASCII."""
    text = " ".join(str(value).split())
    return text if text.isascii() else "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def _address(config, name):
    return f"{config['server']}/{quote(name, safe='')}"


def _request(method, url, body=b"", headers=None):
    request = Request(url, data=body if method != "GET" else None, method=method, headers={"User-Agent": "Nightweaver", **(headers or {})})
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            data = response.read(1_000_000)
            return 200 <= response.status < 300, data
    except HTTPError as error:
        error.close()  # an unclosed error response holds its socket until it is collected
        raise


def _failure(error):
    """A reason that never names the URL, because the URL holds the secret topic."""
    code = getattr(error, "code", None)
    return f"HTTP {code}" if code else type(error).__name__


def _actions(actions):
    items = list(actions)[:MAX_ACTIONS]
    if not items:
        return None
    if all(isinstance(item, dict) for item in items):
        return json.dumps(items, ensure_ascii=True, separators=(",", ":"))
    if all(isinstance(item, str) for item in items):
        return "; ".join(items)
    raise ValueError("Use either action objects or ntfy action strings, not both")


def _attachments(images):
    found = []
    for image in images or ():
        path = Path(image)
        try:
            if path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES:
                found.append(path)
        except OSError:
            continue
        if len(found) == MAX_IMAGES:
            break
    return found


def _send(store, title, message, images, actions, priority, tags):
    config = _config(store)
    if not config:
        return False, "not configured"
    try:
        headers = {"Title": _header(redact(str(title))[:120]), "Priority": str(int(priority)), "Content-Type": "text/plain; charset=utf-8"}
        if tags:
            headers["Tags"] = ",".join(re.sub(r"[^A-Za-z0-9_+-]", "", str(tag)) for tag in tags)
        if encoded := _actions(actions):
            headers["Actions"] = encoded if encoded.isascii() and "\n" not in encoded else _header(encoded)
        body = redact(str(message)).encode("utf-8")[:MAX_BODY].decode("utf-8", "ignore").encode("utf-8")
        url = _address(config, config["topic"])
        ok, _ = _request("POST", url, body, headers)
        if ok and config["images"]:
            for path in _attachments(images):
                try:
                    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    _request("PUT", url, path.read_bytes(), {"Filename": _header(path.name), "Title": headers["Title"],
                                                              "Priority": "2", "Content-Type": kind})
                except Exception:
                    pass  # the message itself arrived; a missing picture is not worth a failure
        return ok, None if ok else "not accepted"
    except Exception as error:
        return False, _failure(error)


def send(store, title, message, *, images=(), actions=(), priority=3, tags=()) -> bool:
    """Best effort, 5 second timeout, never raises. images are sent as separate attachment PUTs (at most 2, each 5 MB or less).
    actions are ntfy action objects such as {"action": "http", "label": "Yes", "url": ..., "method": "POST", "body": ...}."""
    try:
        return _send(store, title, message, images, actions, priority, tags)[0]
    except Exception:
        return False


def send_test(store):
    return _send(store, "Nightweaver test", f"If you can read this on your phone, notifications work. Sent from {platform.node() or 'this computer'}.",
                 (), (), 3, ("white_check_mark",))


def ask(store, run_id, key, question, options, images=()) -> dict:
    """Send a question with one button per option (at most 3, an ntfy limit). A tap POSTs "<run_id>:<key>:<option>" to the reply topic.

    Returns {"asked_at": iso, "options": [...offered], "sent": bool}; pass asked_at to poll_answer."""
    asked = timestamp()
    offered = [str(option) for option in options][:MAX_ACTIONS]
    if not re.fullmatch(r"[0-9a-f]{12}", str(run_id)) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key)) or not offered:
        raise ValueError("ask needs a run id, a key of letters, digits, . _ - and at least one option")
    config, sent = _config(store), False
    if config and config["reply_topic"]:
        reply = _address(config, config["reply_topic"])
        buttons = [{"action": "http", "label": option[:40], "url": reply, "method": "POST", "body": f"{run_id}:{key}:{option}",
                    "clear": True} for option in offered]
        try:
            name = store.get(run_id)["spec"]["title"]
        except Exception:
            name = "Nightweaver"
        sent = send(store, f"{name}: decision needed", f"{question}\nRun {run_id}", images=images, actions=buttons, priority=4,
                    tags=("question",))
    return {"asked_at": asked, "options": offered, "sent": sent}


def poll_answer(store, run_id, key, since_iso):
    """The first option posted to the reply topic for this run and key at or after since_iso, or None."""
    config = _config(store)
    if not config or not config["reply_topic"]:
        return None
    try:
        since = datetime.fromisoformat(since_iso)
        floor = int((since if since.tzinfo else since.replace(tzinfo=timezone.utc)).timestamp())
        ok, raw = _request("GET", f"{_address(config, config['reply_topic'])}/json?poll=1&since={floor}")
        for line in raw.splitlines() if ok else ():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("event") != "message" or type(message.get("time")) is not int:
                continue
            parts = message["time"] >= floor and isinstance(message.get("message"), str) and message["message"].split(":", 2)
            if parts and len(parts) == 3 and parts[0] == run_id and parts[1] == key and parts[2].strip():
                return parts[2]
    except Exception:
        return None
    return None


# ---- quiet hours and the digest hold -------------------------------------------------------------------------------

def _state(store):
    try:
        data = json.loads((Path(store.root) / "notify-state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(store, data):
    nights = data.get("nights", {})
    data["nights"] = {name: nights[name] for name in sorted(nights)[-14:]}
    data["held"] = data.get("held", [])[-KEEP_HELD:]
    write_json(Path(store.root) / "notify-state.json", data)


def _night(quiet, now):
    """The date a night began on while now is inside quiet hours, else None."""
    if not quiet:
        return None
    start, end, minute = quiet[0], quiet[1], now.hour * 60 + now.minute
    if start < end:
        return now.date().isoformat() if start <= minute < end else None
    if minute >= start:
        return now.date().isoformat()
    return (now.date() - timedelta(days=1)).isoformat() if minute < end else None


def _admit(store, config, run_id, title, kind, summary):
    """False when quiet hours are on and tonight's allowance is spent; the message then waits in the digest hold."""
    night = _night(config["quiet"], clock())
    if night is None:
        return True
    with _lock:
        data = _state(store)
        sent = data.setdefault("nights", {}).get(night, 0)
        if sent < config["max_per_night"]:
            data["nights"][night] = sent + 1
            _save(store, data)
            return True
        data.setdefault("held", []).append({"time": timestamp(), "run_id": run_id, "title": title, "event": kind, "summary": summary})
        _save(store, data)
        return False


# ---- the engine hook -----------------------------------------------------------------------------------------------

def _line(text, limit=140):
    first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    first = " ".join(first.split())
    return first if len(first) <= limit else first[:limit - 3].rstrip() + "..."


def _end(text):
    return text if text[-1:] in (".", "!", "?") else text + "."


def _branch(details):
    return str(details.get("branch") or "").removeprefix("refs/heads/")


def summarize(kind, details, best=None):
    """A short human line for an event. Never file contents, environment values or tokens."""
    d = details or {}
    score = d.get("score") if d.get("score") is not None else d.get("best_score", best)
    tail = f" Score {score}/100." if score is not None and kind not in ("goal_met", "pursuit_stopped") else ""
    where = f" Result on {_branch(d)}." if _branch(d) else ""
    if kind == "goal_met":
        return f"Goal met in wave {d.get('wave', 1)} with judge score {d.get('score')}/100.{where}"
    if kind == "run_completed":
        return f"Completed.{where}{tail}"
    if kind == "pursuit_stopped":
        return (f"Stopped: {STOPS.get(d.get('reason'), str(d.get('reason') or 'no progress').replace('_', ' '))} after "
                f"{d.get('waves', '?')} waves, best judge score {d.get('best_score')}/100.")
    if kind == "blocked":
        return _end("Blocked: " + (_line(d.get("cause") or d.get("issue")) or "needs a decision")) + tail
    if kind == "provider_unavailable":
        return (f"{d.get('provider', 'A provider')} {d.get('role', 'route')} is unavailable "
                f"({str(d.get('reason') or 'unknown').replace('_', ' ')}); the run is blocked.{tail}")
    if kind == "provider_parked":
        wake = ""
        try:
            wake = " until " + datetime.fromisoformat(d["until"]).astimezone().strftime("%H:%M")
        except (KeyError, TypeError, ValueError):
            pass
        return f"{d.get('provider', 'A provider')} usage limit reached; waiting{wake}.{tail}"
    if kind == "model_mismatch":
        return f"{d.get('provider')} {d.get('role')} asked for {d.get('requested')} but was served {d.get('served')}."
    return kind.replace("_", " ").capitalize() + "." + tail


def on_transition(store, run_id, title, kind, details, following, state):
    """Called by Run.set after a transition commits. A no-op unless notifications are configured; never raises, never waits."""
    try:
        config = _config(store)
        if not config:
            return
        events = [(kind, details or {})]
        for item in [following] if isinstance(following, tuple) else following or ():
            if isinstance(item, (tuple, list)) and len(item) == 2:
                events.append((item[0], item[1] or {}))
        every = dict(events)
        chosen = {name: data for name, data in every.items() if name in config["events"] and name != "notify_failed"}
        if "goal_met" in chosen:  # one message for a goal that was met and the run that completed with it
            chosen["goal_met"] = {**chosen["goal_met"], "branch": every.get("run_completed", {}).get("branch")}
            chosen.pop("run_completed", None)
        if chosen:
            best = state.get("best_score") if isinstance(state, dict) else None
            thread = threading.Thread(target=_deliver, args=(store, run_id, title, list(chosen.items()), best), name="nightweaver-notify",
                                      daemon=True)
            with _lock:
                if not _exits:
                    _exits.append(atexit.register(flush))
                _threads[:] = [item for item in _threads if item.is_alive()] + [thread]
            thread.start()
    except Exception:
        pass


def _deliver(store, run_id, title, items, best):
    for kind, details in items:
        try:
            config = _config(store)
            summary = redact(summarize(kind, details, best))
            if config and _admit(store, config, run_id, title, kind, summary):
                ok, why = _send(store, title, f"{summary}\nRun {run_id}", (), (), PRIORITY.get(kind, 3), (TAGS.get(kind, "bell"),))
                if not ok:
                    _failed(store, run_id, kind, why)
        except Exception as error:
            _failed(store, run_id, kind, _failure(error))


def _failed(store, run_id, kind, why):
    """Record one notify_failed event per run so a dead server cannot flood the log."""
    try:
        with _lock:
            if not store.events(run_id, limit=1, kinds=["notify_failed"]):
                store.transition(run_id, "notify_failed", {"event": kind, "issue": f"Could not deliver a notification ({why})"})
    except Exception:
        pass


def flush(timeout=8.0):
    """Give notifications still in flight a bounded moment to leave, as the process exits."""
    deadline = time.monotonic() + timeout
    for thread in list(_threads):
        thread.join(max(0.0, deadline - time.monotonic()))


# ---- the morning digest --------------------------------------------------------------------------------------------

def _later(text, now):
    """text as a moment when it is still ahead of now, else None."""
    try:
        moment = datetime.fromisoformat(text)
        return moment if moment.tzinfo and moment > now else None
    except (TypeError, ValueError):
        return None


TERMINAL = ("pursuit_stopped", "provider_unavailable", "budget_exhausted", "repeated_failure_stopped", "verification_inconsistent",
            "dispatcher_run_failed", "blocked")


def digest(store, hours=12.0, now=None):
    """Runs that changed in the last `hours`, grouped, plus anything held back overnight."""
    now = now or datetime.now(timezone.utc)
    cutoff, groups = (now - timedelta(hours=hours)).astimezone(timezone.utc).isoformat(timespec="milliseconds"), {}
    for row in store.runs():
        if row["updated"] < cutoff:
            continue
        state, spec = row["state"], row["spec"]
        branch = str(state.get("result_ref") or "").removeprefix("refs/heads/") if state.get("accepted") != state.get("base") else ""
        score = f"best {state['best_score']}/100" if state.get("best_score") is not None else ""
        extras = " · ".join(part for part in (score, f"branch {branch}" if branch else "") if part)
        phase = state["phase"]
        if phase == "done":
            group, note = "completed", extras
        elif phase == "blocked":
            last = store.events(row["id"], limit=1, kinds=list(TERMINAL))
            stopped = last and last[0]["kind"] == "pursuit_stopped"
            group = "stopped" if stopped else "blocked"
            reason = STOPS.get(last[0]["details"].get("reason"), "") if stopped else _line(state.get("last_error"), 110)
            note = " · ".join(part for part in (reason, extras) if part)
        elif state.get("parks") and (wake := _later(state.get("retry_at"), now)):
            group, note = "parked", f"waiting for a usage limit until {wake.astimezone():%H:%M}" + (" · " + extras if extras else "")
        else:
            group, note = "running", f"{phase}, milestone {min(state['step'] + 1, len(spec['milestones']))} of {len(spec['milestones'])}"
        groups.setdefault(group, []).append(f"- {spec['title']} ({row['id']})" + (f": {note}" if note else ""))
    held = _state(store).get("held", [])
    counts = {name: len(groups.get(name, [])) for name in ("completed", "stopped", "blocked", "parked", "running")}
    words = {"completed": "done", "stopped": "stopped", "blocked": "blocked", "parked": "parked", "running": "running"}
    title = "Nightweaver digest: " + (", ".join(f"{n} {words[name]}" for name, n in counts.items() if n) or "no runs changed")
    lines = [f"Last {hours:g} hours."]
    for name, heading in (("completed", "Completed"), ("stopped", "Stopped"), ("blocked", "Blocked"), ("parked", "Parked"),
                          ("running", "Still running")):
        if groups.get(name):
            lines += ["", heading, *groups[name]]
    if held:
        lines += ["", f"Held while quiet hours were on ({len(held)})"]
        lines += [f"- {item.get('title')}: {item.get('summary')}" for item in held[-20:]]
    message = "\n".join(lines)
    if len(message.encode("utf-8")) > MAX_BODY:
        message = message.encode("utf-8")[:MAX_BODY - 40].decode("utf-8", "ignore").rsplit("\n", 1)[0] + "\n... (more in the Nightweaver app)"
    return {"title": title, "message": redact(message), "counts": counts, "held": len(held)}


def send_digest(store, hours=12.0):
    """Build and send the digest; the held messages are cleared only when it was delivered."""
    report = digest(store, hours)
    ok, why = _send(store, report["title"], report["message"], (), (), 3, ("sunrise",))
    if ok:
        with _lock:
            data = _state(store)
            if data.get("held"):
                data["held"] = []
                _save(store, data)
    return {**report, "sent": ok, **({} if ok else {"issue": why})}
