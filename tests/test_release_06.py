from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

from examples.agtx_contender import BROWSER_HANDOFF, needs_approval, save_drafts
from nightweaver.benchmark import create_suite, paired_outcomes
from nightweaver.browser import run_browser, validate_browser, visual_match
from nightweaver.diagnostics import database_backup, database_check, preflight, replay_browser
from nightweaver.engine import create_run, git
from nightweaver.reports import metrics, recovery_metrics
from nightweaver.spec import validate
from nightweaver.store import Store


class Measurements(unittest.TestCase):
    def test_paired_exact_test_discards_ties_and_inconclusives(self):
        rows = []
        for i in range(8):
            for name, accepted in (("a", True), ("b", i == 6)):
                outcome = "provider_blocked" if name == "b" and i == 7 else ("accepted" if accepted else "rejected")
                rows.append(dict(task="one", repeat=i, baseline="sha", contender=name, accepted=accepted,
                                 phase="done" if accepted else "blocked", outcome=outcome))
        pair, = paired_outcomes(rows)
        self.assertEqual((pair["discordant_pairs"], pair["scored_pairs"], pair["inconclusive_pairs"]), (6, 7, 1))
        self.assertEqual(pair["exact_two_sided_p"], .03125)
        self.assertIsNone(paired_outcomes([r for r in rows if r["repeat"] == 6])[0]["exact_two_sided_p"])

    def test_repair_and_mixed_results_require_matching_evidence(self):
        first = {"name": "click", "tree": "a", "contract_sha256": "b", "exit": 1, "duration_ms": 4}
        sequence = [("worker_started", {}), ("gate_finished", first), ("attempt_rejected", {}),
                    ("worker_started", {}), ("gate_finished", {**first, "contract_sha256": "c", "exit": 0}),
                    ("milestone_accepted", {}), ("worker_started", {}), ("milestone_accepted", {})]
        def measure():
            return recovery_metrics([{"kind": k, "details": d} for k, d in sequence])
        self.assertEqual(measure()["mixed_gate_results"], [])
        sequence.append(("gate_finished", {**first, "exit": 0, "duration_ms": 26}))
        result = measure()
        self.assertEqual((result["first_pass_milestones"], result["repaired_milestones"]), (1, 1))
        self.assertEqual(len(result["mixed_gate_results"]), 1)
        self.assertEqual(result["slowest_gate"]["duration_ms"], 26)

    def test_detects_operator_prompt_and_declares_browser_ownership(self):
        self.assertIn("Windows", BROWSER_HANDOFF)
        menu = "1. Yes, proceed (y)\n3. No, and tell Codex what to do differently (esc)\nPress enter to confirm"
        self.assertTrue(needs_approval(menu))
        self.assertFalse(needs_approval("Command requires approval"))
        self.assertFalse(needs_approval(menu + "\nold\n" * 22))

    def test_database_backup_preserves_live_wal_and_detects_orphans(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "live")
            source = store.root / "runs.sqlite3"
            with store.connect() as active:
                active.execute("INSERT INTO projects VALUES('fixture','repo','git')")
                active.commit()
                output = root / "backup.sqlite3"
                self.assertTrue(database_backup(source, output)["ok"])
                self.assertTrue(database_check(output)["ok"])
                with closing(sqlite3.connect(output)) as restored, restored:
                    self.assertEqual(restored.execute("SELECT name FROM projects").fetchone(), ("fixture",))
                    restored.execute("INSERT INTO events(run_id,time,kind,details) VALUES('missing','','','{}')")
                self.assertIn("orphan events: 1", database_check(output)["problems"])
                self.assertTrue(database_check(source)["ok"])
                with self.assertRaises(ValueError):
                    database_backup(source, output)
                with self.assertRaises(ValueError):
                    database_backup(source, source)
            output.write_bytes(b"invalid database")
            self.assertFalse(database_check(output)["ok"])


