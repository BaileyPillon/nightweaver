"""Nightweaver 0.19 notifications and served-model logging: opt-in ntfy messages, the decision inbox, quiet hours, the
morning digest, and the model id each provider call reports."""

import base64
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import test_windows as fixtures

from nightweaver import notify
from nightweaver.cli import main
from nightweaver.engine import Run, model_drift
from nightweaver.insights import diagnosis
from nightweaver.providers import canonical_model, same_model, served_models
from nightweaver.report_activity import category
from nightweaver.store import timestamp

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a7d1d8c10000000049454e44ae426082")
NIGHT = datetime(2026, 11, 22, 2, 0).astimezone()
NOON = datetime(2026, 11, 21, 12, 0).astimezone()


class FakeNtfy:
    """A loopback ntfy server that records every request and answers polls from a scripted list."""

    def __init__(self):
        self.requests, self.replies, self.status, self.redirect = [], [], 200, False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def record(self):
                length = int(self.headers.get("Content-Length") or 0)
                parts = urlsplit(self.path)
                outer.requests.append({"method": self.command, "path": parts.path, "query": parse_qs(parts.query),
                                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(length)})

            def answer(self, body=b'{"id":"fake"}'):
                self.send_response(301 if outer.redirect else outer.status)
                if outer.redirect:
                    self.send_header("Location", outer.url + "/elsewhere")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.record()
                self.answer()

            do_PUT = do_POST

            def do_GET(self):
                self.record()
                self.answer("".join(json.dumps(item) + "\n" for item in outer.replies).encode() if outer.status == 200 else b"")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def of(self, method=None):
        notify.flush()
        return [r for r in self.requests if method in (None, r["method"])]


def at(moment):
    return patch("nightweaver.notify.clock", return_value=moment)


