"""Opt-in phone notifications and a decision inbox over the ntfy protocol.

ntfy takes a plain HTTP POST or PUT to <server>/<topic> and lets a phone app subscribe to that topic. Replies are read by
polling <server>/<reply_topic>/json. Nothing is ever sent unless providers.json holds a notify.ntfy block, which only
`nightweaver notify setup` writes. Topics are random because anyone who knows a topic name can read it."""

import atexit
import base64
from contextlib import contextmanager
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
# Every way a run can stop unattended is a default event: the user must not find a run blocked overnight with no message.
DEFAULT_EVENTS = ("goal_met", "run_completed", "pursuit_stopped", "blocked", "provider_unavailable", "provider_parked",
                  "model_mismatch", "budget_exhausted", "repeated_failure_stopped", "verification_inconsistent",
                  "local_advice_required", "guard_stopped", "dispatcher_run_failed")
# What `notify setup` wrote before the terminal stops were defaults. A saved list that is exactly this, without the events_version
# mark that setup now writes, was never customised, so it reads as the current defaults. Any other list is the user's choice, and
# setup_text and status say which newer stops it leaves out.
EVENTS_VERSION = 2
LEGACY_EVENTS =("goal_met", "run_completed", "pursuit_stopped", "blocked", "provider_unavailable", "provider_parked", "model_mismatch")
OPTIONAL_EVENTS = ("wave_completed", "milestone_accepted", "judge_invalid", "plan_invalid", "independence_degraded")
MAX_IMAGES, MAX_IMAGE_BYTES, MAX_BODY, MAX_ACTIONS, KEEP_HELD, HELD_ROOM = 2, 5 * 1024 * 1024, 3800, 3, 200, 2400
LEASE, RETRY_AFTER = 60, 60  # seconds a release owns the messages it is sending; seconds before a failed release is tried again
LOUD = ("blocked", "provider_unavailable", "pursuit_stopped", "model_mismatch", "budget_exhausted", "repeated_failure_stopped",
        "verification_inconsistent", "local_advice_required", "guard_stopped", "dispatcher_run_failed")
PRIORITY = {**dict.fromkeys(LOUD, 4), "provider_parked": 2}
TAGS = {"goal_met": "white_check_mark", "run_completed": "white_check_mark", "pursuit_stopped": "octagonal_sign",
        "blocked": "warning", "provider_unavailable": "no_entry", "provider_parked": "hourglass_flowing_sand", "model_mismatch": "mag",
        **dict.fromkeys(LOUD[4:], "warning")}
DIGEST_ONLY = ("provider_parked",)  # a usage-limit wait is never worth a night's allowance or a morning message; the digest reports it
STOPS = {"wave_limit": "the wave limit was reached", "plateau": "scores stopped improving", "milestone_limit": "the milestone limit was reached"}
NOTE = ("Anyone who knows a topic name can read it, because ntfy topics have no password. These names are random: keep them "
        "private and do not paste them into chats, issues or screenshots.")

_cache, _lock, _threads, _exits, _files = {}, threading.Lock(), [], [], threading.Lock()


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
    events = DEFAULT_EVENTS if block.get("events_version") != EVENTS_VERSION and set(events) == set(LEGACY_EVENTS) else events
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
    upgraded = not events and old.get("events_version") != EVENTS_VERSION and set(chosen) == set(LEGACY_EVENTS)
    chosen = list(DEFAULT_EVENTS) if upgraded else chosen
    unknown = [item for item in chosen if item not in allowed]
    if unknown:
        raise ValueError(f"Unknown notification events: {', '.join(unknown)}. Choose from: {', '.join(sorted(allowed))}")
    block = {"server": server or old.get("server") or DEFAULT_SERVER,
             "topic": old["topic"] if keep else secrets.token_urlsafe(24),
             "reply_topic": old["reply_topic"] if keep and old.get("reply_topic") else secrets.token_urlsafe(24),
             "events": chosen, "events_version": EVENTS_VERSION, "quiet_hours": quiet_hours if quiet_hours is not None else old.get("quiet_hours", "23:00-07:00"),
             "max_per_night": max_per_night if max_per_night is not None else old.get("max_per_night", 3),
             "images": images if images is not None else old.get("images", True)}
    block["server"] = str(block["server"]).strip().rstrip("/")
    valid(block)
    write_json(settings_path(store), {**settings, "notify": {**notify, "ntfy": block}})
    forget()
    return {**block, "new_topics": not keep, "events_upgraded": upgraded}


