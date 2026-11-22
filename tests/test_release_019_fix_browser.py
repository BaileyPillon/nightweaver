"""Nightweaver 0.19 review fixes for discovery and the browser runner: unrecognised test suites, new repositories,
public/ and docs/ sites, visual baselines on a fallback browser and the favicon exemption."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from nightweaver import browser, discovery
from nightweaver import wish as wishing
from nightweaver.checks import evidence_folder, run_gate
from nightweaver.cli import main
from nightweaver.diagnostics import preflight, probe_browser_channels
from nightweaver.engine import git
from nightweaver.spec import gate_spec, validate
from nightweaver.store import Store

MISSING = "BrowserType.launch: Executable doesn't exist at C:\\pw\\chrome-headless-shell.exe\n| Please run: playwright install |"
PAGE = ('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="icon" href="data:,"><title>Fix</title><style>body{margin:0;font:20px sans-serif}</style>'
        "<main><h1>Fix fixture</h1><p>Hello</p></main></html>")
UNITTEST = "import unittest\nclass T(unittest.TestCase):\n def test_a(self): pass\n"
NOT_FOUND = "Failed to load resource: the server responded with a status of 404 (Not Found)"
HTTP_PAGE = "http://127.0.0.1:8080/index.html"


def repository(root, files):
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Nightweaver Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(data, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "fixture", "--allow-empty")
    return root


class FakeDriver:
    """Stands in for sync_playwright(): each channel either launches or raises the scripted error."""

    def __init__(self, **outcomes):
        self.outcomes, self.calls = outcomes, []
        self.chromium = SimpleNamespace(launch=self.launch)

    def launch(self, channel=None, timeout=None):
        self.calls.append(channel)
        outcome = self.outcomes.get(channel or "bundled", "ok")
        if outcome != "ok":
            raise RuntimeError(outcome)
        page = MagicMock()
        page.title.return_value = "Nightweaver preflight"
        return MagicMock(new_page=MagicMock(return_value=page), version=f"fake-{channel or 'bundled'}")


@contextmanager
def fake_playwright(driver):
    @contextmanager
    def session():
        yield driver

    api = SimpleNamespace(sync_playwright=session)
    with patch.dict(sys.modules, {"playwright": SimpleNamespace(sync_api=api), "playwright.sync_api": api}):
        yield


class TempCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="nw fix browser ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.count = 0
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        browser._FALLBACKS.clear()
        self.addCleanup(browser._FALLBACKS.clear)

    def discover(self, files, objective="Build the page", **kwargs):
        self.count += 1
        return discovery.discover(repository(self.root / f"repo{self.count}", files), objective, {"kind": "codex"}, **kwargs)


class ForeignTestSuiteTests(TempCase):
    """#4: a repository whose tests discovery cannot run never gets a smoke gate in their place."""

    def refused(self, files, evidence):
        with self.assertRaises(ValueError, msg=files) as caught:
            self.discover(files)
        message = str(caught.exception)
        self.assertTrue(message.startswith("No supported committed test suite found."), message)
        self.assertIn(evidence, message)
        self.assertIn("Nightweaver template", message, "the failure says how to add a gate")
        self.assertIn('"argv"', message)

    def test_tests_in_other_ecosystems_keep_the_no_suite_failure_even_beside_a_page(self):
        placed = {"public/index.html": PAGE}
        for files, evidence in (
            ({"go.mod": "module x\n", "main_test.go": "package main\n", **placed}, "main_test.go"),
            ({"Cargo.toml": "[package]\nname='x'\n", "src/lib.rs": "#[test] fn t() {}\n", **placed}, "Cargo.toml"),
            ({"pom.xml": "<project/>", "src/test/java/AppTest.java": "class AppTest {}", **placed}, "AppTest.java"),
            ({"build.gradle": "plugins {}", "index.html": PAGE}, "build.gradle"),
            ({"Gemfile": "source 'x'", "spec/app_spec.rb": "describe 1", "index.html": PAGE}, "app_spec.rb"),
            ({"composer.json": "{}", "src/UserTest.php": "<?php", "index.html": PAGE}, "UserTest.php"),
            ({"src/pkg/test_core.py": UNITTEST, "index.html": PAGE}, "test_core.py"),
            ({"app.test.js": "test('a', () => {})", "index.html": PAGE}, "app.test.js"),
            ({"tests/app.js": "x", "index.html": PAGE}, "tests/app.js"),
            ({"Makefile": "all:\n\techo hi\n\ntest:\n\tgo test ./...\n", "index.html": PAGE}, "Makefile"),
            ({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n", "docs/index.html": PAGE}, "pyproject.toml"),
            ({"tox.ini": "[tox]\n", "index.html": PAGE}, "tox.ini"),
            ({"App.sln": "", "index.html": PAGE}, "App.sln"),
        ):
            with self.subTest(evidence=evidence):
                self.refused(files, evidence)

    def test_a_placeholder_npm_script_beside_javascript_tests_keeps_raising(self):
        placeholder = json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
        with self.assertRaisesRegex(ValueError, "placeholder test script"):
            self.discover({"package.json": placeholder, "tests/app.test.js": "import test from 'node:test'", "index.html": PAGE})

    def test_a_plain_web_repository_still_gets_the_smoke_gate(self):
        plain = {"index.html": PAGE, "style.css": "body{}", "app.js": "", "latest.js": "", "contest.py": "", "test.html": PAGE,
                 "LICENSE": "MIT", "package.json": json.dumps({"scripts": {"build": "tsc"}}),
                 "Makefile": "all:\n\tcp a b\n", "pyproject.toml": '[project]\ndescription = "A toxic comment viewer"\n'}
        self.assertEqual([g["name"] for g in self.discover(plain)["gates"]], ["Browser smoke"])

    def test_recognised_suites_are_untouched(self):
        plan = self.discover({"index.html": PAGE, "tests/test_a.py": UNITTEST})
        self.assertEqual([g["name"] for g in plan["gates"]], ["Python unittest tests"])

    def test_a_repository_without_marks_keeps_the_short_message(self):
        with self.assertRaises(ValueError) as caught:
            self.discover({"main.py": "print(1)", "README.md": "# Tool"})
        self.assertEqual(str(caught.exception), "No supported committed test suite found. Use a Nightweaver template with an explicit check.")


class NewRepositoryTests(TempCase):
    """#24: only a web goal makes a brand-new repository ask for an index.html."""

    NEW = {"README.md": "# New", "LICENSE": "MIT", ".gitignore": "node_modules/\n"}
    TOOL = "Write a Python command line tool that renames files by date"

    def test_web_goal_reads_the_objective(self):
        for text in ("Create a landing page for a bakery", "Build a website", "Make a browser game on a canvas", "A dashboard UI",
                     "Draw an SVG logo", "Style the responsive HTML and CSS", "Restyle the site", "A webapp for recipes"):
            self.assertTrue(discovery.web_goal(text), text)
        for text in (self.TOOL, "Port the parser to Rust", "Fix the websocket reconnect bug", "Add a webhook handler", "Refactor the cache"):
            self.assertFalse(discovery.web_goal(text), text)

    def test_a_new_repository_needs_a_web_goal(self):
        for files in (self.NEW, {}):
            for web in (None, False):
                with self.subTest(files=bool(files), web=web), self.assertRaises(ValueError) as caught:
                    self.discover(files, self.TOOL, web=web)
                self.assertTrue(str(caught.exception).startswith("No supported committed test suite found."))
                self.assertIn("--web", str(caught.exception))
                self.assertIn("template", str(caught.exception))
            plan = self.discover(files, "Create a landing page for a bakery", web=True)
            self.assertEqual([g["name"] for g in plan["gates"]], ["Browser smoke"])
            self.assertIn("index.html exists", plan["acceptance"][0])

    def test_the_hint_never_changes_a_repository_that_has_a_page(self):
        for web in (None, False, True):
            self.assertEqual(self.discover({"index.html": PAGE}, self.TOOL, web=web)["gates"][0]["browser"]["page"], "index.html")

    def preview(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.root / "home"), "oneshot", *map(str, argv), "--preview"])
        return code, out.getvalue(), err.getvalue()

    def test_oneshot_judges_the_objective_and_accepts_an_explicit_choice(self):
        repo = repository(self.root / "fresh", self.NEW)
        code, out, err = self.preview(repo, self.TOOL)
        self.assertEqual(code, 1, out)
        self.assertIn("--web", err)
        code, out, _ = self.preview(repo, "Build a landing page for a bakery")
        self.assertEqual((code, json.loads(out)["gates"][0]["name"]), (0, "Browser smoke"))
        code, out, _ = self.preview(repo, self.TOOL, "--web")
        self.assertEqual((code, json.loads(out)["gates"][0]["name"]), (0, "Browser smoke"))
        code, _, err = self.preview(repo, "Build a landing page for a bakery", "--no-web")
        self.assertEqual(code, 1, err)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class WishHintTests(TempCase):
    """#24 in wish: the wish's words decide before the brief writer is paid, then the brief's visual flag overrules them."""

    BRIEF = {"title": "A page", "objective": "Build it.", "acceptance": ["a", "b", "c", "d"], "visual": True, "conflicts": []}

    def run_wish(self, repo, wish, visual):
        calls = []

        def write(config, plan, text, folder):
            calls.append(plan["gates"])
            return {**self.BRIEF, "visual": visual}, folder / "brief-writer-1.jsonl", 1

        out, err = io.StringIO(), io.StringIO()
        with patch.object(wishing, "write_brief", write), patch.object(wishing, "ollama_advisory", return_value=(None, None)), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.root / "home"), "wish", str(repo), wish, "--preview"])
        return code, out.getvalue(), err.getvalue(), calls

    def test_a_new_repository_with_a_web_wish_gets_the_smoke_gate(self):
        repo = repository(self.root / "fresh", NewRepositoryTests.NEW)
        code, out, err, calls = self.run_wish(repo, "Build a landing page for a bakery", True)
        self.assertEqual(code, 0, err + out)
        self.assertEqual([[g["name"] for g in gates] for gates in calls], [["Browser smoke"]])

    def test_a_new_repository_with_a_tool_wish_stops_before_the_brief_writer(self):
        repo = repository(self.root / "fresh", NewRepositoryTests.NEW)
        code, _, err, calls = self.run_wish(repo, NewRepositoryTests.TOOL, False)
        self.assertEqual(code, 1)
        self.assertIn("--web", err)
        self.assertEqual(calls, [])

    def test_a_brief_that_is_not_visual_withdraws_a_smoke_gate_the_words_suggested(self):
        repo = repository(self.root / "fresh", NewRepositoryTests.NEW)
        code, _, err, calls = self.run_wish(repo, "Write a web scraper that saves each page as text", False)
        self.assertEqual(code, 1)
        self.assertIn("No supported committed test suite found", err)
        self.assertEqual(len(calls), 1, "the brief was written once, then the gate was withdrawn")
        self.assertEqual(list((self.root / "home" / "wishes").iterdir())[0].joinpath("template.json").exists(), False)


