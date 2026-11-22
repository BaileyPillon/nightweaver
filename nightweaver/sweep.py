"""Interaction sweep: the supervisor clicks the visible controls of a page and photographs each state it reaches."""

from re import compile as pattern, fullmatch, sub
from time import monotonic
import unicodedata

from nightweaver.safety import redact

SETTLE_MS, CLICK_MS, BUDGET_SECONDS, CANDIDATES = 600, 3000, 45, 300
CONTROLS = "button,[role=button],[role=tab],[role=radio],[role=switch],input[type=radio],input[type=checkbox],summary"
STEMS = r"delet|remov|reset|clear|eras|log[\s_-]*out|sign[\s_-]*out|buy|pay|purchas|(?:re)?send|submit|unsubscrib"
DESTRUCTIVE = pattern(r"(?i)\b(?:" + STEMS + ")")  # A word start, so Payments and Deleting match but cameras does not.
OPENED = ("popup", "dialog", "modal")

# One preamble shared by listing, re-finding and modal counting, so a control is named the same way each time.
PREAMBLE = """
const squash = s => (s || '').replace(/\\s+/g, ' ').trim();
const words = n => squash(n.innerText ?? n.textContent);
const visible = el => {
  const box = el.getBoundingClientRect(), style = getComputedStyle(el);
  // 1px boxes and boxes left of or above the page are "screen reader only" tricks, not controls a person can use.
  return box.width >= 2 && box.height >= 2 && box.right > 0 && box.bottom + scrollY > 0 && style.visibility !== 'hidden'
    && style.display !== 'none' && style.pointerEvents !== 'none' && !el.closest('[inert]');
};
const roleOf = el => squash(el.getAttribute('role')).split(' ')[0].toLowerCase()
  || (el.tagName === 'SUMMARY' ? 'summary' : el.tagName === 'INPUT' ? el.type : 'button');
const nameOf = el => {
  let name = squash((el.getAttribute('aria-labelledby') || '').split(/\\s+/)
    .map(id => document.getElementById(id)).filter(Boolean).map(words).join(' '));
  name = name || squash(el.getAttribute('aria-label'));
  if (!name && el.labels && el.labels.length) name = squash([...el.labels].map(words).join(' '));
  name = name || words(el) || squash(el.getAttribute('title'));
  const image = el.querySelector('img[alt]');
  name = name || (image ? squash(image.getAttribute('alt')) : '');
  // Icon-only controls fall back to their identifiers so they can still be labelled.
  return (name || squash(el.id || el.getAttribute('name') || el.getAttribute('data-testid'))).slice(0, 80);
};
const candidates = () => [...document.querySelectorAll('__CONTROLS__')]
  .filter(el => !el.matches('a[href]') && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true' && visible(el))
  .slice(0, __CANDIDATES__).map(el => ({el, role: roleOf(el), name: nameOf(el),
    submit: el.tagName === 'BUTTON' && el.type === 'submit' && !!el.form,
    hint: ['id', 'name', 'data-testid', 'title', 'aria-label'].map(a => el.getAttribute(a) || '').join(' ')}));
""".replace("__CONTROLS__", CONTROLS).replace("__CANDIDATES__", str(CANDIDATES))
LIST = "() => {" + PREAMBLE + "return candidates().map(({role, name, submit, hint}) => ({role, name, submit, hint}));}"
FIND = "([role, name]) => {" + PREAMBLE + "const hit = candidates().find(c => c.role === role && c.name === name); return hit ? hit.el : null;}"
MODALS = ("() => {" + PREAMBLE + "return [...document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]')]"
          ".filter(visible).length;}")
FRAMES = ("() => new Promise(done => { const stop = setTimeout(done, 1000);"
          " requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(stop); done(); })); })")
OVERFLOW = "Math.max(0,document.documentElement.scrollWidth-innerWidth)"


def validate_sweep(raw):
    from nightweaver.spec import fields, integer
    fields(raw, "max_controls", "sweep")
    return {"max_controls": integer(raw.get("max_controls", 8), "sweep max_controls", 1, 16)}


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


def choose(candidates):
    """Apply the safety filters and de-duplicate by role and name, keeping DOM order."""
    chosen, skipped, seen = [], [], set()
    for item in candidates:
        name, role = clean(item["name"]), item["role"]
        reason = ("no accessible name" if not name else "submit button inside a form" if item["submit"] else
                  "destructive name" if DESTRUCTIVE.search(name) or DESTRUCTIVE.search(spoken(item["hint"])) else None)
        if reason:
            if len(skipped) < 32:
                skipped.append({"name": name, "role": role, "reason": reason})
        elif (role, name.casefold()) not in seen:
            seen.add((role, name.casefold()))
            chosen.append({"name": item["name"], "role": role})
    return chosen, skipped


def brief(error):
    return str(redact(next((line.strip() for line in str(error).splitlines() if line.strip()), type(error).__name__)))[:200]


def settle(page, timeout):
    page.wait_for_timeout(SETTLE_MS)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
        page.evaluate(FRAMES)
    except Exception:
        pass  # A page that is still navigating is dealt with by the caller.


