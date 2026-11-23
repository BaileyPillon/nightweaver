"""Interaction sweep: the supervisor clicks the visible controls of a page and photographs each state it reaches."""

from contextlib import contextmanager
from re import compile as pattern, fullmatch, sub
from time import monotonic
from urllib.parse import urlsplit
import unicodedata

from nightweaver import stall
from nightweaver.safety import redact

SETTLE_MS, CLICK_MS, OBSCURED_MS, QUERY_MS = 600, 3000, 1000, 3000
GRACE = 10  # Seconds past a call's own timeout that the sweep waits for the browser to answer it. Past that the call is abandoned.
QUIET_MS, QUIET_MAX_MS, STABLE_MS, STABLE_STEP_MS = 300, 1500, 2000, 150
BUDGET_SECONDS, CANDIDATES, MISSES, OPTIONS = 45, 300, 16, 6
CONTROLS = ("button,[role=button],[role=tab],[role=radio],[role=switch],[role=checkbox],input[type=radio],input[type=checkbox],"
            "input[type=button],summary,select:not([multiple]),[aria-pressed]:not(input):not(textarea),"
            "[aria-expanded]:not(input):not(textarea):not(select),[onclick][tabindex]")
STEMS = (r"delet|remov|reset|clear|eras|log[\s_-]*out|sign[\s_-]*out|buy|pay|purchas|(?:re)?send|submit|(?:un)?subscrib|cancel|empt|discard"
         r"|archiv|drop(?!down)|destroy|wipe|revok|deactivat|disconnect|publish|deploy|confirm|check[\s_-]*out|orders?\b|donat|transfer|withdraw")
DESTRUCTIVE = pattern(r"(?i)\b(?:" + STEMS + ")")  # A word start, so Payments and Deleting match but cameras does not.
OPENED = ("popup", "dialog", "modal")
UNREACHED = ("covered by another element", "outside the visible page", "clipped by its container")
STOPS = {"limit": "control limit", "budget": "time budget", "misses": "too many controls could not be clicked",
         "stuck": "page unresponsive", "state": "a reload would drop the state the page's steps built", "error": "sweep error",
         "popups": "the page keeps opening popups, or will not let them close"}

