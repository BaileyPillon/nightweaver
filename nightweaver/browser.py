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
    fields(raw, "page url heading steps viewports channel timeout_ms", "browser")
    if ("page" in raw) == ("url" in raw):
        raise ValueError("Browser needs exactly one of page or loopback url")
    out = dict(raw)
    if "page" in raw:
        out["page"] = artifact(raw["page"])
    else:
        url = urlparse(text(raw, "url"))
        if url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password:
            raise ValueError("Browser URLs must be explicit loopback HTTP(S) addresses")
    if "heading" in raw:
        out["heading"] = text(raw, "heading")
    if "channel" in raw and raw["channel"] not in ("msedge", "chrome", "chromium"):
        raise ValueError("Unsupported browser channel")
    out["timeout_ms"] = integer(raw.get("timeout_ms", 10000), "browser timeout_ms", 100, 120000)
    out["steps"] = array(raw.get("steps", []), "browser steps", 100)
    for step in out["steps"]:
        fields(step, "action selector value", "browser step")
        action = text(step, "action")
        if action not in ("click", "fill", "press", "check", "visible", "text", "count"):
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
def local_page(config, root):
    if "url" in config:
        yield config["url"]
        return
    page = (root / config["page"]).resolve(strict=True)
    if not page.is_relative_to(root) or not page.is_file():
        raise ValueError("Browser page must be a file inside the candidate checkout")
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root))) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/{quote(page.relative_to(root).as_posix())}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def run_browser(config, root, output):
    from playwright.sync_api import sync_playwright, expect
    config = validate_browser(config)
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with local_page(config, root) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=config.get("channel"))
        try:
            for viewport in config["viewports"]:
                name = viewport["name"]
                errors = []
                context = browser.new_context(viewport={key: viewport[key] for key in ("width", "height")}, reduced_motion="reduce")
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                page = context.new_page()
                page.set_default_timeout(config["timeout_ms"])
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                result = {"viewport": name, "passed": False, "errors": errors, "completed_steps": 0}
                try:
                    response = page.goto(url, wait_until="networkidle", timeout=config["timeout_ms"])
                    if response is None or response.status >= 400:
                        raise ValueError(f"Page response: {response.status if response else 'missing'}")
                    if config.get("heading"):
                        expect(page.get_by_role("heading", name=config["heading"], exact=True)).to_be_visible()
                    for step in config["steps"]:
                        locator = page.locator(step["selector"])
                        action = step["action"]
                        if action == "visible":
                            expect(locator).to_be_visible()
                        elif action == "text":
                            expect(locator).to_have_text(step["value"])
                        elif action == "count":
                            expect(locator).to_have_count(step["value"])
                        elif action in ("fill", "press"):
                            getattr(locator, action)(step["value"])
                        else:
                            getattr(locator, action)()
                        result["completed_steps"] += 1
                    result["overflow_px"] = page.evaluate("Math.max(0,document.documentElement.scrollWidth-innerWidth)")
                    if result["overflow_px"] > 1:
                        raise ValueError(f"Horizontal overflow: {result['overflow_px']}px")
                    result["passed"] = not errors
                except Exception as error:
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
                    finally:
                        context.close()
                    results.append(result)
        finally:
            browser.close()
    receipt = {"passed": bool(results) and all(row["passed"] for row in results), "viewports": results}
    write_json(output / "browser.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run_browser(json.loads(args.config), Path.cwd(), args.output)
    except Exception as error:
        result = {"passed": False, "error": str(error)}
        write_json(args.output / "browser.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