class Base(unittest.TestCase):
    configured = fixtures.WindowsRunTest.configured

    def setUp(self):
        fixtures.WindowsRunTest.setUp(self)
        self.ntfy = FakeNtfy()
        self.addCleanup(self.ntfy.close)
        notify.forget()
        self.addCleanup(notify.forget)

    def enable(self, **options):
        """Configure through the real setup, so the tests read what the command writes. Quiet hours are off unless asked for."""
        options.setdefault("quiet_hours", "")
        result = notify.setup(self.store, server=self.ntfy.url, **options)
        self.topic, self.reply = result["topic"], result["reply_topic"]
        return result

    def run_row(self, **extra):
        return Run(self.store, self.configured(fixtures.fixture("good"), **extra))

    def cli(self, *args, code=0):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.dict(os.environ):
            actual = main(["--home", str(self.store.root), "notify", *map(str, args)])
        self.assertEqual(actual, code, err.getvalue() + out.getvalue())
        return out.getvalue(), err.getvalue()

    def events(self, run, *kinds):
        return self.store.events(run, limit=None, kinds=kinds)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class SetupAndSilence(Base):
    def test_setup_writes_random_topics_keeps_other_settings_and_sends_nothing(self):
        (self.store.root / "providers.json").write_text(json.dumps({"deepseek": {"api_key_file": "key.txt"}}), encoding="utf-8")
        text, _ = self.cli("setup", "--server", self.ntfy.url)
        settings = json.loads((self.store.root / "providers.json").read_text(encoding="utf-8"))
        block = settings["notify"]["ntfy"]
        self.assertEqual(settings["deepseek"], {"api_key_file": "key.txt"})
        self.assertEqual((block["server"], block["events"], block["quiet_hours"], block["max_per_night"], block["images"]),
                         (self.ntfy.url, list(notify.DEFAULT_EVENTS), "23:00-07:00", 3, True))
        for name in ("topic", "reply_topic"):
            self.assertGreaterEqual(len(block[name]), 24)
            self.assertRegex(block[name], r"^[A-Za-z0-9_-]+$")
        self.assertNotEqual(block["topic"], block["reply_topic"])
        self.assertIn(f"{self.ntfy.url}/{block['topic']}", text)
        self.assertIn("ntfy app", text)
        self.assertIn("Anyone who knows a topic name can read it", text)
        self.assertEqual(self.ntfy.of(), [], "setup must not contact the server")
        self.assertTrue(notify.configured(self.store))

    def test_a_second_setup_keeps_the_topics_until_rotated(self):
        first = notify.setup(self.store, server=self.ntfy.url)
        again = notify.setup(self.store, events=["blocked"], max_per_night=1)
        self.assertEqual((again["topic"], again["reply_topic"], again["events"], again["new_topics"]), (first["topic"], first["reply_topic"], ["blocked"], False))
        rotated = notify.setup(self.store, rotate=True)
        self.assertNotIn(rotated["topic"], (first["topic"], first["reply_topic"]))
        self.assertTrue(rotated["new_topics"])

    def test_setup_rejects_bad_input_without_writing(self):
        for args in (["--events", "nonsense"], ["--quiet-hours", "late"], ["--server", "ftp://x"], ["--max-per-night", "-1"],
                     ["--server", "https://user:pw@host.example"]):
            with self.subTest(args):
                self.cli("setup", *args, code=1)
        self.assertFalse((self.store.root / "providers.json").exists())

    def test_events_and_quiet_hours_options(self):
        self.cli("setup", "--server", self.ntfy.url, "--events", "blocked,goal_met", "model_mismatch", "--quiet-hours", "", "--no-images")
        block = json.loads((self.store.root / "providers.json").read_text(encoding="utf-8"))["notify"]["ntfy"]
        self.assertEqual((block["events"], block["quiet_hours"], block["images"]), (["blocked", "goal_met", "model_mismatch"], "", False))
        self.assertIsNone(notify._config(self.store)["quiet"])

    def test_nothing_is_ever_sent_when_not_configured(self):
        run = self.run_row()
        with patch("nightweaver.notify.threading.Thread", side_effect=AssertionError("no thread when unconfigured")):
            for kind in notify.DEFAULT_EVENTS:
                run.set(kind, {"issue": "x", "score": 90})
        self.assertFalse(notify.configured(self.store))
        self.assertFalse(notify.send(self.store, "t", "m"))
        self.assertEqual(notify.ask(self.store, run.id, "k", "q?", ["a"])["sent"], False)
        self.assertIsNone(notify.poll_answer(self.store, run.id, "k", timestamp()))
        self.assertEqual(self.ntfy.of(), [])
        self.cli("test", code=1)
        self.cli("digest", code=1)
        self.assertEqual(self.ntfy.of(), [])

    def test_a_broken_configuration_means_off_never_an_error(self):
        for content in ("{not json", json.dumps({"notify": {"ntfy": {"server": "ftp://x", "topic": "abc"}}}), json.dumps({"notify": []}),
                        json.dumps({"notify": {"ntfy": {"server": self.ntfy.url}}})):
            with self.subTest(content):
                (self.store.root / "providers.json").write_text(content, encoding="utf-8")
                notify.forget()
                self.assertFalse(notify.configured(self.store))
                self.assertFalse(notify.send(self.store, "t", "m"))
                self.run_row().set("blocked", {"issue": "x"})
        self.assertEqual(self.ntfy.of(), [])

    def test_status_masks_topics_and_off_removes_the_block(self):
        self.enable()
        out, _ = self.cli("status")
        status = json.loads(out)
        self.assertTrue(status["configured"])
        self.assertNotIn(self.topic, out)
        self.assertNotIn(self.reply, out)
        self.assertIn(self.topic[:4] + "...", status["topic"])
        self.assertEqual((status["server"], status["max_per_night"], status["held_for_digest"]), (self.ntfy.url, 3, 0))
        removed = json.loads(self.cli("off")[0])
        self.assertEqual(removed, {"configured": False, "removed": True})
        self.assertFalse(notify.configured(self.store))
        self.assertEqual(json.loads(self.cli("status")[0])["configured"], False)
        self.assertFalse(json.loads(self.cli("off")[0])["removed"])
        self.assertEqual(self.ntfy.of(), [])

    def test_test_command_sends_exactly_one_message(self):
        self.enable()
        self.assertEqual(json.loads(self.cli("test")[0]), {"sent": True})
        (request,) = self.ntfy.of()
        self.assertEqual((request["method"], request["path"], request["headers"]["title"]), ("POST", "/" + self.topic, "Nightweaver test"))
        self.assertIn(b"notifications work", request["body"])
        self.ntfy.status = 500
        out, _ = self.cli("test", code=3)
        self.assertEqual(json.loads(out), {"sent": False, "issue": "HTTP 500"})
        self.assertNotIn(self.topic, out)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class Hook(Base):
    def test_configured_runs_send_for_chosen_kinds_with_the_right_headers(self):
        self.enable()
        run = self.run_row(title="Weaver build")
        run.set("goal_met", {"wave": 2, "score": 91, "commit": "abc"}, following_event=[("run_completed", {"branch": "refs/heads/nightweaver/x", "commit": "abc"})])
        run.set("attempt_rejected", {"issue": "no"})
        run.set("process_heartbeat", {})
        run.set("milestone_accepted", {"step": 1})
        (request,) = self.ntfy.of("POST")
        self.assertEqual(request["path"], "/" + self.topic)
        head = request["headers"]
        self.assertEqual((head["title"], head["priority"], head["tags"]), ("Weaver build", "3", "white_check_mark"))
        self.assertTrue(head["content-type"].startswith("text/plain"))
        body = request["body"].decode("utf-8")
        self.assertEqual(body, f"Goal met in wave 2 with judge score 91/100. Result on nightweaver/x.\nRun {run.id}",
                         "a met goal and its completion are one message")

    def test_every_default_kind_has_a_one_line_summary_and_unlisted_kinds_stay_silent(self):
        self.enable()
        run = self.run_row()
        cases = {"run_completed": ({"branch": "refs/heads/nightweaver/y", "commit": "c"}, "Completed. Result on nightweaver/y."),
                 "pursuit_stopped": ({"reason": "plateau", "best_score": 70, "waves": 4}, "Stopped: scores stopped improving after 4 waves, best judge score 70/100."),
                 "blocked": ({"issue": "Gate failed\nsecond line", "cause": "Milestone 1 failed its check three times"}, "Blocked: Milestone 1 failed its check three times."),
                 "provider_unavailable": ({"provider": "deepseek", "role": "worker", "reason": "credentials_or_balance"}, "deepseek worker is unavailable (credentials or balance); the run is blocked."),
                 "provider_parked": ({"provider": "codex", "role": "worker", "until": "2026-11-22T05:30:00+00:00"}, "codex usage limit reached; waiting until "),
                 "model_mismatch": ({"provider": "deepseek", "role": "worker", "requested": "deepseek-v4-pro", "served": "deepseek-v4-flash"}, "deepseek worker asked for deepseek-v4-pro but was served deepseek-v4-flash.")}
        for kind, (details, _) in cases.items():
            run.set(kind, details)
        sent = {r["headers"]["tags"]: r for r in self.ntfy.of("POST")}
        self.assertEqual(len(self.ntfy.of("POST")), len(cases))
        bodies = [r["body"].decode() for r in self.ntfy.of("POST")]
        for kind, (_, expected) in cases.items():
            with self.subTest(kind):
                self.assertTrue(any(b.startswith(expected) and b.endswith(f"\nRun {run.id}") for b in bodies), (expected, bodies))
        self.assertEqual({r["headers"]["priority"] for r in self.ntfy.of("POST") if "warning" in r["headers"]["tags"] or "no_entry" in r["headers"]["tags"]}, {"4"})
        self.assertEqual(sent["hourglass_flowing_sand"]["headers"]["priority"], "2")

    def test_a_custom_event_list_is_obeyed_and_judge_score_rides_along(self):
        self.enable(events=["blocked", "wave_completed"])
        run = self.run_row()
        self.store.transition(run.id, "goal_judged", {}, best_score=64)
        run.set("run_completed", {"branch": "refs/heads/nightweaver/x"})
        run.set("goal_met", {"wave": 1, "score": 90})
        run.set("wave_completed", {"wave": 1, "commit": "c", "branch": "refs/heads/nightweaver/x"}, best_score=64)
        run.set("blocked", {"issue": "Review kept asking for changes"}, phase="blocked", best_score=64)
        bodies = [r["body"].decode() for r in self.ntfy.of("POST")]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(bodies[0].startswith("Wave completed. Score 64/100."), bodies)
        self.assertTrue(bodies[1].startswith("Blocked: Review kept asking for changes. Score 64/100."), bodies)

    def test_non_ascii_titles_are_encoded_for_the_header(self):
        self.enable()
        run = self.run_row(title="Café Zürich \u2728")
        run.set("blocked", {"issue": "Pause naïve"})
        (request,) = self.ntfy.of("POST")
        title = request["headers"]["title"]
        self.assertTrue(title.startswith("=?UTF-8?B?") and title.endswith("?="))
        self.assertEqual(base64.b64decode(title[10:-2]).decode("utf-8"), "Café Zürich \u2728")
        self.assertIn("Pause naïve", request["body"].decode("utf-8"))

    def test_messages_are_short_summaries_without_secrets(self):
        self.enable()
        run = self.run_row()
        issue = "Gate crashed with api_key=sk-abcdefghijklmnop1234 in the environment\nTraceback line two\n" + "x" * 5000
        run.set("blocked", {"issue": issue})
        run.set("provider_unavailable", {"issue": "Bearer abcdef1234567890", "provider_message": "token sk-zzzzzzzzzzzzzzzzzz", "provider": "claude",
                                         "role": "judge", "reason": "authentication"})
        bodies = [r["body"].decode() for r in self.ntfy.of("POST")]
        joined = "\n".join(bodies)
        for leak in ("sk-abcdefghijklmnop1234", "Traceback", "xxxxxxxxxx", "Bearer abcdef", "sk-zzzz", "provider_message"):
            self.assertNotIn(leak, joined)
        self.assertIn("[REDACTED]", bodies[0])
        self.assertTrue(all(len(b) < 260 for b in bodies), [len(b) for b in bodies])

    def test_a_real_run_that_completes_sends_one_message(self):
        self.enable(quiet_hours="")
        run = self.configured(fixtures.fixture("good"), title="End to end")
        self.assertEqual(Run(self.store, run).run(), "done")
        (request,) = self.ntfy.of("POST")
        self.assertEqual(request["headers"]["title"], "End to end")
        self.assertEqual(request["body"].decode(), f"Completed. Result on nightweaver/{run}.\nRun {run}")

    def test_the_runner_never_waits_for_a_slow_server(self):
        self.enable()
        gate = threading.Event()
        started = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            gate.wait(10)
            return True, None

        run = self.run_row()
        with patch("nightweaver.notify._send", side_effect=slow):
            began = datetime.now()
            run.set("blocked", {"issue": "x"})
            self.assertLess((datetime.now() - began).total_seconds(), 2)
            self.assertTrue(started.wait(5), "the send runs on its own thread")
            gate.set()
            notify.flush()

    def test_failures_are_swallowed_and_recorded_once_without_looping(self):
        self.enable()
        settings = json.loads((self.store.root / "providers.json").read_text(encoding="utf-8"))
        settings["notify"]["ntfy"]["events"] += ["notify_failed"]
        (self.store.root / "providers.json").write_text(json.dumps(settings), encoding="utf-8")
        self.ntfy.status = 500
        run = self.run_row()
        run.set("blocked", {"issue": "first"}, phase="blocked")
        run.set("provider_unavailable", {"provider": "codex", "role": "worker", "reason": "usage_limit"})
        notify.flush()
        run.set("notify_failed", {"issue": "explicit"})
        posts = self.ntfy.of("POST")
        self.assertEqual(len(posts), 2, "both real events were attempted and notify_failed was never sent")
        failed = self.events(run.id, "notify_failed")
        self.assertEqual(len(failed), 2, "one recorded by the hook plus the explicit one")
        hook = [e for e in failed if e["details"].get("issue", "").startswith("Could not deliver")]
        self.assertEqual(len(hook), 1)
        self.assertEqual(hook[0]["details"], {"event": "blocked", "issue": "Could not deliver a notification (HTTP 500)"})
        self.assertNotIn(self.topic, json.dumps(failed))
        self.assertEqual(self.store.get(run.id)["state"]["phase"], "blocked", "a failed notification changes nothing about the run")

    def test_an_unreachable_server_does_not_raise_or_stall(self):
        self.enable()
        self.ntfy.close()
        started = datetime.now()
        self.assertFalse(notify.send(self.store, "t", "m", images=[], actions=[{"action": "view", "label": "x", "url": "http://x.invalid"}]))
        run = self.run_row()
        run.set("blocked", {"issue": "x"})
        notify.flush()
        self.assertLess((datetime.now() - started).total_seconds(), 12)
        (event,) = self.events(run.id, "notify_failed")
        self.assertNotIn(self.topic, json.dumps(event))
        self.assertEqual(event["details"]["event"], "blocked")

    def test_a_redirect_is_a_failure_not_a_silent_success(self):
        self.enable()
        self.ntfy.redirect = True
        self.assertFalse(notify.send(self.store, "t", "m"))
        self.assertEqual([r["method"] for r in self.ntfy.of()], ["POST"], "the redirect is not followed")
        run = self.run_row()
        run.set("blocked", {"issue": "x"})
        notify.flush()
        self.assertEqual(self.events(run.id, "notify_failed")[0]["details"]["issue"], "Could not deliver a notification (HTTP 301)")

    def test_send_never_raises(self):
        self.enable()
        with patch("nightweaver.notify._send", side_effect=RuntimeError("boom")):
            self.assertFalse(notify.send(self.store, "t", "m"))
        self.assertFalse(notify.send(self.store, "t", "m", actions=[{"action": "view"}, "view, x, http://x"]), "mixed action styles are refused, not raised")
        self.assertFalse(notify.send(self.store, "t", "m", priority="loud"))
        self.assertEqual(self.ntfy.of(), [])


