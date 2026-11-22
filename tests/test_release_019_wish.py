"""nightweaver wish: one line in, a fully specified goal-pursuit run out."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_windows as fixtures

from nightweaver import wish as wishing
from nightweaver.cli import main
from nightweaver.engine import git
from nightweaver.spec import read_plan, validate
from nightweaver.store import Store

HELPER = str(Path(__file__).with_name("wish_fixture.py").resolve())
ACCEPTANCE = ["styles.css defines the teal palette tokens --atlas-bg and --atlas-ink at :root",
              "The header keeps the current navigation links and their order",
              "At 390x844 the card grid collapses to one column without horizontal scrolling",
              "A screenshot at 1440x900 shows the hero heading above the fold",
              "No existing file under tests/ is edited"]
OBJECTIVE = ("Redesign the Atlas landing page with a calm, teal-on-charcoal art direction. Keep the existing navigation, copy "
             "and routes. Do not change the tests.")


def brief(**extra):
    return {"title": "Atlas teal redesign", "objective": OBJECTIVE, "acceptance": list(ACCEPTANCE), "visual": True,
            "conflicts": ["Wish says 'dark' and 'light': resolved as dark with a light print stylesheet"], **extra}


def fenced(value=None):
    return "Here is the brief you asked for.\n```json\n" + json.dumps(brief() if value is None else value) + "\n```\nLet me know."


def judged(score=92):
    return {"verdict": "MET", "score": score, "reason": "Everything observed", "gaps": [],
            "criteria": [{"criterion": "Looks right", "met": True, "note": "Observed"}]}


class Ollama:
    """A loopback server that answers /api/tags like Ollama does."""

    def __init__(self, models=("qwen3.8:27b",), delay=0.0):
        self.hits = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.hits.append(self.path)
                time.sleep(delay)
                body = json.dumps({"models": [{"name": m, "model": m} for m in models]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up on a slow answer

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/tags"
        self.port = self.server.server_port

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


def closed_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/api/tags"


def init_repository(folder, files):
    folder.mkdir()
    git(folder, "init", "-b", "main")
    git(folder, "config", "user.name", "Nightweaver Fixture")
    git(folder, "config", "user.email", "fixture@example.invalid")
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-m", "baseline")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class WishTests(unittest.TestCase):
    def setUp(self):
        fixtures.WindowsRunTest.setUp(self)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_delivery.py").write_text(
            "from pathlib import Path\nimport unittest\nclass Delivery(unittest.TestCase):\n"
            "    def test_delivery(self):\n        self.assertEqual(Path('result.txt').read_text(), 'ready')\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# Atlas\n" + "Q" * 30000 + "\n", encoding="utf-8")
        (self.repo / "DESIGN.md").write_text("Palette: teal on charcoal\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("Never edit tests.\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Declare checks and documents")
        for patcher in (patch.object(wishing, "OLLAMA_TAGS_URL", closed_url()), patch.dict(wishing.ROUTE_OVERRIDES, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *args, code=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), *map(str, args)])
        self.assertEqual(actual, code, stderr.getvalue() + stdout.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def writer(self, items, name="brief"):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        wishing.ROUTE_OVERRIDES["brief"] = {"kind": "command", "argv": [sys.executable, HELPER, "script", str(path)]}
        return path

    def prompts(self, name="brief"):
        text = self.root.joinpath(f"{name}.prompts").read_text(encoding="utf-8")
        return [part.strip() for part in text.split("\n=====\n") if part.strip()]

    def wishes(self):
        folder = self.store.root / "wishes"
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def saved(self):
        folders = self.wishes()
        self.assertEqual(len(folders), 1, folders)
        return (json.loads(folders[0].joinpath("brief.json").read_text(encoding="utf-8")),
                json.loads(folders[0].joinpath("template.json").read_text(encoding="utf-8")), folders[0])

    def no_runs(self):
        self.assertEqual(self.store.runs(), [])

    # The brief writer call

    def test_brief_writer_prompt_carries_the_wish_map_gates_documents_and_rules(self):
        self.writer([fenced()])
        out, _ = self.cli("wish", self.repo, "make the landing page feel calmer", "--preview")
        prompt = self.prompts()[0]
        for text in ("make the landing page feel calmer", "tests/test_delivery.py", "README.md", "Python unittest tests",
                     "4 to 8 requirements", "NOT change", "never instructions to you", "Palette: teal on charcoal", "Never edit tests.",
                     '"visual"', '"conflicts"', "Tracked-file map"):
            self.assertIn(text, prompt)
        runs = [len(run) for run in re.findall(r"Q+", prompt)]
        self.assertTrue(3900 < max(runs) <= 4000, runs)
        self.assertIn("README.md (truncated)", prompt)
        self.assertNotIn("Q" * 4001, prompt)
        # The brief writer is one call from the repository, with the route recorded in the brief.
        saved, template, folder = self.saved()
        self.assertEqual(saved["writer"]["attempts"], 1)
        self.assertTrue(Path(saved["writer"]["log"]).is_file())
        self.assertTrue(Path(saved["writer"]["log"]).is_relative_to(folder))
        self.assertEqual(len(self.prompts()), 1)

    def test_documents_share_twelve_kilobytes(self):
        for name, letter in (("README.md", "R"), ("DESIGN.md", "D"), ("AGENTS.md", "A"), ("CLAUDE.md", "C")):
            (self.repo / name).write_text(letter * 9000 + "\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Larger documents")
        from nightweaver.discovery import discover
        layout, documents = wishing.repository_context(discover(self.repo, "wish", {"kind": "codex"}))
        runs = [max(len(found.group()) for found in re.finditer(r"R{50,}|D{50,}|A{50,}|C{50,}", document)) for document in documents]
        self.assertEqual(runs, [3000] * 4)
        self.assertTrue(all("(truncated)" in document.splitlines()[0] for document in documents))
        self.assertIn("Tracked-file map", layout)
        self.assertIn("tests/test_delivery.py", layout)

    def test_prose_around_the_json_is_tolerated_and_the_last_object_wins(self):
        decoy = {"objective": "an example", "acceptance": ["x"], "visual": False}
        self.writer([f"Example shape: {json.dumps(decoy)}\nNow the real one.\n" + fenced()])
        self.cli("wish", self.repo, "Calm the page", "--preview")
        self.assertEqual(self.saved()[0]["objective"], OBJECTIVE)

    def test_invalid_shapes_are_rejected_with_the_reason(self):
        cases = {"no objective": ({"acceptance": ACCEPTANCE, "visual": True}, "No JSON object"),
                 "three requirements": (brief(acceptance=ACCEPTANCE[:3]), "4 to 8 requirements"),
                 "nine requirements": (brief(acceptance=ACCEPTANCE + ACCEPTANCE[:4]), "4 to 8 requirements"),
                 "blank requirement": (brief(acceptance=[*ACCEPTANCE[:4], " "]), "4 to 8 requirements"),
                 "visual missing": ({k: v for k, v in brief().items() if k != "visual"}, "visual must be true or false"),
                 "visual text": (brief(visual="yes"), "visual must be true or false"),
                 "objective blank": (brief(objective="  "), "objective must be text"),
                 "title number": (brief(title=3), "title must be text"),
                 "conflicts text": (brief(conflicts="none"), "conflicts must be a list")}
        for label, (value, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, message):
                wishing.read_brief(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "empty"):
            wishing.read_brief("")
        derived = wishing.read_brief(json.dumps(brief(title=None)))
        self.assertTrue(derived["title"].startswith("Redesign the Atlas landing page"))
        self.assertEqual(len(wishing.read_brief(json.dumps(brief(title="x" * 300)))["title"]), 100)

    def test_one_invalid_answer_is_retried_with_the_reason(self):
        self.writer(["I cannot do JSON today", fenced()])
        out, _ = self.cli("wish", self.repo, "Calm the page", "--preview")
        self.assertEqual(len(self.prompts()), 2)
        self.assertNotIn("previous answer was rejected", self.prompts()[0])
        self.assertIn("Your previous answer was rejected: No JSON object with a 'objective' field", self.prompts()[1])
        self.assertEqual(self.saved()[0]["writer"]["attempts"], 2)

    def test_invalid_twice_is_an_error_and_registers_nothing(self):
        self.writer(["not json at all", brief(acceptance=ACCEPTANCE[:2])])
        _, err = self.cli("wish", self.repo, "Calm the page", "--start", code=1)
        self.assertIn("no valid brief twice", err)
        self.assertIn("4 to 8 requirements", err)
        self.assertIn("Nothing was registered", err)
        self.no_runs()
        self.assertEqual(len(self.prompts()), 2)
        folder = self.wishes()[0]
        self.assertFalse((folder / "brief.json").exists() or (folder / "template.json").exists())
        self.assertTrue((folder / "brief-writer-2.jsonl").is_file(), "the logs stay for diagnosis")

    def test_writer_failures_surface(self):
        wishing.ROUTE_OVERRIDES["brief"] = {"kind": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(7)"]}
        _, err = self.cli("wish", self.repo, "Calm the page", "--preview", code=1)
        self.assertIn("The brief writer exited 7", err)
        wishing.ROUTE_OVERRIDES["brief"] = {"kind": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(9)"],
                                            "unavailable_exit_code": 9}
        _, err = self.cli("wish", self.repo, "Calm the page", "--preview", code=1)
        self.assertIn("The brief writer (command) is unavailable", err)
        self.no_runs()

    def test_brief_route_defaults_and_overrides(self):
        def route(**flags):
            return wishing.brief_route(argparse.Namespace(**{"brief_kind": None, "brief_model": None, "brief_effort": None, **flags}))
        self.assertEqual(route(), {"kind": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        self.assertEqual(route(brief_kind="claude", brief_model="claude-x"), {"kind": "claude", "model": "claude-x", "effort": "xhigh"})
        self.assertEqual(route(brief_effort="high"), {"kind": "claude", "model": "claude-opus-5-5", "effort": "high"})
        self.assertEqual(route(brief_kind="codex"), {"kind": "codex"}, "another family does not inherit Claude's model")
        self.assertEqual(route(brief_kind="deepseek", brief_model="deepseek-v4-pro", brief_effort="max"),
                         {"kind": "deepseek", "model": "deepseek-v4-pro", "effort": "max"})
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            route(brief_kind="deepseek", brief_effort="xhigh")
        self.assertEqual(wishing.BRIEF_ROUTE, wishing.OPUS)
        self.assertIsNot(route(), wishing.OPUS)

    # The template

    def test_default_roster_and_pursuit_settings_with_ollama_present(self):
        self.writer([fenced()])
        with Ollama() as ollama, patch.object(wishing, "OLLAMA_TAGS_URL", ollama.url):
            out, _ = self.cli("wish", self.repo, "Calm the page", "--preview")
        self.assertEqual(ollama.hits, ["/api/tags"])
        saved, template, _ = self.saved()
        self.assertEqual(template["worker"], {"kind": "codex", "model": "gpt-6.2-astra", "effort": "xhigh"})
        self.assertEqual(template["reviewer"], {"kind": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        self.assertEqual(template["advisory_ollama"], {"model": "qwen3.8:27b", "port": ollama.port, "think": "xhigh", "required": False,
                                                       "timeout": 600, "max_tokens": 4096})
        pursue = template["pursue"]
        self.assertEqual(pursue["judge"], {"kind": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        self.assertEqual(pursue["planner"], {"kind": "deepseek", "model": "deepseek-v4-pro", "effort": "max"})
        self.assertEqual({k: pursue[k] for k in ("max_waves", "wave_size", "target_score", "plateau")},
                         {"max_waves": 3, "wave_size": 2, "target_score": 88, "plateau": 2})
        self.assertEqual(pursue["criteria"], ACCEPTANCE)
        self.assertEqual(template["acceptance"], ACCEPTANCE)
        self.assertNotIn("references", pursue)
        self.assertEqual((template["budgets"], template["attempts"], template["agent_timeout"]),
                         ({"max_calls": 80, "max_seconds": 21600}, 3, 1800))
        self.assertEqual([g["name"] for g in template["gates"]], ["Python unittest tests"])
        self.assertEqual((template["title"], template["objective"]), ("Atlas teal redesign", OBJECTIVE))
        self.assertEqual(template["milestones"], [{"title": "Deliver the goal", "task": OBJECTIVE}])
        self.assertEqual(template["expected_base"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(saved["conflicts"], brief()["conflicts"])
        self.assertEqual(saved["notes"], [])
        self.assertIn("ollama qwen3.8:27b", out)
        # The saved template is a plan the rest of Nightweaver accepts, and it holds no credential fields.
        folder = self.wishes()[0]
        self.assertEqual(read_plan(folder / "template.json", self.store)["pursue"]["criteria"], ACCEPTANCE)
        self.assertNotRegex(folder.joinpath("template.json").read_text(), r'"(?:api_key\w*|oauth_token\w*|password|profile_dir)"')

    def test_visual_goal_adds_explored_directions_only_when_this_build_supports_them(self):
        self.writer([fenced()])
        self.cli("wish", self.repo, "Calm the page", "--preview")
        pursue = self.saved()[1]["pursue"]
        probe = {**self.saved()[1], "pursue": {**pursue, "explore": {"directions": 3}}}
        try:
            validate(probe, store=self.store)
            supported = True
        except ValueError:
            supported = False
        self.assertEqual(pursue.get("explore"), {"directions": 3} if supported else None)

    def test_explore_block_follows_the_validator_and_the_visual_flag(self):
        template = {"pursue": {"judge": {"kind": "claude"}}}
        with patch.object(wishing, "validate", return_value={}) as accepted:
            self.assertTrue(wishing.with_explore(template, self.store))
        self.assertEqual(template["pursue"]["explore"], {"directions": 3})
        self.assertEqual(accepted.call_args.args[0]["pursue"]["explore"], {"directions": 3})
        template = {"pursue": {"judge": {"kind": "claude"}}}
        with patch.object(wishing, "validate", side_effect=ValueError("Unknown pursue fields: explore")):
            self.assertFalse(wishing.with_explore(template, self.store))
        self.assertEqual(template, {"pursue": {"judge": {"kind": "claude"}}})
        # A goal that is not visual never asks for directions, even where they exist.
        self.writer([fenced(brief(visual=False))])
        with patch.object(wishing, "with_explore") as explore:
            self.cli("wish", self.repo, "Speed up the parser", "--preview")
        explore.assert_not_called()
        self.assertFalse(self.saved()[0]["visual"])

    def test_fast_roster_uses_defaults_and_skips_reviewer_and_advisory(self):
        self.writer([fenced()])
        with Ollama() as ollama, patch.object(wishing, "OLLAMA_TAGS_URL", ollama.url):
            out, _ = self.cli("wish", self.repo, "Calm the page", "--roster", "fast", "--preview")
        self.assertEqual(ollama.hits, [], "a fast roster does not even look for Ollama")
        _, template, _ = self.saved()
        self.assertEqual(template["worker"], {"kind": "codex"})
        self.assertNotIn("reviewer", template)
        self.assertNotIn("advisory_ollama", template)
        self.assertEqual(template["pursue"]["judge"], {"kind": "claude"})
        self.assertEqual(template["pursue"]["planner"], {"kind": "claude"})
        self.assertEqual(template["pursue"]["criteria"], ACCEPTANCE)
        self.assertIn("reviewer  none", out)

    def test_settings_flags_reach_the_pursuit_block(self):
        reference = self.root / "target.png"
        reference.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a7d1d8c10000000049454e44ae426082"))
        self.writer([fenced()])
        self.cli("wish", self.repo, "Calm the page", "--preview", "--waves", "5", "--target-score", "92", "--reference", reference)
        pursue = self.saved()[1]["pursue"]
        self.assertEqual((pursue["max_waves"], pursue["target_score"], pursue["references"]), (5, 92, [str(reference.resolve())]))

    def test_ollama_is_left_out_with_a_note_when_absent_unlisted_or_slow(self):
        self.writer([fenced()])
        out, _ = self.cli("wish", self.repo, "Calm the page", "--preview")
        saved, template, _ = self.saved()
        self.assertNotIn("advisory_ollama", template)
        self.assertEqual(len(saved["notes"]), 1)
        self.assertIn("no Ollama answered", saved["notes"][0])
        self.assertIn("Note: advisory_ollama left out", out)
        with Ollama(models=("llama3:8b",)) as ollama, patch.object(wishing, "OLLAMA_TAGS_URL", ollama.url):
            self.assertEqual(wishing.ollama_advisory()[0], None)
            self.assertIn("qwen3.8:27b is not installed", wishing.ollama_advisory()[1])
        with Ollama(delay=1.5) as ollama, patch.object(wishing, "OLLAMA_TAGS_URL", ollama.url), patch.object(wishing, "OLLAMA_TIMEOUT", 0.3):
            started = time.monotonic()
            block, note = wishing.ollama_advisory()
            self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(block, None)
        self.assertIn("within 0.3 s", note)
        with Ollama(models=("qwen3.8:27b",)) as ollama, patch.object(wishing, "OLLAMA_TAGS_URL", ollama.url):
            self.assertEqual(wishing.ollama_advisory()[0]["port"], ollama.port)

    def test_web_repository_without_tests_gets_the_browser_smoke_gate(self):
        site = self.root / "site"
        init_repository(site, {"index.html": "<!doctype html><title>Site</title><h1>Hi</h1>", "README.md": "A site\n"})
        self.writer([fenced()])
        self.cli("wish", site, "Calm the page", "--preview")
        saved, template, _ = self.saved()
        self.assertEqual(template["gates"], [{"name": "Browser smoke", "kind": "browser", "browser": {"page": "index.html", "sweep": {}}}])
        self.assertEqual(template["acceptance"][:5], ACCEPTANCE)
        self.assertIn("390x844", template["acceptance"][5])
        self.assertEqual(template["pursue"]["criteria"], template["acceptance"])
        self.assertIn("Browser smoke (browser, page index.html", self.prompts()[0])
        self.assertIn("Checks: Browser smoke (browser, page index.html", self.cli("wish", site, "Calm again", "--preview")[0])

    def test_discovery_failure_stops_before_any_model_call(self):
        bare = self.root / "bare"
        init_repository(bare, {"main.py": "print('hi')\n"})
        self.writer([fenced()])
        _, err = self.cli("wish", bare, "Calm the page", "--start", code=1)
        self.assertIn("No supported committed test suite found", err)
        self.assertFalse(self.root.joinpath("brief.prompts").exists())
        self.assertEqual(self.wishes(), [])
        self.no_runs()

    def test_options_are_refused_before_the_brief_writer_is_called(self):
        self.writer([fenced()])
        for label, flags, message in (("waves", ["--waves", "0"], "max_waves"), ("target", ["--target-score", "101"], "target_score"),
                                      ("reference", ["--reference", str(self.root / "absent.png")], "Reference image not found"),
                                      ("wait", ["--wait"], "--wait requires --start"),
                                      ("guard", ["--guard-restarts", "1"], "--guard-restarts requires --start")):
            with self.subTest(label):
                _, err = self.cli("wish", self.repo, "Calm the page", *flags, code=1)
                self.assertIn(message, err)
        _, err = self.cli("wish", self.repo, "   ", code=1)
        self.assertIn("nonempty line", err)
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            self.cli("wish", self.repo, "Calm the page", "--preview", "--start")
        self.assertFalse(self.root.joinpath("brief.prompts").exists())
        self.assertEqual(self.wishes(), [])
        wishing.ROUTE_OVERRIDES.clear()
        _, err = self.cli("wish", self.repo, "Calm the page", "--brief-kind", "deepseek", "--brief-effort", "xhigh", code=1)
        self.assertIn("Unsupported effort", err)
        self.assertEqual(self.wishes(), [])

    def test_target_may_be_a_bound_project_and_must_exist(self):
        self.cli("project", "bind", "atlas", self.repo)
        self.writer([fenced()])
        out, _ = self.cli("wish", "atlas", "Calm the page", "--preview")
        saved, template, _ = self.saved()
        self.assertEqual((template["project"], template["repository"], saved["project"]), ("atlas", str(self.repo.resolve()), "atlas"))
        self.assertIn("(project atlas)", out)
        self.assertEqual(validate(template, store=self.store)["project"], "atlas")
        _, err = self.cli("wish", "nowhere", "Calm the page", "--preview", code=1)
        self.assertIn("neither a bound project", err)
        _, err = self.cli("wish", self.root / "absent", "Calm the page", "--preview", code=1)
        self.assertIn("neither a bound project", err)

    # Stopping points

    def test_preview_writes_files_and_makes_no_planner_call(self):
        self.writer([fenced()])
        with (patch("nightweaver.cli.plan_goal", side_effect=AssertionError("planner called")),
              patch("nightweaver.cli.invoke", side_effect=AssertionError("planner called")),
              patch("nightweaver.cli.background", side_effect=AssertionError("launched"))):
            out, _ = self.cli("wish", self.repo, "Calm the page", "--preview")
        saved, template, folder = self.saved()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["brief-writer-1.input.txt", "brief-writer-1.jsonl", "brief.json",
                                                                     "objective.txt", "template.json"])
        self.assertEqual(folder.joinpath("objective.txt").read_text(encoding="utf-8"), OBJECTIVE + "\n")
        self.assertEqual(saved["wish"], "Calm the page")
        self.assertEqual(saved["acceptance"], ACCEPTANCE)
        self.assertTrue(saved["wish_id"] and saved["created"] and saved["roster"] == "default")
        self.assertIn("Preview only", out)
        self.assertNotIn("nightweaver goal", out)
        self.no_runs()

    def test_default_stops_with_the_exact_launch_commands_and_the_template_launches(self):
        self.writer([fenced()])
        out, _ = self.cli("wish", self.repo, "Calm the page", "--waves", "4")
        self.no_runs()
        _, template, folder = self.saved()
        for text in (f"Wish {folder.name}: Atlas teal redesign", "Objective:", "  1. styles.css defines", "Visual goal: yes",
                     "Conflicts resolved:", "dark with a light print stylesheet", "Checks: Python unittest tests", "Roster (default):",
                     "worker    codex gpt-6.2-astra xhigh", "reviewer  claude claude-opus-5-5 xhigh", "planner   deepseek deepseek-v4-pro max",
                     "Pursuit: up to 4 waves of 2 milestones, target score 88, stop after 2 flat judgments",
                     "Limits: 80 model calls, 6 h, 3 attempts per milestone, 30 min per call", "Brief writer: command (CLI default model), 1 attempt",
                     f"Raw brief-writer log: {folder / 'brief-writer-1.jsonl'}"):
            self.assertIn(text, out)
        template_path, objective_path = (str(folder / name).replace("'", "''") for name in ("template.json", "objective.txt"))
        goal_line = next(line for line in out.splitlines() if " goal " in line)
        self.assertEqual(goal_line.strip(), f"nightweaver --home '{str(self.store.root)}' goal '{template_path}' "
                                             f"(Get-Content -Raw -Encoding utf8 '{objective_path}') --planner deepseek --model deepseek-v4-pro "
                                             "--effort max --start")
        again = next(line for line in out.splitlines() if " wish " in line)
        self.assertTrue(again.strip().endswith("--roster default --waves 4 --start"), again)
        self.assertIn(f"'{self.repo}' 'Calm the page'", again)
        # What `goal` receives from that command is exactly the saved template and objective.
        plan = self.root / "planned.json"
        plan.write_text(json.dumps({"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}), encoding="utf-8")
        launched, _ = self.cli("goal", folder / "template.json", folder.joinpath("objective.txt").read_text(encoding="utf-8"),
                               "--plan-from", plan, "--enqueue")
        spec = self.store.get(json.loads(launched)["run_id"])["spec"]
        self.assertEqual((spec["objective"], spec["acceptance"], spec["pursue"]["criteria"]), (OBJECTIVE, ACCEPTANCE, ACCEPTANCE))

    def test_start_hands_the_template_to_the_goal_planning_path(self):
        self.writer([fenced()])
        planner = self.root / "planner.json"
        planner.write_text(json.dumps([{"milestones": [{"title": "Restyle", "task": "Apply the teal palette"}]}]), encoding="utf-8")
        route = {"kind": "command", "argv": [sys.executable, HELPER, "script", str(planner)]}
        with (patch("nightweaver.cli.planner_route", return_value=route) as chosen, patch("nightweaver.cli.background", return_value=4242) as spawn):
            out, _ = self.cli("wish", self.repo, "Calm the page", "--start", "--waves", "2")
        args = chosen.call_args.args[0]
        self.assertEqual((args.planner, args.model, args.effort, args.start, args.enqueue, args.profile_dir),
                         ("deepseek", "deepseek-v4-pro", "max", True, False, None))
        run_id = re.search(r"^Run id: (\S+)$", out, re.M).group(1)
        self.assertEqual(json.loads(out[out.index("{"):])["runner_pid"], 4242)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1:], (run_id, False))
        spec = self.store.get(run_id)["spec"]
        self.assertEqual((spec["objective"], spec["acceptance"], spec["title"]), (OBJECTIVE, ACCEPTANCE, "Atlas teal redesign"))
        self.assertEqual(spec["pursue"]["criteria"], ACCEPTANCE)
        self.assertEqual((spec["pursue"]["max_waves"], spec["pursue"]["wave_size"], spec["pursue"]["target_score"]), (2, 2, 88))
        self.assertEqual([g["name"] for g in spec["milestones"][0]["gates"]], ["Python unittest tests"])
        self.assertEqual([m["title"] for m in spec["milestones"]], ["Restyle"])
        self.assertEqual(spec["reviewer"]["model"], "claude-opus-5-5")
        self.assertEqual(spec["milestones"][0]["worker"], {"kind": "codex", "model": "gpt-6.2-astra", "effort": "xhigh"})
        prompt = self.prompts("planner")[0]
        for text in (OBJECTIVE, ACCEPTANCE[0], "Python unittest tests", "Run limits"):
            self.assertIn(text, prompt)
        self.assertEqual(spec["expected_base"], git(self.repo, "rev-parse", "HEAD"))

    def test_a_failing_planner_keeps_the_saved_brief_and_registers_nothing(self):
        self.writer([fenced()])
        failing = {"kind": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}
        with patch("nightweaver.cli.planner_route", return_value=failing), patch("nightweaver.cli.background") as spawn:
            out, err = self.cli("wish", self.repo, "Calm the page", "--start", code=1)
        self.assertIn("Planner exited 3", err)
        self.assertIn("stay saved in", err)
        self.assertIn("Atlas teal redesign", out)
        spawn.assert_not_called()
        self.no_runs()
        self.assertTrue(self.saved()[2].joinpath("template.json").is_file())

    def test_start_can_run_under_the_crash_supervisor(self):
        self.writer([fenced()])
        planner = self.root / "planner.json"
        planner.write_text(json.dumps([{"milestones": [{"title": "Restyle", "task": "Apply the palette"}]}]), encoding="utf-8")
        route = {"kind": "command", "argv": [sys.executable, HELPER, "script", str(planner)]}
        with patch("nightweaver.cli.planner_route", return_value=route), patch("nightweaver.cli.background", return_value=77) as spawn:
            out, _ = self.cli("wish", self.repo, "Calm the page", "--start", "--guard-restarts", "2")
        run_id = re.search(r"^Run id: (\S+)$", out, re.M).group(1)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1:], (run_id, False, 2))
        self.assertEqual(self.store.get(run_id)["state"]["guard_limit"], 2)

    def test_start_runs_to_done_with_scripted_routes(self):
        self.writer([fenced(brief(visual=False))])
        planner, judge = self.root / "planner.json", self.root / "judge.json"
        planner.write_text(json.dumps([{"milestones": [{"title": "Deliver", "task": "Write result.txt"}]}]), encoding="utf-8")
        judge.write_text(json.dumps([judged()]), encoding="utf-8")
        scripted = lambda path: {"kind": "command", "argv": [sys.executable, HELPER, "script", str(path)]}
        wishing.ROUTE_OVERRIDES.update(worker={"kind": "command", "argv": fixtures.fixture("good")},
                                       reviewer={"kind": "command", "argv": fixtures.fixture("review")},
                                       judge=scripted(judge), planner=scripted(planner))
        with patch("nightweaver.cli.planner_route", return_value=scripted(planner)):
            out, _ = self.cli("wish", self.repo, "Make the delivery ready", "--start", "--wait")
        run_id = re.search(r"^Run id: (\S+)$", out, re.M).group(1)
        self.assertEqual(json.loads(out[out.index("{"):])["phase"], "done")
        row = self.store.get(run_id)
        self.assertEqual(row["state"]["phase"], "done")
        self.assertEqual(row["spec"]["objective"], OBJECTIVE)
        kinds = [e["kind"] for e in self.store.events(run_id, limit=None)]
        self.assertIn("goal_met", kinds)
        self.assertIn(ACCEPTANCE[1], self.prompts("judge")[0])
        self.assertEqual(git(self.repo, "show", f"nightweaver/{run_id}:result.txt"), "ready")


class WishDefaultsTests(unittest.TestCase):
    def test_the_owners_roster_and_probe_are_the_defaults(self):
        opus = {"kind": "claude", "model": "claude-opus-5-5", "effort": "xhigh"}
        self.assertEqual(wishing.roster("default"), {"worker": {"kind": "codex", "model": "gpt-6.2-astra", "effort": "xhigh"},
                                                     "reviewer": opus, "judge": opus,
                                                     "planner": {"kind": "deepseek", "model": "deepseek-v4-pro", "effort": "max"}})
        self.assertEqual(wishing.roster("fast"), {"worker": {"kind": "codex"}, "judge": {"kind": "claude"}, "planner": {"kind": "claude"}})
        wishing.roster("default")["judge"]["effort"] = "low"
        self.assertEqual(wishing.roster("default")["judge"], opus, "routes are copies, never shared state")
        self.assertEqual((wishing.OLLAMA_TAGS_URL, wishing.OLLAMA_MODEL, wishing.OLLAMA_TIMEOUT),
                         ("http://127.0.0.1:11435/api/tags", "qwen3.8:27b", 2))
        self.assertEqual(wishing.ROUTE_OVERRIDES, {}, "the test hook is empty unless a test fills it")
        self.assertEqual(wishing.LIMITS, {"max_calls": 80, "max_seconds": 21600})


if __name__ == "__main__":
    unittest.main()