def setup_text(result):
    address = f"{result['server']}/{result['topic']}"
    lines = ["Nightweaver notifications are set up. Nothing was sent.", "",
             "1. Install the ntfy app on your phone (Android, iOS or the web app at " + result["server"] + ").",
             "2. In the app choose Subscribe to topic" + ("" if result["server"] == DEFAULT_SERVER else " (use your own server)") + ":",
             f"     topic   {result['topic']}", f"     server  {result['server']}", f"   or open {address}", "",
             "Decision buttons answer on a separate private reply topic that Nightweaver reads. You do not subscribe to it.", "",
             NOTE, "Run `nightweaver notify setup --rotate` for new names if one leaks.", "",
             "Sends " + ", ".join(result["events"]) + ".", f"Quiet hours {result['quiet_hours'] or 'off'}: at most "
             f"{result['max_per_night']} messages a night (usage-limit waits never count and only appear in the digest). The rest are held and "
             "sent as one message soon after quiet hours end, by whatever Nightweaver process is running then (a dispatcher or a run), or "
             "with `nightweaver notify digest`.",
             "Run `nightweaver notify test` to send one test message."]
    if result.get("events_upgraded"):
        lines.insert(-1, "Your list was the old default, so it now includes the newer stops: " + ", ".join(
            name for name in DEFAULT_EVENTS if name not in LEGACY_EVENTS) + ".")
    elif off := _left_out(result["events"]):
        lines.insert(-1, "Not sent, because your events list leaves them out: " + ", ".join(off) + ". These are how a run stops unattended; "
                         "add them with `nightweaver notify setup --events " + " ".join([*result["events"], *off]) + "`.")
    if not result["server"].startswith("https://"):
        lines.insert(-1, "This server is not https, so messages cross the network unencrypted.")
    return "\n".join(lines)


def _left_out(events):
    """The stops that became default events after the list was written and that it does not send. Empty for a list with none of
    the older events: that is a new, deliberate choice."""
    return [name for name in DEFAULT_EVENTS if name not in LEGACY_EVENTS and name not in events] if set(events) & set(LEGACY_EVENTS) else []


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
            "events": list(config["events"]), **({"stops_not_sent": left} if (left := _left_out(config["events"])) else {}),
            "quiet_hours": config["quiet_text"], "max_per_night": config["max_per_night"],
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