# One preamble shared by listing, re-finding, hit-testing and observing, so a control is named the same way each time.
PREAMBLE = r"""
const squash = s => (s || '').replace(/\s+/g, ' ').trim();
const words = n => squash(n.innerText ?? n.textContent);
const visible = el => {
  const box = el.getBoundingClientRect(), style = getComputedStyle(el);
  // 1px boxes and boxes left of or above the page are "screen reader only" tricks, not controls a person can use.
  return box.width >= 2 && box.height >= 2 && box.right > 0 && box.bottom + scrollY > 0 && style.visibility !== 'hidden'
    && style.display !== 'none' && style.pointerEvents !== 'none' && !el.closest('[inert]');
};
const given = el => squash(el.getAttribute('role')).split(' ')[0].toLowerCase();
const roleOf = el => given(el) || (el.tagName === 'SUMMARY' ? 'summary' : el.tagName === 'SELECT' ? 'select' : el.tagName === 'INPUT' ? el.type : 'button');
const nameOf = el => {
  let name = squash((el.getAttribute('aria-labelledby') || '').split(/\s+/)
    .map(id => document.getElementById(id)).filter(Boolean).map(words).join(' '));
  name = name || squash(el.getAttribute('aria-label'));
  if (!name && el.labels && el.labels.length) name = squash([...el.labels].map(words).join(' '));
  name = name || (el.tagName === 'SELECT' ? '' : words(el)) || (el.tagName === 'INPUT' && el.type === 'button' ? squash(el.value) : '')
    || squash(el.getAttribute('title'));
  const image = el.querySelector('img[alt]');
  name = name || (image ? squash(image.getAttribute('alt')) : '');
  // Icon-only controls fall back to their identifiers so they can still be labelled.
  return (name || squash(el.id || el.getAttribute('name') || el.getAttribute('data-testid'))).slice(0, 80);
};
// A link that only toggles something on the page (href "#" or empty, with a widget role) is a control; a real link is not.
const link = el => el.matches('a[href]') && !(/^(#.*|javascript:.*)?$/i.test((el.getAttribute('href') || '').trim())
  && (/^(tab|button|switch|radio|checkbox)$/.test(given(el)) || el.hasAttribute('aria-expanded') || el.hasAttribute('aria-pressed')));
// Radios and checkboxes are often 1px or 0x0 behind a styled label: the label is what a person clicks.
const proxy = el => el.tagName === 'INPUT' && /^(radio|checkbox)$/.test(el.type) ? [...(el.labels || [])].find(visible) || null : null;
// Would a click at the centre of `el` land on it? Mirrors the hit-target check that makes a real click wait and fail.
const reach = el => {
  const box = el.getBoundingClientRect(), x = box.left + box.width / 2, y = box.top + box.height / 2;
  const inside = x >= 0 && y >= 0 && x < innerWidth && y < innerHeight;
  for (let p = el; p; p = p.parentElement) {
    const style = getComputedStyle(p);
    if (!inside && style.position === 'fixed') return 'outside the visible page';
    if (p !== el && /hidden|clip/.test(style.overflowX + style.overflowY)) {
      const room = p.getBoundingClientRect();
      if ((room.width || room.height) && (x < room.left || x > room.right || y < room.top || y > room.bottom)) return 'clipped by its container';
    }
  }
  if (!inside) return '';
  const hit = document.elementFromPoint(x, y), label = hit && hit.closest('label');
  return hit && (hit === el || el.contains(hit) || (label && label.control === el)) ? '' : 'covered by another element';
};
const candidates = () => [...document.querySelectorAll('__CONTROLS__')]
  .filter(el => !link(el) && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true')
  .map(el => ({el, target: visible(el) ? el : proxy(el)})).filter(c => c.target).slice(0, __CANDIDATES__)
  .flatMap(({el, target}) => {
    const base = {el, target, role: roleOf(el), option: null, reach: reach(target),
      submit: el.tagName === 'BUTTON' && el.type === 'submit' && !!el.form,
      hint: ['id', 'name', 'data-testid', 'title', 'aria-label'].map(a => el.getAttribute(a) || '').join(' ')};
    const name = nameOf(el);
    if (el.tagName !== 'SELECT') return [{...base, name}];
    // A select is one control per other option: each choice is a state to photograph.
    return [...el.options].map((o, option) => ({o, option})).filter(({o}) => !o.disabled && !o.selected && o.value !== '')
      .slice(0, __OPTIONS__).map(({o, option}) => ({...base, option, name: ((name ? name + ': ' : '') + squash(o.text)).slice(0, 80)}));
  });
""".replace("__CONTROLS__", CONTROLS).replace("__CANDIDATES__", str(CANDIDATES)).replace("__OPTIONS__", str(OPTIONS))
LIST = "() => {" + PREAMBLE + "return candidates().map(({role, name, submit, hint, reach, option}) => ({role, name, submit, hint, reach, option}));}"
# Every candidate with this role and name, with the data the safety rules need, so Python picks the first one it would accept.
FIND = ("([role, name]) => {" + PREAMBLE + "const hits = candidates().filter(c => c.role === role && c.name === name);"
        "return {els: hits.map(c => c.target), meta: hits.map(({role, name, submit, hint, reach, option}) => ({role, name, submit, hint, reach, option}))};}")
