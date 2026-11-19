"""Declarative local browser acceptance checks. Playwright is an optional dependency."""

import argparse
from contextlib import ExitStack, contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from threading import Thread
from time import monotonic
from urllib.parse import quote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightweaver.safety import redact, write_json
from nightweaver.server import CandidateStartupFailure


def validate_browser(raw):
    from nightweaver.spec import array, artifact, fields, integer, text
    fields(raw, "page url server heading steps viewports channel timeout_ms wait_until offline network", "browser")
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
    checkpoints = set()
    for step in out["steps"]:
        fields(step, "action selector locator value", "browser step")
        action = text(step, "action")
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
        if action not in ("click", "fill", "press", "check", "visible", "hidden", "enabled", "disabled",
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
        delta = ImageChops.difference(reference.convert("RGB"), capture.convert("RGB"))
        red, green, blue = delta.split()
        histogram = ImageChops.lighter(red, ImageChops.lighter(green, blue)).histogram()
        changed = sum(histogram[contract.get("channel_delta", 0) + 1:])
        total = capture.width * capture.height
        result.setdefault("visual_matches", []).append({"step": prefix, "changed_pixels": changed,
                                                       "total_pixels": total, "baseline_sha256": contract["sha256"]})
        if changed * 1000000 > contract.get("max_changed_ppm", 0) * total:
            delta.save(output / (prefix + ".diff.png"))
            raise ValueError(f"Visual regression: {changed}/{total} pixels differ")


def run_step(page, step, index, result, output, timeout, root):
    from playwright.sync_api import expect
    action = step["action"]
    if action == "reload":
        response = page.reload(wait_until="load", timeout=timeout)
        if response and response.status >= 400:
            raise ValueError(f"Reload returned HTTP {response.status}")
        return
    if action == "checkpoint":
        file = f"checkpoint.{result['viewport']}.{step['value']}.png"
        page.screenshot(path=str(output / file), animations="disabled")
        result["checkpoints"].append({"step": index, "name": step["value"], "file": file})
        return
    locator = semantic_locator(page, step)
    prefix = f"{result['viewport']}.{index:02d}"
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


@contextmanager
def browser_runtime(channel, *, timeout=None):
    with ExitStack() as stack:
        try:
            from playwright.sync_api import sync_playwright
            driver = stack.enter_context(sync_playwright())
            browser = driver.chromium.launch(channel=channel, timeout=timeout)
            stack.callback(browser.close)
        except Exception as error:
            raise BrowserRuntimeUnavailable(f"Cannot start {channel or 'bundled Chromium'}: {error}") from error
        yield browser


def run_browser(config, root, output):
    config = validate_browser(config)
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with browser_runtime(config.get("channel")) as browser:
        from playwright.sync_api import expect
        with local_page(config, root, output) as (url, server_check):
            for viewport in config["viewports"]:
                name = viewport["name"]
                errors = []
                offline = config.get("offline", False)
                context = browser.new_context(viewport={key: viewport[key] for key in ("width", "height")},
                                              **{"reduced_motion": "reduce", **{k: v for k, v in viewport.items()
                                                  if k not in ("name", "width", "height")}}, offline=offline,
                                              service_workers="block" if offline else "allow")
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                page = context.new_page()
                page.set_default_timeout(config["timeout_ms"])
                def record_error(message):
                    if len(errors) < 20:
                        errors.append(str(message)[:2000])
                page.on("pageerror", record_error)
                page.on("console", lambda message: record_error(message.text) if message.type == "error" else None)
                if offline:
                    context.on("request", lambda req: record_error("External request during offline playback")
                               if req.url.startswith(("http:", "https:")) else None)
                result = {"viewport": name, "passed": False, "errors": errors, "completed_steps": 0, "checkpoints": []}
                started = monotonic()
                network = config.get("network", {})
                request_count = 0

                def count_request(req):
                    nonlocal request_count
                    if urlparse(req.url).scheme in ("http", "https"):
                        request_count += 1

                def failed_request(req, reason):
                    if network.get("require_success") and urlparse(req.url).scheme in ("http", "https"):
                        record_error(f"{reason} ({req.resource_type}) {urlparse(req.url).path[:240]}")

                if "network" in config:
                    context.on("request", count_request)
                    context.on("requestfailed", lambda req: failed_request(req, "Request failed"))
                    context.on("response", lambda res: failed_request(res.request, f"HTTP {res.status}") if res.status >= 400 else None)
                result["step_durations_ms"] = []
                stage = {"action": "navigate"}
                try:
                    server_check()
                    response = page.goto(url, wait_until=config["wait_until"], timeout=config["timeout_ms"])
                    if (response is None and not offline) or (response is not None and response.status >= 400):
                        raise ValueError(f"Page response: {response.status if response else 'missing'}")
                    if config.get("heading"):
                        stage = {"action": "heading", "name": config["heading"]}
                        expect(page.get_by_role("heading", name=config["heading"], exact=True)).to_be_visible(timeout=config["timeout_ms"])
                    for index, step in enumerate(config["steps"], 1):
                        stage = {"index": index, "action": step["action"], "selector": step.get("selector", step.get("locator"))}
                        server_check()
                        step_start = monotonic()
                        try:
                            run_step(page, step, index, result, output, config["timeout_ms"], root)
                        finally:
                            result["step_durations_ms"].append(round((monotonic() - step_start) * 1000))
                        result["completed_steps"] += 1
                    stage = {"action": "layout"}
                    result["overflow_px"] = page.evaluate("Math.max(0,document.documentElement.scrollWidth-innerWidth)")
                    if result["overflow_px"] > 1:
                        raise ValueError(f"Horizontal overflow: {result['overflow_px']}px")
                    server_check()
                    result["passed"] = not errors
                except BrowserRuntimeUnavailable:
                    raise
                except Exception as error:
                    result["failed_at"] = stage
                    errors.append(str(error)[:4000])
                finally:
                    try:
                        page.screenshot(path=str(output / f"{name}.png"), full_page=False, animations="disabled")
                        result["screenshot"] = f"{name}.png"
                    except Exception as error:
                        result["passed"] = False
                        errors.append(f"Screenshot unavailable: {error}")
                    try:
                        context.tracing.stop(path=str(output / f"{name}.trace.zip"))
                        result["trace"] = f"{name}.trace.zip"
                    except Exception as error:
                        result["passed"] = False
                        record_error(f"Trace unavailable: {error}")
                    finally:
                        context.close()
                    if "network" in config:
                        result["http_requests"] = request_count
                        if "max_requests" in network and request_count > network["max_requests"]:
                            record_error(f"HTTP request budget exceeded: {request_count}/{network['max_requests']}")
                    result["duration_ms"] = round((monotonic() - started) * 1000)
                    result["passed"] = result["passed"] and not errors
                    results.append(result)
    receipt = {"passed": bool(results) and all(row["passed"] for row in results), "viewports": results}
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
        raw = args.config_file.read_text(encoding="utf-8") if args.config_file else args.config
        result = run_browser(json.loads(raw), Path.cwd(), args.output)
    except Exception as error:
        kind = "verification_error"
        if isinstance(error, BrowserRuntimeUnavailable):
            kind = "browser_environment"
        elif isinstance(error, CandidateStartupFailure):
            kind = "candidate_startup"
        result = {"passed": False, "failure_kind": kind, "error": redact(str(error))[:4000]}
        write_json(args.output / "browser.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