def _send(store, title, message, images, actions, priority, tags, info=None):
    """(accepted, reason). When info is a dict it receives the id and server time ntfy answered with."""
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
        ok, answer = _request("POST", url, body, headers)
        if ok and info is not None:
            try:
                reply = json.loads(answer)
                info.update({name: reply[name] for name in ("id", "time") if isinstance(reply, dict) and name in reply})
            except ValueError:
                pass
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

    Returns {"asked_at": iso, "since_id": ntfy message id, "clock": "server" or "local", "options": [...offered], "sent": bool}.
    asked_at is ntfy's own time for the question when the server reported it, so nothing compares this PC's clock with the server's.
    Pass the whole result to poll_answer."""
    offered = [str(option) for option in options][:MAX_ACTIONS]
    if not re.fullmatch(r"[0-9a-f]{12}", str(run_id)) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(key)) or not offered:
        raise ValueError("ask needs a run id, a key of letters, digits, . _ - and at least one option")
    config, sent, info = _config(store), False, {}
    if config and config["reply_topic"]:
        reply = _address(config, config["reply_topic"])
        buttons = [{"action": "http", "label": option[:40], "url": reply, "method": "POST", "body": f"{run_id}:{key}:{option}",
                    "clear": True} for option in offered]
        try:
            name = store.get(run_id)["spec"]["title"]
        except Exception:
            name = "Nightweaver"
        try:
            sent = _send(store, f"{name}: decision needed", f"{question}\nRun {run_id}", images, buttons, 4, ("question",), info)[0]
        except Exception:
            sent = False
    served = info.get("time") if type(info.get("time")) is int and info["time"] > 0 else None
    marker = info.get("id") if isinstance(info.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", info["id"]) else None
    asked = datetime.fromtimestamp(served, timezone.utc).isoformat(timespec="milliseconds") if served else timestamp()
    return {"asked_at": asked, "since_id": marker, "clock": "server" if served else "local", "options": offered, "sent": sent}


def poll_answer(store, run_id, key, since, options=None):
    """The first offered option posted to the reply topic for this run and key after the question, or None.

    since is the dict ask() returned: polling then starts at the question's own message id and, when the server reported its
    time, compares only server times with each other. A bare asked_at string still works but is read against the server clock as
    given. options (the offered ones; taken from the ask() result when not passed) bound the answer: anything else is ignored."""
    config = _config(store)
    if not config or not config["reply_topic"]:
        return None
    try:
        marker, server = None, False
        if isinstance(since, dict):
            marker, server = since.get("since_id"), since.get("clock") == "server"
            options = since.get("options") if options is None else options
            since = since.get("asked_at")
        offered = None if options is None else [str(option) for option in options]
        if marker is not None and not (isinstance(marker, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", marker)):
            marker = None
        moment = datetime.fromisoformat(since)
        floor = int((moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp())
        if marker:
            floor = floor if server else 0  # a local stamp must not be compared with ntfy's clock: the message id orders the replies
        ok, raw = _request("GET", f"{_address(config, config['reply_topic'])}/json?poll=1&since={marker or floor}")
        for line in raw.splitlines() if ok else ():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("event") != "message" or type(message.get("time")) is not int:
                continue
            parts = message["time"] >= floor and isinstance(message.get("message"), str) and message["message"].split(":", 2)
            if (parts and len(parts) == 3 and parts[0] == run_id and parts[1] == key and parts[2].strip()
                    and (offered is None or parts[2] in offered)):
                return parts[2]
    except Exception:
        return None
    return None


# ---- quiet hours and the digest hold -------------------------------------------------------------------------------

def _state(store):
    path = Path(store.root) / "notify-state.json"
    for wait in range(8):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except PermissionError:  # Windows refuses a read while another process is swapping the file in
            time.sleep(0.01 * (wait + 1))
        except (OSError, ValueError):
            return {}
    return {}


def _save(store, data):
    nights, held = data.get("nights", {}), data.get("held", [])
    data["nights"] = {name: nights[name] for name in sorted(nights)[-14:]}
    if len(held) > KEEP_HELD:  # when the hold overflows, parks go first: they only ever matter to the digest
        drop = {id(item) for item in [item for item in held if item.get("event") in DIGEST_ONLY][:len(held) - KEEP_HELD]}
        held = [item for item in held if id(item) not in drop]
    data["held"] = held[-KEEP_HELD:]
    for wait in range(12):
        try:
            return write_json(Path(store.root) / "notify-state.json", data)
        except PermissionError:  # os.replace fails while a reader has the old file open
            if wait == 11:
                raise
            time.sleep(0.02 * (wait + 1))


@contextmanager
def _guard(store, wait=5.0):
    """One writer at a time across threads and processes: a locked byte of notify-state.lock. After `wait` seconds the caller
    goes ahead unlocked, because a counter that is a little off is better than a message that is never sent."""
    with _files:
        handle, locked = None, False
        try:
            import msvcrt
            handle = (Path(store.root) / "notify-state.lock").open("a+b")
            deadline = time.monotonic() + wait
            while not locked:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            if handle is not None:
                if locked:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                handle.close()


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


def _hold(data, run_id, title, kind, summary, failed=False):
    data.setdefault("held", []).append({"id": secrets.token_hex(6), "time": timestamp(), "run_id": run_id, "title": title, "event": kind,
                                        "summary": summary, **({"failed": True} if failed else {})})


def _admit(store, config, run_id, title, kind, summary):
    """(True, night) when the message may go out now. During quiet hours that reserves one of tonight's slots under the lock, so
    concurrent runs cannot overspend, and the caller returns it with _hold_failed if the send fails. (False, None) when the
    allowance is spent or the kind is digest-only: the message then waits in the hold."""
    night = _night(config["quiet"], clock())
    if night is None:
        return True, None
    with _guard(store):
        data = _state(store)
        sent = data.setdefault("nights", {}).get(night, 0)
        if kind not in DIGEST_ONLY and sent < config["max_per_night"]:
            data["nights"][night] = sent + 1
            _save(store, data)
            return True, night
        _hold(data, run_id, title, kind, summary)
        _save(store, data)
        return False, None


def _hold_failed(store, night, run_id, title, kind, summary):
    """A send that failed does not use the night's allowance and is not lost: it waits for the next flush or the digest."""
    with _guard(store):
        data = _state(store)
        if night:
            nights = data.setdefault("nights", {})
            nights[night] = max(0, nights.get(night, 0) - 1)
        _hold(data, run_id, title, kind, summary, failed=True)
        _save(store, data)
    _cache["released"] = time.monotonic()  # a server that just failed is not tried again by release_due for RETRY_AFTER seconds


