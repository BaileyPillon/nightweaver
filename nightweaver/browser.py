"""Declarative local browser acceptance checks. Playwright is an optional dependency."""

import argparse
from contextlib import ExitStack, contextmanager
from collections import Counter
from functools import partial
from importlib.metadata import version
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from threading import Thread
from time import monotonic
from urllib.parse import quote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightweaver import journeys
from nightweaver.spec import strict_json
from nightweaver.safety import redact, write_json
from nightweaver.server import CandidateStartupFailure


def validate_browser(raw):
    from nightweaver.spec import array, artifact, fields, integer, text
    fields(raw, "page url server heading steps viewports channel timeout_ms wait_until offline network scenarios", "browser")
    if sum(key in raw for key in ("page", "url", "server")) != 1:
        raise ValueError("Browser needs exactly one of page, loopback url or owned server")
    out = dict(raw)
    if type(raw.get("offline", False)) is not bool or (raw.get("offline") and "page" not in raw):
        raise ValueError("Offline browser checks require a local page and a boolean offline option")
    if "network" in raw:
        network = raw["network"]
        fields(network, "require_success max_requests", "network")
        if type(network.get("require_success", False)) is not bool:
            raise ValueError("network.require_success must be boolean")
        if "max_requests" in network:
            integer(network["max_requests"], "max_requests", 0, 100000)
    if "page" in raw:
        out["page"] = artifact(raw["page"])
    elif "url" in raw:
        url = urlparse(text(raw, "url"))
        try:
            if url.port == 0:
                raise ValueError("port zero")
        except ValueError as error:
            raise ValueError("Browser URL port must be an integer from 1 to 65535") from error
        if url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password:
            raise ValueError("Browser URLs must be explicit loopback HTTP(S) addresses")
    else:
        from nightweaver.server import validate_server
        out["server"] = validate_server(raw["server"])
    out["wait_until"] = raw.get("wait_until", "load")
    if out["wait_until"] not in ("load", "domcontentloaded", "networkidle"):
        raise ValueError("wait_until must be load, domcontentloaded or networkidle")
    if "heading" in raw:
        out["heading"] = text(raw, "heading")
    if "channel" in raw and raw["channel"] not in ("msedge", "chrome", "chromium"):
        raise ValueError("Unsupported browser channel")
    out["timeout_ms"] = integer(raw.get("timeout_ms", 10000), "browser timeout_ms", 100, 120000)
    out["steps"] = array(raw.get("steps", []), "browser steps", 100)
    validate_steps(out["steps"], out.get("offline", False))
    if "scenarios" in out:
        journeys.validate_scenarios(out, validate_steps)
    out["viewports"] = array(raw.get("viewports", [
        {"name": "desktop", "width": 1440, "height": 900}, {"name": "mobile", "width": 390, "height": 844}
    ]), "viewports", 8)
    if not out["viewports"]:
        raise ValueError("At least one browser viewport is required")
    names = set()
    for viewport in out["viewports"]:
        fields(viewport, "name width height is_mobile has_touch device_scale_factor color_scheme reduced_motion forced_colors", "viewport")
        for key in ("is_mobile", "has_touch"):
            if key in viewport and type(viewport[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        if "device_scale_factor" in viewport:
            integer(viewport["device_scale_factor"], "device_scale_factor", 1, 4)
        for key, choices in (("color_scheme", ("light", "dark")), ("reduced_motion", ("reduce", "no-preference")),
                             ("forced_colors", ("active", "none"))):
            if key in viewport and viewport[key] not in choices:
                raise ValueError(f"Unsupported viewport {key}")
        name = text(viewport, "name")
        if name != viewport["name"] or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name) or name in names:
            raise ValueError("Viewport names must be unique lowercase identifiers")
        artifact(name + ".png")
        names.add(name)
        integer(viewport.get("width"), "viewport width", 240, 3840)
        integer(viewport.get("height"), "viewport height", 240, 2160)
    return out


def validate_steps(steps, offline):
    from nightweaver.spec import artifact, fields, integer, text
    if len(steps) > 100:
        raise ValueError("Each browser journey supports at most 100 combined steps")
    checkpoints = set()
    for step in steps:
        fields(step, "action selector locator value", "browser step")
        action = text(step, "action")
        if action != step["action"]:
            raise ValueError("Browser action names cannot have surrounding whitespace")
        if action in journeys.PAGE_ACTIONS | journeys.LOCATOR_ACTIONS:
            journeys.validate_action(step, offline)
        if action in journeys.PAGE_ACTIONS:
            continue
        if action in ("click", "check", "uncheck", "visible", "hidden", "enabled", "disabled", "focused") and "value" in step:
            raise ValueError(f"{action} takes no value")
        if action == "reload":
            if set(step) != {"action"}:
                raise ValueError("reload takes no selector or value")
            continue
        if action == "checkpoint":
            label = text(step, "value")
            if (set(step) != {"action", "value"} or label != step["value"] or
                    not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", label) or label in checkpoints):
                raise ValueError("Use a unique lowercase checkpoint name and omit selector")
            checkpoints.add(label)
            continue
        if action not in journeys.LOCATOR_ACTIONS and action not in ("click", "fill", "press", "check", "visible", "hidden", "enabled", "disabled",
                          "focused", "text", "count", "aria", "visual_match", "value", "checked", "unchecked"):
            raise ValueError(f"Unsupported browser action: {action}")
        if ("selector" in step) == ("locator" in step):
            raise ValueError("Use exactly one selector or semantic locator")
        if "selector" in step:
            text(step, "selector")
        else:
            loc = step["locator"]
            fields(loc, "role name label test_id", "locator")
            if set(loc) not in ({"role"}, {"role", "name"}, {"label"}, {"test_id"}):
                raise ValueError("Use role with optional name, label or test_id")
            for key in loc:
                text(loc, key)
        if action == "aria":
            text(step, "value")
        if action == "visual_match":
            match = step.get("value")
            fields(match, "baseline sha256 channel_delta max_changed_ppm", "visual match")
            artifact(text(match, "baseline"))
            if not re.fullmatch(r"[a-f0-9]{64}", text(match, "sha256")):
                raise ValueError("Visual baseline needs an operator-pinned SHA-256")
            integer(match.get("channel_delta", 0), "channel_delta", 0, 255)
            integer(match.get("max_changed_ppm", 0), "max_changed_ppm", 0, 1000000)
        if action in ("checked", "unchecked") and "value" in step:
            raise ValueError(f"{action} takes no value")
        if action in ("fill", "press", "text", "value") and not isinstance(step.get("value"), str):
            raise ValueError(f"{action} needs a string value")
        if action == "count":
            integer(step.get("value"), "browser count", 0, 1000000)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def local_page(config, root, output):
    if "server" in config:
        from nightweaver.server import managed_server
        with managed_server(config["server"], root, output) as server:
            yield server
        return
    if "url" in config:
        yield config["url"], lambda: None
        return
    page = (root / config["page"]).resolve()
    if not page.is_relative_to(root) or not page.is_file():
        raise CandidateStartupFailure(f"Expected a page inside the candidate checkout: {config['page']}")
    if config.get("offline"):
        yield page.as_uri(), lambda: None
        return
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root))) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/{quote(page.relative_to(root).as_posix())}", lambda: None
        finally:
            server.shutdown()
            thread.join(timeout=5)