HIT = "(el) => {" + PREAMBLE + "return reach(el);}"
# Counts every window.open the page makes at the moment it makes it, not when the popup's event reaches Python, under the control whose
# click led to it: `tag` is the control being clicked, and a timer or animation frame the page sets inherits the tag it was set under,
# so a popup that comes three seconds later, or on every tick of an interval, is still that control's. (A popup that follows a
# network round trip or another kind of wait is whichever control was clicked last.) Installed once per document.
LEDGER = r"""
const ledger = window.__nightweaverOpens || (() => {
  const state = {n: 0, tag: 0, by: {}};
  Object.defineProperty(window, '__nightweaverOpens', {value: state});
  const open = window.open, later = queueMicrotask.bind(window);
  window.open = function (...args) { state.n++; state.by[state.tag] = (state.by[state.tag] || 0) + 1; return open.apply(this, args); };
  for (const name of ['setTimeout', 'setInterval', 'requestAnimationFrame']) {
    const schedule = window[name];
    try {
      window[name] = function (callback, ...rest) {
        if (typeof callback !== 'function') return schedule.call(this, callback, ...rest);
        const tag = state.tag;
        return schedule.call(this, function (...args) {
          const before = state.tag;
          state.tag = tag;
          // The tag stays until the microtasks the callback queued (the code after an await) have run.
          try { return callback.apply(this, args); } finally { later(() => { state.tag = before; }); }
        }, ...rest);
      };
    } catch (error) {}  // A page that froze its timers keeps them, and its later popups are credited to the control clicked last.
  }
  return state;
})();
"""
# The document's identity, popups opened through window.open by control, and open modals.
OBSERVE = ("() => {" + PREAMBLE + LEDGER + "return {origin: performance.timeOrigin, opens: ledger.n, by: {...ledger.by}, modals: [...document.querySelectorAll("
           "'dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]')].filter(visible).length};}")
TAG = "(tag) => {" + LEDGER + "ledger.tag = tag;}"
FRAMES = ("() => new Promise(done => { const stop = setTimeout(done, 1000);"
          " requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(stop); done(); })); })")
OVERFLOW = "() => Math.max(0,document.documentElement.scrollWidth-innerWidth)"
ALIVE = "() => true"
STUB = "<!doctype html><title>Blocked</title><link rel=icon href=data:,><p>Nightweaver's interaction sweep does not load other sites."


class Stuck(Exception):
    """The page stopped answering the supervisor, for example inside a click handler that never returns."""


@contextmanager
def within(seconds):
    """Every call to the browser goes through here: the call, and the answer the driver gives, must be over within `seconds`. Playwright's
    own timeouts are kept by the driver and the browser, and a renderer that is wedged answers neither, so this deadline is the sweep's
    own. Past it the call is abandoned and the page counts as stuck; so does every later call, so the sweep ends instead of waiting
    on each of them in turn."""
    if stall.watch.stalled:
        raise Stuck("The page stopped responding")
    try:
        with stall.watch.limit(seconds):
            yield
    except stall.Stall as error:
        raise Stuck("The page stopped responding") from error


def ask(page, script, arg=None, timeout=QUERY_MS, raw=False):
    """Run a JavaScript function in the page with a deadline. A bare page.evaluate waits forever for a page that is stuck in a
    handler, because Playwright gives it no timeout; wait_for_function does, and also waits for a promise. `raw` returns a handle."""
    with within(timeout / 1000 + GRACE):
        try:
            handle = page.wait_for_function("(arg) => { const f = " + script + "; return Promise.resolve(f(arg)).then(value => ({value})); }",
                                            arg=arg, timeout=timeout)
        except Exception as error:
            if type(error).__name__ == "TimeoutError":
                raise Stuck("The page stopped responding") from error
            raise
        return handle.get_property("value") if raw else handle.json_value()["value"]


def pause(page, ms):
    with within(ms / 1000 + GRACE):
        page.wait_for_timeout(ms)


def validate_sweep(raw):
    from nightweaver.spec import fields, integer
    fields(raw, "max_controls allow_writes", "sweep")
    out = {"max_controls": integer(raw.get("max_controls", 8), "sweep max_controls", 1, 16)}
    if "allow_writes" in raw:
        if type(raw["allow_writes"]) is not bool:
            raise ValueError("sweep allow_writes must be boolean")
        out["allow_writes"] = raw["allow_writes"]
    return out


def clean(name):
    """Control names are page text that later reaches reviewer prompts: keep them short, printable and on one line."""
    spaced = " ".join(str(name).split())  # Line breaks and tabs become spaces before the unprintable characters go.
    return " ".join("".join(c for c in spaced if c.isprintable()).split())[:80]


