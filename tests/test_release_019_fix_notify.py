"""Nightweaver 0.19 review fixes for notifications: terminal blocks reach the phone, the quiet-hours allowance and the digest hold
lose nothing, the state file survives several processes, and the question inbox no longer trusts the two clocks to agree."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_windows as fixtures
from test_release_019_notify import Base, NIGHT, NOON, at

from nightweaver import notify
from nightweaver.cli import dispatch
from nightweaver.engine import Run
from nightweaver.recovery import supervise
from nightweaver.store import timestamp

MORNING = datetime(2026, 11, 22, 8, 0).astimezone()
ROOT = Path(__file__).resolve().parent.parent
HAMMER = """
import sys
from datetime import datetime
from unittest.mock import patch
from nightweaver import notify
from nightweaver.store import Store
store, run, count, name = Store(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
with patch("nightweaver.notify.clock", return_value=datetime(2026, 11, 22, 2, 0).astimezone()):
    for number in range(count):
        notify._deliver(store, run, "Hammer", [("blocked", {"cause": f"worker {name} message {number}"})], None)
"""


class ServerClock:
    """A loopback ntfy whose clock is not this computer's: every POST is answered with an id and the server's own time."""

    def __init__(self, behind):
        self.requests, self.replies, self.behind = [], [], behind
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(json.dumps({"id": "question01", "time": outer.now() - outer.behind, "event": "message"}).encode())

            def do_GET(self):
                parts = urlsplit(self.path)
                outer.requests.append(parse_qs(parts.query))
                self.reply("".join(json.dumps(item) + "\n" for item in outer.replies).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def now():
        return int(datetime.now(timezone.utc).timestamp())

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(os.name == "nt", "Windows controller")
class TerminalBlocks(Base):
    def test_every_way_a_run_stops_unattended_is_a_default_event(self):
        self.assertTrue({"budget_exhausted", "repeated_failure_stopped", "verification_inconsistent", "local_advice_required",
                         "guard_stopped", "dispatcher_run_failed", "blocked"} <= set(notify.DEFAULT_EVENTS))
        notify.setup(self.store, server=self.ntfy.url, events=["guard_stopped", "dispatcher_run_failed"])

    def test_budget_repeated_failure_inconsistent_verification_and_advice_stops_send_a_message(self):
        self.enable()
        run = self.run_row(title="Overnight build")
        run.set("budget_exhausted", {"issue": "Run provider-call budget exhausted"}, phase="blocked")
        run.set("repeated_failure_stopped", {"issue": "x", "count": 3, "cause": "Stopped: gate unit tests failed 3 times on the same build"},
                phase="blocked")
        run.set("verification_inconsistent", {"name": "unit tests", "issue": "unit tests produced both passes and failures"}, phase="blocked")
        run.set("local_advice_required", {"model": "qwen3"}, phase="blocked")
        posts = self.ntfy.of("POST")
        self.assertEqual([r["headers"]["priority"] for r in posts], ["4"] * 4)
        self.assertEqual([r["body"].decode().splitlines()[0] for r in posts],
                         ["Stopped: Run provider-call budget exhausted.", "Stopped: gate unit tests failed 3 times on the same build.",
                          "Stopped: check unit tests gave inconsistent results on the same build.",
                          "Stopped: the required local review (qwen3) failed."])

    def test_a_real_run_that_stops_on_a_repeated_failure_is_not_silent(self):
        self.enable()
        run = self.configured(fixtures.fixture("bad"), attempts=5, retry={"repeated_failure_limit": 2, "initial_seconds": 1}, title="Stuck")
        self.assertEqual(Run(self.store, run).run(), "blocked")
        (post,) = self.ntfy.of("POST")
        self.assertTrue(post["body"].decode().startswith("Stopped: "), post["body"])
        self.assertIn(f"Run {run}", post["body"].decode())

    def test_a_dispatcher_failure_reaches_the_notifier_without_paths(self):
        self.enable()
        run = self.configured(fixtures.fixture("good"), enqueue=True, title="Queued job")
        with patch("nightweaver.cli.Run.run", side_effect=OSError(r"C:\Users\Bailey\private plans\notes.txt: access denied")):
            self.assertEqual(dispatch(self.store, False), [{"run_id": run, "phase": "blocked"}])
        (post,) = self.ntfy.of("POST")
        body = post["body"].decode()
        self.assertEqual(self.store.get(run)["state"]["phase"], "blocked")
        self.assertEqual((post["headers"]["title"], post["headers"]["priority"]), ("Queued job", "4"))
        self.assertTrue(body.startswith("Stopped: the dispatcher could not run this: "), body)
        for leak in ("Bailey", "private plans", "notes.txt", "C:\\"):
            self.assertNotIn(leak, body)

    def test_a_guard_that_gives_up_notifies_and_the_digest_calls_the_run_stuck(self):
        self.enable()
        run = self.configured(fixtures.fixture("good"), title="Crash loop")

        class Child:
            pid = 4242

            @staticmethod
            def wait():
                return 1

        with patch("nightweaver.recovery.launch_runner", return_value=Child()):
            self.assertEqual(supervise(self.store, run, 0), "limit_reached")
        (post,) = self.ntfy.of("POST")
        self.assertEqual(post["headers"]["priority"], "4")
        self.assertTrue(post["body"].decode().startswith("The runner kept crashing and the restart limit was reached"), post["body"])
        self.assertNotEqual(self.store.get(run)["state"]["phase"], "blocked", "the guard leaves the run in a live phase")
        report = notify.digest(self.store, 12)
        self.assertEqual((report["counts"]["blocked"], report["counts"]["running"]), (1, 0))
        self.assertIn("restart limit was reached", report["message"])

    def test_a_guard_that_merely_finished_or_paused_stays_quiet(self):
        self.enable()
        run = self.configured(fixtures.fixture("good"))
        for reason in ("done", "paused", "blocked", "contended", "queue_owned"):
            notify.announce(self.store, run, "guard_stopped", {"reason": reason})
        self.assertEqual(self.ntfy.of("POST"), [])
        notify.announce(self.store, run, "guard_stopped", {"reason": "limit_reached"})
        self.assertEqual(len(self.ntfy.of("POST")), 1)
        self.assertEqual(notify.digest(self.store, 12)["counts"]["running"], 1, "an old guard_stopped does not outlive a restarted guard")

    def test_a_restarted_guard_is_running_again_in_the_digest(self):
        run = self.configured(fixtures.fixture("good"))
        self.store.transition(run, "guard_stopped", {"reason": "limit_reached"})
        self.assertEqual(notify.digest(self.store, 12)["counts"]["blocked"], 1)
        self.store.transition(run, "guard_started", {"pid": 1, "limit": 2})
        self.assertEqual(notify.digest(self.store, 12)["counts"], {"completed": 0, "stopped": 0, "blocked": 0, "parked": 0, "running": 1})


@unittest.skipUnless(os.name == "nt", "Windows controller")
class BlockedCause(Base):
    def blocked(self, **details):
        self.run_row().set("blocked", details, phase="blocked")
        (post,) = self.ntfy.of("POST")
        return post["body"].decode().splitlines()[0]

    def test_a_judge_failure_keeps_the_exit_code_and_drops_the_log_tail(self):
        self.enable()
        tail = 'ool_use","name":"Read","input":{"file_path":"C:\\\\Users\\\\Bailey\\\\secret plans\\\\notes.txt"}} ' + "z" * 500
        line = self.blocked(issue=f"The judge exited 1: {tail}")
        self.assertEqual(line, "Blocked: The judge exited 1.")

    def test_paths_and_token_like_words_never_leave_the_machine(self):
        self.enable()
        line = self.blocked(issue="git failed: fatal: unable to access 'C:/Users/Bailey/repo/.git/index.lock': denied, then "
                                  "/home/bailey/.ssh/id_rsa and src\\app\\main.py with a3f9c2e1b4d5a6f7a3f9c2e1b4d5a6f7 left over")
        for leak in ("Bailey", "bailey", "index.lock", "id_rsa", "main.py", "a3f9c2e1"):
            self.assertNotIn(leak, line)
        self.assertTrue(line.startswith("Blocked: git failed: fatal: unable to access <path>"), line)
        self.assertLessEqual(len(line), 160)

    def test_log_fragments_give_a_fixed_phrase_and_a_long_cause_is_cut(self):
        self.enable()
        line = self.blocked(issue='{"type":"assistant","message":{"content":"password hunter2 in the file"}}')
        self.assertEqual(line, "Blocked: details are in the run log.")
        notify.forget()
        self.ntfy.requests.clear()
        self.assertLessEqual(len(notify.summarize("blocked", {"issue": "word " * 200})), 160)

    def test_the_cause_and_phase_a_user_can_act_on_are_kept(self):
        self.enable()
        line = self.blocked(issue="ignored", cause="Stopped: gate unit tests failed 3 times on the same build", during="checking")
        self.assertEqual(line, "Blocked while checking: Stopped: gate unit tests failed 3 times on the same build.")

    def test_the_digest_and_the_hold_are_just_as_clean(self):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        run = self.run_row(title="Leaky")
        self.store.transition(run.id, "blocked", {"issue": "x"}, phase="blocked", last_error=r"open C:\Users\Bailey\vault\keys.txt failed")
        with at(NIGHT):
            self.run_row().set("blocked", {"issue": r"open C:\Users\Bailey\vault\keys.txt failed"})
            notify.flush()
        text = (self.store.root / "notify-state.json").read_text(encoding="utf-8") + notify.digest(self.store, 12)["message"]
        self.assertNotIn("Bailey", text)
        self.assertNotIn("keys.txt", text)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ModelMismatches(Base):
    def mismatch(self, served):
        return {"provider": "gemini", "role": "worker", "requested": "gemini-2.5-pro", "served": served}

    def test_one_call_served_by_two_other_models_sends_two_messages(self):
        self.enable()
        run = self.run_row()
        run.set("model_mismatch", self.mismatch("gemini-2.5-flash"), following_event=[("model_mismatch", self.mismatch("gemini-2.5-flash-lite"))])
        bodies = [r["body"].decode() for r in self.ntfy.of("POST")]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("served gemini-2.5-flash." in body for body in bodies), bodies)
        self.assertTrue(any("served gemini-2.5-flash-lite." in body for body in bodies), bodies)

    def test_the_same_pair_twice_is_one_message(self):
        self.enable()
        run = self.run_row()
        run.set("model_mismatch", self.mismatch("gemini-2.5-flash"), following_event=[("model_mismatch", self.mismatch("gemini-2.5-flash"))])
        self.assertEqual(len(self.ntfy.of("POST")), 1)

    def test_a_met_goal_and_its_completion_are_still_one_message(self):
        self.enable()
        self.run_row().set("goal_met", {"wave": 1, "score": 95}, following_event=[("run_completed", {"branch": "refs/heads/nightweaver/z"}),
                                                                                  ("model_mismatch", self.mismatch("gemini-2.5-flash"))])
        self.assertEqual(len(self.ntfy.of("POST")), 2, "the goal and one mismatch")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class QuietHours(Base):
    def test_parks_never_spend_the_nightly_allowance(self):
        self.enable(max_per_night=3, quiet_hours="23:00-07:00")
        run = self.run_row()
        with at(NIGHT):
            for number in range(4):
                run.set("provider_parked", {"provider": "codex", "role": "worker", "until": "2026-11-22T05:30:00+00:00"})
                notify.flush()
            run.set("goal_met", {"wave": 2, "score": 93})
            notify.flush()
        (post,) = self.ntfy.of("POST")
        self.assertTrue(post["body"].decode().startswith("Goal met in wave 2"), post["body"])
        state = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["nights"], {"2026-11-21": 1})
        self.assertEqual([item["event"] for item in state["held"]], ["provider_parked"] * 4)

    def test_by_day_a_park_is_still_sent(self):
        self.enable(quiet_hours="23:00-07:00")
        with at(NOON):
            self.run_row().set("provider_parked", {"provider": "codex", "role": "worker"})
            notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 1)

    def test_held_terminal_messages_are_sent_at_the_first_opportunity_after_quiet_hours(self):
        self.enable(max_per_night=1, quiet_hours="23:00-07:00")
        run = self.run_row(title="Night run")
        with at(NIGHT):
            run.set("blocked", {"cause": "First stop"})
            run.set("goal_met", {"wave": 3, "score": 97})
            run.set("provider_parked", {"provider": "codex", "role": "worker"})
            notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 1)
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 2)
        with at(MORNING):
            run.set("blocked", {"cause": "Morning stop"})
            notify.flush()
        posts = [r["body"].decode() for r in self.ntfy.of("POST")]
        self.assertEqual(len(posts), 3, posts)
        self.assertIn("Morning stop", posts[1])
        self.assertIn("Night run: Goal met in wave 3 with judge score 97/100.", posts[2])
        self.assertNotIn("usage limit", posts[2], "parks wait for the digest")
        state = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))
        self.assertEqual([item["event"] for item in state["held"]], ["provider_parked"])

    def test_a_large_hold_is_released_in_several_messages_and_nothing_is_lost(self):
        self.enable(quiet_hours="23:00-07:00")
        held = [{"id": f"{number:012d}", "time": timestamp(), "run_id": "0" * 12, "title": "Backlog", "event": "blocked",
                 "summary": f"Blocked: reason {number} " + "w" * 180} for number in range(40)]
        (self.store.root / "notify-state.json").write_text(json.dumps({"nights": {}, "held": held}), encoding="utf-8")
        with at(MORNING):
            self.run_row().set("blocked", {"cause": "Now"})
            notify.flush()
        posts = [r["body"].decode() for r in self.ntfy.of("POST")]
        self.assertGreaterEqual(len(posts), 3)
        self.assertEqual(sum(f"reason {number} " in "\n".join(posts) for number in range(40)), 40)
        self.assertEqual(json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"], [])

    def test_a_failed_send_costs_no_allowance_and_is_held_not_lost(self):
        self.enable(max_per_night=2, quiet_hours="23:00-07:00")
        run = self.run_row(title="Flaky net")
        self.ntfy.status = 503
        with at(NIGHT):
            run.set("blocked", {"cause": "Lost while the server was down"})
            notify.flush()
        state = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["nights"].get("2026-11-21", 0), 0)
        self.assertEqual([(item["event"], item.get("failed")) for item in state["held"]], [("blocked", True)])
        self.assertEqual(len(self.events(run.id, "notify_failed")), 1)
        self.ntfy.status = 200
        self.ntfy.requests.clear()
        with at(NIGHT):
            for number in range(3):
                run.set("blocked", {"cause": f"After the outage {number}"})
                notify.flush()
        self.assertEqual(len(self.ntfy.of("POST")), 2, "both slots of the night were still there")
        report = notify.digest(self.store, 12)
        self.assertIn("Lost while the server was down", report["message"])
        self.assertIn("(not delivered)", report["message"])

    def test_a_failed_daytime_send_is_delivered_with_the_next_message(self):
        self.enable(quiet_hours="23:00-07:00")
        run = self.run_row()
        self.ntfy.status = 500
        with at(NOON):
            run.set("blocked", {"cause": "Missed"})
            notify.flush()
        self.ntfy.status = 200
        self.ntfy.requests.clear()
        with at(NOON):
            run.set("blocked", {"cause": "Fresh"})
            notify.flush()
        posts = [r["body"].decode() for r in self.ntfy.of("POST")]
        self.assertEqual(len(posts), 2)
        self.assertIn("Missed", posts[1])
        self.assertEqual(json.loads(self.cli("status")[0])["held_for_digest"], 0)

    def test_a_failed_release_keeps_everything_held(self):
        self.enable(quiet_hours="23:00-07:00")
        state = {"nights": {}, "held": [{"id": "a" * 12, "time": timestamp(), "run_id": "0" * 12, "title": "T", "event": "blocked", "summary": "Blocked: s."}]}
        (self.store.root / "notify-state.json").write_text(json.dumps(state), encoding="utf-8")
        real = notify._send

        def second_fails(*args, **kwargs):
            second_fails.calls += 1
            return real(*args, **kwargs) if second_fails.calls == 1 else (False, "HTTP 500")

        second_fails.calls = 0
        with at(MORNING), patch("nightweaver.notify._send", side_effect=second_fails):
            self.run_row().set("blocked", {"cause": "Now"})
            notify.flush()
        self.assertEqual(len(json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"]), 1)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class DigestHold(Base):
    def hold(self, count):
        self.enable(max_per_night=0, quiet_hours="23:00-07:00")
        run = self.run_row(title="Held run")
        with at(NIGHT):
            for number in range(count):
                run.set("blocked", {"cause": f"Stop {number:02d} " + "detail " * 17})
                notify.flush()
        return json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"]

    def test_the_digest_clears_only_the_messages_it_listed(self):
        held = self.hold(30)
        self.assertEqual(len(held), 30)
        report = notify.send_digest(self.store, 12)
        self.assertTrue(report["sent"])
        left = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"]
        (post,) = self.ntfy.of("POST")
        body = post["body"].decode()
        self.assertLessEqual(len(post["body"]), notify.MAX_BODY)
        self.assertTrue(0 < len(left) < 30, len(left))
        for item in held:
            self.assertEqual(item["summary"] in body, item not in left, item["summary"])
        self.assertIn(f"{len(left)} more", body)
        self.assertEqual([item["summary"] for item in left], [item["summary"] for item in held[30 - len(left):]], "the oldest go first")
        notify.send_digest(self.store, 12)
        self.assertEqual(json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"], [])

    def test_a_message_held_while_the_digest_was_in_flight_survives_it(self):
        self.hold(3)

        def hold_another(*args, **kwargs):
            data = notify._state(self.store)
            notify._hold(data, "f" * 12, "Late", "blocked", "Blocked: arrived mid-send.")
            notify._save(self.store, data)
            return True, None

        with patch("nightweaver.notify._send", side_effect=hold_another):
            self.assertTrue(notify.send_digest(self.store, 12)["sent"])
        left = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))["held"]
        self.assertEqual([item["summary"] for item in left], ["Blocked: arrived mid-send."])

    def test_print_and_a_failed_digest_clear_nothing(self):
        self.hold(2)
        notify.digest(self.store, 12)
        self.ntfy.status = 500
        self.assertFalse(notify.send_digest(self.store, 12)["sent"])
        self.assertEqual(len(notify._state(self.store)["held"]), 2)

    def test_the_hold_overflow_drops_parks_before_terminal_messages(self):
        data = {"held": [{"id": "p%d" % n, "event": "provider_parked", "summary": "p"} for n in range(notify.KEEP_HELD)]}
        data["held"] += [{"id": "t%d" % n, "event": "goal_met", "summary": "g"} for n in range(5)]
        notify._save(self.store, data)
        kept = notify._state(self.store)["held"]
        self.assertEqual(len(kept), notify.KEEP_HELD)
        self.assertEqual([item["id"] for item in kept if item["event"] == "goal_met"], ["t%d" % n for n in range(5)])


@unittest.skipUnless(os.name == "nt", "Windows controller")
class SharedState(Base):
    def test_four_processes_never_lose_a_message_or_an_allowance_count(self):
        self.enable(max_per_night=1000, quiet_hours="23:00-07:00")
        run = self.run_row().id
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        workers = [subprocess.Popen([sys.executable, "-c", HAMMER, str(self.store.root), run, "15", str(name)], cwd=ROOT, env=env,
                                    stderr=subprocess.PIPE, text=True) for name in range(4)]
        errors = [worker.communicate(timeout=120)[1] for worker in workers]
        self.assertEqual([worker.returncode for worker in workers], [0] * 4, errors)
        state = json.loads((self.store.root / "notify-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(self.ntfy.of("POST")), 60)
        self.assertEqual(state["nights"], {"2026-11-21": 60}, "every admitted message was counted")
        self.assertEqual(state.get("held", []), [])
        self.assertEqual(self.events(run, "notify_failed"), [])

    def test_a_permission_error_from_os_replace_is_retried_not_fatal(self):
        self.enable(max_per_night=5, quiet_hours="23:00-07:00")
        real, refusals = os.replace, [0]

        def busy(source, target):
            if str(target).endswith("notify-state.json") and refusals[0] < 3:
                refusals[0] += 1
                raise PermissionError(5, "Access is denied")
            return real(source, target)

        with at(NIGHT), patch("nightweaver.safety.os.replace", side_effect=busy):
            admitted, night = notify._admit(self.store, notify._config(self.store), "0" * 12, "T", "blocked", "Blocked: x.")
        self.assertEqual((admitted, night, refusals[0]), (True, "2026-11-21", 3))
        self.assertEqual(notify._state(self.store)["nights"], {"2026-11-21": 1})

    def test_a_read_that_collides_with_a_writer_is_retried(self):
        self.enable(quiet_hours="23:00-07:00")
        (self.store.root / "notify-state.json").write_text(json.dumps({"nights": {"2026-11-21": 2}}), encoding="utf-8")
        real, refusals = Path.read_text, [0]

        def busy(path, *args, **kwargs):
            if path.name == "notify-state.json" and refusals[0] < 2:
                refusals[0] += 1
                raise PermissionError(5, "Access is denied")
            return real(path, *args, **kwargs)

        with patch("nightweaver.notify.Path.read_text", busy):
            self.assertEqual(notify._state(self.store), {"nights": {"2026-11-21": 2}})
        self.assertEqual(refusals[0], 2)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class QuestionInbox(Base):
    def serve(self, behind):
        server = ServerClock(behind)
        self.addCleanup(server.close)
        notify.setup(self.store, server=server.url, quiet_hours="")
        self.reply = notify._config(self.store)["reply_topic"]
        return server

    def test_the_question_is_stamped_with_the_servers_clock_and_polled_by_id(self):
        run = "0123456789ab"
        for behind in (7200, -7200):  # the server clock two hours behind this PC, then two hours ahead
            with self.subTest(behind=behind):
                server = self.serve(behind)
                asked = notify.ask(self.store, run, "pick", "Which?", ["One", "Two"])
                served = server.now() - behind
                self.assertEqual((asked["since_id"], asked["clock"], asked["sent"]), ("question01", "server", True))
                self.assertLess(abs(datetime.fromisoformat(asked["asked_at"]).timestamp() - served), 5)
                server.replies = [{"id": "r1", "event": "message", "time": served + 20, "message": f"{run}:pick:Two"}]
                self.assertEqual(notify.poll_answer(self.store, run, "pick", asked), "Two")
                self.assertEqual(server.requests[-1], {"poll": ["1"], "since": ["question01"]})

    def test_a_local_stamp_is_never_compared_with_the_servers_clock(self):
        server = self.serve(0)
        run = "0123456789ab"
        asked = {"asked_at": timestamp(), "since_id": "abc123", "clock": "local", "options": ["Two"]}
        server.replies = [{"id": "r1", "event": "message", "time": server.now() - 7200, "message": f"{run}:pick:Two"}]
        self.assertEqual(notify.poll_answer(self.store, run, "pick", asked), "Two")
        self.assertEqual(server.requests[-1]["since"], ["abc123"])

    def test_only_an_offered_option_is_an_answer(self):
        server = self.serve(0)
        run = "0123456789ab"
        asked = notify.ask(self.store, run, "pick", "Which?", ["One", "Two"])
        now = server.now()
        server.replies = [{"id": "r1", "event": "message", "time": now + 1, "message": f"{run}:pick:Maybe"},
                          {"id": "r2", "event": "message", "time": now + 2, "message": f"{run}:pick:two"},
                          {"id": "r3", "event": "message", "time": now + 3, "message": f"{run}:pick:One"}]
        self.assertEqual(notify.poll_answer(self.store, run, "pick", asked), "One")
        self.assertEqual(notify.poll_answer(self.store, run, "pick", asked["asked_at"], options=["Two"]), None)
        self.assertEqual(notify.poll_answer(self.store, run, "pick", asked["asked_at"], options=["Maybe"]), "Maybe")
        server.replies = server.replies[:2]
        self.assertIsNone(notify.poll_answer(self.store, run, "pick", asked))


if __name__ == "__main__":
    unittest.main()
