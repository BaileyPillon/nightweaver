"""Browser cases and evidence must identify the actual visitor and viewport."""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import test_release_09 as previous
import test_windows as fixtures

from nightweaver.browser import browser_output, run_browser, run_step, validate_browser, visual_match
from nightweaver.checks import read_browser_evidence
from nightweaver.engine import git

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<link rel=icon href="data:,"><button id=clear aria-describedby=desc
 onclick="sessionStorage.clear();localStorage.removeItem('saved'); fetch('receipt.json')">Remove</button>
<p id=desc>Clear the saved item</p><label for=city>City</label><select id=city><option>Oslo</option><option>Paris</option></select>
<input id=pin type=checkbox checked><button id=scroll style="margin-top:120vh;display:block">Off screen</button>"""


def state(area, key, value):
    return {"action": "storage", "value": {"area": area, "key": key, "equals": value}}


def view(name="desktop"):
    return {"name": name, "width": 640, "height": 480}


def read_receipt(config, out, exit_code):
    receipt = {"exit": exit_code, "artifacts": []}
    read_browser_evidence(validate_browser(config), out, receipt, 32 * 1024 * 1024)
    return receipt


class BrowserInputTests(unittest.TestCase):
    def test_invalid_conditions_stop_in_validation(self):
        for step in [
            {"action": " click ", "selector": "a"},
            {"action": "click", "selector": "a", "value": "ignored"},
            {"action": "in_viewport", "selector": "a", "value": True},
            {"action": "uncheck", "selector": "input", "value": False},
            {"action": "reopen", "selector": "body"},
            {"action": "url_path", "value": "https://example.invalid"},
            {"action": "storage", "value": {"key": "k", "area": "session"}},
            {
                "action": "response",
                "value": {"method": "GET", "path": "/", "status": True},
            },
            {"action": "attribute", "selector": "button", "value": {"name": "title"}},
        ]:
            with self.subTest(step=step), self.assertRaises(ValueError):
                validate_browser({"page": "index.html", "steps": [step]})
        for bad_url in (
            "http://localhost:0",
            "http://localhost:70000",
            "http://localhost:wrong",
        ):
            with self.assertRaisesRegex(ValueError, "port"):
                validate_browser({"url": bad_url})
        for scenarios in (
            [],
            [{"name": "fresh"}, {"name": "fresh"}],
            [{"name": "bad.name"}],
            [{"name": "con"}],
            [{"name": "old", "session_storage": {"x": 42}}],
            [{"name": "large", "local_storage": {"k": "a" * 65536}}],
        ):
            with (
                self.subTest(scenarios=str(scenarios)[:80]),
                self.assertRaises(ValueError),
            ):
                validate_browser({"page": "index.html", "scenarios": scenarios})
        check = {"action": "checkpoint", "value": "saved"}
        with self.assertRaises(ValueError):
            validate_browser(
                {
                    "page": "index.html",
                    "steps": [check],
                    "scenarios": [{"name": "old", "steps": [check]}],
                }
            )
        with self.assertRaises(ValueError):
            validate_browser(
                {
                    "page": "index.html",
                    "offline": True,
                    "scenarios": [{"name": "one", "local_storage": {"a": "1"}}],
                }
            )

    def test_reload_respects_the_wait_strategy(self):
        page = Mock()
        page.reload.return_value.status = 200
        run_step(
            page,
            {"action": "reload"},
            1,
            {"viewport": "x"},
            Path("."),
            1000,
            Path("."),
            "domcontentloaded",
        )
        page.reload.assert_called_once_with(wait_until="domcontentloaded", timeout=1000)

    def test_transparency_and_visual_receipt_have_numeric_evidence(self):
        from PIL import Image

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            ref = root / "ref.png"
            Image.new("RGBA", (2, 2), (30, 40, 50, 0)).save(ref)
            capture = Image.new("RGBA", (2, 2), (30, 40, 50, 255))
            target = Mock()
            target.bounding_box.return_value = {"width": 2, "height": 2}
            target.screenshot.side_effect = lambda path, **kwargs: capture.save(path)
            contract = {
                "baseline": "ref.png",
                "sha256": hashlib.sha256(ref.read_bytes()).hexdigest(),
            }
            row = {}
            with self.assertRaisesRegex(ValueError, "4/4"):
                visual_match(target, contract, root, root, "phone.01", row)
            self.assertEqual(row["visual_matches"][0]["changed_ppm"], 1000000)
            self.assertTrue((root / "phone.01.diff.png").is_file())

    def test_legacy_contradictions_and_missing_images_cannot_pass(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for file in ("desktop.png", "desktop.trace.zip"):
                (root / file).write_bytes(b"fixture")
            config = {
                "page": "index.html",
                "viewports": [view()],
                "network": {"max_requests": 0},
            }
            row = {
                "viewport": "desktop",
                "passed": True,
                "errors": [],
                "completed_steps": 0,
                "http_requests": 0,
            }
            for change in (
                {"http_requests": 5},
                {"overflow_px": 42},
                {"overflow_px": True},
            ):
                (root / "browser.json").write_text(
                    json.dumps({"passed": True, "viewports": [{**row, **change}]})
                )
                with self.subTest(change=change), self.assertRaises(ValueError):
                    read_receipt(config, root, 0)
            step = {
                "action": "visual_match",
                "selector": "body",
                "value": {"baseline": "ref.png", "sha256": "f" * 64},
            }
            config["steps"] = [step]
            row["completed_steps"] = 1
            (root / "browser.json").write_text(
                json.dumps({"passed": True, "viewports": [row]})
            )
            with self.assertRaisesRegex(ValueError, "actual.png"):
                read_receipt(config, root, 0)
            (root / "desktop.01.actual.png").write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "visual measurement"):
                read_receipt(config, root, 0)
            (root / "browser.json").write_text(
                '{"passed":false,"passed":true,"viewports":[]}'
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                read_receipt(config, root, 0)

    def test_cli_keeps_old_evidence_and_rejects_ambiguous_json(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "evidence"
            out.mkdir()
            receipt = out / "browser.json"
            receipt.write_text("existing")
            config = root / "config.json"
            config.write_text('{"page":"a","page":"b"}', encoding="utf-8-sig")
            args = [
                sys.executable,
                "-m",
                "nightweaver.browser",
                "--config-file",
                str(config),
                "--output",
            ]
            self.assertEqual(
                subprocess.run(
                    args + [str(out)], capture_output=True, check=False
                ).returncode,
                1,
            )
            self.assertEqual(receipt.read_text(), "existing")
            newer = root / "new"
            with browser_output(newer):
                busy = subprocess.run(args + [str(newer)], capture_output=True, check=False)
                self.assertEqual(busy.returncode, 1)
                self.assertIn(b"active verifier", busy.stderr)
                self.assertFalse((newer / "browser.json").exists())
            proc = subprocess.run(args + [str(newer)], capture_output=True, check=False)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Duplicate JSON", (newer / "browser.json").read_text())


@unittest.skipUnless(os.name == "nt", "Windows verifier")
class WindowsJourneys(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    cli = previous.OperatorRelease.cli
    plan = previous.OperatorRelease.plan

    def set_page(self):
        (self.repo / "index.html").write_text(PAGE)
        (self.repo / "receipt.json").write_text("{}")
        git(self.repo, "add", "index.html", "receipt.json")
        git(self.repo, "commit", "-m", "Add saved form")

    def test_isolated_cases_tab_lifetime_and_model_free_reporting(self):
        self.set_page()
        shared = [
            {"action": "accessible_name", "selector": "#clear", "value": "Remove"},
            {
                "action": "accessible_description",
                "selector": "#clear",
                "value": "Clear the saved item",
            },
            {"action": "in_viewport", "selector": "#clear", "value": 1},
            {"action": "select", "locator": {"label": "City"}, "value": "Paris"},
            {"action": "value", "selector": "select", "value": "Paris"},
            {"action": "uncheck", "selector": "#pin"},
            {"action": "unchecked", "selector": "#pin"},
            {
                "action": "attribute",
                "selector": "#pin",
                "value": {"name": "disabled", "equals": None},
            },
            {"action": "url_path", "value": "/index.html"},
        ]
        steps = [
            state("session", "draft", "y"),
            state("local", "saved", "x"),
            {"action": "reload"},
            state("session", "draft", "y"),
            {"action": "reopen"},
            state("session", "draft", None),
            state("local", "saved", "x"),
            {"action": "click", "selector": "#clear"},
            {
                "action": "response",
                "value": {"method": "GET", "path": "/receipt.json", "status": 200},
            },
            {"action": "reload"},
            state("local", "saved", None),
            {"action": "checkpoint", "value": "removed"},
        ]
        config = {
            "page": "index.html",
            "timeout_ms": 1600,
            "viewports": [view("phone"), view("desktop")],
            "steps": shared,
            "scenarios": [
                {
                    "name": "seeded",
                    "local_storage": {"saved": "x"},
                    "session_storage": {"draft": "y"},
                    "steps": steps,
                },
                {
                    "name": "empty",
                    "steps": [
                        state("local", "saved", None),
                        state("session", "draft", None),
                    ],
                },
            ],
        }
        gate = {
            "kind": "browser",
            "name": "Saved form",
            "browser": config,
            "timeout": 90,
        }
        plan = self.plan(gates=[gate], attempts=1)
        pre = self.cli("preflight", plan)
        self.assertEqual(pre["browser_work"][0]["contexts"], 4)
        self.assertEqual(
            pre["browser_work"][0]["steps"], 2 * (2 * len(shared) + len(steps) + 2)
        )
        rid = self.cli("add", plan)["run_id"]
        self.assertEqual(
            {r["status"] for r in self.cli("browser-matrix", rid)["cases"]}, {"pending"}
        )
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        matrix = self.cli("browser-matrix", rid)["cases"]
        self.assertTrue(
            all(
                r["status"] == "passed"
                and r["passed_samples"] == 1
                and r["runtime"]["playwright"]
                for r in matrix
            )
        )
        self.assertEqual({r["scenario"] for r in matrix}, {"empty", "seeded"})
        self.assertTrue(
            all(
                r["navigation_ms"] >= 0 and r["network"]["requests"] > 0 for r in matrix
            )
        )
        seeded = next(r for r in matrix if r["scenario"] == "seeded")
        self.assertEqual(seeded["checkpoints"][0]["overflow_px"], 0)
        self.assertEqual(len(self.cli("browser-coverage", rid)["viewports"]), 4)
        actions = self.cli("browser-hotspots", rid)["actions"]
        self.assertTrue(
            any(
                r["scenario"] == "seeded" and r["action"] == "response" for r in actions
            )
        )
        self.assertTrue(all(r["timed_observations"] == 1 for r in actions))
        md = self.root / "ci.md"
        self.cli("ci-summary", rid, "--output", md)
        self.assertIn("4 / 4", md.read_text())
        self.store.transition(
            rid,
            "gate_finished",
            {
                "step": 1,
                "gate_index": 1,
                "name": "Saved form",
                "artifact_error": "unavailable",
                "exit": None,
            },
        )
        self.assertTrue(
            all(
                r["status"] == "unscored" and r["passed_samples"] == 1
                for r in self.cli("browser-matrix", rid)["cases"]
            )
        )

    def test_responses_consumed_once_and_cannot_cross_scenarios(self):
        self.set_page()
        response = {
            "action": "response",
            "value": {"method": "GET", "status": 200, "path": "/receipt.json"},
        }
        config = {
            "page": "index.html",
            "timeout_ms": 450,
            "viewports": [view()],
            "scenarios": [
                {
                    "name": "first",
                    "steps": [
                        {"action": "click", "selector": "#clear"},
                        response,
                        response,
                    ],
                },
                {"name": "second", "steps": [response]},
            ],
        }
        out = self.root / "out"
        report = run_browser(config, self.repo, out)
        self.assertFalse(report["passed"])
        self.assertEqual([v["completed_steps"] for v in report["viewports"]], [2, 0])
        detail = read_receipt(config, out, 1)
        self.assertEqual(
            [v["scenario"] for v in detail["browser_failures"]], ["first", "second"]
        )
        self.assertEqual(
            detail["browser_coverage"][0]["response_matches"][0]["index"], 2
        )
        tampered = deepcopy(report)
        tampered["viewports"][0]["response_matches"] = []
        (out / "browser.json").write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "HTTP observations"):
            read_receipt(config, out, 1)
        tampered["viewports"][1]["scenario"] = "first"
        (out / "browser.json").write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            read_receipt(config, out, 1)

    def test_late_budget_and_console_stages(self):
        self.set_page()
        cfg = {
            "page": "index.html",
            "viewports": [view()],
            "network": {"max_requests": 0},
        }
        out = self.root / "budget"
        report = run_browser(cfg, self.repo, out)
        r = report["viewports"][0]
        self.assertFalse(r["passed"])
        self.assertEqual(r["failed_at"]["action"], "request_budget")
        self.assertGreater(r["error_counts"]["request_budget"], 0)
        (self.repo / "index.html").write_text(
            '<link rel="icon" href="data:,"><script>for(let i=0;i<25;i++)console.error("repeat"); console.error("distinct")</script>'
        )
        report = run_browser(
            {"page": "index.html", "viewports": [view()]}, self.repo, self.root / "logs"
        )
        r = report["viewports"][0]
        self.assertEqual(r["error_counts"]["console"], 26)
        self.assertEqual(r["errors"], ["repeat", "distinct"])
        self.assertEqual(r["failed_at"]["action"], "navigate")

    def test_worker_repairs_named_case_then_advances(self):
        self.set_page()
        worker = self.root / "repair.py"
        worker.write_text(
            """from pathlib import Path
