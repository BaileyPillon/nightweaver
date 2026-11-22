"""nightweaver wish: a visual brief on a repository with committed tests still gives the judge and the reviewer screenshots."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_release_019_fix_browser import PAGE, UNITTEST, repository

from nightweaver import discovery
from nightweaver import wish as wishing
from nightweaver.checks import evidence_folder, run_gate
from nightweaver.cli import main
from nightweaver.diagnostics import probe_browser_channels
from nightweaver.engine import git
from nightweaver.spec import gate_spec, validate
from nightweaver.store import Store

BRIEF = {"title": "Calm the page", "objective": "Calm the page.", "visual": True, "conflicts": [],
         "acceptance": ["styles.css defines --bg at :root", "The header keeps its links", "At 390x844 the grid is one column",
                        "A screenshot at 1440x900 shows the hero above the fold"]}
NODE = {"package.json": '{"scripts": {"test": "node --test"}}', "web/site.test.js": "import test from 'node:test';\ntest('a', () => {});\n"}
TESTED = {"tests/test_site.py": UNITTEST, "README.md": "A site\n", "dist/index.html": PAGE}
ASSETS = PAGE.replace("<main>", '<link rel="stylesheet" href="/site.css"><script src="/app.js"></script><main>')
SITE = {"tests/test_site.py": UNITTEST, "dist/index.html": ASSETS, "dist/site.css": "h1{color:#036}", "dist/app.js": "document.title = 'ready'"}
UNREADY = {"status": "unavailable", "action": "Install a browser."}


def browser_ready():
    return bool(importlib.util.find_spec("playwright")) and not probe_browser_channels(["bundled"])


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="nw wish visual ")
        self.addCleanup(folder.cleanup)
        self.root, self.count, self.runs, self.shown = Path(folder.name), 0, 0, []
        for patcher in (patch.dict(os.environ), patch.object(wishing, "ollama_advisory", return_value=(None, None)),
                        patch.dict(wishing.ROUTE_OVERRIDES, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def repo(self, files):
        self.count += 1
        return repository(self.root / f"repo{self.count}", files)

    def wish(self, repo, visual=True, *flags, acceptance=None):
        """Run `wish --preview` with a scripted brief writer. Returns (exit code, stdout, stderr, saved template or None)."""
        def write(config, plan, text, folder):
            self.shown.append([g["name"] for g in plan["gates"]])
            return {**BRIEF, "visual": visual, **({"acceptance": acceptance} if acceptance else {})}, folder / "brief-writer-1.jsonl", 1

        self.runs += 1
        self.home = home = self.root / f"home{self.runs}"
        out, err = io.StringIO(), io.StringIO()
        with patch.object(wishing, "write_brief", write), redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(home), "wish", str(repo), "Calm the page", *([] if "--start" in flags else ["--preview"]), *flags])
        saved = sorted((home / "wishes").glob("*/template.json")) if (home / "wishes").is_dir() else []
        return code, out.getvalue(), err.getvalue(), json.loads(saved[0].read_text(encoding="utf-8")) if saved else None

    def discovered(self, repo):
        return discovery.discover(repo, "Calm the page", {"kind": "codex"})["gates"]


@unittest.skipUnless(os.name == "nt", "Windows controller")
class VisualGateTests(Case):
    def test_tested_repository_keeps_every_test_gate_and_gains_the_visual_smoke_gate_on_its_dist_page(self):
        repo = self.repo({**TESTED, **NODE})
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        found = self.discovered(repo)
        self.assertEqual([g["name"] for g in found], ["Python unittest tests", "Node tests"])
        self.assertEqual(template["gates"][:-1], found, "the discovered gates are untouched and come first")
        self.assertEqual(template["gates"][-1], {"name": "Visual smoke", "kind": "browser",
                                                 "browser": {"page": "dist/index.html", "root": "dist", "sweep": {}}})
        self.assertEqual(self.shown, [["Python unittest tests", "Node tests"]], "the brief writer is shown the operator's checks only")
        validate(template)
        self.assertEqual(gate_spec(template["gates"][-1])["browser"]["root"], "dist")
        self.assertEqual([item for item in template["acceptance"] if "loads over HTTP" in item],
                         ["dist/index.html loads over HTTP without console or page errors and has no horizontal overflow at 1440x900 "
                          "(desktop) and 390x844 (phone)."])
        self.assertEqual(template["acceptance"][:4], BRIEF["acceptance"])
        self.assertEqual(template["pursue"]["criteria"], template["acceptance"])
        self.assertIn("explore", template["pursue"])
        self.assertIn("Checks: Python unittest tests; Node tests + Visual smoke (browser, page dist/index.html, desktop and phone viewports)", out)
        self.assertIn("Note: The discovered checks have no browser gate, so Visual smoke was added beside them: dist/index.html at desktop "
                      "and phone sizes, served with dist/ as the web root, so the judge and the milestone reviewer get screenshots.", out)

    def test_the_first_tracked_entry_page_wins_and_its_folder_is_the_root(self):
        order = wishing.ENTRY_PAGES
        self.assertEqual(order, ("index.html", "public/index.html", "docs/index.html", "dist/index.html", "build/index.html",
                                 "site/index.html", "www/index.html"))

        def added(files):
            repo = self.repo({"tests/test_site.py": UNITTEST, **files})
            plan = {"repository": str(repo), "expected_base": git(repo, "rev-parse", "HEAD"), "gates": [{"name": "Tests", "argv": ["x"]}]}
            return wishing.with_visual_gate(plan, BRIEF, [])[1]

        for number, page in enumerate(order):
            with self.subTest(page=page):
                folder = page.rpartition("/")[0]
                gate = added({page: PAGE, **{p: PAGE for p in order[number + 1:]}})
                self.assertEqual(gate["browser"], {"page": page, **({"root": folder} if folder else {}), "sweep": {}})
        self.assertEqual(added({p: PAGE for p in order})["browser"], {"page": "index.html", "sweep": {}}, "a root page needs no served root")
        self.assertEqual(added({"docs/index.html": PAGE, "dist/index.html": PAGE})["browser"]["page"], "docs/index.html")

    def test_a_non_visual_brief_adds_no_browser_gate(self):
        repo = self.repo({**TESTED, **NODE})
        code, out, err, template = self.wish(repo, False)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(template["gates"], self.discovered(repo))
        self.assertFalse([g for g in template["gates"] if g.get("kind") == "browser"])
        self.assertNotIn("Visual smoke", out)
        self.assertNotIn("Note:", out)
        self.assertFalse([item for item in template["acceptance"] if "loads over HTTP" in item])

    def test_a_discovered_browser_gate_means_nothing_is_added(self):
        repo = self.repo({"index.html": PAGE, "README.md": "A site\n"})
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual([g["name"] for g in template["gates"]], ["Browser smoke"])
        self.assertEqual(len(template["acceptance"]), len(BRIEF["acceptance"]) + 1, "only the discovery's own line is added")
        self.assertNotIn("Visual smoke", out)
        self.assertNotIn("Note:", out)

    def test_any_browser_gate_among_the_discovered_ones_counts(self):
        custom = {"name": "Site check", "kind": "browser", "browser": {"server": {"argv": ["python", "serve.py", "{port}"]}}}
        plan = {**discovery.discover(self.repo(TESTED), "Calm the page", {"kind": "codex"})}
        plan["gates"] = [*plan["gates"], custom]
        notes = []
        self.assertEqual(wishing.with_visual_gate(plan, BRIEF, notes), (plan, None))
        self.assertEqual(notes, [])

    def test_no_committed_entry_page_adds_nothing_and_says_the_judge_will_have_no_screenshots(self):
        repo = self.repo({"tests/test_site.py": UNITTEST, "README.md": "A site\n", "src/app.js": "x"})
        (repo / "dist").mkdir()
        (repo / "dist" / "index.html").write_text(PAGE, encoding="utf-8")  # on disk but never committed
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(template["gates"], self.discovered(repo))
        self.assertNotIn("Visual smoke (browser", out)
        text = " ".join(out.split())
        self.assertIn("Note: Visual goal, but no committed index.html, public/, docs/, dist/, build/, site/ or www/ entry page was found, so no "
                      "browser gate was added: the judge and the milestone reviewer will have no screenshots.", text)
        self.assertIn("template with a browser check that owns the server", text)
        saved = json.loads(next((self.home / "wishes").glob("*/brief.json")).read_text(encoding="utf-8"))
        self.assertTrue(any("no screenshots" in note for note in saved["notes"]))
        self.assertFalse([item for item in template["acceptance"] if "loads over HTTP" in item])

    def test_a_project_served_by_a_package_script_gets_no_bare_file_server_gate(self):
        repo = self.repo({"tests/test_site.py": UNITTEST, "index.html": PAGE, "package.json": '{"scripts": {"dev": "vite"}}'})
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(template["gates"], self.discovered(repo))
        self.assertIn("a package.json script (dev, start, preview or serve) serves this project", " ".join(out.split()))
        self.assertIn("will have no screenshots", " ".join(out.split()))

    def test_the_fast_roster_gets_the_gate_too_and_still_validates(self):
        repo = self.repo(TESTED)
        code, out, err, template = self.wish(repo, True, "--roster", "fast")
        self.assertEqual(code, 0, err + out)
        self.assertEqual([g["name"] for g in template["gates"]], ["Python unittest tests", "Visual smoke"])
        self.assertEqual(template["gates"][-1]["browser"], {"page": "dist/index.html", "root": "dist", "sweep": {}})
        self.assertNotIn("reviewer", template)
        self.assertEqual(template["pursue"]["judge"], {"kind": "claude"})
        validate(template)
        self.assertIn("reviewer  none", out)
        self.assertIn("+ Visual smoke (browser, page dist/index.html", out)

    def test_the_loads_cleanly_line_is_added_only_when_the_brief_does_not_already_say_it(self):
        repo = self.repo(TESTED)
        said = ["styles.css defines --bg at :root", "The header keeps its links",
                "dist/index.html loads without console errors and with no horizontal overflow at 1440x900 and 390x844",
                "The hero shows above the fold"]
        template = self.wish(repo, True, acceptance=said)[3]
        self.assertEqual(template["acceptance"], said)
        self.assertEqual(template["gates"][-1]["name"], "Visual smoke")
        for partial in (["At 390x844 the grid is one column without horizontal scrolling", "The hero shows at 1440x900", "a", "b"],
                        ["Loads without console errors", "At the desktop size the hero fits", "a", "b"]):
            self.assertEqual(len(self.wish(repo, True, acceptance=partial)[3]["acceptance"]), len(partial) + 1, partial)

    def test_start_without_a_browser_refuses_before_anything_is_saved(self):
        repo = self.repo(TESTED)
        with patch("nightweaver.readiness.browser_readiness", return_value=UNREADY):
            code, out, err, template = self.wish(repo, True, "--start")
        self.assertEqual(code, 1, out)
        self.assertIn("Visual smoke needs a browser available before starting this OneShot. Install a browser.", err)
        self.assertIn("nothing was saved or registered", err)
        self.assertEqual(self.shown, [["Python unittest tests"]], "the brief writer ran once")
        self.assertEqual(list((self.home / "wishes").glob("*")), [], "no folder is left behind")
        self.assertEqual(Store(self.home).runs(), [])
        with patch("nightweaver.readiness.browser_readiness", return_value=UNREADY):  # a preview never needs the browser
            self.assertEqual(self.wish(repo)[0], 0)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Install the browser extra")
class VisualGateRunTests(Case):
    @classmethod
    def setUpClass(cls):
        if not browser_ready():
            raise unittest.SkipTest("No browser channel (bundled Chromium, Edge or Chrome) can launch on this machine")

    def setUp(self):
        super().setUp()
        from nightweaver import browser
        browser._FALLBACKS.clear()
        self.addCleanup(browser._FALLBACKS.clear)

    def run_added(self, repo, mutate=None):
        template = self.wish(repo)[3]
        gate = template["gates"][-1]
        self.assertEqual(gate["name"], "Visual smoke")
        if mutate:
            mutate(gate)
        self.count += 1
        return run_gate(gate_spec(gate), repo, evidence_folder(self.root / f"evidence{self.count}", 1), git(repo, "rev-parse", "HEAD^{tree}"))

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_the_added_gate_passes_on_a_dist_site_with_root_absolute_assets(self):
        receipt = self.run_added(self.repo(SITE))
        self.assertEqual(receipt["exit"], 0, receipt.get("browser_failures"))

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_the_root_is_what_makes_it_pass_and_a_missing_asset_still_fails(self):
        unrooted = self.run_added(self.repo(SITE), lambda gate: gate["browser"].pop("root"))
        self.assertEqual(unrooted["exit"], 1, "served from the repository root the /site.css and /app.js requests would 404")
        self.assertTrue(any("404" in failure["error"] for failure in unrooted["browser_failures"]), unrooted["browser_failures"])
        missing = self.run_added(self.repo({name: data for name, data in SITE.items() if name != "dist/app.js"}))
        self.assertEqual(missing["exit"], 1)
        self.assertTrue(any("404" in failure["error"] for failure in missing["browser_failures"]), missing["browser_failures"])


if __name__ == "__main__":
    unittest.main()