def slug(name):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return "".join(c if c.isalnum() else "-" for c in ascii_name).strip("-")[:32].strip("-") or "control"


def spoken(hint):
    """Identifiers as words, so btnDelete and pay_now read like labels."""
    return sub(r"[_.\-]+", " ", sub(r"([a-z0-9])([A-Z])", r"\1 \2", hint))


def refusal(item):
    """Why a control is left alone, or None. Applied when the page is listed and again to the control that is found to be clicked."""
    name = clean(item["name"])
    return ("no accessible name" if not name else "submit button inside a form" if item["submit"] else
            "destructive name" if DESTRUCTIVE.search(name) or DESTRUCTIVE.search(spoken(item["hint"])) else item.get("reach") or None)


def choose(candidates):
    """Apply the safety filters and de-duplicate by role and name, keeping DOM order."""
    chosen, skipped, seen = [], [], set()
    for item in candidates:
        name, role, reason = clean(item["name"]), item["role"], refusal(item)
        if reason:
            if len(skipped) < 32:
                skipped.append({"name": name, "role": role, "reason": reason})
        elif (role, name.casefold()) not in seen:
            seen.add((role, name.casefold()))
            chosen.append({"name": item["name"], "role": role})
    return chosen, skipped


def brief(error):
    return str(redact(next((line.strip() for line in str(error).splitlines() if line.strip()), type(error).__name__)))[:200]


def racing(error):
    return "context was destroyed" in str(error)


def settle(page, timeout):
    pause(page, SETTLE_MS)
    try:
        with within(min(timeout, CLICK_MS) / 1000 + GRACE):
            page.wait_for_load_state("domcontentloaded", timeout=min(timeout, CLICK_MS))
        ask(page, FRAMES, timeout=2000)
    except Exception:
        pass  # A page that is still navigating or stuck is dealt with by the caller's next question.


def quiet(page, events):
    """Wait until nothing new has been reported for QUIET_MS (at most QUIET_MAX_MS) so a late error still lands on its own control."""
    end, seen, since = monotonic() + QUIET_MAX_MS / 1000, len(events), monotonic()
    while monotonic() < end and (monotonic() - since) * 1000 < QUIET_MS:
        pause(page, 50)
        if len(events) != seen:
            seen, since = len(events), monotonic()


def capture(page, path, timeout, deadline):
    """Photograph the page once two frames in a row are identical (at most STABLE_MS), so a script-driven tween is shown finished."""
    end, frame, last = monotonic() + STABLE_MS / 1000, None, None
    while True:
        with within(timeout / 1000 + GRACE):
            frame = page.screenshot(full_page=False, animations="disabled", timeout=timeout)
        if frame == last or monotonic() > min(end, deadline):
            break
        last = frame
        pause(page, STABLE_STEP_MS)
    path.write_bytes(frame)  # The record names this file only after it exists.


def shut(page, known, seconds=8):
    """Close every page the sweep did not start with: the popups, and the popups they opened in turn. The browser's own list is used,
    not the popup events, so one whose event is late (or never comes) is closed too. Each is closed on its own, so one that will not
    close does not leave the others open. Page.close() takes no timeout, but it goes through the browser, not the page, so a popup
    whose script never returns closes as promptly as any other. The list is read again after each round, as a popup may have opened
    another, and the closing ends when it is empty. A page that opens popups as fast as they are closed leaves some after `seconds`;
    their number is what comes back."""
    def open_popups():
        try:
            return [other for other in page.context.pages if other is not page and other not in known and not other.is_closed()]
        except Exception:
            return []  # The context is going away.

    end = monotonic() + seconds
    while monotonic() < end:
        left = open_popups()
        if not left:
            return 0
        for popup in left:
            if monotonic() > end:
                break
            try:
                with within(GRACE):
                    popup.close()
            except Stuck:
                raise
            except Exception:
                pass  # Already closed.
    return len(open_popups())


def measure(page):
    try:
        return int(ask(page, OVERFLOW))
    except Stuck:
        raise
    except Exception:
        return 0