def semantic_locator(page, step):
    if "selector" in step:
        return page.locator(step["selector"])
    loc = step["locator"]
    if "role" in loc:
        return page.get_by_role(loc["role"], name=loc.get("name"), exact=True)
    if "label" in loc:
        return page.get_by_label(loc["label"], exact=True)
    return page.get_by_test_id(loc["test_id"])


def visual_match(locator, contract, root, output, prefix, result):
    try:
        from PIL import Image, ImageChops
    except ImportError as error:
        raise BrowserRuntimeUnavailable("Visual comparisons require Pillow in the Nightweaver browser environment") from error
    baseline = (root / contract["baseline"]).resolve(strict=True)
    if not baseline.is_relative_to(root) or not baseline.is_file() or baseline.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Invalid visual baseline file")
    data = baseline.read_bytes()
    if hashlib.sha256(data).hexdigest() != contract["sha256"]:
        raise ValueError("Visual baseline hash differs from the plan")
    bounds = locator.bounding_box()
    if not bounds or bounds["width"] * bounds["height"] > 16_000_000:
        raise ValueError("Visual comparison region must be visible and at most 16 megapixels")
    actual = output / (prefix + ".actual.png")
    locator.screenshot(path=str(actual), animations="disabled", scale="css", caret="hide")
    with Image.open(BytesIO(data), formats=("PNG",)) as reference, Image.open(actual) as capture:
        if reference.size != capture.size or reference.width * reference.height > 16_000_000:
            raise ValueError(f"Visual dimensions differ or exceed limit: {reference.size}, {capture.size}")
        delta = ImageChops.difference(reference.convert("RGBA"), capture.convert("RGBA"))
        red, green, blue, alpha = delta.split()
        maximum = ImageChops.lighter(ImageChops.lighter(red, alpha), ImageChops.lighter(green, blue))
        histogram = maximum.histogram()
        changed = sum(histogram[contract.get("channel_delta", 0) + 1:])
        total = capture.width * capture.height
        result.setdefault("visual_matches", []).append({"step": prefix, "changed_pixels": changed,
                                                       "total_pixels": total, "changed_ppm": round(changed * 1000000 / total, 2),
                                                       "allowed_ppm": contract.get("max_changed_ppm", 0), "baseline_sha256": contract["sha256"]})
        if changed * 1000000 > contract.get("max_changed_ppm", 0) * total:
            maximum.save(output / (prefix + ".diff.png"))
            raise ValueError(f"Visual regression: {changed}/{total} pixels differ")