@unittest.skipUnless(os.name == "nt", "native runner")
class RunDiagnostics(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="nw release ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "index.html").write_text('<link rel="icon" href="data:,"><h1>Archive</h1>', encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "archive")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.store = Store(self.root / "runtime")
        self.raw = {"repository": str(self.repo), "title": "Archive", "objective": "Test the archive",
                    "worker": {"kind": "command", "argv": [sys.executable, "--version"]},
                    "budgets": {"max_seconds": 120}, "context_files": ["index.html"],
                    "gates": [{"name": "open", "kind": "browser", "browser": {"page": "index.html", "heading": "Archive"}}],
                    "milestones": [{"title": "Archive", "task": "Open the archive"}]}

    def test_drafts_include_new_files_without_changing_the_index_or_merging(self):
        work = self.root / "work"
        git(self.repo, "worktree", "add", "-b", "worker", str(work))
        (work / "index.html").write_text("work in progress", encoding="utf-8")
        (work / "new.txt").write_text("new draft", encoding="utf-8")
        (work / ".agtx").mkdir()
        (work / ".agtx" / "notes").write_text("private board notes")
        before = git(work, "status", "--porcelain")
        receipt, = save_drafts(self.repo, self.base, self.root)
        data = (self.root / receipt["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), receipt["sha256"])
        self.assertIn(b"new draft", data)
        self.assertNotIn(b"private board notes", data)
        self.assertEqual(git(work, "status", "--porcelain"), before)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base)

    def test_preview_preflight_and_replay_leave_run_and_source_untouched(self):
        spec = validate(self.raw)
        self.assertTrue(preflight(self.store, spec)["ready"])
        (self.root / "plan.json").write_text(json.dumps(self.raw))
        suite = self.root / "suite.json"
        suite.write_text(json.dumps({"name": "Preview", "tasks": [{"name": "A", "plan": "plan.json"}],
                                    "contenders": [{"name": "alone", "worker": {"kind": "codex"}, "mode": "standalone"}],
                                    "repetitions": 2}))
        preview = create_suite(self.store, suite, preview=True)
        self.assertEqual((preview["trials"], preview["configured_call_cap_total"], preview["configured_seconds_total"]),
                         (2, 2, 240))
        self.assertEqual(self.store.runs(), [])
        run_id = create_run(self.store, spec)
        self.store.transition(run_id, "milestone_accepted", {"commit": self.base, "step": 1}, phase="done")
        before = self.store.events(run_id)
        (self.repo / "index.html").write_text("user edit")
        report = replay_browser(self.store, run_id)
        self.assertTrue(report["passed"])
        self.assertEqual(self.store.events(run_id), before)
        self.assertEqual((self.repo / "index.html").read_text(), "user edit")
        with self.assertRaises(ValueError):
            replay_browser(self.store, run_id, 2)

    def test_preflight_collects_every_invalid_visual_baseline(self):
        (self.repo / "good.png").write_bytes(b"good")
        (self.repo / "wrong.png").write_bytes(b"wrong")
        (self.repo / "big.png").write_bytes(b"\0" * (32 * 1024 * 1024 + 1))
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "inner.png").write_bytes(b"x")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "baselines")
        good = hashlib.sha256(b"good").hexdigest()

        def step(name, digest=good):
            return {"action": "visual_match", "selector": "h1", "value": {"baseline": name, "sha256": digest}}

        raw = json.loads(json.dumps(self.raw))
        raw["gates"][0]["browser"]["steps"] = [step("good.png"), step("missing.png"), step("wrong.png"),
                                               step("big.png"), step("dir")]
        report = preflight(self.store, validate(raw))
        text = "\n".join(report["errors"])
        for name in ("missing.png", "wrong.png", "big.png", "dir"):
            self.assertIn(name, text)
        self.assertNotIn("good.png", text)
        self.assertFalse(report["ready"])

    def test_limit_metrics_use_only_recorded_worker_exit(self):
        for code, outcome in ((124, "worker_timeout"), (125, "worker_idle_timeout"), (126, "worker_output_limit"),
                              (1, "rejected"), (None, "rejected")):
            run_id = create_run(self.store, validate(self.raw))
            self.store.transition(run_id, "attempt_rejected", {"phase": "worker", "exit": code})
            self.store.transition(run_id, "blocked", {"issue": "exhausted"}, phase="blocked")
            self.assertEqual(metrics(self.store, run_id)["outcome"], outcome)