def _held_lines(held, room, mark=True):
    """The oldest held messages that fit in `room` bytes, as (lines, entries): only those entries may be cleared afterwards. mark
    tags the ones whose first send failed; the release message is itself the delivery, so it does not."""
    lines, shown, used = [], [], 0
    for item in held:
        line = f"- {_line(item.get('title'), 80)}: {_line(item.get('summary'), 220)}" + (" (not delivered)" if mark and item.get("failed") else "")
        used += len(line.encode("utf-8")) + 1
        if used > room:
            break
        lines.append(line)
        shown.append(item)
    return lines, shown


def _key(item):
    return item.get("id") or json.dumps(item, sort_keys=True)  # a hold written before messages had ids is matched whole


def _claim(store):
    """Under the lock only: pick the oldest releasable held messages that fit one message and stamp them as being sent, so another
    process leaves them alone for LEASE seconds. The network send happens outside the lock."""
    with _guard(store):
        data, now = _state(store), time.time()
        held = data.get("held", [])
        for item in held:
            item.setdefault("id", secrets.token_hex(6))
        ready = [item for item in held if item.get("event") not in DIGEST_ONLY
                 and not (isinstance(item.get("claimed"), (int, float)) and now - item["claimed"] < LEASE)]
        lines, shown = _held_lines(ready, HELD_ROOM, mark=False)
        for item in shown:
            item["claimed"] = now
        if shown:
            _save(store, data)
        return lines, shown, len(ready)


def _settle(store, shown, sent):
    """Under the lock only: re-read the hold and drop exactly the messages that left (or give the others back), so anything another
    process held while the send was in flight is kept."""
    ids = {item["id"] for item in shown}
    with _guard(store):
        data = _state(store)
        if sent:
            data["held"] = [item for item in data.get("held", []) if item.get("id") not in ids]
        for item in data.get("held", []):
            if item.get("id") in ids:
                item.pop("claimed", None)
        _save(store, data)


def _release(store):
    """Outside quiet hours, send what was held back (overnight, or after a failed send) as a few messages and clear only what they
    carried. Digest-only parks stay for the digest. A failed send leaves everything held."""
    if not any(item.get("event") not in DIGEST_ONLY for item in _state(store).get("held", [])):
        return
    for _ in range(5):
        lines, shown, total = _claim(store)
        if not shown:
            return
        sent = False
        try:
            more = total - len(shown)
            sent, _why = _send(store, f"Nightweaver: {total} held message{'s' * (total != 1)}",
                               "Held back while quiet hours were on or delivery failed.\n" + "\n".join(lines)
                               + (f"\n... and {more} more" if more else ""), (), (), 4, ("sunrise",))
        finally:
            _settle(store, shown, sent)
        if not sent:
            return


# ---- the engine hook -----------------------------------------------------------------------------------------------

def _line(text, limit=140):
    first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    first = " ".join(first.split())
    return first if len(first) <= limit else first[:limit - 3].rstrip() + "..."