def contain(context, home, writes, blocked):
    """Keep the sweep's clicks from changing anything outside the page. Writes (anything but GET and HEAD) and navigations to other
    sites are answered locally; the page sees success and nothing leaves. Same-origin writes pass only with `allow_writes`."""
    def handle(route):
        try:
            request = route.request
            parts = urlsplit(request.url)
            same = f"{parts.scheme}://{parts.netloc}" == home
            method, origin = request.method.upper(), request.headers.get("origin")
            if parts.scheme not in ("http", "https") or (method == "OPTIONS" and same):
                return route.continue_()
            try:
                leaving = not same and request.is_navigation_request() and request.frame.parent_frame is None
            except Exception:
                leaving = not same and request.is_navigation_request()  # A popup's first request has no frame yet.
            headers = {"access-control-allow-origin": origin or "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*",
                       **({"access-control-allow-credentials": "true"} if origin else {})}
            if method == "OPTIONS":  # A preflight for a request that will not be sent.
                return route.fulfill(status=204, headers=headers)
            if leaving:
                blocked.append(request.url)
                return route.fulfill(status=200, content_type="text/html", body=STUB)
            if method not in ("GET", "HEAD") and not (same and writes):
                blocked.append(request.url)
                return route.fulfill(status=200, content_type="application/json", body="{}", headers=headers)
            route.continue_()
        except Exception:
            pass  # The page or context went away while the request was in flight.
    context.route("**/*", handle)