class BrowserContracts(unittest.TestCase):
    def test_visual_tolerance_boundaries_and_pinned_reference(self):
        from PIL import Image
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()  # run_browser passes a resolved root; match that for 8.3 temp paths
            image = Image.new("RGB", (2, 2), (10, 20, 30))
            reference = root / "reference.png"
            image.save(reference)
            image.putpixel((0, 0), (13, 20, 30))

            class Region:
                def bounding_box(self):
                    return {"width": 2, "height": 2}

                def screenshot(self, path, **kwargs):
                    image.save(path)

            config = {"baseline": reference.name, "sha256": hashlib.sha256(reference.read_bytes()).hexdigest()}
            result = {}
            visual_match(Region(), {**config, "channel_delta": 3}, root, root, "a", result)
            self.assertEqual(result["visual_matches"][-1]["changed_pixels"], 0)
            visual_match(Region(), {**config, "max_changed_ppm": 250000}, root, root, "b", result)
            self.assertEqual(result["visual_matches"][-1]["changed_pixels"], 1)
            with self.assertRaisesRegex(ValueError, "Visual regression"):
                visual_match(Region(), {**config, "max_changed_ppm": 249999}, root, root, "c", result)
            image.save(reference)
            with self.assertRaisesRegex(ValueError, "hash"):
                visual_match(Region(), config, root, root, "d", result)

    def test_rejects_ambiguous_then_invalid_configuration(self):
        for overrides in (
            {"steps": [{"action": "click", "selector": "a", "locator": {"label": "go"}}]},
            {"steps": [{"action": "visible", "locator": {"role": "button", "test_id": "go"}}]},
            {"network": {"max_requests": True}}, {"viewports": [{"name": "x", "is_mobile": "yes"}]},
            {"steps": [{"action": "visual_match", "selector": "button", "value": {"baseline": "../x", "sha256": "0"*64}}]},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_browser({"page": "index.html", **overrides})

    def test_visual_accessibility_mobile_and_network_contracts_in_chromium(self):
        from PIL import Image
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "chip.png"
            Image.new("RGB", (24, 24), (10, 20, 30)).save(baseline)
            contract = {"baseline": "chip.png", "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()}
            html = """<meta name=viewport content="width=device-width,initial-scale=1"><link rel=icon href="data:,">
              <style>#chip{position:absolute;top:300px;left:8px;height:24px;width:24px;background:rgb(10,20,30)}</style>
              <main><h1>Archive</h1><label>Find <input></label><button>Open</button>
              <div id=chip data-testid=chip></div><p id=env></p></main>
              <script>document.querySelector('#env').textContent = [innerWidth, devicePixelRatio,
              navigator.maxTouchPoints>0, matchMedia('(prefers-color-scheme: dark)').matches,
              matchMedia('(prefers-reduced-motion: reduce)').matches].join('/');</script>"""
            (root / "index.html").write_text(html, encoding="utf-8")
            viewport = {"name": "phone", "width": 360, "height": 600, "is_mobile": True, "has_touch": True,
                        "device_scale_factor": 2, "color_scheme": "dark", "reduced_motion": "reduce"}
            config = {"page": "index.html", "viewports": [viewport], "timeout_ms": 750,
                      "network": {"require_success": True, "max_requests": 2}, "steps": [
                      {"action": "fill", "locator": {"label": "Find"}, "value": "test"},
                      {"action": "click", "locator": {"role": "button", "name": "Open"}},
                      {"action": "aria", "locator": {"role": "main"}, "value": '- heading "Archive" [level=1]'},
                      {"action": "text", "selector": "#env", "value": "360/2/true/true/true"},
                      {"action": "visual_match", "locator": {"test_id": "chip"}, "value": contract}]}
            result = run_browser(config, root, root / "pass")["viewports"][0]
            self.assertTrue(result["passed"], result["errors"])
            self.assertEqual(result["visual_matches"][0]["changed_pixels"], 0)
            self.assertEqual(len(result["step_durations_ms"]), 5)
            self.assertGreater(result["duration_ms"], 0)
            (root / "index.html").write_text(html.replace("10,20,30", "90,20,30") + '<img src="missing.png?secret=never">')
            result = run_browser(config, root, root / "fail")["viewports"][0]
            self.assertFalse(result["passed"])
            self.assertEqual(result["visual_matches"][0]["changed_pixels"], 576)
            self.assertTrue((root / "fail" / "phone.05.diff.png").is_file())
            self.assertTrue(any("HTTP 404" in error for error in result["errors"]))
            self.assertGreaterEqual(result["http_requests"], 2)
            config["steps"] = [{"action": "aria", "locator": {"role": "main"}, "value": '- heading "Missing"'}]
            config["network"]["max_requests"] = 0
            result = run_browser(config, root, root / "aria")["viewports"][0]
            self.assertFalse(result["passed"])
            self.assertTrue((root / "aria" / "phone.01.aria.yml").is_file())
            self.assertTrue(any("request budget exceeded" in e for e in result["errors"]))