def _scrub(word):
    core = word.strip("\"'()[]<>,;")
    if re.match(r"(?:[A-Za-z]:[\\/]|~[\\/])", core) or "\\" in core or core.count("/") >= 2 or re.fullmatch(r"/[^/]+", core):
        return "<path>"
    return "<redacted>" if re.fullmatch(r"[A-Za-z0-9_+=.-]{24,}", core) else word


def _cause(text, limit=140):
    """One short line fit for a phone: redacted, cut where a provider's own output starts ("exited 1: ..."), with paths and
    long token-like words masked. Text that is a fragment of a JSON log gives "", so the caller says something fixed instead."""
    line = redact(_line(text, 1000))
    line = re.sub(r"(\bexited\s+-?\d+)\s*:.*", r"\1", line)
    if re.search(r'[{\[]\s*"|"\s*:|\\["nrt]', line):
        return ""
    line = " ".join(_scrub(word) for word in line.split())
    return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."


# Raw error text (a git error, a provider's stderr, str(exception)) can hold names, paths and file content. Known failures become a
# fixed phrase; anything else keeps only its plain leading words, up to the first path, quoted name, identifier or long token.
KNOWN = ((r"\bfatal:|^git\b|\bgit (?:failed|exited|returned|error)\b", "a git command failed"),
         (r"access (?:is )?denied|permission denied|PermissionError|WinError 5\b", "access was denied"),
         (r"no such file|cannot find the (?:file|path)|FileNotFoundError|WinError 2\b|is not recognized as an internal or external command",
          "a file or program was not found"),
         (r"timed out|TimeoutExpired", "a step timed out"), (r"no space left|not enough space|disk full", "the disk is full"))
_WORD = r"[^\W\d_](?:[^\W\d_]|['’-]){0,22}"
PLAIN = re.compile(rf"\(?(?:{_WORD}|\d{{1,6}}|\w+=\[REDACTED\]|\[REDACTED\])\)?[.,;:!?]*|[-–—:;,.!?()]+")
QUOTED = re.compile(r"""(?<![^\W_])(['"`])(?:(?!\1).)*\1(?![^\W_])""")


def _plain(text, limit=140):
    """Raw error text made fit for a phone: "" when nothing safe is left, so the caller says something fixed instead."""
    line = redact(_line(text, 1000))
    line = re.sub(r"(\b(?:exited|exit code|exit status|terminated with code)\s+-?\d+)\s*:.*", r"\1", line)
    for pattern, phrase in KNOWN:
        if re.search(pattern, line, re.I):
            return phrase
    if re.search(r'[{\[]\s*"|"\s*:|\\["nrt]', line):
        return ""
    words = []
    for word in QUOTED.sub("<redacted>", line).split():
        if not PLAIN.fullmatch(word):
            words.append("<path>" if re.search(r"[\\/]|^[A-Za-z]:|^~", word) else "<redacted>")
            break
        words.append(word)
    if not any(PLAIN.fullmatch(word) and any(ch.isalpha() for ch in word) for word in words):
        return ""
    line = " ".join(words)
    return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."


def _reason(text, limit=140):
    """Why a run is blocked. Nightweaver's own "Stopped: ..." lines are kept (scrubbed); anything else is raw error text."""
    return _cause(text, limit) if _line(text, 20).startswith("Stopped:") else _plain(text, limit)


def _end(text):
    return text if text[-1:] in (".", "!", "?") else text + "."


def _branch(details):
    return str(details.get("branch") or "").removeprefix("refs/heads/")