class PageRootTests(TempCase):
    """#25: a site kept in public/ or docs/ is served with that folder as its root."""

    def test_validation_accepts_a_root_around_the_page_and_rejects_the_rest(self):
        config = browser.validate_browser({"page": "public/index.html", "root": "public"})
        self.assertEqual((config["page"], config["root"]), ("public/index.html", "public"))
        for raw in ({"page": "index.html", "root": "public"}, {"page": "public/index.html", "root": "pub"},
                    {"page": "public/index.html", "root": "../public"}, {"page": "public/index.html", "root": "/public"},
                    {"page": "public/index.html", "root": "public", "offline": True},
                    {"url": "http://127.0.0.1:8080/", "root": "public"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                browser.validate_browser(raw)

    def test_the_page_folder_is_the_http_root(self):
        site = repository(self.root / "site", {"public/index.html": PAGE, "public/style.css": "body{}", "secret.txt": "outside"})
        output = self.root / "out"
        output.mkdir()
        with browser.local_page({"page": "public/index.html", "root": "public"}, site.resolve(), output) as (url, _):
            base = url.rsplit("/", 1)[0]
            self.assertTrue(url.endswith("/index.html"), url)
            self.assertEqual(urlopen(base + "/style.css").read(), b"body{}")
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/secret.txt")
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
        with browser.local_page({"page": "public/index.html"}, site.resolve(), output) as (url, _):
            self.assertTrue(url.endswith("/public/index.html"), url)
            self.assertEqual(urlopen(url.rsplit("/", 2)[0] + "/secret.txt").read(), b"outside")

    def test_discovery_roots_public_and_docs_pages_only(self):
        self.assertEqual(self.discover({"public/index.html": PAGE})["gates"][0]["browser"],
                         {"page": "public/index.html", "root": "public", "sweep": {}})
        self.assertEqual(self.discover({"docs/index.html": PAGE})["gates"][0]["browser"],
                         {"page": "docs/index.html", "root": "docs", "sweep": {}})
        plan = self.discover({"index.html": PAGE})
        self.assertEqual(plan["gates"][0]["browser"], {"page": "index.html", "sweep": {}})
        for gate in self.discover({"public/index.html": PAGE})["gates"]:
            self.assertEqual(gate_spec(gate)["browser"]["root"], "public")


def browser_ready():
    return bool(importlib.util.find_spec("playwright")) and not probe_browser_channels(["bundled"])


class Site:
    """A loopback site: `pages` maps a path to HTML, and /favicon.ico answers with the status in `icon`."""

    def __init__(self, pages, icon=404):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/favicon.ico":
                    self.send_response(outer.icon)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                entry = outer.pages.get(path)
                kind, text = entry if isinstance(entry, tuple) else ("text/html", entry or "")
                body = text.replace("%PORT%", str(outer.server.server_port)).encode()
                self.send_response(200 if path in outer.pages else 404)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.pages, self.icon = pages, icon
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.server.server_port}{path}"


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Install the browser extra")
class RealBrowserCase(TempCase):
    @classmethod
    def setUpClass(cls):
        if not browser_ready():
            raise unittest.SkipTest("No browser channel can launch on this machine")

    def smoke(self, files):
        """Run the discovered smoke gate for a repository through the normal check path."""
        repo = repository(self.root / "run", files)
        plan = discovery.discover(repo, "Build the page", {"kind": "codex"})
        gate = gate_spec(plan["gates"][0])
        return run_gate(gate, repo, evidence_folder(self.root / "evidence", 1), git(repo, "rev-parse", "HEAD^{tree}"))

    def visit(self, site, path="/", steps=()):
        self.count += 1
        config = {"url": site.url(path), "steps": list(steps), "viewports": [{"name": "desktop", "width": 900, "height": 700}]}
        return browser.run_browser(config, self.root, self.root / f"out{self.count}")


class PageRootRunTests(RealBrowserCase):
    SITE = {"public/index.html": PAGE.replace("<main>", '<link rel="stylesheet" href="/style.css"><script src="/app.js"></script><main>'),
            "public/style.css": "h1{color:#036}", "public/app.js": "document.title = 'ready'"}

    def test_root_absolute_assets_load_from_a_public_site(self):
        receipt = self.smoke(self.SITE)
        self.assertEqual(receipt["exit"], 0, receipt.get("browser_failures"))

    def test_a_missing_root_absolute_asset_still_fails(self):
        receipt = self.smoke({name: data for name, data in self.SITE.items() if name != "public/app.js"})
        self.assertEqual(receipt["exit"], 1)
        self.assertTrue(any("404" in failure["error"] for failure in receipt["browser_failures"]), receipt["browser_failures"])


class VisualFallbackTests(TempCase):
    """#23: a pixel baseline is pinned to the browser build that drew it, so no installed browser stands in."""

    STEP = {"action": "visual_match", "selector": "h1", "value": {"baseline": "ref.png", "sha256": "0" * 64}}

    def test_uses_visual_sees_top_level_and_scenario_steps(self):
        self.assertFalse(browser.uses_visual({"steps": [{"action": "click", "selector": "a"}]}))
        self.assertTrue(browser.uses_visual({"steps": [self.STEP]}))
        self.assertTrue(browser.uses_visual({"steps": [], "scenarios": [{"name": "a", "steps": []}, {"name": "b", "steps": [self.STEP]}]}))

    def test_launch_without_fallback_fails_closed_with_a_clear_message(self):
        driver = FakeDriver(bundled=MISSING)
        with self.assertRaises(RuntimeError) as caught:
            browser.launch_browser(driver, None, 5000, allow_fallback=False)
        self.assertEqual(driver.calls, [None])
        message = str(caught.exception)
        self.assertIn("visual_match baselines are pinned to the browser build that drew them", message)
        self.assertIn("no installed Edge or Chrome fallback was tried", message)
        self.assertIn("Install the bundled Chromium", message)
        browser._FALLBACKS["bundled"] = {"requested": "bundled", "used": "msedge", "reason": "old"}
        driver = FakeDriver(bundled=MISSING)
        with self.assertRaises(RuntimeError):
            browser.launch_browser(driver, None, 5000, allow_fallback=False)
        self.assertEqual(driver.calls, [None], "a fallback cached by another gate is not reused")
        browser._FALLBACKS.clear()
        self.assertEqual(browser.launch_browser(FakeDriver(bundled=MISSING), None, 5000)[1], "msedge", "other gates keep the fallback")

    def test_a_visual_gate_without_the_bundled_chromium_is_a_browser_environment_failure(self):
        config = {"page": "index.html", "steps": [self.STEP]}
        with patch("nightweaver.browser.image_tools"), fake_playwright(FakeDriver(bundled=MISSING)):
            with self.assertRaises(browser.BrowserRuntimeUnavailable) as caught:
                browser._verify_browser(config, self.root, self.root)
        self.assertIn("visual_match baselines", str(caught.exception))
        output, buffer = self.root / "out", io.StringIO()
        argv = ["nightweaver.browser", "--config", json.dumps(config), "--output", str(output)]
        with patch("nightweaver.browser.image_tools"), fake_playwright(FakeDriver(bundled=MISSING)):
            with patch.object(sys, "argv", argv), redirect_stdout(buffer):
                self.assertEqual(browser.main(), 1)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["failure_kind"], "browser_environment")
        self.assertIn("visual_match", report["error"])

    def test_a_pinned_browser_folder_keeps_its_own_message(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "C:/empty-cache"
        with self.assertRaises(RuntimeError) as caught:
            browser.launch_browser(FakeDriver(bundled=MISSING), None, 5000, allow_fallback=False)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH is set, so no installed Edge or Chrome fallback", str(caught.exception))
        self.assertNotIn("baselines", str(caught.exception))

    def test_missing_pillow_is_reported_before_any_browser_starts(self):
        driver = FakeDriver(bundled=MISSING)
        with patch.dict(sys.modules, {"PIL": None}), fake_playwright(driver), self.assertRaisesRegex(browser.BrowserRuntimeUnavailable, "Pillow"):
            browser._verify_browser({"page": "index.html", "steps": [self.STEP]}, self.root, self.root)
        self.assertEqual(driver.calls, [])

    def test_other_gates_keep_the_fallback(self):
        with fake_playwright(FakeDriver(bundled=MISSING)), browser.browser_runtime(None) as instance:
            self.assertEqual(instance.version, "fake-msedge")

    def test_the_preflight_probe_fails_closed_for_a_pinned_channel_only(self):
        resolved = {}
        with fake_playwright(FakeDriver(bundled=MISSING)):
            errors = probe_browser_channels(["bundled"], resolved, pinned={"bundled"})
            self.assertEqual(len(errors), 1)
            self.assertIn("visual_match baselines", errors[0])
            self.assertEqual(resolved, {})
            self.assertEqual(probe_browser_channels(["bundled"], resolved), [])
        self.assertEqual(resolved["bundled"]["used"], "msedge")

    @unittest.skipUnless(os.name == "nt", "native runner")
    def test_preflight_pins_the_channels_that_carry_visual_baselines(self):
        repo = repository(self.root / "project", {"index.html": PAGE})
        store = Store(self.root / "runtime")

        def plan(**browser_config):
            return validate({"repository": str(repo), "title": "T", "objective": "Check", "worker": {"kind": "command", "argv": [sys.executable, "--version"]},
                             "budgets": {"max_seconds": 120}, "milestones": [{"title": "T", "task": "Check"}],
                             "gates": [{"name": "open", "kind": "browser", "browser": {"page": "index.html", **browser_config}}]})

        for config, expected in (({"steps": [self.STEP]}, {"bundled"}), ({"steps": [self.STEP], "channel": "msedge"}, {"msedge"}), ({}, set())):
            with patch("nightweaver.diagnostics.probe_browser_channels", return_value=[]) as probe:
                preflight(store, plan(**config))
            self.assertEqual(probe.call_args.args[2], expected, config)


class FaviconExemptionTests(TempCase):
    """#45: only the browser's own failed same-origin /favicon.ico fetch is exempt from the console contract."""

    def message(self, text, url):
        return SimpleNamespace(text=text, location={"url": url})

    def test_the_exemption_is_narrow(self):
        icon = "http://127.0.0.1:8080/favicon.ico"
        exempt = lambda text, url, requested=(), page=HTTP_PAGE: browser.favicon_failure(self.message(text, url), page, requested)
        self.assertTrue(exempt(NOT_FOUND, icon))
        self.assertTrue(exempt("Failed to load resource: net::ERR_FAILED", icon))
        self.assertTrue(exempt(NOT_FOUND, "file:///favicon.ico", page="file:///C:/site/index.html"))
        self.assertFalse(exempt(NOT_FOUND, "http://localhost:8080/favicon.ico"), "another origin")
        self.assertFalse(exempt(NOT_FOUND, "http://127.0.0.1:9090/favicon.ico"), "another port")
        self.assertFalse(exempt(NOT_FOUND, "https://127.0.0.1:8080/favicon.ico"), "another scheme")
        self.assertFalse(exempt(NOT_FOUND, "file:///favicon.ico"), "a file icon for an http page")
        self.assertFalse(exempt("Failed to load resource: the server responded with a status of 500 (Internal Server Error)", icon))
        self.assertFalse(exempt("Failed to load resource: the server responded with a status of 4040 (x)", icon))
        self.assertFalse(exempt("Failed to load resource: the server responded with a status of 403 (Forbidden)", icon))
        self.assertFalse(exempt(NOT_FOUND, icon, requested={icon}), "the page asked for it")
        self.assertFalse(exempt(NOT_FOUND, icon + "?v=2"), "a versioned icon is the page's own")
        self.assertFalse(exempt("TypeError: favicon.ico", icon))
        self.assertFalse(exempt(NOT_FOUND, "http://127.0.0.1:8080/img/favicon.ico"))


class FaviconRunTests(RealBrowserCase):
    PLAIN = "<!doctype html><title>t</title><main><h1>Plain</h1></main>"

    def failures(self, receipt):
        return [error for view in receipt["viewports"] for error in view["errors"]]

    def test_a_plain_page_with_no_favicon_passes_on_every_channel(self):
        with Site({"/": self.PLAIN}, icon=404) as site:
            receipt = self.visit(site)
        self.assertTrue(receipt["passed"], self.failures(receipt))

    def test_a_page_that_names_a_missing_favicon_fails(self):
        page = '<!doctype html><title>t</title><link rel="icon" href="/favicon.ico"><main><h1>Logo</h1><img src="/favicon.ico" alt="logo"></main>'
        with Site({"/": page}, icon=404) as site:
            receipt = self.visit(site)
        self.assertFalse(receipt["passed"])
        self.assertTrue(any("Failed to load resource" in error for error in self.failures(receipt)), self.failures(receipt))

    def test_a_script_request_for_the_favicon_fails(self):
        page = ("<!doctype html><title>t</title><main><h1>Fetch</h1><button id=go>go</button><p id=out></p></main>"
                "<script>go.onclick = () => fetch('/' + 'favi' + 'con.ico').then(r => out.textContent = 'done ' + r.status)</script>")
        with Site({"/": page}, icon=404) as site:
            receipt = self.visit(site, steps=[{"action": "click", "selector": "#go"}, {"action": "text", "selector": "#out", "value": "done 404"}])
        self.assertFalse(receipt["passed"])
        self.assertTrue(any("Failed to load resource" in error for error in self.failures(receipt)), self.failures(receipt))

    def test_an_external_script_that_asks_for_the_favicon_at_load_fails(self):
        pages = {"/": "<!doctype html><title>t</title><main><h1>Ext</h1></main><script src='/app.js'></script>",
                 "/app.js": ("text/javascript", "fetch('/' + 'favi' + 'con.ico').catch(() => {})")}
        with Site(pages, icon=404) as site:
            receipt = self.visit(site, steps=[{"action": "visible", "selector": "h1"}])
        self.assertFalse(receipt["passed"])
        self.assertTrue(any("Failed to load resource" in error for error in self.failures(receipt)), self.failures(receipt))

    def test_a_cross_origin_favicon_request_fails(self):
        page = ("<!doctype html><title>t</title><main><h1>Other</h1></main>"
                "<script>new Image().src = 'http://localhost:%PORT%/' + 'favi' + 'con.ico'</script>")
        with Site({"/": page}, icon=404) as site:
            receipt = self.visit(site, steps=[{"action": "visible", "selector": "h1"}])
        self.assertFalse(receipt["passed"])
        self.assertTrue(any("Failed to load resource" in error for error in self.failures(receipt)), self.failures(receipt))

    def test_a_server_error_for_the_implicit_favicon_fails_where_the_browser_asks_for_it(self):
        with Site({"/": self.PLAIN}, icon=500) as site:
            receipt = self.visit(site)
        if receipt["browser_channel"] == "bundled":
            self.assertTrue(receipt["passed"], "the headless shell never asks for a favicon")
        else:
            self.assertFalse(receipt["passed"])
            self.assertTrue(any("status of 500" in error for error in self.failures(receipt)), self.failures(receipt))


if __name__ == "__main__":
    unittest.main()