def document(page, timeout):
    """Identity of the current document: it changes on a real navigation or reload, never on pushState or a hash change."""
    for _ in range(3):
        try:
            return page.evaluate("performance.timeOrigin")
        except Exception:
            try:
                page.wait_for_load_state("load", timeout=timeout)
            except Exception:
                pass
    return None


def measure(page):
    try:
        return int(page.evaluate(OVERFLOW))
    except Exception:
        return 0


def run(page, out, *, limit, timeout, output, restore, events, mark, issue):
    """Click up to `limit` controls in DOM order and record each resulting state in `out` (updated in place).

    `events` is the case's running list of (category, message) errors, `mark` sets the stage that prefixes new
    errors with the control name, `restore` reloads the original page and `issue` records a failure."""
    chosen, out["sweep_skipped"] = choose(page.evaluate(LIST))
    out["sweep_found"] = len(chosen)
    popups, dialogs = [], []

    def on_popup(popup):
        popups.append(popup)

    def on_dialog(dialog):
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("popup", on_popup)
    page.on("dialog", on_dialog)
    deadline, overflow = monotonic() + BUDGET_SECONDS, measure(page)
    try:
        for index, control in enumerate(chosen[:limit], 1):
            if monotonic() > deadline:
                out["sweep_truncated"] = "time budget"
                break
            name, role = clean(control["name"]), control["role"]
            mark({"action": "sweep", "control": name, "control_index": index})
            record = {"index": index, "name": name, "role": role, "screenshot": None, "console_errors": 0, "page_errors": 0,
                      "overflow_px": overflow, "navigated": False, "opened": []}
            out["sweep"].append(record)
            popups.clear()
            dialogs.clear()
            before, origin, modals = len(events), document(page, timeout), page.evaluate(MODALS)
            try:
                handle = page.evaluate_handle(FIND, [control["role"], control["name"]]).as_element()
                if handle is None:
                    raise LookupError("The control is no longer visible and enabled")
                handle.click(timeout=min(timeout, CLICK_MS))
            except Exception as error:
                record["skipped"] = "Not clicked: " + brief(error)
                mark({"action": "sweep"})
                continue
            settle(page, timeout)
            current = document(page, timeout)
            navigated = current is None or origin is None or current != origin
            seen = events[before:]
            overflow_now = 0 if navigated else measure(page)
            record.update(
                navigated=navigated, overflow_px=overflow_now,
                console_errors=sum(kind == "console" for kind, _ in seen), page_errors=sum(kind == "page_error" for kind, _ in seen),
                opened=[kind for kind, hit in zip(OPENED, (bool(popups), bool(dialogs), not navigated and page.evaluate(MODALS) > modals)) if hit])
            record["errors"] = [message[:300] for kind, message in seen if kind in ("console", "page_error")][:3]
            record["screenshot"] = f"sweep-{index:02d}-{slug(name)}.png"
            page.screenshot(path=str(output / record["screenshot"]), full_page=False, animations="disabled")
            if overflow_now > 1 and overflow_now > overflow:
                issue("layout", f"Horizontal overflow: {overflow_now}px")
            overflow = overflow_now
            if navigated or record["opened"]:
                for popup in popups:
                    try:
                        popup.close()
                    except Exception:
                        pass
                restore()
                overflow = measure(page)
            mark({"action": "sweep"})
    finally:
        page.remove_listener("popup", on_popup)
        page.remove_listener("dialog", on_dialog)


def validate_receipt(config, report, nonempty, passed):
    """Check the sweep list in browser.json against the plan and the files that were really produced."""
    records = report.get("sweep")
    if not isinstance(records, list) or len(records) > config["max_controls"]:
        raise ValueError("Invalid browser sweep receipt")
    if type(report.get("sweep_found")) is not int or report["sweep_found"] < len(records):
        raise ValueError("Invalid browser sweep count")
    if not isinstance(report.get("sweep_skipped"), list) or any(not isinstance(s, dict) for s in report["sweep_skipped"]):
        raise ValueError("Invalid browser sweep skip list")
    rows, last = [], 0
    for record in records:
        if not isinstance(record, dict) or type(record.get("index")) is not int or record["index"] <= last:
            raise ValueError("Invalid browser sweep order")
        last, shot = record["index"], record.get("screenshot")
        if (not isinstance(record.get("name"), str) or not isinstance(record.get("role"), str)
                or any(type(record.get(k)) is not int or record[k] < 0 for k in ("console_errors", "page_errors", "overflow_px"))
                or type(record.get("navigated")) is not bool or not isinstance(record.get("opened"), list)
                or any(kind not in OPENED for kind in record["opened"])):
            raise ValueError(f"Invalid browser sweep record {last}")
        if shot is None:
            if not isinstance(record.get("skipped"), str):
                raise ValueError(f"Browser sweep record {last} has neither a screenshot nor a reason")
        elif (not isinstance(shot, str) or not fullmatch(rf"sweep-{last:02d}-[a-z0-9-]{{1,32}}\.png", shot) or shot not in nonempty):
            raise ValueError(f"Missing or empty browser sweep screenshot for control {last}: {shot}")
        if passed and (record["console_errors"] or record["page_errors"] or record["overflow_px"] > 1):
            raise ValueError(f"A passing browser verdict contradicts the sweep record for {record['name']}")
        rows.append({k: v for k, v in record.items() if k != "errors"})
    return rows