def summarize(kind, details, best=None):
    """A short human line for an event. Raw error text is cut to its plain leading words or replaced by a fixed phrase (_plain), so
    no file contents, paths, environment values, tokens or provider log text go out."""
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
        during = d.get("during") if re.fullmatch(r"[a-z_]{1,24}", str(d.get("during") or "")) else None
        raw = d.get("cause") or d.get("issue")
        cause = _reason(d.get("cause")) or _reason(d.get("issue")) or ("details are in the run log" if raw else "needs a decision")
        return _end(f"Blocked{f' while {during}' if during else ''}: {cause}") + tail
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
    if kind == "budget_exhausted":
        return _end("Stopped: " + (_cause(d.get("issue")) or "a run budget was used up")) + tail
    if kind == "repeated_failure_stopped":
        return _end(_cause(d.get("cause")) or f"Stopped: the same failure happened {d.get('count', 'several')} times") + tail
    if kind == "verification_inconsistent":
        return f"Stopped: check {_cause(d.get('name'), 60) or 'a gate'} gave inconsistent results on the same build.{tail}"
    if kind == "local_advice_required":
        return f"Stopped: the required local review ({_cause(d.get('model'), 60) or 'local model'}) failed.{tail}"
    if kind == "guard_stopped":
        return "The runner kept crashing and the restart limit was reached; the run is stopped and needs attention." + tail
    if kind == "dispatcher_run_failed":
        return _end("Stopped: the dispatcher could not run this: " + (_plain(d.get("issue"), 100) or "see the run log")) + tail
    return kind.replace("_", " ").capitalize() + "." + tail


def _worth(kind, details):
    """guard_stopped is also how a supervisor says it finished or was paused; only giving up on a crashing runner is news."""
    return kind != "guard_stopped" or details.get("reason") == "limit_reached"


def _mismatch(data):
    return ("model_mismatch", *(str(data.get(field)) for field in ("provider", "role", "requested", "served")))


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
        chosen, places = [], {}
        for name, data in events:
            if name not in config["events"] or name == "notify_failed" or not _worth(name, data):
                continue
            # One message per kind, but one per distinct model_mismatch pair: a single call can be served by several other models.
            key = _mismatch(data) if name == "model_mismatch" else name
            if key not in places:
                places[key] = len(chosen)
                chosen.append((name, data))
            elif name != "model_mismatch":
                chosen[places[key]] = (name, data)
        if any(name == "goal_met" for name, _ in chosen):  # one message for a goal that was met and the run that completed with it
            branch = next((data.get("branch") for name, data in events if name == "run_completed"), None)
            chosen = [(name, {**data, "branch": branch} if name == "goal_met" else data) for name, data in chosen if name != "run_completed"]
        if chosen:
            _spawn(_deliver, store, run_id, title, chosen, state.get("best_score") if isinstance(state, dict) else None)
        else:
            release_due(store)  # any transition is a chance to send what the night held back
    except Exception:
        pass


def _spawn(target, *args):
    """Run target(*args, after) on a daemon thread. Each waits for the one before it, so messages leave in the order they happened."""
    with _lock:
        live = [item for item in _threads if item.is_alive()]
        thread = threading.Thread(target=target, args=(*args, live[-1] if live else None), name="nightweaver-notify", daemon=True)
        if not _exits:
            _exits.append(atexit.register(flush))
        thread.start()
        _threads[:] = live + [thread]


def _pending(store):
    return (Path(store.root) / "notify-state.json").is_file() and any(
        item.get("event") not in DIGEST_ONLY for item in _state(store).get("held", []))


def release_due(store):
    """Send what quiet hours or a failed send held back, as soon as quiet hours are over, without waiting for another notifying
    event: the engine, the guard and the dispatcher loop call this wherever they are alive. A stat when there is nothing held, never
    raises or waits, and a failed attempt is not repeated for RETRY_AFTER seconds. Returns whether an attempt was started."""
    try:
        config = _config(store)
        if not config or _night(config["quiet"], clock()) is not None or not _pending(store):
            return False
        if time.monotonic() - _cache.get("released", -RETRY_AFTER) < RETRY_AFTER:
            return False
        _cache["released"] = time.monotonic()
        _spawn(_wake, store)
        return True
    except Exception:
        return False


def _wake(store, after=None):
    if after is not None:
        after.join(10)
    try:
        _release(store)
        if not _pending(store):
            _cache.pop("released", None)  # all sent: the next night's hold may be released at once
    except Exception:
        pass


def announce(store, run_id, kind, details, state=None, following=None):
    """on_transition for events written straight through store.transition (the guard and the dispatcher), which have no Run.set."""
    try:
        if _config(store):
            on_transition(store, run_id, store.get(run_id)["spec"]["title"], kind, details, following, state)
    except Exception:
        pass


