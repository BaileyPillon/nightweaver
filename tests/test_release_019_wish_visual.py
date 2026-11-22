"""nightweaver wish: a visual brief on a repository with committed tests still gives the judge and the reviewer screenshots, but only
from a page that is the site the wish changes, that a bare file server can show, and that passes on the committed baseline."""

import importlib.util
import io
import json
import os
import re
import subprocess
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
READY = {"status": "ready", "channel": "bundled"}
STUB = '<!doctype html><meta http-equiv="refresh" content="0; url=docs/"><title>Moved</title>'
GENERATED = PAGE.replace("<title>", '<meta name="generator" content="Docutils 0.21"><title>')
TKINTER = "import tkinter\nroot = tkinter.Tk()\nroot.mainloop()\n"
LOADS = "dist/index.html loads over HTTP without console or page errors and has no horizontal overflow at 1440x900 (desktop) and 390x844 (phone)."
REAL_PROBE = wishing.probe_visual_gate
OVERFLOW = PAGE.replace("<main>", '<main style="width:2200px">')


def browser_ready():
    return bool(importlib.util.find_spec("playwright")) and not probe_browser_channels(["bundled"])


class Case(unittest.TestCase):
    real = False  # Run the real baseline probe and the real browser check instead of scripted ones

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="nw wish visual ")
        self.addCleanup(folder.cleanup)
        self.root, self.count, self.runs, self.shown, self.probed = Path(folder.name), 0, 0, [], []
        self.outcome = ("pass", "")
        patchers = [patch.dict(os.environ), patch.object(wishing, "ollama_advisory", return_value=(None, None)),
                    patch.dict(wishing.ROUTE_OVERRIDES, clear=False)]
        if not self.real:
            patchers += [patch("nightweaver.readiness.browser_readiness", return_value=READY),
                         patch.object(wishing, "probe_visual_gate", side_effect=self.probe)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    def probe(self, plan, gate, folder):
        self.probed.append(gate["browser"]["page"])
        return self.outcome

    def repo(self, files):
        self.count += 1
        return repository(self.root / f"repo{self.count}", files)

    def wish(self, repo, visual=True, *flags, acceptance=None, **extra):
        """Run `wish --preview` with a scripted brief writer. Returns (exit code, stdout, stderr, saved template or None)."""
        def write(config, plan, text, folder):
            self.shown.append([g["name"] for g in plan["gates"]])
            return {**BRIEF, "visual": visual, **({"acceptance": acceptance} if acceptance else {}), **extra}, folder / "brief-writer-1.jsonl", 1

        self.runs += 1
        self.home = home = self.root / f"home{self.runs}"
        out, err = io.StringIO(), io.StringIO()
        with patch.object(wishing, "write_brief", write), redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(home), "wish", str(repo), "Calm the page", *([] if "--start" in flags else ["--preview"]), *flags])
        saved = sorted((home / "wishes").glob("*/template.json")) if (home / "wishes").is_dir() else []
        return code, out.getvalue(), err.getvalue(), json.loads(saved[0].read_text(encoding="utf-8")) if saved else None

    def notes(self):
        return json.loads(next((self.home / "wishes").glob("*/brief.json")).read_text(encoding="utf-8"))["notes"]

    def discovered(self, repo):
        return discovery.discover(repo, "Calm the page", {"kind": "codex"})["gates"]

    def entry(self, files, **brief):
        """(page, why not) the entry-page choice for a tested repository holding these files, with no browser involved."""
        repo = self.repo({"tests/test_site.py": UNITTEST, **files})
        plan = {"repository": str(repo), "expected_base": git(repo, "rev-parse", "HEAD"), "gates": [{"name": "Tests", "argv": ["x"]}]}
        return wishing.entry_page(plan, {**BRIEF, **brief})


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
        self.assertEqual(self.probed, ["dist/index.html"], "the gate was run once on the committed baseline before it was added")
        validate(template)
        self.assertEqual(gate_spec(template["gates"][-1])["browser"]["root"], "dist")
        self.assertEqual([item for item in template["acceptance"] if "loads over HTTP" in item], [LOADS])
        self.assertEqual(template["acceptance"][:4], BRIEF["acceptance"])
        self.assertEqual(template["pursue"]["criteria"], template["acceptance"])
        self.assertIn("explore", template["pursue"])
        self.assertIn("Checks: Python unittest tests; Node tests + Visual smoke (browser, page dist/index.html, desktop and phone viewports)", out)
        self.assertIn("Note: The discovered checks have no browser gate, so Visual smoke was added beside them: dist/index.html at desktop "
                      "and phone sizes, served with dist/ as the web root, so the judge and the milestone reviewer get screenshots. "
                      "It passes on the committed baseline.", out)

    def test_the_first_tracked_entry_page_wins_and_its_folder_is_the_root(self):
        order = wishing.ENTRY_PAGES
        self.assertEqual(order, ("index.html", "public/index.html", "docs/index.html", "dist/index.html", "build/index.html",
                                 "site/index.html", "www/index.html"))

        def added(files):
            repo = self.repo({"tests/test_site.py": UNITTEST, **files})
            plan = {"repository": str(repo), "expected_base": git(repo, "rev-parse", "HEAD"), "gates": [{"name": "Tests", "argv": ["x"]}]}
            return wishing.with_visual_gate(plan, BRIEF, [], self.root)[1]

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
        self.assertEqual(self.probed, [])

    def test_a_discovered_browser_gate_means_nothing_is_added(self):
        repo = self.repo({"index.html": PAGE, "README.md": "A site\n"})
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual([g["name"] for g in template["gates"]], ["Browser smoke"])
        self.assertEqual(len(template["acceptance"]), len(BRIEF["acceptance"]) + 1, "only the discovery's own line is added")
        self.assertNotIn("Visual smoke", out)
        self.assertNotIn("Note:", out)
        self.assertEqual(self.probed, [])

    def test_any_browser_gate_among_the_discovered_ones_counts(self):
        custom = {"name": "Site check", "kind": "browser", "browser": {"server": {"argv": ["python", "serve.py", "{port}"]}}}
        plan = {**discovery.discover(self.repo(TESTED), "Calm the page", {"kind": "codex"})}
        plan["gates"] = [*plan["gates"], custom]
        notes = []
        self.assertEqual(wishing.with_visual_gate(plan, BRIEF, notes, self.root), (plan, None))
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
        self.assertTrue(any("no screenshots" in note for note in self.notes()))
        self.assertFalse([item for item in template["acceptance"] if "loads over HTTP" in item])
        self.assertEqual(self.probed, [])

    def test_a_project_served_by_a_package_script_gets_no_bare_file_server_gate(self):
        repo = self.repo({"tests/test_site.py": UNITTEST, "index.html": PAGE, "package.json": '{"scripts": {"dev": "vite"}}'})
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(template["gates"], self.discovered(repo))
        self.assertIn("a package.json script (dev, start, preview or serve) serves this project", " ".join(out.split()))
        self.assertIn("will have no screenshots", " ".join(out.split()))
        self.assertEqual(self.probed, [])

    def test_the_fast_roster_gets_the_gate_too_and_the_note_names_only_the_judge(self):
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
        text = " ".join(out.split())
        self.assertIn("so the judge gets screenshots.", text)
        self.assertNotIn("milestone reviewer", text)
        self.assertFalse([note for note in self.notes() if "milestone reviewer" in note], "the saved note says the same")
        bare = self.repo({"tests/test_site.py": UNITTEST, "README.md": "A site\n"})
        text = " ".join(self.wish(bare, True, "--roster", "fast")[1].split())
        self.assertIn("so no browser gate was added: the judge will have no screenshots.", text)
        self.assertNotIn("milestone reviewer", text)

    def test_the_loads_cleanly_line_is_added_only_when_the_brief_does_not_already_say_it(self):
        repo = self.repo(TESTED)
        said = ["styles.css defines --bg at :root", "The header keeps its links",
                "dist/index.html loads without console errors and with no horizontal overflow at 1440x900 and 390x844",
                "The hero shows above the fold"]
        template = self.wish(repo, True, acceptance=said)[3]
        self.assertEqual(template["acceptance"], said)
        self.assertEqual(template["gates"][-1]["name"], "Visual smoke")
        for other in (["The page never scrolls horizontally on phone or desktop and shows no console errors", "a", "b", "c"],
                      ["Desktop and mobile show no page errors and no horizontal scrolling", "a", "b", "c"]):
            self.assertEqual(self.wish(repo, True, acceptance=other)[3]["acceptance"], other, other)
        for partial in (["At 390x844 the grid is one column without horizontal scrolling", "The hero shows at 1440x900", "a", "b"],
                        ["Loads without console errors", "At the desktop size the hero fits", "a", "b"],
                        ["The page on desktop and mobile has overflow:hidden on the carousel", "a2", "a3", "a4"],
                        ["The nav collapses into an overflow menu on phone and lists every link on desktop", "c", "d", "e"],
                        ["Desktop and mobile show the console panel and an overflow list", "c", "d", "e"],
                        ["Mobile has no vertical scroll but desktop gets a horizontal scroll strip, and the console errors list is shown", "c", "d", "e"]):
            self.assertEqual(self.wish(repo, True, acceptance=partial)[3]["acceptance"], [*partial, LOADS], partial)

    def test_start_without_a_browser_goes_on_with_a_note_like_the_no_page_case(self):
        repo = self.repo({**TESTED, **NODE})
        with patch("nightweaver.readiness.browser_readiness", return_value=UNREADY):
            code, out, err, template = self.wish(repo)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(template["gates"], self.discovered(repo), "no gate: nothing could run it here")
            text = " ".join(out.split())
            self.assertIn("Note: Visual goal, but no browser is available on this machine, so no browser gate was added: the judge and the "
                          "milestone reviewer will have no screenshots. Install a browser. Then run the wish again.", text)
            self.assertEqual(self.probed, [])
            with patch("nightweaver.cli.plan_goal", return_value={"run_id": "run-1"}) as planned:
                code, out, err, template = self.wish(repo, True, "--start")
        self.assertEqual(code, 0, err + out)
        planned.assert_called_once()
        self.assertEqual(planned.call_args.args[2]["gates"], self.discovered(repo))
        self.assertIn("Run id: run-1", out)
        self.assertEqual(self.shown, [["Python unittest tests", "Node tests"]] * 2, "the same brief-writer calls as before")

    def test_a_gate_that_fails_on_the_untouched_baseline_is_not_added_and_says_why(self):
        repo = self.repo(TESTED)
        self.outcome = ("fail", "desktop: Failed to load resource: the server responded with a status of 404 (File not found)")
        code, out, err, template = self.wish(repo)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(template["gates"], self.discovered(repo))
        text = " ".join(out.split())
        self.assertIn("Note: Visual goal, but the Visual smoke check on dist/index.html fails on the untouched baseline under a bare file "
                      "server (desktop: Failed to load resource: the server responded with a status of 404 (File not found)), so no "
                      "browser gate was added: the judge and the milestone reviewer will have no screenshots.", text)
        self.assertIn("template with a browser check that owns the server", text)
        self.assertFalse([item for item in template["acceptance"] if "loads over HTTP" in item])
        self.outcome = ("overflow", "desktop 2200px, phone 2200px")
        code, out, err, template = self.wish(repo)
        self.assertEqual(template["gates"][-1]["name"], "Visual smoke", "overflow is an edit away, so the gate stays and the worker is told")
        self.assertIn("On the committed baseline it fails only on horizontal overflow (desktop 2200px, phone 2200px), which the worker is "
                      "asked to fix.", " ".join(out.split()))

    def test_a_redirect_stub_is_skipped_for_the_page_it_points_to(self):
        stub = {"index.html": STUB, "docs/index.html": PAGE}
        self.assertEqual(self.entry(stub), ("docs/index.html", None))
        for tiny in ('<script>window.location.replace("docs/")</script>', '<script>location.href = "docs/index.html"</script>',
                     '<meta content="3;url=docs/" http-equiv="Refresh">'):
            self.assertEqual(self.entry({"index.html": tiny, "docs/index.html": PAGE}), ("docs/index.html", None), tiny)
        page, why = self.entry({"index.html": STUB})
        self.assertEqual((page, why), (None, "no usable entry page: index.html only redirects to another page"))
        # A real page that merely mentions location, or reloads itself without a target, is still a page.
        busy = PAGE.replace("</main>", "</main><script>" + "if (location.hash == '#x') go(location.href = '/y');" + "x".ljust(2200, "x") + "</script>")
        self.assertEqual(self.entry({"index.html": busy}), ("index.html", None))
        self.assertEqual(self.entry({"index.html": PAGE.replace("<title>", '<meta http-equiv="refresh" content="300"><title>')}),
                         ("index.html", None))

    def test_generated_and_template_pages_are_skipped(self):
        for name, page in (("generator meta", GENERATED), ("sphinx footer", PAGE.replace("</main>", "Built with Sphinx using a theme.</main>")),
                           ("doxygen", PAGE.replace("<main>", "<!-- Generated by Doxygen 1.9 --><main>")), ("front matter", "---\nlayout: home\n---\n" + PAGE)):
            with self.subTest(name):
                self.assertEqual(self.entry({"docs/index.html": page, "dist/index.html": PAGE})[0], "dist/index.html", "the next tracked page is used")
                self.assertIsNone(self.entry({"docs/index.html": page})[0])
        self.assertEqual(self.entry({"docs/index.html": GENERATED}),
                         (None, "no usable entry page: docs/index.html is template source or generated output that nothing in the run rebuilds"))

    def test_build_output_is_no_entry_page_when_the_project_builds_it(self):
        site = {"dist/index.html": PAGE, "src/page.template.html": "<main></main>"}
        for scripts in ('{"build": "node build.js", "test": "node --test"}', '{"build:css": "sass in out"}'):
            page, why = self.entry({**site, "package.json": '{"scripts": %s}' % scripts})
            self.assertIsNone(page, scripts)
            self.assertIn("dist/index.html is build output (package.json has a build script) and nothing in the run rebuilds it", why)
        self.assertEqual(self.entry({"build/index.html": PAGE, "package.json": '{"scripts": {"build": "x"}}'})[0], None)
        # Another page is still used, and a project that does not build (or has an empty build script) keeps its dist/ page.
        self.assertEqual(self.entry({**site, "public/index.html": PAGE, "package.json": '{"scripts": {"build": "x"}}'}), ("public/index.html", None))
        self.assertEqual(self.entry({**site, "package.json": '{"scripts": {"test": "node --test", "build": " "}}'}), ("dist/index.html", None))
        self.assertEqual(self.entry({**site, "package.json": '{"scripts": {"test": "node --test"}}'}), ("dist/index.html", None))
        self.assertEqual(self.entry(site), ("dist/index.html", None))

    def test_a_package_json_with_a_byte_order_mark_is_still_read(self):
        bom = chr(0xFEFF)
        page, why = self.entry({"index.html": PAGE, "package.json": bom + '{"scripts": {"dev": "vite"}}'})
        self.assertIsNone(page)
        self.assertIn("a package.json script (dev, start, preview or serve) serves this project", why)
        self.assertIsNone(self.entry({"dist/index.html": PAGE, "package.json": bom + '{"scripts": {"build": "x"}}'})[0])

    def test_a_visual_wish_about_a_desktop_or_game_app_is_not_given_the_docs_page(self):
        app = {"app.py": TKINTER, "docs/index.html": PAGE.replace("<title>Fix", "<title>User guide")}
        page, why = self.entry(app)
        self.assertIsNone(page)
        self.assertIn("imports a desktop or game toolkit and the brief does not say a committed page is what the wish changes", why)
        for toolkit in ("import pygame\n", "from PyQt5 import QtWidgets\n", "  import curses\n", "from kivy.app import App\n"):
            self.assertIsNone(self.entry({"game/main.py": toolkit, "docs/index.html": PAGE})[0], toolkit)
        self.assertEqual(self.entry({"tools/pygame_utils.py": "import pygame_gui_helpers_not_a_toolkit\nx = 'import tkinter'\n", "docs/index.html": PAGE}),
                         ("docs/index.html", None), "a word in a string or a longer module name is not an import")
        self.assertEqual(self.entry(app, web=True), ("docs/index.html", None), "unless the brief says the page is what the wish changes")
        repo = self.repo({"tests/test_site.py": UNITTEST, **app})
        self.assertEqual(self.wish(repo)[3]["gates"], self.discovered(repo), "the preview of the same wish adds nothing")
        self.assertEqual(self.probed, [])
        self.assertIn("Visual goal, but this repository imports a desktop or game toolkit", self.notes()[0])
        self.assertEqual(self.wish(repo, True, web=True)[3]["gates"][-1]["browser"]["page"], "docs/index.html")

    def test_the_brief_says_whether_a_page_is_what_the_wish_changes(self):
        page, why = self.entry({"dist/index.html": PAGE}, web=False)
        self.assertEqual((page, why), (None, "the brief says no committed page is what this wish changes"))
        self.assertEqual(self.entry({"dist/index.html": PAGE}, web=True), ("dist/index.html", None))
        self.assertEqual(self.entry({"dist/index.html": PAGE}), ("dist/index.html", None), "no word from the brief: a visual wish is about the page")
        repo = self.repo(TESTED)
        code, out, err, template = self.wish(repo, True, web=False)
        self.assertEqual((code, template["gates"]), (0, self.discovered(repo)), err)
        self.assertIn("the brief says no committed page is what this wish changes", " ".join(out.split()))
        self.assertEqual(self.probed, [])
        saved = json.loads(next((self.home / "wishes").glob("*/brief.json")).read_text(encoding="utf-8"))
        self.assertIs(saved["web"], False)
        self.assertEqual(wishing.read_brief(json.dumps({**BRIEF, "web": True}))["web"], True)
        self.assertNotIn("web", wishing.read_brief(json.dumps(BRIEF)), "an older writer's answer still reads")
        with self.assertRaisesRegex(ValueError, "web must be true or false"):
            wishing.read_brief(json.dumps({**BRIEF, "web": "yes"}))
        prompt = wishing.brief_prompt({"repository": str(repo), "expected_base": git(repo, "rev-parse", "HEAD"), "gates": [], "acceptance": []}, "Calm")
        self.assertIn('"visual": true, "web": true', prompt)
        self.assertIn("- web: for a visual goal, true when the repository map shows a committed HTML page", prompt)

    def test_export_tree_holds_the_committed_files_and_touches_nothing_in_the_repository(self):
        repo = self.repo({**TESTED, "dist/site.css": "h1{}"})
        (repo / "dist" / "index.html").write_text("<p>edited, not committed</p>", encoding="utf-8")
        (repo / "dist" / "stray.js").write_text("untracked", encoding="utf-8")
        before = git(repo, "status", "--porcelain", "--untracked-files=all")
        site = self.root / "export" / "tree"
        wishing.export_tree(repo, git(repo, "rev-parse", "HEAD"), site, self.root / "export" / "index")
        self.assertEqual(sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()),
                         ["README.md", "dist/index.html", "dist/site.css", "tests/test_site.py"])
        self.assertEqual((site / "dist" / "index.html").read_text(encoding="utf-8"), PAGE, "the committed page, not the edited one")
        self.assertEqual(git(repo, "status", "--porcelain", "--untracked-files=all"), before)
        self.assertEqual(sorted(p.name for p in (repo / ".git").glob("index*")), ["index"], "the repository's own index is the only one")
        with self.assertRaisesRegex(ValueError, "more than 0 MB"):
            wishing.export_tree(repo, git(repo, "rev-parse", "HEAD"), self.root / "export2", self.root / "export2.index", limit=1)

    def test_the_probe_reads_the_gate_receipt(self):
        repo = self.repo(TESTED)
        plan = {"repository": str(repo), "expected_base": git(repo, "rev-parse", "HEAD"), "gates": []}
        gate = {"name": "Visual smoke", "kind": "browser", "browser": {"page": "dist/index.html", "root": "dist", "sweep": {}}}

        def failing(*rows):
            return {"exit": 1, "browser_failures": [{"viewport": view, "stage": {"action": "layout"}, "error": errors[0], "errors": list(errors)}
                                                    for view, *errors in rows]}

        def probe(*receipts, error=None, calls=None):
            self.count += 1
            folder = self.root / f"wish{self.count}"
            with patch("nightweaver.checks.run_gate", side_effect=error or list(receipts)) as run:
                result = REAL_PROBE(plan, gate, folder)
            self.assertEqual(run.call_count, calls or (1 if error else len(receipts)), "a failure is tried a second time, nothing else is")
            if receipts:
                site = Path(run.call_args.args[1])
                self.assertFalse(site.exists(), "the throwaway checkout is removed")
                self.assertEqual((site / "..").resolve(), (folder / "visual-probe").resolve())
                self.assertTrue((folder / "visual-probe" / "evidence" / f"gate-0{run.call_count}").is_dir(), "the evidence stays beside the brief")
                self.assertEqual(run.call_args.args[3], git(repo, "rev-parse", "HEAD^{tree}"))
            return result

        flaky = failing(("desktop", "After clicking 'Gallery': Failed to load resource: net::ERR_CONNECTION_REFUSED"))
        self.assertEqual(probe({"exit": 0}), ("pass", ""))
        self.assertEqual(probe(flaky, {"exit": 0}), ("pass", ""), "a failure that does not repeat does not cost the run its screenshots")
        self.assertEqual(probe(failing(("desktop", "Horizontal overflow: 760px"), ("mobile", "Horizontal overflow: 1810px"))),
                         ("overflow", "desktop 760px, mobile 1810px"))
        self.assertEqual(probe(failing(("mobile", "Horizontal overflow: 20px"))), ("overflow", "mobile 20px"))
        self.assertEqual(probe(failing(("desktop", "Failed to load resource: 404", "Horizontal overflow: 20px")), flaky)[0], "fail",
                         "a console error beside the overflow is not an edit away")
        mixed = failing(("desktop", "Horizontal overflow: 9px"), ("mobile", 'Failed to resolve module specifier "react"'))
        self.assertEqual(probe(mixed, mixed), ("fail", 'desktop: Horizontal overflow: 9px; mobile: Failed to resolve module specifier "react"'))
        self.assertEqual(probe({"exit": None, "artifact_error": "Browser verifier returned no diagnosis"}, {"exit": None, "artifact_error": "again"}),
                         ("fail", "again"))
        self.assertEqual(probe({"exit": 1}, {"exit": 1}), ("fail", "exit 1"))
        self.assertEqual(probe(error=ValueError("boom")), ("fail", "it could not be run: boom"))
        self.assertEqual(probe(error=OSError("disk full\nsecond line")), ("fail", "it could not be run: disk full second line"))


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Install the browser extra")
class VisualGateRunTests(Case):
    real = True

    @classmethod
    def setUpClass(cls):
        if not browser_ready():
            raise unittest.SkipTest("No browser channel (bundled Chromium, Edge or Chrome) can launch on this machine")

    def setUp(self):
        super().setUp()
        from nightweaver import browser
        browser._FALLBACKS.clear()
        self.addCleanup(browser._FALLBACKS.clear)

    def added(self, repo, **brief):
        code, out, err, template = self.wish(repo, True, **brief)
        self.assertEqual(code, 0, err + out)
        return template, " ".join(out.split())

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
        probe = next(self.home.glob("wishes/*/visual-probe"))
        self.assertTrue(any(probe.joinpath("evidence", "gate-01").rglob("*.png")), "the baseline screenshots stay with the brief")
        self.assertFalse(probe.joinpath("checkout").exists(), "the throwaway checkout is gone")

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_the_root_is_what_makes_it_pass_and_a_missing_asset_still_fails(self):
        unrooted = self.run_added(self.repo(SITE), lambda gate: gate["browser"].pop("root"))
        self.assertEqual(unrooted["exit"], 1, "served from the repository root the /site.css and /app.js requests would 404")
        self.assertTrue(any("404" in failure["error"] for failure in unrooted["browser_failures"]), unrooted["browser_failures"])
        template, out = self.added(self.repo({name: data for name, data in SITE.items() if name != "dist/app.js"}))
        self.assertEqual([g["name"] for g in template["gates"]], ["Python unittest tests"], "a missing asset fails the baseline, so no gate")
        self.assertIn("fails on the untouched baseline", out)
        self.assertIn("404", out)

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_a_page_that_needs_a_backend_gets_no_gate_that_could_never_pass(self):
        for fetch in ("fetch('/api/items').then(r => r.json()).then(d => { document.title = d.length })",
                      "fetch('/api/items').then(r => r.json()).catch(() => {})"):
            with self.subTest(fetch=fetch):
                repo = self.repo({"app.py": "print('serves /api/items')\n", "tests/test_app.py": UNITTEST,
                                  "public/index.html": PAGE.replace("</main>", f"</main><script>{fetch}</script>")})
                template, out = self.added(repo)
                self.assertEqual(template["gates"], self.discovered(repo))
                self.assertIn("the Visual smoke check on public/index.html fails on the untouched baseline under a bare file server", out)
                self.assertIn("404", out)
                self.assertIn("will have no screenshots", out)
                self.assertFalse([item for item in template["acceptance"] if "loads over HTTP" in item])

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_a_bundler_page_under_any_script_name_gets_no_gate(self):
        repo = self.repo({"tests/test_site.py": UNITTEST, "vite.config.js": "export default {}\n",
                          "index.html": PAGE.replace("</main>", '</main><script type="module" src="/src/main.js"></script>'),
                          "src/main.js": "import React from 'react'\nconsole.log(React)\n",
                          "package.json": '{"scripts": {"start:dev": "vite", "build": "vite build", "test": "node --test"}}'})
        template, out = self.added(repo)
        self.assertEqual(template["gates"], self.discovered(repo))
        self.assertIn("fails on the untouched baseline under a bare file server", out)
        self.assertIn('Failed to resolve module specifier "react"', out)

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_a_redirect_stub_gives_way_to_the_page_it_points_to_and_that_page_passes(self):
        template, out = self.added(self.repo({"tests/test_site.py": UNITTEST, "index.html": STUB, "docs/index.html": PAGE}))
        self.assertEqual(template["gates"][-1], {"name": "Visual smoke", "kind": "browser",
                                                 "browser": {"page": "docs/index.html", "root": "docs", "sweep": {}}})
        self.assertIn("It passes on the committed baseline.", out)

    @unittest.skipUnless(os.name == "nt", "Windows controller")
    def test_a_baseline_that_only_overflows_keeps_the_gate_for_the_worker_to_fix(self):
        template, out = self.added(self.repo({"tests/test_site.py": UNITTEST, "dist/index.html": OVERFLOW}))
        self.assertEqual(template["gates"][-1]["name"], "Visual smoke")
        self.assertRegex(out, r"On the committed baseline it fails only on horizontal overflow \(desktop \d+px, mobile \d+px\), which the "
                              r"worker is asked to fix\.")
        self.assertTrue([item for item in template["acceptance"] if "loads over HTTP" in item])


if __name__ == "__main__":
    unittest.main()