def run(page, out, *, limit, timeout, output, restore, events, mark, issue, writes=False, stepped=False, overflow=0):
    """Click up to `limit` controls in DOM order and record each resulting state in `out` (updated in place).

    `events` is the case's running list of (category, message) errors, `mark` sets the stage that prefixes new errors with the
    control name, `restore` reloads the original page and `issue` records a failure. `stepped` says the case ran steps that a
    reload would not replay, `overflow` is the base layout check's measurement and `writes` lets same-origin writes through.
    Every question put to the page has a deadline, and the record of a control is complete whatever goes wrong."""
    stall.watch.clear()
    records, known, refused, queue = out["sweep"], set(), {}, []
    popups, dialogs, blocked, state = [], [], [], {}
    started_with = set(page.context.pages)  # Every other page of the context is a popup of the sweep's making.
    ledger = {"origin": None, "seen": {}, "counted": 0, "extra": 0}  # The popups credited so far, by control (see LEDGER).
    deadline, base, cursor, faults, clicked, misses, stop = monotonic() + BUDGET_SECONDS, overflow, len(events), 0, 0, 0, ""
    home = "{0.scheme}://{0.netloc}".format(urlsplit(page.url))

    def on_popup(popup):
        popups.append(popup)

    def on_dialog(dialog):
        if dialog.type == "beforeunload":
            dialog.accept()  # A page that guards leaving must not veto the sweep's own reload.
        else:
            dialogs.append(dialog.type)
            dialog.dismiss()

    def opened_by(record, kind):
        record["opened"] = [k for k in OPENED if k == kind or k in record["opened"]]

    def credit(now, heard, current):
        """Give each window.open the page has made since the last look to the control whose click led to it, which may well be an
        earlier one than the control the sweep is on. A popup the count cannot see (a link that opens a new tab, a window.open the page
        called through a reference it kept) is `current`'s, by the arrival of its event. `heard` is the number of popup events, read
        before the page was asked: an event never comes ahead of the call that caused it. Returns {control index: popups}."""
        moved = ledger["origin"] not in (None, now["origin"])
        if ledger["origin"] != now["origin"]:
            ledger.update(origin=now["origin"], seen={})  # A new document counts from nothing.
        credited = {}
        for tag, total in now["by"].items():
            new, ledger["seen"][tag] = total - ledger["seen"].get(tag, 0), total
            if new > 0:
                credited[int(tag)] = new
        ledger["counted"] += sum(credited.values())
        extra = max(0, heard - ledger["counted"])
        if extra > ledger["extra"] and current is not None and not moved:
            credited[current["index"]] = credited.get(current["index"], 0) + extra - ledger["extra"]
        ledger["extra"] = max(ledger["extra"], extra)
        for index in credited:
            if 1 <= index <= len(records):
                opened_by(records[index - 1], "popup")
        return credited

    def look(current=None):
        """Read the page's identity and popup counts and credit the new popups. Returns the state and who the popups are credited to."""
        nonlocal state
        heard = len(popups)
        seen = ask(page, OBSERVE)
        credited = credit(seen, heard, current)
        state = {**seen, "dialogs": len(dialogs)}
        return state, credited

    def scan():
        """List the controls again: those a click uncovered or revealed join the end of the queue."""
        found, skipped = choose(ask(page, LIST))
        for item in skipped:
            if len(refused) < 64:
                refused.setdefault((item["role"], item["name"], item["reason"]), item)
        for control in found:
            key = (control["role"], clean(control["name"]).casefold())
            if key not in known:
                known.add(key)
                queue.append(control)
        out["sweep_found"] = len(known)

    def find(control):
        with within(QUERY_MS / 1000 + 2 * GRACE):  # The question, and the handles taken from its answer.
            handle = ask(page, FIND, [control["role"], control["name"]], raw=True)
            meta = handle.get_property("meta").json_value()
            reasons = [refusal(item) for item in meta]
            if None not in reasons:
                raise LookupError(reasons[0] if meta else "The control is no longer visible and enabled")
            at = reasons.index(None)
            return handle.get_property("els").get_property(str(at)).as_element(), meta[at]["option"]

    def fold(record):
        """Errors that arrive after a control's own photo are still its doing: they go on its record, not the next control's."""
        nonlocal cursor
        later, cursor = events[cursor:], len(events)
        if record is not None:
            record["console_errors"] += sum(kind == "console" for kind, _ in later)
            record["page_errors"] += sum(kind == "page_error" for kind, _ in later)
            record["errors"] = (record["errors"] + [message[:300] for kind, message in later if kind in ("console", "page_error")])[:3]

    def reload():
        with within(timeout / 1000 + GRACE):
            restore()

    def recover(gone, modal, since):
        """Return to the page the sweep started from. A reload would not replay the page's steps, so a stepped case stops instead."""
        nonlocal overflow, stop
        if shut(page, started_with):
            issue("sweep", "The page keeps opening popups, or will not let them close")
            stop = "popups"
            return
        if not stepped:
            reload()
            overflow = base
        elif gone or modal:
            if not gone:
                ask(page, ALIVE, timeout=2000)  # A key press takes no timeout: find out first that the page answers.
                with within(GRACE):
                    page.keyboard.press("Escape")
                pause(page, 200)
            if gone or ask(page, OBSERVE)["modals"] > since["modals"]:
                stop = "state"
                return
        look()

    def catch_up(previous):
        """A navigation, popup or dialog that landed after the previous control's photo is that control's, not the next one's."""
        fold(previous)
        heard = len(popups)
        now = ask(page, OBSERVE)
        gone, talked = now["origin"] != state["origin"], len(dialogs) > state["dialogs"]
        popped = credit(now, heard, previous)
        if not (gone or popped or talked):
            return
        if previous is not None:
            previous["navigated"] = previous["navigated"] or gone
            if talked:
                opened_by(previous, "dialog")
        recover(gone, False, state)

    def visit(control, record):
        nonlocal cursor, overflow
        handle, option = find(control)
        covered = ""
        try:
            with within(OBSCURED_MS / 1000 + GRACE):
                handle.scroll_into_view_if_needed(timeout=OBSCURED_MS)
            covered = ask(page, HIT, handle)
        except Stuck:
            raise
        except Exception:
            pass  # Playwright's own actionability check has the last word.
        pre, name = dict(state), record["name"]
        mark({"action": "sweep", "control": name, "control_index": record["index"]})
        cursor = before = len(events)
        counts = len(popups), len(dialogs), len(blocked)
        ask(page, TAG, record["index"])  # From here the page's popups, and those its timers open later, are this control's.
        try:
            ms = min(timeout, OBSCURED_MS if covered else CLICK_MS)
            with within(ms / 1000 + GRACE):
                handle.click(timeout=ms) if option is None else handle.select_option(index=option, timeout=ms)
        except Stuck:
            raise
        except Exception as error:
            record["skipped"] = "Not clicked: " + brief(error)
            ask(page, ALIVE, timeout=2000)  # Stuck, if the click hung the page rather than missed the control.
            return False
        settle(page, timeout)
        quiet(page, events)
        now, credited = look(record)
        gone = now["origin"] != pre["origin"]
        fresh = 0 if gone else credited.get(record["index"], 0)
        started = monotonic()
        while len(popups) - counts[0] < fresh and monotonic() - started < 2:
            pause(page, 50)  # The count says a popup was opened; the event that carries its page may still be on its way.
        modal = not gone and now["modals"] > pre["modals"]
        overflow_now = 0 if gone else measure(page)
        opened = [kind for kind, hit in zip(OPENED, ("popup" in record["opened"], len(dialogs) > counts[1], modal)) if hit]
        record.update(navigated=gone, opened=opened, overflow_px=overflow_now)
        shot = f"sweep-{record['index']:02d}-{slug(name)}.png"
        capture(page, output / shot, min(timeout, 5000), deadline)
        record["screenshot"] = shot
        seen = events[before:]
        record.update(console_errors=sum(kind == "console" for kind, _ in seen), page_errors=sum(kind == "page_error" for kind, _ in seen),
                      errors=[message[:300] for kind, message in seen if kind in ("console", "page_error")][:3],
                      blocked_requests=len(blocked) - counts[2])
        cursor = len(events)
        if overflow_now > 1 and overflow_now > overflow:
            record["overflow_added_px"] = overflow_now - overflow
            issue("layout", f"Horizontal overflow: {overflow_now}px")
        overflow = overflow_now
        if gone or opened or credited:
            recover(gone, modal, pre)
        return True

    page.on("popup", on_popup)
    page.on("dialog", on_dialog)
    with within(GRACE):
        contain(page.context, home, writes, blocked)
    try:
        try:
            scan()
            look()
        except Stuck:
            issue("sweep", "The page stopped responding")
            stop = "stuck"
        while not stop:
            if not queue:
                break
            if clicked >= limit or misses >= MISSES or monotonic() > deadline:
                stop = "limit" if clicked >= limit else "misses" if misses >= MISSES else "budget"
                break
            record = None
            try:
                catch_up(records[-1] if records else None)
                if stop:
                    break
                control = queue.pop(0)
                name = clean(control["name"])
                record = {"index": len(records) + 1, "name": name, "role": control["role"], "screenshot": None, "console_errors": 0,
                          "page_errors": 0, "overflow_px": 0, "overflow_added_px": 0, "navigated": False, "opened": [],
                          "blocked_requests": 0, "errors": []}
                records.append(record)
                try:
                    took = visit(control, record)
                except LookupError as error:
                    record["skipped"], took = "Not clicked: " + brief(error), False
                clicked, misses = clicked + took, misses + (not took)
                if not stop:
                    scan()
            except Stuck:
                if record is not None and record["screenshot"] is None:
                    record.setdefault("skipped", "Not completed: the page stopped responding")
                issue("sweep", "The page stopped responding")
                stop = "stuck"
            except Exception as error:
                if record is not None and record["screenshot"] is None:
                    record.setdefault("skipped", "Not completed: " + brief(error))
                if record is not None and racing(error):
                    record["navigated"] = True  # A document that went away under a question navigated.
                if racing(error) and faults < 3:
                    faults += 1  # The page navigated under a question: carry on from the original page.
                    try:
                        if not stepped:
                            reload()
                            overflow = base
                            look()
                            scan()
                            continue
                    except Exception as again:
                        error = again
                issue("sweep", f"Interaction sweep failed: {brief(error)}")
                stop = "error"
    finally:
        if stop in ("", "limit", "misses", "budget") and records:
            try:
                look(records[-1])  # Whatever the last control's timers opened since the last look is still its doing.
            except Exception:
                pass
        fold(records[-1] if records else None)
        for record in records:
            if record["screenshot"] is None:
                record.setdefault("skipped", "Not completed")
        # A control that was covered at first but was clicked later is not "left alone"; one refused for its name always is.
        out["sweep_skipped"] = [item for item in refused.values()
                                if item["reason"] not in UNREACHED or (item["role"], item["name"].casefold()) not in known][:32]
        out["sweep_unvisited"] = [clean(control["name"]) for control in queue][:16]
        if stop:
            out["sweep_truncated"] = STOPS[stop]
        mark({"action": "sweep"})
        # The route stays: the caller closes the context next, and unroute waits on the page, which hangs when the page is stuck.
        for cleanup in (lambda: page.remove_listener("popup", on_popup), lambda: page.remove_listener("dialog", on_dialog),
                        lambda: shut(page, started_with)):
            try:
                cleanup()
            except Exception:
                pass