def _deliver(store, run_id, title, items, best, after=None):
    if after is not None:
        after.join(10)
    for kind, details in items:
        night = summary = None
        try:
            config = _config(store)
            if not config:
                continue
            summary = redact(summarize(kind, details, best))
            admitted, night = _admit(store, config, run_id, title, kind, summary)
            if not admitted:
                continue
            ok, why = _send(store, title, f"{summary}\nRun {run_id}", (), (), PRIORITY.get(kind, 3), (TAGS.get(kind, "bell"),))
            if not ok:
                _hold_failed(store, night, run_id, title, kind, summary)
                _failed(store, run_id, kind, why)
            elif _night(config["quiet"], clock()) is None:
                try:
                    _release(store)  # the server is reachable and quiet hours are over: send what waited
                except Exception:
                    pass
        except Exception as error:
            if summary is not None:
                try:
                    _hold_failed(store, night, run_id, title, kind, summary)
                except Exception:
                    pass
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


def _gave_up(store, run_id):
    """True when the newest word from the guard is that it stopped on its restart limit: the run is then stuck, not running."""
    last = store.events(run_id, limit=1, kinds=["guard_stopped", "guard_started", "guard_launch_admitted"])
    return bool(last) and last[0]["kind"] == "guard_stopped" and last[0]["details"].get("reason") == "limit_reached"


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
            reason = STOPS.get(last[0]["details"].get("reason"), "") if stopped else _reason(state.get("last_error"), 110)
            note = " · ".join(part for part in (reason, extras) if part)
        elif _gave_up(store, row["id"]):
            group, note = "blocked", " · ".join(part for part in ("the runner kept crashing and its restart limit was reached", extras) if part)
        elif state.get("parks") and (wake := _later(state.get("retry_at"), now)):
            group, note = "parked", f"waiting for a usage limit until {wake.astimezone():%H:%M}" + (" · " + extras if extras else "")
        else:
            group, note = "running", f"{phase}, milestone {min(state['step'] + 1, len(spec['milestones']))} of {len(spec['milestones'])}"
        groups.setdefault(group, []).append(f"- {spec['title']} ({row['id']})" + (f": {note}" if note else ""))
    held = _state(store).get("held", [])
    counts = {name: len(groups.get(name, [])) for name in ("completed", "stopped", "blocked", "parked", "running")}
    words = {"completed": "done", "stopped": "stopped", "blocked": "blocked", "parked": "parked", "running": "running"}
    title = "Nightweaver digest: " + (", ".join(f"{n} {words[name]}" for name, n in counts.items() if n) or "no runs changed")
    lines, shown = [f"Last {hours:g} hours."], []
    if held:  # first, so the length cap trims run groups and never the messages that are cleared once this is delivered
        body, shown = _held_lines(held, HELD_ROOM)
        lines += ["", f"Held while quiet hours were on ({len(held)})", *body]
        if len(shown) < len(held):
            lines.append(f"... and {len(held) - len(shown)} more, kept for the next digest")
    for name, heading in (("completed", "Completed"), ("stopped", "Stopped"), ("blocked", "Blocked"), ("parked", "Parked"),
                          ("running", "Still running")):
        if groups.get(name):
            lines += ["", heading, *groups[name]]
    message = "\n".join(lines)
    if len(message.encode("utf-8")) > MAX_BODY:
        message = message.encode("utf-8")[:MAX_BODY - 40].decode("utf-8", "ignore").rsplit("\n", 1)[0] + "\n... (more in the Nightweaver app)"
    return {"title": title, "message": redact(message), "counts": counts, "held": len(held), "held_shown": len(shown), "included": shown}


def send_digest(store, hours=12.0):
    """Build and send the digest; only the held messages it listed are cleared, and only when it was delivered."""
    report = digest(store, hours)
    ok, why = _send(store, report["title"], report["message"], (), (), 3, ("sunrise",))
    if ok and report["included"]:
        with _guard(store):
            data, sent = _state(store), {_key(item) for item in report["included"]}  # re-read: runs may have held more meanwhile
            kept = [item for item in data.get("held", []) if _key(item) not in sent]
            if len(kept) != len(data.get("held", [])):
                data["held"] = kept
                _save(store, data)
    return {**report, "sent": ok, **({} if ok else {"issue": why})}