def run_step(page, step, index, result, output, timeout, root, wait_until="load"):
    from playwright.sync_api import expect
    action = step["action"]
    case = journeys.case_prefix(result.get("scenario"), result["viewport"])
    if action == "reload":
        response = page.reload(wait_until=wait_until, timeout=timeout)
        if response and response.status >= 400:
            raise ValueError(f"Reload returned HTTP {response.status}")
        return
    if action == "checkpoint":
        file = f"checkpoint.{case}.{step['value']}.png"
        page.screenshot(path=str(output / file), animations="disabled")
        result["checkpoints"].append({"step": index, "name": step["value"], "file": file,
                                      "overflow_px": page.evaluate("Math.max(0,document.documentElement.scrollWidth-innerWidth)")})
        return
    locator = semantic_locator(page, step)
    prefix = f"{case}.{index:02d}"
    if action in journeys.LOCATOR_ACTIONS:
        return journeys.assert_element(locator, action, step.get("value"), timeout)
    if action == "visual_match":
        return visual_match(locator, step["value"], root, output, prefix, result)
    if action == "aria":
        try:
            expect(locator).to_match_aria_snapshot(step["value"], timeout=timeout)
        except AssertionError:
            if locator.count() == 1:
                (output / (prefix + ".aria.yml")).write_text(locator.aria_snapshot(timeout=timeout)[:64000], encoding="utf-8")
            raise
        return
    if action in ("checked", "unchecked"):
        expect(locator).to_be_checked(checked=action == "checked", timeout=timeout)
    elif action in ("visible", "hidden", "enabled", "disabled", "focused"):
        getattr(expect(locator), "to_be_" + action)(timeout=timeout)
    elif action in ("text", "count", "value"):
        getattr(expect(locator), "to_have_" + action)(step["value"], timeout=timeout)
    elif action in ("fill", "press"):
        getattr(locator, action)(step["value"])
    else:
        getattr(locator, action)()


class BrowserRuntimeUnavailable(RuntimeError):
    """The verification browser could not start."""


FALLBACK_CHANNELS = ("msedge", "chrome")
_FALLBACKS = {}  # Per process: the installed channel that replaced a missing bundled Chromium.


def bundled_missing(error):
    text = str(error)
    return "Executable doesn't exist" in text or "playwright install" in text


def fallback_allowed():
    """Setting PLAYWRIGHT_BROWSERS_PATH asks for exactly the Chromium kept there, so that stays fail-closed."""
    return not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


def first_line(error):
    return str(redact(next((l.strip() for l in str(error).splitlines() if l.strip()), type(error).__name__)))[:300]


def launch_browser(driver, channel, timeout):
    """Return (browser, channel used, fallback). Only the bundled default may fall back to an installed Edge or Chrome."""
    if channel is not None:
        return driver.chromium.launch(channel=channel, timeout=timeout), channel, None
    cached = _FALLBACKS.get("bundled") if fallback_allowed() else None
    if cached:
        try:
            return driver.chromium.launch(channel=cached["used"], timeout=timeout), cached["used"], dict(cached)
        except Exception:
            _FALLBACKS.clear()
    try:
        return driver.chromium.launch(timeout=timeout), "bundled", None
    except Exception as error:
        if not bundled_missing(error):
            raise
        if not fallback_allowed():
            raise RuntimeError(f"{error}\nPLAYWRIGHT_BROWSERS_PATH is set, so no installed Edge or Chrome fallback was tried.") from error
        failures = []
        for alternate in FALLBACK_CHANNELS:
            try:
                browser = driver.chromium.launch(channel=alternate, timeout=timeout)
            except Exception as other:
                failures.append(f"{alternate}: {first_line(other)}")
                continue
            fallback = {"requested": "bundled", "used": alternate, "reason": first_line(error)}
            _FALLBACKS["bundled"] = fallback
            return browser, alternate, dict(fallback)
        raise RuntimeError(f"{error}\nInstalled browser fallbacks also failed: {'; '.join(failures)}") from error