import sys
prompt=sys.stdin.read()
if '"scenario": "returning"' in prompt and '"stage":' in prompt:
    Path('index.html').write_text('<p id="status"></p><script>statusNode=document.querySelector("p");statusNode.textContent=localStorage.getItem("saved") || "empty"</script>')
else:
    Path('index.html').write_text('<p id="status">empty</p>')
""",
            encoding="utf8",
        )
        scenarios = [
            {
                "name": "fresh",
                "steps": [{"action": "text", "selector": "#status", "value": "empty"}],
            },
            {
                "name": "returning",
                "local_storage": {"saved": "history"},
                "steps": [
                    {"action": "text", "selector": "#status", "value": "history"}
                ],
            },
        ]
        gate = {
            "kind": "browser",
            "name": "Collections",
            "browser": {
                "page": "index.html",
                "scenarios": scenarios,
                "timeout_ms": 700,
                "viewports": [view()],
            },
        }
        route = {"kind": "command", "argv": [sys.executable, str(worker)]}
        plan = self.plan(
            gates=[],
            worker=route,
            attempts=2,
            milestones=[
                {
                    "title": "Restore collections",
                    "task": "Restore saved status",
                    "paths": ["index.html"],
                    "gates": [gate],
                },
                {
                    "title": "Record delivery",
                    "task": "Write result",
                    "paths": ["result.txt"],
                    "worker": {"kind": "command", "argv": fixtures.fixture("good")},
                    "gates": [{"name": "Record", "argv": fixtures.fixture("gate")}],
                },
            ],
        )
        rid = self.cli("add", plan)["run_id"]
        self.assertEqual(self.cli("run", rid)["phase"], "done")
        found = {r["scenario"]: r for r in self.cli("browser-matrix", rid)["cases"]}
        self.assertEqual(
            (
                found["fresh"]["passed_samples"],
                found["returning"]["failed_samples"],
                found["returning"]["passed_samples"],
            ),
            (2, 1, 1),
        )
        self.assertEqual(self.cli("metrics", rid)["accepted_milestones"], 2)
