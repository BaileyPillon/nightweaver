"""Nightweaver 0.19 browser readiness: channel fallback, the doctor entry and smoke gates for web repositories."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nightweaver import browser, discovery, readiness
from nightweaver.checks import evidence_folder, run_gate
from nightweaver.cli import main
from nightweaver.diagnostics import probe_browser_channels
from nightweaver.engine import git
from nightweaver.spec import gate_spec
from nightweaver.store import Store

MISSING = "BrowserType.launch: Executable doesn't exist at C:\\pw\\chrome-headless-shell.exe\n+---+\n| Please run: playwright install |\n+---+"
NOT_FOUND = "BrowserType.launch: Chromium distribution 'msedge' is not found at C:\\Edge\\msedge.exe\nRun \"playwright install msedge\""
SMOKE = {"name": "Browser smoke", "kind": "browser", "browser": {"page": "index.html"}}
PAGE = ('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="icon" href="data:,"><title>Smoke</title><style>body{margin:0;font:20px sans-serif}</style>'
        "<main><h1>Smoke fixture</h1><p>Hello</p></main></html>")


class FakeDriver:
    """Stands in for sync_playwright(): each channel either launches or raises the scripted error."""

    def __init__(self, **outcomes):
        self.outcomes, self.calls = outcomes, []
        self.chromium = SimpleNamespace(launch=self.launch)

    def launch(self, channel=None, timeout=None):
        self.calls.append(channel)
        outcome = self.outcomes.get(channel or "bundled", "ok")
        if isinstance(outcome, str) and outcome != "ok":
            raise RuntimeError(outcome)
        page = MagicMock()
        page.title.return_value = "Nightweaver preflight"
        return MagicMock(new_page=MagicMock(return_value=page), version=f"fake-{channel or 'bundled'}")


@contextmanager
def fake_playwright(driver):
    api = SimpleNamespace(sync_playwright=lambda: _driver_context(driver))
    with patch.dict(sys.modules, {"playwright": SimpleNamespace(sync_api=api), "playwright.sync_api": api}):
        yield


@contextmanager
def _driver_context(driver):
    yield driver


class FallbackTests(unittest.TestCase):
    def setUp(self):
        browser._FALLBACKS.clear()
        self.addCleanup(browser._FALLBACKS.clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def test_a_pinned_browser_folder_fails_closed_like_an_explicit_channel(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "C:/empty-cache"
        driver = FakeDriver(bundled=MISSING)
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BROWSERS_PATH is set, so no installed Edge or Chrome fallback"):
            browser.launch_browser(driver, None, 5000)
        self.assertEqual(driver.calls, [None])
        browser._FALLBACKS["bundled"] = {"requested": "bundled", "used": "msedge", "reason": "old"}
        driver = FakeDriver(bundled=MISSING)
        with self.assertRaises(RuntimeError):
            browser.launch_browser(driver, None, 5000)
        self.assertEqual(driver.calls, [None], "a cached fallback is ignored while the folder is pinned")

    def test_missing_bundled_chromium_falls_back_to_edge(self):
        driver = FakeDriver(bundled=MISSING)
        _, channel, fallback = browser.launch_browser(driver, None, 5000)
        self.assertEqual(channel, "msedge")
        self.assertEqual(fallback, {"requested": "bundled", "used": "msedge",
                                    "reason": "BrowserType.launch: Executable doesn't exist at C:\\pw\\chrome-headless-shell.exe"})
        self.assertEqual(browser.launch_browser(driver, None, 5000)[1], "msedge")
        self.assertEqual(driver.calls, [None, "msedge", "msedge"], "the resolved channel is cached for this process")

    def test_chrome_is_tried_after_edge_and_total_failure_names_every_attempt(self):
        driver = FakeDriver(bundled=MISSING, msedge=NOT_FOUND)
        _, channel, fallback = browser.launch_browser(driver, None, 5000)
        self.assertEqual((channel, fallback["used"], driver.calls), ("chrome", "chrome", [None, "msedge", "chrome"]))
        browser._FALLBACKS.clear()
        driver = FakeDriver(bundled=MISSING, msedge=NOT_FOUND, chrome="chrome is not found")
        with self.assertRaises(RuntimeError) as caught:
            browser.launch_browser(driver, None, 5000)
        message = str(caught.exception)
        self.assertIn("Executable doesn't exist", message)
        self.assertIn("msedge: BrowserType.launch: Chromium distribution 'msedge' is not found", message)
        self.assertIn("chrome: chrome is not found", message)

    def test_explicit_channel_fails_closed(self):
        for channel in ("msedge", "chrome", "chromium"):
            driver = FakeDriver(**{channel: MISSING})
            with self.assertRaises(RuntimeError):
                browser.launch_browser(driver, channel, 5000)
            self.assertEqual(driver.calls, [channel], "an explicit channel never tries another browser")
        self.assertEqual(browser._FALLBACKS, {})

    def test_only_a_missing_bundled_executable_triggers_fallback(self):
        driver = FakeDriver(bundled="Timeout 5000ms exceeded while launching")
        with self.assertRaises(RuntimeError):
            browser.launch_browser(driver, None, 5000)
        self.assertEqual(driver.calls, [None])

    def test_a_stale_cached_channel_is_discarded(self):
        browser._FALLBACKS["bundled"] = {"requested": "bundled", "used": "msedge", "reason": "old"}
        driver = FakeDriver(bundled=MISSING, msedge="crashed")
        _, channel, _ = browser.launch_browser(driver, None, 5000)
        self.assertEqual((channel, driver.calls), ("chrome", ["msedge", None, "msedge", "chrome"]))

    def test_runtime_reports_the_channel_that_ran(self):
        driver = FakeDriver(bundled=MISSING)
        info = {}
        with fake_playwright(driver), browser.browser_runtime(None, info=info) as instance:
            self.assertEqual(instance.version, "fake-msedge")
        self.assertEqual((info["requested"], info["used"]), ("bundled", "msedge"))
        self.assertEqual(info["fallback"]["used"], "msedge")
        info = {}
        browser._FALLBACKS.clear()
        with fake_playwright(FakeDriver()), browser.browser_runtime(None, info=info):
            pass
        self.assertEqual(info, {"requested": "bundled", "used": "bundled", "fallback": None})

    def test_runtime_with_an_explicit_missing_channel_is_an_environment_error(self):
        with fake_playwright(FakeDriver(msedge=NOT_FOUND)), self.assertRaises(browser.BrowserRuntimeUnavailable) as caught:
            with browser.browser_runtime("msedge"):
                pass
        self.assertIn("Cannot start msedge", str(caught.exception))

    def test_probe_reports_the_channel_preflight_will_use(self):
        resolved = {}
        with fake_playwright(FakeDriver(bundled=MISSING)):
            self.assertEqual(probe_browser_channels(["bundled", "bundled"], resolved), [])
        self.assertEqual(resolved["bundled"]["used"], "msedge")
        self.assertEqual(resolved["bundled"]["fallback"]["requested"], "bundled")
        resolved = {}
        with fake_playwright(FakeDriver(msedge=NOT_FOUND)):
            errors = probe_browser_channels(["msedge", "chrome"], resolved)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("msedge: Cannot start msedge"))
        self.assertEqual(resolved, {"chrome": {"used": "chrome"}})

    def test_probe_still_fails_when_no_browser_can_start(self):
        with fake_playwright(FakeDriver(bundled=MISSING, msedge=NOT_FOUND, chrome=NOT_FOUND)):
            errors = probe_browser_channels(["bundled"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Executable doesn't exist", errors[0])
        self.assertIn("chrome:", errors[0])


class DoctorBrowserTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        package = self.root / "playwright"
        (package / "driver/package").mkdir(parents=True)
        (package / "driver/package/browsers.json").write_text(json.dumps({"browsers": [
            {"name": "chromium", "revision": "1000"}, {"name": "chromium-headless-shell", "revision": "1000"}]}))
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.patches = [
            patch("nightweaver.readiness.find_spec", return_value=SimpleNamespace(submodule_search_locations=[str(package)])),
            patch("nightweaver.readiness.version", return_value="9.9.9"),
            patch("nightweaver.readiness.playwright_cache", return_value=self.cache),
            patch.dict(os.environ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def install(self, revision):
        folder = self.cache / f"chromium_headless_shell-{revision}" / "chrome-headless-shell-win64"
        folder.mkdir(parents=True)
        (folder / "chrome-headless-shell.exe").write_bytes(b"MZ")

    def system(self, **found):
        return patch("nightweaver.readiness.installed_channel", side_effect=lambda channel: found.get(channel))

    def test_bundled_chromium_is_ready(self):
        self.install("1000")
        with self.system(msedge="C:/Edge/msedge.exe"), patch("nightweaver.browser.browser_runtime", side_effect=AssertionError("no launch")):
            report = readiness.browser_readiness()
        self.assertEqual((report["status"], report["channel"]), ("ready", "bundled"))
        self.assertEqual(report["playwright"], {"installed": True, "version": "9.9.9"})
        self.assertTrue(report["bundled"]["present"])
        self.assertEqual(report["channels"], {"msedge": "C:/Edge/msedge.exe", "chrome": None})

    def test_edge_or_chrome_is_a_ready_fallback_when_the_pinned_chromium_is_missing(self):
        self.install("999")
        with self.system(msedge="C:/Edge/msedge.exe", chrome="C:/Chrome/chrome.exe"):
            report = readiness.browser_readiness()
        self.assertEqual((report["status"], report["channel"]), ("ready_fallback", "msedge"))
        self.assertFalse(report["bundled"]["present"])
        self.assertEqual((report["bundled"]["expected_revision"], report["bundled"]["installed_revisions"]), ("1000", ["999"]))
        self.assertIn("python -m playwright install chromium", report["action"])
        self.assertIn("expects revision 1000, found 999", report["action"])
        with self.system(chrome="C:/Chrome/chrome.exe"):
            self.assertEqual(readiness.browser_readiness()["channel"], "chrome")

    def test_a_pinned_browser_folder_disables_the_fallback_in_doctor_too(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.cache)
        with self.system(msedge="C:/Edge/msedge.exe"):
            report = readiness.browser_readiness()
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH is set", report["action"])
        self.assertIn("python -m playwright install chromium", report["action"])
        self.install("1000")
        with self.system(msedge="C:/Edge/msedge.exe"):
            self.assertEqual(readiness.browser_readiness()["status"], "ready")

    def test_no_browser_at_all_is_unavailable_with_a_fix(self):
        with self.system():
            report = readiness.browser_readiness()
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("python -m playwright install chromium", report["action"])
        self.assertEqual(report["playwright"]["installed"], True)

    def test_missing_playwright_is_optional_and_never_raises(self):
        with patch("nightweaver.readiness.find_spec", return_value=None), self.system(msedge="C:/Edge/msedge.exe"):
            report = readiness.browser_readiness()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["playwright"], {"installed": False})
        self.assertTrue(report["optional"])
        self.assertIn("pip install playwright", report["action"])
        with patch("nightweaver.readiness.find_spec", side_effect=ValueError("broken")):
            self.assertEqual(readiness.browser_readiness()["status"], "unavailable")

    def test_doctor_adds_the_browser_field_and_keeps_its_shape_and_exit_code(self):
        store = Store(self.root / "home")
        with patch("nightweaver.readiness.browser_readiness", return_value={"status": "unavailable", "optional": True}):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--home", str(store.root), "doctor"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(report), {"data_directory", "executables", "browser"})
        self.assertEqual(set(report["executables"]), {"git", "claude", "codex", "gemini", "ollama"})
        self.assertEqual(report["browser"]["status"], "unavailable")


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


class SmokeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="nw smoke ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.count = 0

    def discover(self, files, objective="Build the page"):
        self.count += 1
        return discovery.discover(repository(self.root / f"repo{self.count}", files), objective, {"kind": "codex"})

    def test_root_index_without_tests_gets_a_smoke_gate_with_unchanged_budgets(self):
        plan = self.discover({"index.html": PAGE, "style.css": "body{}", "README.md": "# Site"})
        self.assertEqual(plan["gates"], [SMOKE])
        self.assertEqual(plan["protected_paths"], [], "the worker must be free to edit the page")
        self.assertEqual(plan["budgets"], {"max_calls": 15, "max_seconds": 3600})
        self.assertIn("index.html exists", plan["acceptance"][0])
        self.assertIn("390x844", plan["acceptance"][0])
        gate = gate_spec(plan["gates"][0])
        self.assertEqual(gate["kind"], "browser")
        self.assertEqual([(v["name"], v["width"], v["height"]) for v in gate["browser"]["viewports"]],
                         [("desktop", 1440, 900), ("mobile", 390, 844)])
        self.assertEqual(gate["browser"]["page"], "index.html")

    def test_public_and_docs_pages_are_accepted_in_priority_order(self):
        self.assertEqual(self.discover({"public/index.html": PAGE, "docs/index.html": PAGE})["gates"][0]["browser"],
                         {"page": "public/index.html"})
        self.assertEqual(self.discover({"docs/index.html": PAGE})["gates"][0]["browser"], {"page": "docs/index.html"})
        self.assertEqual(self.discover({"index.html": PAGE, "public/index.html": PAGE})["gates"][0]["browser"],
                         {"page": "index.html"})

    def test_a_new_repository_targets_the_page_the_worker_will_create(self):
        for files in ({"README.md": "# New", "LICENSE": "MIT", ".gitignore": "node_modules/\n"}, {".gitkeep": ""}, {}):
            plan = self.discover(files, "Create a landing page for a bakery")
            self.assertEqual(plan["gates"], [SMOKE], files)
            self.assertIn("index.html exists", plan["acceptance"][0])

    def test_non_web_repositories_keep_the_existing_failure(self):
        message = "No supported committed test suite found. Use a Nightweaver template with an explicit check."
        for files in ({"main.py": "print('hi')", "README.md": "# Tool"}, {"src/app.cs": "class A {}"}, {"result.txt": "x"},
                      {"package.json": json.dumps({"name": "lib", "scripts": {"build": "tsc"}}), "src/lib.ts": ""}):
            with self.assertRaises(ValueError, msg=files) as caught:
                self.discover(files)
            self.assertEqual(str(caught.exception), message)

    def test_a_served_web_project_gets_a_clear_message_instead_of_a_bare_file_server(self):
        files = {"package.json": json.dumps({"scripts": {"dev": "vite"}}), "index.html": PAGE, "src/main.js": ""}
        with self.assertRaises(ValueError) as caught:
            self.discover(files)
        message = str(caught.exception)
        self.assertTrue(message.startswith("No supported committed test suite found."))
        self.assertIn("browser check that owns the server", message)
        self.assertIn('"kind": "browser"', message)
        self.assertIn("{port}", message)

    def test_existing_suites_are_not_supplemented_with_a_smoke_gate(self):
        plan = self.discover({"index.html": PAGE, "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n def test_a(self): pass"})
        self.assertEqual([g["name"] for g in plan["gates"]], ["Python unittest tests"])
        self.assertNotIn("acceptance", plan)

    def test_placeholder_npm_script_stays_an_error_unless_a_static_page_is_the_only_check(self):
        placeholder = json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
        self.assertEqual(self.discover({"package.json": placeholder, "index.html": PAGE})["gates"], [SMOKE])
        for files in ({"package.json": placeholder, "main.py": "pass"},
                      {"package.json": placeholder, "index.html": PAGE, "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n def test_a(self): pass"}):
            with self.assertRaisesRegex(ValueError, "placeholder test script"):
                self.discover(files)

    def test_verify_tools_requires_a_usable_browser_but_not_an_argv(self):
        with patch("nightweaver.readiness.browser_readiness", return_value={"status": "ready_fallback", "action": ""}):
            discovery.verify_tools([SMOKE])
        with patch("nightweaver.readiness.browser_readiness", return_value={"status": "unavailable", "action": "Install a browser."}):
            with self.assertRaisesRegex(ValueError, "Browser smoke needs a browser.*Install a browser"):
                discovery.verify_tools([SMOKE])

    def test_oneshot_preview_prints_the_smoke_plan_through_the_normal_cli(self):
        repo = repository(self.root / "preview", {"index.html": PAGE})
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--home", str(self.root / "home"), "oneshot", str(repo), "Polish the page", "--preview"])
        plan = json.loads(output.getvalue())
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(plan["gates"], [SMOKE])
        self.assertEqual(plan["expected_base"], git(repo, "rev-parse", "HEAD"))


def browser_ready():
    if not importlib.util.find_spec("playwright"):
        return False
    return not probe_browser_channels(["bundled"])


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Install the browser extra")
class SmokeGateRunTests(unittest.TestCase):
    """Run the discovered gate through the normal check path against a real browser."""

    @classmethod
    def setUpClass(cls):
        if not browser_ready():
            raise unittest.SkipTest("No browser channel can launch on this machine")

    def run_smoke(self, files):
        folder = tempfile.TemporaryDirectory(prefix="nw smoke run ")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        repo = repository(root / "repo", files)
        plan = discovery.discover(repo, "Build the page", {"kind": "codex"})
        gate = gate_spec(plan["gates"][0])
        return run_gate(gate, repo, evidence_folder(root / "evidence", 1), git(repo, "rev-parse", "HEAD^{tree}"))

    def test_a_valid_page_passes_in_both_viewports_with_screenshots_and_the_channel_that_ran(self):
        receipt = self.run_smoke({"index.html": PAGE})
        self.assertEqual(receipt["exit"], 0, receipt)
        self.assertIsNone(receipt.get("artifact_error"))
        self.assertEqual([(c["viewport"], c["passed"]) for c in receipt["browser_coverage"]], [("desktop", True), ("mobile", True)])
        names = {a["name"] for a in receipt["artifacts"]}
        self.assertLessEqual({"desktop.png", "mobile.png", "browser.json"}, names)
        for name in ("desktop.png", "mobile.png"):
            path = Path(next(a["path"] for a in receipt["artifacts"] if a["name"] == name))
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        runtime = receipt["browser_runtime"]
        self.assertIn(runtime["channel"], ("bundled", "msedge", "chrome"))
        report = json.loads(Path(receipt["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(report["browser_channel"], runtime["channel"])
        if runtime["channel"] == "bundled":
            self.assertNotIn("browser_channel_fallback", report)
        else:
            fallback = report["browser_channel_fallback"]
            self.assertEqual((fallback["requested"], fallback["used"]), ("bundled", runtime["channel"]))
            self.assertIn("Executable doesn't exist", fallback["reason"])
            self.assertEqual(runtime["browser_channel_fallback"], fallback)

    def test_horizontal_overflow_fails_the_smoke_gate_with_a_repairable_diagnosis(self):
        wide = PAGE.replace("<h1>", '<div style="width:1600px">wide</div><h1>')
        receipt = self.run_smoke({"index.html": wide})
        self.assertEqual(receipt["exit"], 1)
        self.assertTrue(any("Horizontal overflow" in f["error"] for f in receipt["browser_failures"]), receipt["browser_failures"])

    def test_a_new_repository_fails_clearly_until_the_worker_creates_the_page(self):
        receipt = self.run_smoke({"README.md": "# New"})
        self.assertEqual(receipt["exit"], 1)
        self.assertEqual(receipt["browser_failures"][0]["stage"], {"action": "startup"})
        self.assertIn("Expected a page inside the candidate checkout: index.html", receipt["browser_failures"][0]["error"])


if __name__ == "__main__":
    unittest.main()