@contextmanager
def browser_runtime(channel, *, timeout=None, info=None):
    """Start the verification browser. `info`, when given, receives the requested and used channel and any fallback."""
    with ExitStack() as stack:
        try:
            from playwright.sync_api import sync_playwright

            driver = stack.enter_context(sync_playwright())
            browser, used, fallback = launch_browser(driver, channel, timeout)
            stack.callback(browser.close)
        except Exception as error:
            raise BrowserRuntimeUnavailable(
                f"Cannot start {channel or 'bundled Chromium'}: {error}"
            ) from error
        if info is not None:
            info.update(requested=channel or "bundled", used=used, fallback=fallback)
        yield browser


def warm_up(browser):
    """Installed Edge and Chrome draw their first screenshot slowly. Pay that once, outside the timed cases."""
    try:
        context = browser.new_context()
        try:
            page = context.new_page()
            page.set_content("<!doctype html><title>Nightweaver warm-up</title>")
            page.screenshot(timeout=30000)
        finally:
            context.close()
    except Exception:
        pass  # A browser that is really broken fails the first case with its own diagnosis.


def favicon_failure(message):
    """Installed Edge and Chrome fetch /favicon.ico on the first page. The headless shell never does, so it is not a page error."""
    url = (getattr(message, "location", None) or {}).get("url", "")
    return message.text.startswith("Failed to load resource") and urlparse(url).path == "/favicon.ico"


def run_case(
    browser, config, scenario, viewport, steps, root, output, url, server_check
):
    from playwright.sync_api import expect

    case = journeys.case_prefix(scenario.get("name"), viewport["name"])
    timeout, offline = config["timeout_ms"], config.get("offline", False)
    context = browser.new_context(
        viewport={k: viewport[k] for k in ("width", "height")},
        **{
            "reduced_motion": "reduce",
            **{
                k: v
                for k, v in viewport.items()
                if k not in ("name", "width", "height")
            },
        },
        offline=offline,
        storage_state=journeys.storage_state(url, scenario),
        service_workers="block" if offline else "allow",
    )
    result = {
        "viewport": viewport["name"],
        "passed": False,
        "errors": [],
        "completed_steps": 0,
        "checkpoints": [],
        "step_durations_ms": [],
    }
    if scenario:
        result["scenario"] = scenario["name"]
    stage = {"action": "navigate"}
    error_counts = Counter()
    first_error_stage = None
    started = monotonic()

    def issue(category, message):
        nonlocal first_error_stage
        error_counts[category] += 1
        if first_error_stage is None:
            first_error_stage = stage.copy()
        message = str(redact(str(message)))[:2000]
        if len(result["errors"]) < 20 and message not in result["errors"]:
            result["errors"].append(message)

    def watch_page(page):
        page.set_default_timeout(timeout)
        page.on("pageerror", lambda err: issue("page_error", err))
        page.on(
            "console",
            lambda msg: issue("console", msg.text) if msg.type == "error" and not favicon_failure(msg) else None,
        )
        return page

    def navigate(page):
        response = page.goto(url, wait_until=config["wait_until"], timeout=timeout)
        if (response is None and not offline) or (
            response is not None and response.status >= 400
        ):
            raise ValueError(
                f"Page response: {response.status if response else 'missing'}"
            )

    try:
        page = watch_page(journeys.initial_page(context, url, scenario))
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        traffic = journeys.NetworkObservations(context, url, config, steps, issue)
        try:
            server_check()
            before_nav = monotonic()
            try:
                navigate(page)
            finally:
                result["navigation_ms"] = round((monotonic() - before_nav) * 1000)
            if config.get("heading"):
                stage = {"action": "heading", "name": config["heading"]}
                expect(
                    page.get_by_role("heading", name=config["heading"], exact=True)
                ).to_be_visible(timeout=timeout)
            for index, step in enumerate(steps, 1):
                action = step["action"]
                stage = {
                    "index": index,
                    "action": action,
                    "selector": step.get("selector", step.get("locator")),
                }
                server_check()
                tick = monotonic()
                try:
                    if action == "reopen":
                        page.close()
                        page = watch_page(context.new_page())
                        navigate(page)
                    elif action in ("storage", "url_path"):
                        journeys.assert_page_state(page, step, url, timeout)
                    elif action == "response":
                        value = step["value"]
                        traffic.consume(value, timeout)
                        result.setdefault("response_matches", []).append(
                            {"index": index, **value}
                        )
                    else:
                        run_step(
                            page,
                            step,
                            index,
                            result,
                            output,
                            timeout,
                            root,
                            config["wait_until"],
                        )
                finally:
                    result["step_durations_ms"].append(
                        round((monotonic() - tick) * 1000)
                    )
                result["completed_steps"] += 1
            stage = {"action": "layout"}
            result["overflow_px"] = page.evaluate(
                "Math.max(0,document.documentElement.scrollWidth-innerWidth)"
            )
            if result["overflow_px"] > 1:
                raise ValueError(f"Horizontal overflow: {result['overflow_px']}px")
            server_check()
            result["passed"] = True
        except BrowserRuntimeUnavailable:
            raise
        except Exception as error:
            result["failed_at"] = stage.copy()
            issue("journey", error)
        finally:
            stage = {"action": "screenshot"}
            try:
                file = f"{case}.png"
                page.screenshot(
                    path=str(output / file), full_page=False, animations="disabled"
                )
                result["screenshot"] = file
            except Exception as error:
                issue("screenshot", f"Screenshot unavailable: {error}")
            stage = {"action": "trace"}
            try:
                file = f"{case}.trace.zip"
                context.tracing.stop(path=str(output / file))
                result["trace"] = file
            except Exception as error:
                issue("trace", f"Trace unavailable: {error}")
    finally:
        context.close()
    result["network_summary"] = traffic.summary()
    total = result["network_summary"]["requests"]
    network = config.get("network", {})
    if "network" in config:
        result["http_requests"] = total
        if "max_requests" in network and total > network["max_requests"]:
            stage = {"action": "request_budget"}
            issue(
                "request_budget",
                f"HTTP request budget exceeded: {total}/{network['max_requests']}",
            )
    result["error_counts"] = dict(error_counts)
    result["duration_ms"] = round((monotonic() - started) * 1000)
    result["passed"] = result["passed"] and not error_counts
    if not result["passed"] and first_error_stage:
        result.setdefault("failed_at", first_error_stage)
    return result