def validate_receipt(config, report, nonempty, passed):
    """Check the sweep list in browser.json against the plan and the files that were really produced."""
    records = report.get("sweep")
    if not isinstance(records, list) or len(records) > config["max_controls"] + MISSES:
        raise ValueError("Invalid browser sweep receipt")
    if sum(isinstance(r, dict) and bool(r.get("screenshot")) for r in records) > config["max_controls"]:
        raise ValueError("Invalid browser sweep receipt")
    if type(report.get("sweep_found")) is not int or report["sweep_found"] < len(records):
        raise ValueError("Invalid browser sweep count")
    if not isinstance(report.get("sweep_skipped"), list) or any(not isinstance(s, dict) for s in report["sweep_skipped"]):
        raise ValueError("Invalid browser sweep skip list")
    unvisited = report.get("sweep_unvisited")
    if not isinstance(unvisited, list) or len(unvisited) > 32 or any(not isinstance(name, str) for name in unvisited):
        raise ValueError("Invalid browser sweep unvisited list")
    if report.get("sweep_truncated") is not None and not isinstance(report["sweep_truncated"], str):
        raise ValueError("Invalid browser sweep truncation note")
    rows, last = [], 0
    for record in records:
        if not isinstance(record, dict) or type(record.get("index")) is not int or record["index"] <= last:
            raise ValueError("Invalid browser sweep order")
        last, shot = record["index"], record.get("screenshot")
        counters = ("console_errors", "page_errors", "overflow_px", "overflow_added_px", "blocked_requests")
        if (not isinstance(record.get("name"), str) or not isinstance(record.get("role"), str)
                or any(type(record.get(k)) is not int or record[k] < 0 for k in counters) or record["overflow_added_px"] > record["overflow_px"]
                or type(record.get("navigated")) is not bool or not isinstance(record.get("opened"), list)
                or any(kind not in OPENED for kind in record["opened"])):
            raise ValueError(f"Invalid browser sweep record {last}")
        if shot is None:
            if not isinstance(record.get("skipped"), str):
                raise ValueError(f"Browser sweep record {last} has neither a screenshot nor a reason")
        elif (not isinstance(shot, str) or not fullmatch(rf"sweep-{last:02d}-[a-z0-9-]{{1,32}}\.png", shot) or shot not in nonempty):
            raise ValueError(f"Missing or empty browser sweep screenshot for control {last}: {shot}")
        if passed and (record["console_errors"] or record["page_errors"] or record["overflow_added_px"]):
            raise ValueError(f"A passing browser verdict contradicts the sweep record for {record['name']}")
        rows.append({k: v for k, v in record.items() if k != "errors"})
    return rows


def coverage(report):
    """What the sweep did and did not exercise, from a browser.json that validate_receipt has accepted."""
    records = report["sweep"]
    return {"found": report["sweep_found"], "clicked": sum(bool(r.get("screenshot")) for r in records),
            "not_clicked": [{"name": r["name"], "reason": r["skipped"][:200]} for r in records if isinstance(r.get("skipped"), str)],
            "skipped": [{"name": str(s.get("name", ""))[:80], "reason": str(s.get("reason", ""))[:80]} for s in report["sweep_skipped"]],
            "unvisited": report["sweep_unvisited"], "truncated": report.get("sweep_truncated")}
