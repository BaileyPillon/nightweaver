"""Declarative local browser acceptance checks. Playwright is an optional dependency."""

import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
from threading import Thread
from urllib.parse import quote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightweaver.safety import write_json


def validate_browser(raw):
    from nightweaver.spec import array, artifact, fields, integer, text
    fields(raw, "page url server heading steps viewports channel timeout_ms wait_until offline", "browser")
    if sum(key in raw for key in ("page", "url", "server")) != 1:
        raise ValueError("Browser needs exactly one of page, loopback url or owned server")
    out = dict(raw)
    if type(raw.get("offline", False)) is not bool or (raw.get("offline") and "page" not in raw):
        raise ValueError("Offline browser checks require a local page and a boolean offline option")
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
        fields(step, "action selector value", "browser step")
        action = text(step, "action")
        if action == "checkpoint":
            label = text(step, "value")
            if (set(step) != {"action", "value"} or label != step["value"] or
                    not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", label) or label in checkpoints):
                raise ValueError("Use a unique lowercase checkpoint name and omit selector")
            checkpoints.add(label)
            continue
        if action not in ("click", "fill", "press", "check", "visible", "hidden", "enabled", "disabled",
                          "focused", "text", "count"):
            raise ValueError(f"Unsupported browser action: {action}")
        text(step, "selector")
        if action in ("fill", "press", "text") and not isinstance(step.get("value"), str):
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
        fields(viewport, "name width height", "viewport")
        name = text(viewport, "name")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name) or name in names:
            raise ValueError("Viewport names must be unique lowercase identifiers")
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
    page = (root / config["page"]).resolve(strict=True)
    if not page.is_relative_to(root) or not page.is_file():
        raise ValueError("Browser page must be a file inside the candidate checkout")
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


def run_step(page, step, index, result, output, timeout):
    from playwright.sync_api import expect
    action = step["action"]
    if action == "checkpoint":
        file = f"checkpoint.{result['viewport']}.{step['value']}.png"
        page.screenshot(path=str(output / file), animations="disabled")
        result["checkpoints"].append({"step": index, "name": step["value"], "file": file})
        return
    locator = page.locator(step["selector"])
    if action in ("visible", "hidden", "enabled", "disabled", "focused"):
        getattr(expect(locator), "to_be_" + action)(timeout=timeout)
    elif action in ("text", "count"):
        getattr(expect(locator), "to_have_" + action)(step["value"], timeout=timeout)
    elif action in ("fill", "press"):
        getattr(locator, action)(step["value"])
    else:
        getattr(locator, action)()


def run_browser(config, root, output):
    from playwright.sync_api import sync_playwright, expect
    config = validate_browser(config)
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with local_page(config, root, output) as (url, server_check), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=config.get("channel"))
        try:
            for viewport in config["viewports"]:
                name = viewport["name"]
                errors = []
                offline = config.get("offline", False)
                context = browser.new_context(viewport={key: viewport[key] for key in ("width", "height")},
                                              reduced_motion="reduce", offline=offline,
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
                        stage = {"index": index, "action": step["action"], "selector": step.get("selector")}
                        server_check()
                        run_step(page, step, index, result, output, config["timeout_ms"])
                        result["completed_steps"] += 1
                    stage = {"action": "layout"}
                    result["overflow_px"] = page.evaluate("Math.max(0,document.documentElement.scrollWidth-innerWidth)")
                    if result["overflow_px"] > 1:
                        raise ValueError(f"Horizontal overflow: {result['overflow_px']}px")
                    server_check()
                    result["passed"] = not errors
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
                    result["passed"] = result["passed"] and not errors
                    results.append(result)
        finally:
            browser.close()
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
        result = {"passed": False, "error": str(error)}
        write_json(args.output / "browser.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