@contextmanager
def browser_output(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    marker = output / ".nightweaver-browser-active"
    message = "Browser output must be a new or empty directory with no active verifier"
    try:
        reservation = marker.open("xb")
    except FileExistsError as error:
        raise ValueError(message) from error
    try:
        with reservation:
            if any(path != marker for path in output.iterdir()):
                raise ValueError(message)
            yield output
    finally:
        marker.unlink()


def run_browser(config, root, output):
    with browser_output(output) as reserved:
        return _verify_browser(config, root, reserved)


def _verify_browser(config, root, output):
    config = validate_browser(config)
    root = Path(root).resolve()
    results = []
    channel = {}
    with browser_runtime(config.get("channel"), info=channel) as browser:
        runtime = {
            "chromium": browser.version,
            "playwright": version("playwright"),
            "channel": channel["used"],
        }
        if channel["fallback"]:
            runtime["browser_channel_fallback"] = channel["fallback"]
        if channel["used"] != "bundled":
            warm_up(browser)
        with local_page(config, root, output) as (url, server_check):
            for scenario, viewport, steps in journeys.browser_cases(config):
                results.append(
                    run_case(
                        browser,
                        config,
                        scenario,
                        viewport,
                        steps,
                        root,
                        output,
                        url,
                        server_check,
                    )
                )
    receipt = {
        "schema": "nightweaver.browser.v2",
        "runtime": runtime,
        "browser_channel": channel["used"],
        "passed": bool(results) and all(r["passed"] for r in results),
        "viewports": results,
    }
    if channel["fallback"]:
        receipt["browser_channel_fallback"] = channel["fallback"]
    write_json(output / "browser.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config")
    source.add_argument("--config-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        with browser_output(args.output) as output:
            try:
                raw = args.config_file.read_text(encoding="utf-8-sig") if args.config_file else args.config
                result = _verify_browser(strict_json(raw), Path.cwd(), output)
            except Exception as error:
                kind = "verification_error"
                if isinstance(error, BrowserRuntimeUnavailable):
                    kind = "browser_environment"
                elif isinstance(error, CandidateStartupFailure):
                    kind = "candidate_startup"
                result = {"passed": False, "failure_kind": kind, "error": redact(str(error))[:4000]}
                write_json(output / "browser.json", result)
    except (OSError, ValueError) as error:
        print(redact(str(error)), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
