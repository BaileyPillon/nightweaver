"""Check a static page from the candidate checkout and capture two viewport screenshots."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # The browser may close an optional resource request when the page closes.
        if not isinstance(sys.exception(), ConnectionError):
            super().handle_error(request, client_address)


def check(browser, url, heading, label, width, height, output):
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    try:
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(url, wait_until="load")
        if response is None or not response.ok or page.url != url:
            raise RuntimeError(f"{label}: page failed to load at {url}")
        expect(page.get_by_role("heading", level=1, name=heading, exact=True)).to_be_visible()
        page.evaluate("document.fonts.ready")
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
            raise RuntimeError(f"{label}: page overflows its {width}px viewport")
        if errors:
            raise RuntimeError(f"{label}: uncaught page error: {errors[0]}")
        page.screenshot(path=str(output / f"{label}.png"), animations="disabled")
        print(f"{label} {width}x{height}: heading visible, no horizontal overflow or uncaught page error")
    finally:
        page.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", help="HTML file relative to the candidate checkout")
    parser.add_argument("heading", help="Expected visible main heading")
    parser.add_argument("--channel", help="Use an installed browser such as msedge instead of bundled Chromium")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    page = (root / args.page).resolve(strict=True)
    if not page.is_relative_to(root) or not page.is_file():
        parser.error("page must be a file inside the candidate checkout")
    output = root / ".nightweaver-browser"
    output.mkdir(exist_ok=True)
    with Server(("127.0.0.1", 0), partial(Handler, directory=str(page.parent))) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/{quote(page.name)}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=args.channel)
                for label, width, height in (("desktop", 1440, 900), ("mobile", 390, 844)):
                    check(browser, url, args.heading, label, width, height, output)
                browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