@unittest.skipUnless(os.name == "nt", "Windows controller")
class QuietHoursAndDigest(Base):
    def test_quiet_hours_cap_the_night_and_hold_the_rest_for_the_digest(self):
        self.enable(max_per_night=2, quiet_hours="23:00-07:00")
        run = self.run_row(title="Night shift")
        with at(NIGHT):
            for number in range(4):
                run.set("blocked", {"cause": f"Stop number {number}"})
                notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 2)
        state = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["nights"], {"2026-11-21": 2}, "a night that begins before midnight keeps its start date")
        self.assertEqual([(h["run_id"], h["event"], h["summary"]) for h in state["held"]],
                         [(run.id, "blocked", "Blocked: Stop number 2."), (run.id, "blocked", "Blocked: Stop number 3.")])
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 2)
        with at(NOON):
            for number in range(5):
                run.set("blocked", {"cause": f"Daytime {number}"})
            notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 7, "daytime is unlimited")

    def test_held_messages_are_redacted_on_disk(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        with at(NIGHT):
            self.run_row().set("blocked", {"issue": "Crashed with api_key=sk-abcdefghijklmnop1234 set"})
            notify.flush()
        text = (self.store.root / "notify-state.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        self.assertIn("api_key=[REDACTED]", text)

    def test_the_allowance_counts_one_night_across_midnight_and_resets_the_next(self):
        self.enable(max_per_night=1, quiet_hours="23:00-07:00")
        run = self.run_row()
        before = datetime(2026, 11, 21, 23, 30).astimezone()
        after = datetime(2026, 11, 22, 1, 0).astimezone()
        later = datetime(2026, 11, 23, 0, 30).astimezone()
        for moment, expected in ((before, 1), (after, 1), (later, 2)):
            with at(moment):
                run.set("blocked", {"cause": "again"})
                notify.flush()
            self.assertEqual(len(self.ntfy.of("POST")), expected, moment)
        self.assertEqual(len(json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"]), 1)

    def test_zero_allowance_holds_everything_overnight_and_no_quiet_hours_holds_nothing(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        run = self.run_row()
        with at(NIGHT):
            run.set("blocked", {"cause": "silent"})
            notify.flush()
        self.assertEqual(self.ntfy.of("POST"), [])
        notify.setup(self.store, quiet_hours="")
        with at(NIGHT):
            run.set("blocked", {"cause": "loud"})
            notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 1)

    def build_runs(self):
        done = self.configured(fixtures.fixture("good"), title="Alpha done")
        self.store.transition(done, "run_completed", {}, phase="done", accepted="f" * 40, best_score=92)
        stopped = self.configured(fixtures.fixture("good"), title="Beta stopped")
        self.store.transition(stopped, "pursuit_stopped", {"reason": "plateau", "best_score": 71}, phase="blocked", best_score=71,
                              accepted="e" * 40, last_error="Goal not met after 3 waves")
        blocked = self.configured(fixtures.fixture("good"), title="Gamma blocked")
        self.store.transition(blocked, "provider_unavailable", {"issue": "x"}, phase="blocked",
                              last_error="deepseek reported a billing problem api_key=sk-abcdefghijklmnop1234\nsecond line")
        parked = self.configured(fixtures.fixture("good"), title="Delta parked")
        self.store.transition(parked, "provider_parked", {}, parks=1, retry_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
        running = self.configured(fixtures.fixture("good"), title="Epsilon running")
        old = self.configured(fixtures.fixture("good"), title="Zeta old")
        self.store.transition(old, "run_completed", {}, phase="done")
        with closing(sqlite3.connect(self.store.root / "runs.sqlite3")) as db, db:
            db.execute("UPDATE runs SET updated='2020-01-01T00:00:00.000+00:00' WHERE id=?", (old,))
        return {"done": done, "stopped": stopped, "blocked": blocked, "parked": parked, "running": running, "old": old}

    def test_digest_groups_runs_scores_branches_and_held_messages(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        runs = self.build_runs()
        with at(NIGHT):
            self.run_row(title="Held run").set("model_mismatch", {"provider": "deepseek", "role": "worker", "requested": "deepseek-v4-pro", "served": "deepseek-v4-flash"})
            notify.flush()
        report = notify.digest(self.store, 12)
        self.assertEqual(report["title"], "Nightweaver digest: 1 done, 1 stopped, 1 blocked, 1 parked, 2 running")
        self.assertEqual(report["counts"], {"completed": 1, "stopped": 1, "blocked": 1, "parked": 1, "running": 2})
        message = report["message"]
        self.assertIn(f"Completed\n- Alpha done ({runs['done']}): best 92/100 · branch nightweaver/{runs['done']}", message)
        self.assertIn(f"Stopped\n- Beta stopped ({runs['stopped']}): scores stopped improving · best 71/100 · branch nightweaver/{runs['stopped']}", message)
        self.assertIn(f"- Gamma blocked ({runs['blocked']}): deepseek reported a billing problem api_key=[REDACTED]", message)
        self.assertNotIn("second line", message)
        self.assertNotIn("sk-abcdefghijklmnop1234", message)
        self.assertRegex(message, rf"- Delta parked \({runs['parked']}\): waiting for a usage limit until \d\d:\d\d")
        self.assertIn(f"- Epsilon running ({runs['running']}): queued, milestone 1 of 1", message)
        self.assertNotIn("Zeta old", message)
        self.assertIn("Held while quiet hours were on (1)", message)
        self.assertIn("- Held run: deepseek worker asked for deepseek-v4-pro but was served deepseek-v4-flash.", message)
        narrow = notify.digest(self.store, 12, now=datetime.now(timezone.utc) + timedelta(days=2))
        self.assertEqual(narrow["counts"], {"completed": 0, "stopped": 0, "blocked": 0, "parked": 0, "running": 0})
        self.assertEqual(narrow["title"], "Nightweaver digest: no runs changed")

    def test_a_garbled_retry_time_never_breaks_the_digest(self):
        self.enable()
        run = self.configured(fixtures.fixture("good"), title="Odd retry")
        self.store.transition(run, "provider_parked", {}, parks=1, retry_at="not a time")
        self.store.transition(self.configured(fixtures.fixture("good"), title="Naive retry"), "provider_parked", {}, parks=1, retry_at="2099-01-01T00:00:00")
        self.assertEqual(notify.digest(self.store, 12)["counts"], {"completed": 0, "stopped": 0, "blocked": 0, "parked": 0, "running": 2})

    def test_print_leaves_the_hold_alone_and_sending_clears_it(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        self.build_runs()
        with at(NIGHT):
            self.run_row().set("blocked", {"cause": "Held"})
            notify.flush()
        out, _ = self.cli("digest", "--print")
        self.assertTrue(out.startswith("Nightweaver digest: 1 done"), out)
        self.assertIn("Held while quiet hours were on (1)", out)
        self.assertEqual(self.ntfy.of("POST"), [], "--print sends nothing")
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 1)
        result = json.loads(self.cli("digest", "--hours", "6")[0])
        self.assertTrue(result["sent"])
        self.assertEqual(result["held"], 1)
        (request,) = self.ntfy.of("POST")
        self.assertEqual((request["path"], request["headers"]["title"]), ("/" + self.topic, result["title"]))
        self.assertIn("Held while quiet hours were on (1)", request["body"].decode())
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 0)

    def test_a_failed_digest_keeps_the_hold_and_exits_nonzero(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        with at(NIGHT):
            self.run_row().set("blocked", {"cause": "Held"})
            notify.flush()
        self.ntfy.status = 503
        out, _ = self.cli("digest", code=3)
        self.assertFalse(json.loads(out)["sent"])
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 1)

    def test_print_works_without_setup_and_a_quiet_home_says_so(self):
        out, _ = self.cli("digest", "--print")
        self.assertIn("Nightweaver digest: no runs changed", out)
        self.cli("digest", "--hours", "0", "--print", code=1)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class DecisionInbox(Base):
    def test_ask_builds_one_http_button_per_option(self):
        self.enable()
        run = self.run_row(title="Needs a call")
        asked = notify.ask(self.store, run.id, "ship-it", "Ship wave 2?", ["Ship", "Hold", "Revise, then ship"])
        (request,) = self.ntfy.of("POST")
        self.assertEqual(asked["options"], ["Ship", "Hold", "Revise, then ship"])
        self.assertTrue(asked["sent"])
        self.assertEqual(datetime.fromisoformat(asked["asked_at"]).tzinfo, timezone.utc)
        self.assertEqual((request["path"], request["headers"]["title"]), ("/" + self.topic, "Needs a call: decision needed"))
        self.assertEqual(request["headers"]["priority"], "4")
        self.assertEqual(request["body"].decode(), f"Ship wave 2?\nRun {run.id}")
        actions = json.loads(request["headers"]["actions"])
        self.assertEqual(actions, [{"action": "http", "label": option, "url": f"{self.ntfy.url}/{self.reply}", "method": "POST",
                                    "body": f"{run.id}:ship-it:{option}", "clear": True} for option in asked["options"]])

    def test_ask_offers_at_most_three_buttons_and_checks_its_arguments(self):
        self.enable()
        run = self.run_row()
        asked = notify.ask(self.store, run.id, "k", "q", ["a", "b", "c", "d"])
        self.assertEqual(asked["options"], ["a", "b", "c"])
        self.assertEqual(len(json.loads(self.ntfy.of("POST")[0]["headers"]["actions"])), 3)
        for bad in ((run.id, "has:colon", ["a"]), (run.id, "", ["a"]), (run.id, "k", []), ("not-a-run", "k", ["a"])):
            with self.subTest(bad), self.assertRaises(ValueError):
                notify.ask(self.store, bad[0], bad[1], "q", bad[2])

    def test_ask_attaches_images_only_when_asked(self):
        self.enable()
        run = self.run_row()
        shot = self.root / "shot.png"
        shot.write_bytes(PNG)
        notify.ask(self.store, run.id, "k", "q", ["a"], images=[shot])
        self.assertEqual([r["method"] for r in self.ntfy.of()], ["POST", "PUT"])

    def test_poll_answer_matches_run_key_and_time_and_ignores_the_rest(self):
        self.enable()
        run = "0123456789ab"
        since = datetime(2026, 11, 21, 20, 0, 5, 700000, tzinfo=timezone.utc)
        floor = int(since.timestamp())

        def message(text, time=floor + 30, event="message"):
            return {"id": "m", "time": time, "event": event, "topic": self.reply, "message": text}

        self.ntfy.replies = [message(f"{run}:other:Ship"), message(f"ffffffffffff:k:Ship"), message("garbage"), message(f"{run}:k:"),
                             message(f"{run}:k:   "), message(f"{run}:k:Early", time=floor - 5), message(f"{run}:k:Ship", event="keepalive"),
                             {"event": "message", "time": "soon", "message": f"{run}:k:Bad"}, {"event": "message", "time": floor + 1, "message": 7},
                             message(f"{run}:k:Hold: with colon"), message(f"{run}:k:Ship")]
        self.assertEqual(notify.poll_answer(self.store, run, "k", since.isoformat()), "Hold: with colon", "the first match after the question, colons kept")
        (request,) = self.ntfy.of("GET")
        self.assertEqual((request["path"], request["query"]), ("/" + self.reply + "/json", {"poll": ["1"], "since": [str(floor)]}))
        self.assertEqual(notify.poll_answer(self.store, run, "missing", since.isoformat()), None)
        self.assertEqual(notify.poll_answer(self.store, "ffffffffffff", "other", since.isoformat()), None)
        self.assertEqual(notify.poll_answer(self.store, "ffffffffffff", "k", since.isoformat()), "Ship")

    def test_poll_answer_survives_junk_and_failures(self):
        self.enable()
        run = "0123456789ab"
        self.assertIsNone(notify.poll_answer(self.store, run, "k", "not a time"))
        self.assertIsNone(notify.poll_answer(self.store, run, "k", "2026-11-21T20:00:00"), "an empty topic has no answer")
        self.ntfy.replies = [{"event": "message", "time": 9999999999, "message": f"{run}:k:Late"}]
        self.assertEqual(notify.poll_answer(self.store, run, "k", "2026-11-21T20:00:00"), "Late", "a naive time is read as UTC")
        self.ntfy.status = 500
        self.assertIsNone(notify.poll_answer(self.store, run, "k", timestamp()))
        self.ntfy.close()
        self.assertIsNone(notify.poll_answer(self.store, run, "k", timestamp()))

    def test_the_round_trip(self):
        self.enable()
        run = self.run_row()
        asked = notify.ask(self.store, run.id, "pick", "Which?", ["One", "Two"])
        self.assertIsNone(notify.poll_answer(self.store, run.id, "pick", asked["asked_at"]))
        tapped = json.loads(self.ntfy.of("POST")[0]["headers"]["actions"])[1]["body"]
        self.ntfy.replies = [{"event": "message", "time": int(datetime.now(timezone.utc).timestamp()), "message": tapped}]
        self.assertEqual(notify.poll_answer(self.store, run.id, "pick", asked["asked_at"]), "Two")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class Images(Base):
    def test_images_are_separate_attachment_puts_capped_at_two_and_five_megabytes(self):
        self.enable()
        pictures = []
        for name, data in (("one.png", PNG), ("huge.png", b"\0" * (5 * 1024 * 1024 + 1)), ("two.jpg", b"\xff\xd8\xffJPEG"), ("three.png", PNG)):
            path = self.root / name
            path.write_bytes(data)
            pictures.append(path)
        pictures.insert(1, self.root / "missing.png")
        self.assertTrue(notify.send(self.store, "Shots", "Three screenshots", images=pictures, priority=4, tags=("camera", "warning")))
        post, first, second = self.ntfy.of()
        self.assertEqual([r["method"] for r in (post, first, second)], ["POST", "PUT", "PUT"])
        self.assertEqual((post["headers"]["title"], post["headers"]["priority"], post["headers"]["tags"]), ("Shots", "4", "camera,warning"))
        self.assertEqual(post["body"], b"Three screenshots")
        self.assertEqual((first["headers"]["filename"], first["body"], first["headers"]["content-type"]), ("one.png", PNG, "image/png"))
        self.assertEqual((second["headers"]["filename"], second["body"]), ("two.jpg", b"\xff\xd8\xffJPEG"))
        self.assertEqual({r["path"] for r in (post, first, second)}, {"/" + self.topic})

    def test_images_off_and_failed_messages_send_no_attachments(self):
        self.enable(images=False)
        shot = self.root / "shot.png"
        shot.write_bytes(PNG)
        self.assertTrue(notify.send(self.store, "t", "m", images=[shot]))
        self.assertEqual([r["method"] for r in self.ntfy.of()], ["POST"])
        notify.setup(self.store, images=True)
        self.ntfy.status = 500
        self.assertFalse(notify.send(self.store, "t", "m", images=[shot]))
        self.assertEqual([r["method"] for r in self.ntfy.of()], ["POST", "POST"])


def write(path, *events):
    Path(path).write_text("\n".join(map(line_of, events)) + "\n", encoding="utf-8")


def line_of(event):
    return event if isinstance(event, str) else json.dumps(event)


def uuid7(when):
    stamp = int(when.timestamp() * 1000)
    return str(UUID(int=(stamp << 80) | (7 << 76) | (0xABC << 64) | (2 << 62) | 0x1234567890ABCDE))


def reply(model, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"model": model, "role": "assistant", "content": []}}


class ServedModelParsing(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="Nightweaver served ")))

    def served(self, kind, *events, **config):
        write(self.dir / "call.jsonl", *events)
        return served_models({"kind": kind, **config}, self.dir / "call.jsonl")

    def test_claude_reads_main_thread_responses_not_subagents_or_synthetic_messages(self):
        result = self.served("claude", {"type": "system", "subtype": "init", "model": "claude-opus-5-5"}, reply("claude-opus-5-5"),
                             reply("claude-sonnet-5-5", parent="toolu_1"), reply("<synthetic>"), reply("claude-opus-5-5"), "not json", [1, 2])
        self.assertEqual(result, {"models": ["claude-opus-5-5"], "source": "response"})

    def test_deepseek_reports_the_response_model_and_lists_a_second_id_most_used_first(self):
        result = self.served("deepseek", {"type": "system", "subtype": "init", "model": "deepseek-v4-pro"}, reply("deepseek-v4-flash"),
                             reply("deepseek-v4-pro"), reply("deepseek-v4-pro"))
        self.assertEqual(result, {"models": ["deepseek-v4-pro", "deepseek-v4-flash"], "source": "response"})

    def test_the_init_event_is_only_a_fallback(self):
        result = self.served("claude", {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}, reply("<synthetic>"))
        self.assertEqual(result, {"models": ["claude-sonnet-5-5"], "source": "init"})
        self.assertEqual(self.served("deepseek", {"type": "system", "subtype": "init"}), {"models": [], "source": None})

    def test_gemini_names_its_model_at_init_or_in_the_result_stats(self):
        self.assertEqual(self.served("gemini", {"type": "init", "session_id": "s", "model": "gemini-2.5-pro"}, {"type": "message"}),
                         {"models": ["gemini-2.5-pro"], "source": "init"})
        self.assertEqual(self.served("gemini", {"type": "result", "stats": {"models": {"gemini-2.5-flash": {}}}}),
                         {"models": ["gemini-2.5-flash"], "source": "init"})

    def test_codex_streams_name_no_model_so_the_rollout_is_read(self):
        home = self.dir / "codex home"
        thread = uuid7(datetime.now(timezone.utc))
        folder = home / "sessions" / f"{datetime.now().astimezone():%Y/%m/%d}"
        folder.mkdir(parents=True)
        write(folder / f"rollout-2026-11-21T10-00-00-{thread}.jsonl", {"type": "session_meta", "payload": {"model_provider": "openai"}},
              {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}, {"type": "event_msg", "payload": {"text": "x" * 500}},
              "not json", {"type": "turn_context", "payload": {"model": "gpt-6.2-astra"}}, {"type": "turn_context", "payload": "odd"})
        stream = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
        result = self.served("codex", *stream, profile_dir=str(home))
        self.assertEqual(result, {"models": ["gpt-6.2-astra"], "source": "codex_rollout"}, "the last turn of the thread")
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            self.assertEqual(self.served("codex", *stream)["models"], ["gpt-6.2-astra"], "CODEX_HOME is the default profile")
        self.assertEqual(self.served("codex", *stream, profile_dir=str(self.dir / "elsewhere")), {"models": [], "source": None})
        self.assertEqual(self.served("codex", {"type": "thread.started", "thread_id": "thread-1"}, profile_dir=str(home))["models"], [],
                         "ids that are not UUIDv7 are not searched for")
        self.assertEqual(self.served("codex", {"type": "turn.completed", "usage": {}}, profile_dir=str(home))["models"], [])

    def test_codex_streams_that_do_carry_a_model_win(self):
        self.assertEqual(self.served("codex", {"type": "thread.started", "thread_id": "t", "model": "gpt-9"}),
                         {"models": ["gpt-9"], "source": "stream"})
        self.assertEqual(self.served("codex", {"id": "0", "msg": {"type": "session_configured", "model": "gpt-8"}})["models"], ["gpt-8"])

    def test_other_kinds_and_missing_logs_report_nothing(self):
        for kind in ("command", "ollama"):
            self.assertEqual(self.served(kind, reply("claude-opus-5-5")), {"models": [], "source": None})
        self.assertEqual(served_models({"kind": "claude"}, self.dir / "absent.jsonl"), {"models": [], "source": None})


class Normalization(unittest.TestCase):
    def test_spellings_that_are_the_same_model(self):
        same = [("deepseek-v4-pro", "deepseek-v4-pro"), ("DeepSeek-V4-Pro", "deepseek-v4-pro"), ("deepseek-v4-pro", "deepseek-v4-pro-20261101"),
                ("deepseek-v4-pro-20261101", "deepseek-v4-pro"), ("gpt-6.2-astra", "gpt-6.2-astra-2026-11-01"), ("gpt-6.2-astra", "openai/gpt-6.2-astra"),
                ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"), ("claude-opus-5-5", "claude-opus-5-5-20261101"), ("claude-opus-5-5[1m]", "claude-opus-5-5"),
                ("claude-sonnet-4-20250514", "anthropic.claude-sonnet-4-20250514-v1:0"), ("claude-sonnet-4", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
                ("gpt-4", "gpt-4-0613"), ("claude-sonnet-4.5", "claude-sonnet-4-5"), ("sonnet", "claude-sonnet-5-5"), ("opus[1m]", "claude-opus-5-5"),
                ("haiku", "claude-haiku-4-5-20261001"), ("gemini-2.5-pro", "models/gemini-2.5-pro"), ("gpt-6.2-astra", "gpt-6.2-astra-latest"),
                ("opusplan", "claude-sonnet-5-5"), ("default", "anything"), ("deepseek-chat@20261101", "deepseek-chat")]
        for asked, got in same:
            with self.subTest(asked=asked, got=got):
                self.assertTrue(same_model(asked, got))

    def test_different_models_are_never_merged(self):
        different = [("deepseek-v4-pro", "deepseek-v4-flash"), ("deepseek-v4-pro", "deepseek-flash"), ("gpt-5", "gpt-5-mini"), ("deepseek-v3", "deepseek-v4"),
                     ("gpt-6.2-astra", "gpt-6.1-sol"), ("sonnet", "claude-opus-5-5"), ("opus", "claude-sonnet-5-5"), ("claude-opus-5-5", "claude-opus-5-4"),
                     ("gemini-2.5-pro", "gemini-2.5-flash"), ("deepseek-v4-pro", "deepseek-v4"), ("deepseek-v4", "deepseek-v4-pro")]
        for asked, got in different:
            with self.subTest(asked=asked, got=got):
                self.assertFalse(same_model(asked, got))

    def test_canonical_form(self):
        self.assertEqual(canonical_model("  Anthropic/Claude-Opus-5-5-20261101 "), "claude-opus-5-5")
        self.assertEqual(canonical_model("gpt-6.2-astra"), "gpt-6-2-astra")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ServedModelRecording(Base):
    def settle(self, run, provider, model, *events, role="worker", log=None):
        self.count = getattr(self, "count", 0) + 1
        path = self.root / (log or f"served-{self.count}.jsonl")
        write(path, *events)
        pending = {"call": self.count, "role": role, "provider": provider, "model": model, "started": timestamp(), "log": str(path),
                   "step": 1, "attempt": 1}
        run.finish_call(pending)
        return self.events(run.id, "provider_call_finished")[-1]["details"]

    def test_served_model_is_recorded_and_a_match_is_silent(self):
        run = self.run_row()
        call = self.settle(run, "deepseek", "deepseek-v4-pro", reply("deepseek-v4-pro"), reply("deepseek-v4-pro"))
        self.assertEqual((call["served_model"], call["served_source"], call["model"]), ("deepseek-v4-pro", "response", "deepseek-v4-pro"))
        self.assertNotIn("served_models", call)
        dated = self.settle(run, "claude", "claude-opus-5-5", reply("claude-opus-5-5-20261101"))
        alias = self.settle(run, "claude", "sonnet", reply("claude-sonnet-5-5"))
        self.assertEqual((dated["served_model"], alias["served_model"]), ("claude-opus-5-5-20261101", "claude-sonnet-5-5"))
        self.assertEqual(self.events(run.id, "model_mismatch"), [])
        self.assertNotIn("model_mismatches", self.store.get(run.id)["state"])

    def test_calls_with_nothing_to_read_still_record_the_field(self):
        run = self.run_row()
        for provider, model in (("codex", "gpt-6.2-astra"), ("command", None), ("claude", None)):
            call = self.settle(run, provider, model, {"type": "turn.completed", "usage": {}})
            self.assertIn("served_model", call)
            self.assertIsNone(call["served_model"])
            self.assertNotIn("served_source", call)
        self.assertEqual(self.events(run.id, "model_mismatch"), [])

    def test_a_reroute_is_reported_once_per_route_and_served_id(self):
        run = self.run_row()
        flash = [reply("deepseek-v4-flash")]
        first = self.settle(run, "deepseek", "deepseek-v4-pro", *flash)
        self.settle(run, "deepseek", "deepseek-v4-pro", *flash)
        self.settle(run, "deepseek", "deepseek-v4-pro", *flash)
        self.assertEqual(first["served_model"], "deepseek-v4-flash")
        (event,) = self.events(run.id, "model_mismatch")
        self.assertEqual(event["details"], {"provider": "deepseek", "role": "worker", "requested": "deepseek-v4-pro", "served": "deepseek-v4-flash",
                                            "source": "response", "call": 1,
                                            "issue": "deepseek worker asked for deepseek-v4-pro but was served deepseek-v4-flash"})
        self.settle(run, "deepseek", "deepseek-v4-pro", reply("deepseek-chat"))
        self.settle(run, "deepseek", "deepseek-v4-pro", *flash, role="reviewer")
        self.settle(run, "claude", "claude-opus-5-5", reply("claude-opus-5-4"))
        self.assertEqual([(e["details"]["provider"], e["details"]["role"], e["details"]["served"]) for e in self.events(run.id, "model_mismatch")],
                         [("deepseek", "worker", "deepseek-v4-flash"), ("deepseek", "worker", "deepseek-chat"),
                          ("deepseek", "reviewer", "deepseek-v4-flash"), ("claude", "worker", "claude-opus-5-4")])
        self.assertEqual(len(self.store.get(run.id)["state"]["model_mismatches"]), 4)

    def test_a_call_that_names_two_ids_reports_the_one_that_differs(self):
        run = self.run_row()
        call = self.settle(run, "deepseek", "deepseek-v4-pro", reply("deepseek-v4-pro"), reply("deepseek-v4-pro"), reply("deepseek-v4-flash"))
        self.assertEqual((call["served_model"], call["served_models"]), ("deepseek-v4-pro", ["deepseek-v4-pro", "deepseek-v4-flash"]))
        self.assertEqual([e["details"]["served"] for e in self.events(run.id, "model_mismatch")], ["deepseek-v4-flash"])

    def test_a_route_that_names_no_model_is_never_a_mismatch(self):
        run = self.run_row()
        self.settle(run, "deepseek", None, reply("deepseek-flash"))
        self.settle(run, "codex", None, {"type": "thread.started", "thread_id": "t", "model": "whatever"})
        self.assertEqual(self.events(run.id, "model_mismatch"), [])
        self.assertEqual(model_drift({"provider": "claude", "role": "worker"}, {"models": ["x"], "source": "init"}, []), ([], []))

    def test_codex_uses_the_route_profile_to_find_the_rollout(self):
        home = self.root / "profile codex"
        thread = uuid7(datetime.now(timezone.utc))
        folder = home / "sessions" / f"{datetime.now().astimezone():%Y/%m/%d}"
        folder.mkdir(parents=True)
        write(folder / f"rollout-x-{thread}.jsonl", {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}})
        route = {"kind": "codex", "model": "gpt-6.2-astra", "profile_dir": str(home)}
        run = Run(self.store, self.configured(fixtures.fixture("good"), milestones=[{"title": "Prepare fixture", "task": "Set", "worker": route}]))
        call = self.settle(run, "codex", "gpt-6.2-astra", {"type": "thread.started", "thread_id": thread})
        self.assertEqual((call["served_model"], call["served_source"]), ("gpt-6.1-sol", "codex_rollout"))
        (event,) = self.events(run.id, "model_mismatch")
        self.assertEqual((event["details"]["requested"], event["details"]["served"], event["details"]["source"]),
                         ("gpt-6.2-astra", "gpt-6.1-sol", "codex_rollout"))

    def test_a_parsing_failure_never_fails_the_call(self):
        run = self.run_row()
        with patch("nightweaver.engine.served_models", side_effect=RuntimeError("boom")):
            call = self.settle(run, "deepseek", "deepseek-v4-pro", reply("deepseek-v4-flash"))
        self.assertIsNone(call["served_model"])
        self.assertEqual(self.events(run.id, "model_mismatch"), [])

    def test_a_mismatch_is_attention_worthy_notified_and_labeled(self):
        self.enable(quiet_hours="")
        run = self.run_row()
        self.settle(run, "deepseek", "deepseek-v4-pro", reply("deepseek-v4-flash"))
        (request,) = self.ntfy.of("POST")
        self.assertEqual((request["headers"]["priority"], request["headers"]["tags"]), ("4", "mag"))
        self.assertTrue(request["body"].decode().startswith("deepseek worker asked for deepseek-v4-pro but was served deepseek-v4-flash."))
        report = diagnosis(self.store, run.id)
        self.assertEqual(report["recorded_failure_stages"], {"model_mismatch": 1})
        self.assertEqual(report["last_failure_event"]["kind"], "model_mismatch")
        self.assertEqual(category({"kind": "model_mismatch", "details": {}}), "Problems")
        self.assertEqual(category({"kind": "notify_failed", "details": {}}), "Progress")


if __name__ == "__main__":
    unittest.main()
