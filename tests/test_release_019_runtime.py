"""Nightweaver 0.19 runtime hardening: usage-limit parking, honest token counts, idle and git timeouts,
tool droppings, Claude long-lived tokens, credential hygiene and pursuit-aware diagnostics."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_windows as fixtures

from nightweaver import engine
from nightweaver.cli import dispatch
from nightweaver.diagnostics import database_check, preflight
from nightweaver.engine import Run, git, tree
from nightweaver.providers import (OAUTH_VARIABLE, ProviderUnavailable, adapter, check_provider, claude_auth, invoke,
                                   reset_after, user_environment)
from nightweaver.readiness import diagnose
from nightweaver.recovery import supervise
from nightweaver.safety import redact
from nightweaver.spec import provider, validate
from nightweaver.store import Store, timestamp

PAST = "2020-01-01T00:00:00+00:00"
FAKE_TOKEN = "sk-ant-oat01-" + "Ab1_-" * 12 + "Zz9"
NOW = datetime(2026, 11, 21, 23, 50, tzinfo=timezone.utc)


def zone_available(name):
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


class ResetParsing(unittest.TestCase):
    def test_relative_waits_are_exact(self):
        for text, expected in (("You've hit your usage limit. Try again in 2 hours 13 minutes.", timedelta(hours=2, minutes=13)),
                               ("try again in 47m", timedelta(minutes=47)), ("limit reached, try again in 3h", timedelta(hours=3)),
                               ("resets in 90 minutes", timedelta(minutes=90)), ("try again in 2 days 3 hours", timedelta(days=2, hours=3)),
                               ("Please try again in 6m0s.", timedelta(minutes=6)),
                               ("try again in 12 minutes and 5 seconds", timedelta(minutes=12, seconds=5)),
                               ('{"retry_after": 120}', timedelta(minutes=2)), ('{"resets_in_seconds": 3600}', timedelta(hours=1)),
                               ("retry-after: 30", timedelta(seconds=30))):
            with self.subTest(text):
                self.assertEqual(reset_after(text, NOW), NOW + expected)

    def test_epoch_seconds_in_json_and_the_legacy_pipe_form(self):
        target = int(NOW.timestamp()) + 5400
        for text in (f'{{"type":"rate_limit_event","rate_limit_info":{{"status":"rejected","resetsAt":{target}}}}}',
                     f'{{"reset_at": {target}}}', f'{{"resets_at": "{target}"}}', f'{{"resetAt":{target}.5}}',
                     f"Claude AI usage limit reached|{target}", f'{{"resetsAt": {target * 1000}}}'):
            with self.subTest(text):
                self.assertEqual(reset_after(text, NOW), NOW + timedelta(seconds=5400))
        later = f'{{"resetsAt":{target}}}\n{{"resetsAt":{target + 600}}}'
        self.assertEqual(reset_after(later, NOW), NOW + timedelta(seconds=6000), "the last event is the live window")

    def test_iso_timestamps(self):
        for text, expected in (("resets 2026-11-22T03:00:00Z", datetime(2026, 11, 22, 3, tzinfo=timezone.utc)),
                               ('"resetsAt":"2026-11-22T03:00:00+01:00"', datetime(2026, 11, 22, 2, tzinfo=timezone.utc)),
                               ("try again at 2026-11-22 03:00:00.123456789+0000", datetime(2026, 11, 22, 3, 0, 0, 123456, tzinfo=timezone.utc)),
                               ("reset time: 2026-11-22T03:00 (UTC)", datetime(2026, 11, 22, 3, tzinfo=timezone.utc))):
            with self.subTest(text):
                self.assertEqual(reset_after(text, NOW), expected)

    def test_clock_times_use_the_named_zone_and_roll_to_tomorrow(self):
        self.assertEqual(reset_after("resets 7pm (UTC)", NOW), datetime(2026, 11, 22, 19, tzinfo=timezone.utc))
        self.assertEqual(reset_after("resets 11:55pm (UTC)", NOW), datetime(2026, 11, 21, 23, 55, tzinfo=timezone.utc))
        self.assertEqual(reset_after("resets 11:30pm (UTC)", NOW), datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc), "already past")
        self.assertEqual(reset_after("reset at 12am (GMT)", NOW), datetime(2026, 11, 22, tzinfo=timezone.utc))
        self.assertEqual(reset_after("resets 18:40 (UTC)", NOW), datetime(2026, 11, 22, 18, 40, tzinfo=timezone.utc))
        if zone_available("America/New_York"):
            from zoneinfo import ZoneInfo
            york = ZoneInfo("America/New_York")
            for text, hour, minute in (("resets 3:30pm (America/New_York)", 15, 30), ("You've hit your limit · resets 3pm (America/New_York)", 15, 0)):
                with self.subTest(text):
                    result = reset_after(text, NOW)
                    self.assertEqual((result.astimezone(york).hour, result.astimezone(york).minute), (hour, minute))
                    self.assertTrue(NOW < result <= NOW + timedelta(hours=24))

    def test_local_time_without_a_zone_or_with_an_unknown_one(self):
        for text, hour, minute in (("resets 3pm", 15, 0), ("reset at 5:02 PM", 17, 2), ("try again at 5:02 PM.", 17, 2),
                                   ("5-hour limit reached ∙ resets 11am (Mars/Phobos)", 11, 0), ("resets 6:40pm", 18, 40)):
            with self.subTest(text):
                result = reset_after(text, NOW)
                local = result.astimezone()
                self.assertEqual((local.hour, local.minute), (hour, minute))
                self.assertTrue(NOW < result <= NOW + timedelta(hours=24, minutes=1), result)
        self.assertEqual(reset_after("resets 3pm", NOW.replace(tzinfo=None)).astimezone().hour, 15, "a naive clock is local time")

    def test_dated_times(self):
        self.assertEqual(reset_after("try again at Nov 22nd, 2026 3:42 PM (UTC)", NOW), datetime(2026, 11, 22, 15, 42, tzinfo=timezone.utc))
        self.assertEqual(reset_after("Your limit will reset at Nov 24, 12am (UTC)", NOW), datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.assertEqual(reset_after("resets on Dec 3 at 9:15am (UTC)", NOW), datetime(2026, 12, 3, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(reset_after("resets Nov 20, 3pm (UTC)", NOW), datetime(2027, 11, 20, 15, tzinfo=timezone.utc), "no year and past: next year")
        self.assertEqual(reset_after("resets November 30 18:00 (UTC)", NOW), datetime(2026, 11, 30, 18, tzinfo=timezone.utc))

    def test_unparseable_messages(self):
        for text in ("", "resets soon", "resets 3 times", "resets 25pm", "resets 13:75", "usage limit reached", "retry in 20 steps",
                     "Resets Nov 22 (UTC)", None, 7):
            with self.subTest(text):
                self.assertIsNone(reset_after(text, NOW))

    def test_a_past_epoch_is_returned_as_is(self):
        self.assertEqual(reset_after('{"resetsAt": 1700000000}', NOW), datetime.fromtimestamp(1700000000, timezone.utc))


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ParkTests(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def failing(self, message, times=1, kind="usage_limit", roles=("worker",)):
        real = invoke
        calls = []

        def route(config, role, *args, **kwargs):
            if len(calls) < times and role in roles:
                calls.append(role)
                raise ProviderUnavailable(message, kind)
            return real(config, role, *args, **kwargs)
        route.calls = calls
        return route

    @staticmethod
    def on(kind):
        return [{"title": "Prepare fixture", "task": "Set the value", "worker": {"kind": kind}}]

    def clock(self, run, seen=None):
        def sleeper(_seconds):
            state = self.store.get(run)["state"]
            if seen is not None:
                seen.append(state)
            self.store.transition(run, "fixture_clock", {}, retry_at=PAST)
        return patch("nightweaver.engine.time", SimpleNamespace(sleep=sleeper))

    def events(self, run, *kinds):
        return [e["details"] for e in self.store.events(run, limit=None, kinds=kinds)]

    def test_inline_runner_parks_without_debiting_the_attempt_and_completes_after_the_reset(self):
        run = self.configured(fixtures.fixture("good"), attempts=1)
        flaky, seen = self.failing("You've hit your limit · resets in 2 hours"), []
        started = datetime.now(timezone.utc)
        with patch("nightweaver.engine.invoke", side_effect=flaky), self.clock(run, seen):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(seen), 1)
        parked = seen[0]
        self.assertEqual((parked["phase"], parked["attempts_used"], parked["parks"]), ("ready", 0, 1))
        due = datetime.fromisoformat(parked["retry_at"]) - started
        self.assertTrue(timedelta(hours=2, seconds=80) <= due <= timedelta(hours=2, seconds=200), due)
        (event,) = self.events(run, "provider_parked")
        self.assertEqual((event["park"], event["parsed"], event["role"], event["provider"], event["until"]),
                         (1, True, "worker", "command", parked["retry_at"]))
        self.assertEqual(event["reset"][:4], str(started.year))
        self.assertEqual(self.events(run, "provider_unavailable"), [])
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["parks"], state["provider_calls"]), ("done", 0, 2), "a successful call clears the park count")

    def test_a_parked_reviewer_keeps_its_phase(self):
        run = self.configured(fixtures.fixture("good"), reviewer={"kind": "command", "argv": fixtures.fixture("review")}, attempts=1)
        flaky, seen = self.failing("usage limit: try again in 10 minutes", roles=("reviewer",)), []
        with patch("nightweaver.engine.invoke", side_effect=flaky), self.clock(run, seen):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(seen[0]["phase"], "reviewing")
        self.assertEqual(self.events(run, "provider_parked")[0]["role"], "reviewer")

    def test_dispatch_watch_skips_a_parked_run_until_due_then_finishes_it(self):
        run = self.configured(fixtures.fixture("good"), enqueue=True, attempts=1)
        flaky = self.failing("usage limit reached. Please try again in 3 hours.")
        stop = type("StopWatching", (Exception,), {})
        ticks = []

        def tick(_seconds):
            ticks.append(self.store.get(run)["state"])
            if len(ticks) == 1:
                self.assertEqual(Store(self.store.root).dispatch_runs(), [], "not due yet")
                self.store.transition(run, "fixture_clock", {}, retry_at=PAST)
            else:
                raise stop

        with patch("nightweaver.engine.invoke", side_effect=flaky):
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
            state = self.store.get(run)["state"]
            self.assertEqual((state["phase"], state["parks"], state["attempts_used"]), ("ready", 1, 0))
            with patch("nightweaver.cli.time", SimpleNamespace(sleep=tick)), self.assertRaises(stop):
                dispatch(self.store, True)
        self.assertEqual(ticks[0]["phase"], "ready", "the first pass left the parked run alone")
        self.assertEqual(ticks[1]["phase"], "done")
        self.assertEqual(self.store.get(run)["state"]["provider_calls"], 2)

    def test_one_shot_dispatch_also_resumes_a_due_parked_run(self):
        run = self.configured(fixtures.fixture("good"), enqueue=True, attempts=1)
        with patch("nightweaver.engine.invoke", side_effect=self.failing("resets in 1 hour")):
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
        self.assertEqual(dispatch(Store(self.store.root), False), [], "parked runs are not selected")
        self.store.transition(run, "fixture_clock", {}, retry_at=PAST)
        self.assertEqual(dispatch(Store(self.store.root), False), [{"run_id": run, "phase": "done"}])

    def test_crash_guard_launches_a_runner_that_waits_out_the_park(self):
        run = self.configured(fixtures.fixture("good"), attempts=1)
        self.store.transition(run, "guard_enrolled", {"limit": 2}, guard_limit=2)
        with patch("nightweaver.engine.invoke", side_effect=self.failing("resets in 20 minutes")):
            self.assertEqual(Run(self.store, run).run(defer_retries=True), "waiting")
            launches = []

            def runner(store, run_id, launch_id, retry=False):
                launches.append(run_id)
                code = {"done": 0, "paused": 2, "blocked": 3, "waiting": 4}[Run(store, run_id).run()]
                return SimpleNamespace(pid=4242, wait=lambda: code)

            with patch("nightweaver.recovery.launch_runner", runner), self.clock(run):
                self.assertEqual(supervise(self.store, run), "done")
        self.assertEqual(launches, [run])
        kinds = [e["kind"] for e in self.store.events(run, limit=None)]
        self.assertNotIn("guard_restart_scheduled", kinds)
        self.assertFalse(self.events(run, "guard_child_exited")[0]["unexpected"])
        self.assertEqual(self.store.get(run)["state"]["phase"], "done")

    def test_unparsed_limits_wait_half_an_hour_then_double_up_to_two_hours(self):
        run = self.configured(fixtures.fixture("good"), attempts=1)
        with patch("nightweaver.engine.invoke", side_effect=self.failing("Too many requests", times=5)), self.clock(run):
            self.assertEqual(Run(self.store, run).run(), "done")
        events = self.events(run, "provider_parked")
        self.assertEqual([e["park"] for e in events], [1, 2, 3, 4, 5])
        self.assertEqual([e["seconds"] for e in events], [1800, 3600, 7200, 7200, 7200])
        self.assertEqual({e["parsed"] for e in events}, {False})
        self.assertEqual({e["reset"] for e in events}, {None})

    def test_parks_beyond_max_parks_block_with_a_clear_message_and_a_retry_starts_over(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, retry={"max_parks": 2})
        with patch("nightweaver.engine.invoke", side_effect=self.failing("resets in 10 minutes", times=9)), self.clock(run):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(len(self.events(run, "provider_parked")), 2)
        state = self.store.get(run)["state"]
        self.assertEqual((state["phase"], state["blocked_from"], state["parks"]), ("blocked", "ready", 0))
        self.assertTrue(state["last_error"].startswith("command usage limit was hit again after 2 waits"), state["last_error"])
        self.assertIn("retry.max_parks 2", state["last_error"])
        self.assertEqual(state["provider_issue"]["reason"], "usage_limit")
        with patch("nightweaver.engine.invoke", side_effect=self.failing("resets in 10 minutes", times=1)), self.clock(run):
            self.assertEqual(Run(self.store, run).run(retry=True), "done")

    def test_zero_max_parks_disables_waiting(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, retry={"max_parks": 0})
        with patch("nightweaver.engine.invoke", side_effect=self.failing("resets in 10 minutes", times=9)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "provider_parked"), [])
        self.assertIn("waiting is disabled", self.store.get(run)["state"]["last_error"])

    def test_a_reset_beyond_the_time_budget_blocks_naming_the_reset(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, budgets={"max_seconds": 3600}, milestones=self.on("codex"))
        with patch("nightweaver.engine.invoke", side_effect=self.failing("You've hit your usage limit. Try again in 3 hours.", times=9)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertRegex(state["last_error"], r"^Codex usage limit resets at (?:[A-Z][a-z]{2} \d\d )?\d\d:\d\d, after this run's time budget$")
        self.assertEqual((state["phase"], state["blocked_from"]), ("blocked", "ready"))
        self.assertEqual(self.events(run, "provider_parked"), [])
        message = self.events(run, "provider_unavailable")[0]
        self.assertEqual((message["role"], message["reason"], message["issue"]), ("worker", "usage_limit", state["last_error"]))

    def test_an_unparsed_wait_that_passes_the_budget_blocks_too(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, budgets={"max_seconds": 600}, milestones=self.on("claude"))
        with patch("nightweaver.engine.invoke", side_effect=self.failing("rate limit exceeded", times=9)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertRegex(self.store.get(run)["state"]["last_error"], r"^Claude usage limit gave no reset time and the next retry at .* is after this run's time budget$")

    def test_a_park_longer_than_max_park_seconds_blocks(self):
        run = self.configured(fixtures.fixture("good"), attempts=1, retry={"max_park_seconds": 600}, milestones=self.on("gemini"))
        with patch("nightweaver.engine.invoke", side_effect=self.failing("try again in 2 hours", times=9)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        error = self.store.get(run)["state"]["last_error"]
        self.assertTrue(error.startswith("Gemini usage limit resets at "), error)
        self.assertIn("2.0 hours away", error)
        self.assertIn("retry.max_park_seconds 600", error)

    def test_other_provider_failures_keep_their_behavior(self):
        run = self.configured(fixtures.fixture("good"), attempts=1)
        with patch("nightweaver.engine.invoke", side_effect=self.failing("Selected profile needs login", times=9, kind="authentication")):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        self.assertEqual(self.events(run, "provider_parked"), [])
        transient = self.configured(fixtures.fixture("good"), attempts=1, retry={"initial_seconds": 1})
        with patch("nightweaver.engine.invoke", side_effect=self.failing("socket reset", kind="transient")), self.clock(transient):
            self.assertEqual(Run(self.store, transient).run(), "done")
        self.assertEqual(len(self.events(transient, "provider_retry_scheduled")), 1)

    def test_limits_validate_and_default(self):
        spec = validate({"repository": str(self.repo), "title": "T", "objective": "O", "worker": {"kind": "codex"},
                         "gates": [{"name": "g", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "One", "task": "Do"}]})
        self.assertEqual((spec["retry"]["max_parks"], spec["retry"]["max_park_seconds"]), (8, 21600))
        self.assertNotIn("git_timeout", spec)
        base = {"repository": str(self.repo), "title": "T", "objective": "O", "worker": {"kind": "codex"},
                "gates": [{"name": "g", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "One", "task": "Do"}]}
        for retry in ({"max_parks": 0}, {"max_parks": 50}, {"max_park_seconds": 60}, {"max_park_seconds": 604800}):
            self.assertEqual(validate({**base, "retry": retry})["retry"][next(iter(retry))], next(iter(retry.values())))
        for retry in ({"max_parks": 51}, {"max_parks": -1}, {"max_parks": True}, {"max_park_seconds": 59}, {"max_park_seconds": 604801},
                      {"max_park_seconds": "10"}, {"park_seconds": 5}):
            with self.subTest(retry), self.assertRaises(ValueError):
                validate({**base, "retry": retry})


@unittest.skipUnless(os.name == "nt", "Windows controller")
class CumulativeUsage(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def settle(self, run, provider, *events, name=None):
        self.count = getattr(self, "count", 0) + 1
        log = self.root / (name or f"call-{self.count}.jsonl")
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        pending = {"call": self.count, "role": "worker", "provider": provider, "model": None, "started": timestamp(),
                   "log": str(log), "step": 1, "attempt": self.count}
        run.finish_call(pending)
        return self.store.events(run.id, limit=1, kinds=["provider_call_finished"])[0]["details"]

    @staticmethod
    def codex(thread, input_tokens, output_tokens, cached=0):
        return [{"type": "thread.started", "thread_id": thread}, {"type": "turn.completed", "usage": {
            "input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output_tokens}}]

    def test_a_resumed_codex_thread_counts_only_what_each_call_added(self):
        run = Run(self.store, self.configured(fixtures.fixture("good")))
        first = self.settle(run, "codex", *self.codex("thread-1", 5_900_000, 10_000, 5_000_000))
        second = self.settle(run, "codex", *self.codex("thread-1", 10_500_000, 12_000, 9_000_000))
        third = self.settle(run, "codex", *self.codex("thread-1", 11_000_000, 15_000, 9_500_000))
        self.assertEqual([c["reported_tokens"] for c in (first, second, third)], [5_910_000, 10_512_000, 11_015_000])
        self.assertEqual([c["observed_tokens"] for c in (first, second, third)], [5_910_000, 4_602_000, 503_000])
        self.assertEqual(sum(c["token_channels"]["input_tokens"] for c in (first, second, third)), 11_000_000)
        self.assertEqual(third["token_channels"]["cached_input_tokens"], 500_000)
        self.assertEqual({c["session"] for c in (first, second, third)}, {"thread-1"})
        state = self.store.get(run.id)["state"]
        self.assertEqual((state["observed_tokens"], state["session_usage"]), (11_015_000, {"thread-1": 11_015_000}))
        other = self.settle(run, "codex", *self.codex("thread-2", 2_000_000, 1_000))
        self.assertEqual((other["observed_tokens"], other["reported_tokens"]), (2_001_000, 2_001_000), "first sighting counts in full")
        self.assertEqual(self.store.get(run.id)["state"]["observed_tokens"], 13_016_000)

    def test_counters_that_restart_or_have_no_thread_count_in_full(self):
        run = Run(self.store, self.configured(fixtures.fixture("good")))
        self.settle(run, "codex", *self.codex("thread-1", 900, 100))
        restarted = self.settle(run, "codex", *self.codex("thread-1", 300, 50))
        self.assertEqual(restarted["observed_tokens"], 350)
        self.assertEqual(self.store.get(run.id)["state"]["session_usage"], {"thread-1": 350})
        anonymous = self.settle(run, "codex", {"type": "turn.completed", "usage": {"input_tokens": 70, "output_tokens": 5}})
        self.assertEqual(anonymous["observed_tokens"], 75)
        self.assertNotIn("reported_tokens", anonymous)
        unknown = self.settle(run, "codex", *self.codex("thread-1", 0, 0)[:1])
        self.assertIsNone(unknown["observed_tokens"])
        self.assertEqual(self.store.get(run.id)["state"]["usage_unknown_calls"], 1)

    def test_claude_and_deepseek_stay_per_invocation(self):
        run = Run(self.store, self.configured(fixtures.fixture("good")))
        result = {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40}}
        for number, provider_name in enumerate(("claude", "claude", "deepseek"), 1):
            call = self.settle(run, provider_name, {"type": "system", "session_id": "same-session"}, result)
            self.assertEqual(call["observed_tokens"], 150)
            self.assertNotIn("reported_tokens", call)
        state = self.store.get(run.id)["state"]
        self.assertEqual(state["observed_tokens"], 450)
        self.assertNotIn("session_usage", state)

    def test_recovered_calls_use_the_same_delta(self):
        run = Run(self.store, self.configured(fixtures.fixture("good")))
        self.settle(run, "codex", *self.codex("thread-9", 1000, 0))
        log = self.root / "orphan.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in self.codex("thread-9", 1800, 0)), encoding="utf-8")
        run.finish_call({"call": 2, "role": "worker", "provider": "codex", "started": timestamp(), "log": str(log)}, recovered=True)
        self.assertEqual(self.store.get(run.id)["state"]["observed_tokens"], 1800)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class WatchdogAndGit(unittest.TestCase):
    configured = fixtures.WindowsRunTest.configured

    def setUp(self):
        fixtures.WindowsRunTest.setUp(self)
        self.addCleanup(setattr, engine, "GIT_TIMEOUT", engine.GIT_TIMEOUT)

    def call(self, run, role, **extra):
        def fake(config, mode, path, log, prompt, timeout, paused, session=None, evidence=None, health=None, **_rest):
            seen.update(health or {})
            return 0, None, ""

        seen = {}
        with patch("nightweaver.engine.invoke", side_effect=fake):
            run.call_provider({"kind": "command", "argv": ["x"]}, role, self.repo, self.root / f"{role}.jsonl", "prompt", **extra)
        return seen

    def test_default_idle_timeout_applies_to_model_roles_unless_the_plan_sets_one(self):
        run = Run(self.store, self.configured(fixtures.fixture("good")))
        for role in ("worker", "reviewer", "judge", "planner"):
            self.assertEqual(self.call(run, role)["idle_timeout"], 900, role)
        self.assertNotIn("idle_timeout", self.call(run, "advisory"))
        tuned = Run(self.store, self.configured(fixtures.fixture("good"), budgets={"idle_timeout": 45}))
        self.assertEqual(self.call(tuned, "worker")["idle_timeout"], 45)

    def test_a_silent_stall_is_cut_by_the_default_watchdog(self):
        run = Run(self.store, self.configured(fixtures.fixture("sleep")))
        with patch.object(engine, "IDLE_DEFAULT", 1):
            code, _, _ = run.call_provider({"kind": "command", "argv": fixtures.fixture("sleep")}, "worker", self.repo,
                                           self.root / "stall.jsonl", "prompt")
        self.assertEqual(code, 125, "the 20 second sleep was cut after the default idle window")
        self.assertEqual(self.store.get(run.id)["state"]["provider_calls"], 1)

    def test_git_timeout_validates_and_governs_every_git_call(self):
        base = {"repository": str(self.repo), "title": "T", "objective": "O", "worker": {"kind": "codex"},
                "gates": [{"name": "g", "argv": fixtures.fixture("gate")}], "milestones": [{"title": "One", "task": "Do"}]}
        for value in (30, 90, 3600):
            self.assertEqual(validate({**base, "git_timeout": value})["git_timeout"], value)
        for value in (29, 3601, 0, -5, "90", 90.0, True, None):
            with self.subTest(value), self.assertRaises(ValueError):
                validate({**base, "git_timeout": value})
        seen = []
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[0] == "git":
                seen.append((tuple(argv[3:5]), kwargs["timeout"]))
            return real(argv, *args, **kwargs)

        slow = self.configured(fixtures.fixture("good"), git_timeout=1200)
        with patch.object(engine.subprocess, "run", spy):
            self.assertEqual(Run(self.store, slow).run(), "done")
        added = seen.index((("worktree", "add"), 1200))
        self.assertEqual({timeout for _, timeout in seen[added:]}, {1200}, "every call after the worktree uses the plan's value")
        seen.clear()
        engine.GIT_TIMEOUT = 90
        quick = self.configured(fixtures.fixture("good"), git_timeout=40)
        with patch.object(engine.subprocess, "run", spy):
            self.assertEqual(Run(self.store, quick).run(), "done")
        added = seen.index((("worktree", "add"), 900))
        self.assertEqual({timeout for _, timeout in seen[added + 1:]}, {40}, "the worktree gets at least 900 s, the rest the plan's 40 s")
        engine.GIT_TIMEOUT = 90
        with patch.object(engine.subprocess, "run", spy):
            engine.git(self.repo, "rev-parse", "HEAD")
            engine.git(self.repo, "rev-parse", "HEAD", timeout=7)
        self.assertEqual([timeout for _, timeout in seen[-2:]], [90, 7])

    def test_a_worktree_that_times_out_is_cleaned_up_and_explained(self):
        run = self.configured(fixtures.fixture("good"), git_timeout=60)
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[0] == "git" and tuple(argv[3:5]) == ("worktree", "add"):
                Path(argv[-2]).mkdir(parents=True)
                (Path(argv[-2]) / "half.txt").write_text("partial", encoding="utf-8")
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return real(argv, *args, **kwargs)

        with patch.object(engine.subprocess, "run", spy):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertIn("took more than 900 seconds", state["last_error"])
        self.assertIn("git_timeout", state["last_error"])
        self.assertFalse((self.store.directory(run) / "worktrees" / "01").exists())
        self.assertEqual(Run(self.store, run).run(retry=True), "done", "a retry builds the worktree afresh")


@unittest.skipUnless(os.name == "nt", "Windows controller")
class ToolDroppings(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def worktree(self, base):
        path = self.root / f"wt-{len(list(self.root.glob('wt-*')))}"
        git(self.repo, "worktree", "add", "--detach", str(path), base)
        return path

    def write(self, path, name, text="x"):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def names(self, path):
        return sorted(git(path, "diff", "--cached", "--name-only", self.head).splitlines())

    def test_new_droppings_are_unstaged_and_real_changes_stay(self):
        path = self.worktree(self.head)
        self.write(path, "result.txt", "ready")
        for name in ("debug.log", "chrome_debug.log", ".playwright-mcp/console.txt", "docs/.playwright-mcp/deep/a.png", "crash.dmp",
                     "sub/Mini.DMP", "sub/debug.log", "café[1].dmp"):
            self.write(path, name)
        snapshot = tree(path, self.head, ["result.txt"], [])
        self.assertEqual(self.names(path), ["result.txt"])
        self.assertEqual(git(path, "ls-tree", "-r", "--name-only", snapshot).splitlines(), [".gitignore", "result.txt"])
        self.assertTrue((path / "debug.log").is_file(), "the files stay on disk, only out of the candidate")
        self.assertEqual(tree(path, self.head, ["result.txt"], []), snapshot, "a second snapshot is identical")

    def test_only_droppings_means_no_tracked_changes(self):
        path = self.worktree(self.head)
        self.write(path, "debug.log")
        with self.assertRaisesRegex(ValueError, "no tracked changes"):
            tree(path, self.head, [], [])

    def test_a_tracked_file_with_a_dropping_name_is_a_real_change(self):
        self.write(self.repo, "debug.log", "v1")
        self.write(self.repo, "keep.dmp", "dump")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "Tracked droppings")
        base = git(self.repo, "rev-parse", "HEAD")
        path = self.worktree(base)
        self.write(path, "debug.log", "v2")
        self.write(path, "keep.dmp", "dump 2")
        self.write(path, "sub/debug.log", "new")
        (path / "result.txt").unlink()
        snapshot = tree(path, base, [], [])
        self.assertEqual(sorted(git(path, "diff", "--cached", "--name-only", base).splitlines()), ["debug.log", "keep.dmp", "result.txt"])
        self.assertEqual(git(path, "show", f"{snapshot}:debug.log"), "v2")
        with self.assertRaisesRegex(ValueError, "outside the milestone paths: debug.log, keep.dmp, result.txt"):
            tree(path, base, ["nothing"], [])

    def test_protected_paths_and_scope_still_apply_to_the_remaining_changes(self):
        path = self.worktree(self.head)
        self.write(path, "debug.log")
        self.write(path, "other.txt")
        with self.assertRaisesRegex(ValueError, "outside the milestone paths: other.txt"):
            tree(path, self.head, ["result.txt"], [])
        with self.assertRaisesRegex(ValueError, "protected verification files: other.txt"):
            tree(path, self.head, [], ["other.txt"])

    def test_a_run_accepts_a_worker_that_leaves_a_browser_log(self):
        worker = [sys.executable, "-c", "from pathlib import Path; Path('result.txt').write_text('ready'); Path('debug.log').write_text('chromium')"]
        run = self.configured(worker, milestones=[{"title": "Prepare fixture", "task": "Set the value", "paths": ["result.txt"]}])
        self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(git(self.repo, "ls-tree", "-r", "--name-only", f"nightweaver/{run}").splitlines(), [".gitignore", "result.txt"])
        kinds = [e["kind"] for e in self.store.events(run, limit=None)]
        self.assertEqual((kinds.count("worker_started"), kinds.count("attempt_rejected")), (1, 0), "the first attempt was accepted")


class ClaudeAuthTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="Nightweaver auth ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        clean = {key: value for key, value in os.environ.items() if key not in (
            OAUTH_VARIABLE, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        patcher = patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        registry = patch("nightweaver.providers.user_environment", return_value=None)
        self.registry = registry.start()
        self.addCleanup(registry.stop)

    def token_file(self, text=FAKE_TOKEN + "\n"):
        path = self.root / "claude.token"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_rotated_registry_token_wins_over_the_stale_process_value(self):
        self.registry.return_value = "from-registry"
        os.environ[OAUTH_VARIABLE] = "from-process"
        self.assertEqual(claude_auth({"kind": "claude"}), ("from-registry", "oauth_token_user_env"))
        _, env = adapter({"kind": "claude"}, "worker")
        self.assertEqual(env[OAUTH_VARIABLE], "from-registry")
        self.registry.return_value = "from-process"
        self.assertEqual(claude_auth({"kind": "claude"}), ("from-process", "oauth_token_env"), "equal values keep the process source")

    def test_a_missing_variable_is_read_from_the_user_environment(self):
        self.registry.return_value = FAKE_TOKEN
        self.assertEqual(claude_auth(), (FAKE_TOKEN, "oauth_token_user_env"))
        _, env = adapter({"kind": "claude"}, "worker")
        self.assertEqual(env[OAUTH_VARIABLE], FAKE_TOKEN)
        self.assertNotIn(OAUTH_VARIABLE, os.environ, "the parent process environment is not modified")
        os.environ[OAUTH_VARIABLE] = "  "
        self.assertEqual(claude_auth()[1], "oauth_token_user_env", "a blank variable counts as absent")

    def test_the_registry_reader_uses_hkcu_environment_and_tolerates_absence(self):
        opened = []

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fake_module(values):
            def open_key(root, path):
                opened.append((root, path))
                return Key()

            def query(_key, name):
                if name not in values:
                    raise FileNotFoundError(name)
                return values[name], 1
            return SimpleNamespace(HKEY_CURRENT_USER="HKCU", OpenKey=open_key, QueryValueEx=query)

        # The name imported at the top of this module is the real reader; setUp only replaced the module attribute.
        with patch.dict(sys.modules, {"winreg": fake_module({OAUTH_VARIABLE: f"  {FAKE_TOKEN}  "})}):
            self.assertEqual(user_environment(OAUTH_VARIABLE), FAKE_TOKEN)
            self.assertIsNone(user_environment("NOT_SET"))
        self.assertEqual(opened[0], ("HKCU", "Environment"))
        with patch.dict(sys.modules, {"winreg": fake_module({OAUTH_VARIABLE: 5, "BLANK": "  "})}):
            self.assertIsNone(user_environment(OAUTH_VARIABLE))
            self.assertIsNone(user_environment("BLANK"))
        with patch.dict(sys.modules, {"winreg": None}):
            self.assertIsNone(user_environment(OAUTH_VARIABLE), "no winreg on other platforms")

    def test_oauth_token_file_wins_and_never_leaks(self):
        os.environ[OAUTH_VARIABLE] = "ambient"
        config = {"kind": "claude", "oauth_token_file": str(self.token_file())}
        self.assertEqual(claude_auth(config), (FAKE_TOKEN, "oauth_token_file"))
        argv, env = adapter(config, "worker")
        self.assertEqual(env[OAUTH_VARIABLE], FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, json.dumps(argv))
        for bad, text in (("missing", None), ("blank", " \n"), ("spaced", "two words")):
            with self.subTest(bad):
                path = self.root / f"{bad}.token"
                if text is not None:
                    path.write_text(text, encoding="utf-8")
                with self.assertRaises(ProviderUnavailable) as caught:
                    check_provider({"kind": "claude", "executable": sys.executable, "oauth_token_file": str(path)}, self.root)
                self.assertEqual(caught.exception.kind, "missing_credentials")
                self.assertNotIn("two words", str(caught.exception))

    def test_the_auth_source_falls_back_to_the_cli_login_or_none(self):
        profile = self.root / "profile"
        profile.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(profile)
        self.assertEqual(claude_auth(), (None, "none"))
        (profile / ".credentials.json").write_text("{}", encoding="utf-8")
        self.assertEqual(claude_auth(), (None, "cli_login"))
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(claude_auth({"kind": "claude", "profile_dir": str(other)}), (None, "none"))
        _, env = adapter({"kind": "claude"}, "worker")
        self.assertNotIn(OAUTH_VARIABLE, env)

    def test_child_environments_only_carry_the_credentials_they_use(self):
        os.environ.update({OAUTH_VARIABLE: "oauth", "ANTHROPIC_API_KEY": "anthropic-key", "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
                           "DEEPSEEK_API_KEY": "deepseek-key", "OPENAI_API_KEY": "openai-key", "GEMINI_API_KEY": "gemini-key"})
        others = (OAUTH_VARIABLE, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY")
        _, env = adapter({"kind": "codex"}, "worker")
        self.assertEqual([k for k in (*others, "GEMINI_API_KEY") if k in env], [], "codex")
        self.assertEqual(env["OPENAI_API_KEY"], "openai-key")
        _, env = adapter({"kind": "gemini"}, "worker")
        self.assertEqual([k for k in (*others, "OPENAI_API_KEY") if k in env], [], "gemini")
        self.assertEqual(env["GEMINI_API_KEY"], "gemini-key")
        _, env = adapter({"kind": "claude"}, "worker")
        self.assertEqual((env[OAUTH_VARIABLE], env["ANTHROPIC_API_KEY"], env["ANTHROPIC_AUTH_TOKEN"]), ("oauth", "anthropic-key", "anthropic-token"))
        self.assertEqual([k for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY") if k in env], [])
        key = self.root / "deepseek.key"
        key.write_text("deepseek-key", encoding="utf-8")
        with patch("nightweaver.providers.default_home", return_value=self.root / "home"):
            _, env = adapter({"kind": "deepseek", "api_key_file": str(key)}, "worker")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "deepseek-key")
        self.assertNotIn(OAUTH_VARIABLE, env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        _, env = adapter({"kind": "command", "argv": ["tool"]}, "worker")
        self.assertEqual([k for k in (*others, "OPENAI_API_KEY", "GEMINI_API_KEY") if k in env], [], "command providers get no credentials")
        _, env = adapter({"kind": "command", "argv": ["tool"], "inherit_credentials": True}, "worker")
        self.assertEqual([env[k] for k in others], ["oauth", "anthropic-key", "anthropic-token", "deepseek-key"], "unless the route opts in")

    def test_provider_validation_accepts_oauth_token_file_for_claude_only(self):
        route = provider({"kind": "claude", "oauth_token_file": "secrets/claude.token"}, {"claude", "codex"}, "worker", self.root)
        self.assertEqual(route["oauth_token_file"], str((self.root / "secrets" / "claude.token").resolve()))
        for kind in ("codex", "gemini", "deepseek"):
            with self.subTest(kind), self.assertRaisesRegex(ValueError, "Claude adapter"):
                provider({"kind": kind, "oauth_token_file": "x"}, {kind}, "worker", self.root)
        with self.assertRaisesRegex(ValueError, "Unknown worker fields"):
            provider({"kind": "claude", "oauth_token": "inline"}, {"claude"}, "worker", self.root)
        from nightweaver.spec import anchor_provider_paths
        raw = {"worker": {"kind": "claude", "oauth_token_file": "t.txt"}, "milestones": [{"worker": {"kind": "claude", "oauth_token_file": "u.txt"}}],
               "pursue": {"judge": {"kind": "claude", "oauth_token_file": "v.txt"}}}
        anchor_provider_paths(raw, self.root)
        self.assertEqual(raw["pursue"]["judge"]["oauth_token_file"], str((self.root / "v.txt").resolve()))
        self.assertEqual(raw["worker"]["oauth_token_file"], str((self.root / "t.txt").resolve()))
        self.assertEqual(raw["milestones"][0]["worker"]["oauth_token_file"], str((self.root / "u.txt").resolve()))


class ProviderMessages(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="Nightweaver messages ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target in (patch("nightweaver.providers.user_environment", return_value=None),
                       patch("nightweaver.providers.default_home", return_value=self.root / "home")):
            target.start()
            self.addCleanup(target.stop)

    def run_failed(self, kind, *events):
        def fake(argv, cwd, log, timeout, **kwargs):
            Path(log).write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
            return 1

        config = {"kind": kind, "executable": sys.executable}
        with patch("nightweaver.providers.execute", side_effect=fake):
            return invoke(config, "worker", self.root, self.root / "call.jsonl", "prompt", 10, lambda: False)

    def fail_with(self, kind, *events):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.run_failed(kind, *events)
        return caught.exception

    def test_expired_claude_login_names_the_remedy(self):
        text = ("Your login reached its time limit and cannot be renewed. For unattended jobs use `claude setup-token` and set "
                "CLAUDE_CODE_OAUTH_TOKEN.")
        for message in (text, "this login reached its time limit", "OAuth token cannot be renewed", "run claude setup-token first"):
            with self.subTest(message):
                error = self.fail_with("claude", {"type": "result", "is_error": True, "result": message})
                self.assertEqual(error.kind, "authentication")
                self.assertIn("claude setup-token", str(error))
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", str(error))
                self.assertIn("--retry", str(error))
        generic = self.fail_with("claude", {"type": "result", "is_error": True, "result": "Failed to authenticate. Not logged in"})
        self.assertEqual((generic.kind, "setup-token" in str(generic)), ("authentication", False))
        code, _, _ = self.run_failed("codex", {"type": "turn.failed", "error": {"message": "login reached its time limit"}})
        self.assertEqual(code, 1, "only Claude's wording is an authentication problem; Codex just exits 1")

    def test_usage_limit_carries_the_reset_and_the_providers_words(self):
        error = self.fail_with("claude", {"type": "result", "is_error": True, "result": "You've hit your limit · resets 7pm (UTC)"})
        self.assertEqual(error.kind, "usage_limit")
        self.assertEqual((error.reset_at.hour, error.reset_at.minute, error.reset_at.tzinfo), (19, 0, timezone.utc))
        self.assertIn("resets 7pm (UTC)", str(error))
        self.assertTrue(str(error).startswith("claude reached a rate or usage limit. Retry after the limit resets."))

    def test_the_reset_can_come_from_a_rate_limit_event_in_the_log(self):
        target = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
        error = self.fail_with("claude", {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": target}},
                               {"type": "result", "is_error": True, "result": "Usage limit reached"})
        self.assertEqual(error.kind, "usage_limit")
        self.assertEqual(int(error.reset_at.timestamp()), target)

    def test_a_limit_without_a_time_has_no_reset(self):
        error = self.fail_with("codex", {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        self.assertEqual((error.kind, error.reset_at), ("usage_limit", None))

    def test_codex_relative_reset(self):
        error = self.fail_with("codex", {"type": "turn.failed", "error": {"message": "usage limit. try again in 1 hour 5 minutes."}})
        self.assertTrue(timedelta(minutes=64) < error.reset_at - datetime.now(timezone.utc) <= timedelta(minutes=65))

    def test_messages_never_echo_tokens(self):
        error = self.fail_with("claude", {"type": "result", "is_error": True, "result": f"usage limit for {FAKE_TOKEN} resets 7pm (UTC)"})
        self.assertNotIn(FAKE_TOKEN, str(error))


class Redaction(unittest.TestCase):
    def test_sk_ant_tokens_are_masked_everywhere(self):
        for token in (FAKE_TOKEN, "sk-ant-api03-" + "Zz9" * 20, "sk-ant-oat01-abcdefghijklmnop"):
            with self.subTest(token):
                self.assertEqual(redact(f"token {token} end"), "token [REDACTED] end")
                self.assertEqual(redact(f"CLAUDE_CODE_OAUTH_TOKEN={token}"), "CLAUDE_CODE_OAUTH_TOKEN=[REDACTED]")
                self.assertEqual(redact({"note": [f"x {token}"]}), {"note": ["x [REDACTED]"]})
        self.assertNotIn("sk-ant", redact('{"env": {"CLAUDE_CODE_OAUTH_TOKEN": "' + FAKE_TOKEN + '"}}'))

    def test_a_token_in_an_error_never_reaches_the_store(self):
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / "data")
            repo = Path(folder) / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "T")
            git(repo, "config", "user.email", "t@example.invalid")
            (repo / "a.txt").write_text("a", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            from nightweaver.engine import create_run
            run = create_run(store, validate({"repository": str(repo), "title": "T", "objective": "O", "worker": {"kind": "codex"},
                                              "gates": [{"name": "g", "argv": fixtures.fixture("gate")}],
                                              "milestones": [{"title": "One", "task": "Do"}]}))
            store.transition(run, "provider_unavailable", {"issue": f"bad {FAKE_TOKEN}"}, last_error=f"bad {FAKE_TOKEN}")
            dump = json.dumps(store.get(run)) + json.dumps(store.events(run, limit=None))
            self.assertNotIn(FAKE_TOKEN, dump)
            self.assertNotIn("Ab1_-Ab1_-", dump)


@unittest.skipUnless(os.name == "nt", "Windows controller")
class PursuitAwareChecks(unittest.TestCase):
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def route(self, mode, **extra):
        return {"kind": "command", "argv": fixtures.fixture(mode), **extra}

    def database(self):
        return database_check(self.store.root / "runs.sqlite3")

    def pursuit(self):
        return self.configured(fixtures.fixture("good"), pursue={"judge": self.route("review"), "judge_fallbacks": [self.route("implement")],
                                                                  "planner": self.route("gate")})

    def test_db_check_accepts_judging_and_planning_runs_at_the_end_of_the_cursor(self):
        run = self.pursuit()
        self.assertTrue(self.database()["ok"])
        for phase in ("judging", "planning"):
            self.store.transition(run, "fixture_phase", {}, phase=phase, step=1)
            self.assertEqual(self.database(), {"file": str(self.store.root / "runs.sqlite3"), "ok": True, "problems": []}, phase)
        self.store.transition(run, "fixture_stop", {}, phase="blocked", blocked_from="judging", step=1)
        self.assertTrue(self.database()["ok"], "a stopped pursuit sits at the end of the cursor")
        self.store.transition(run, "fixture_stop", {}, phase="blocked", blocked_from="planning", step=1)
        self.assertTrue(self.database()["ok"])

    def test_db_check_still_rejects_cursor_and_phase_corruption(self):
        run = self.pursuit()
        for changes in ({"phase": "judging", "step": 0}, {"phase": "planning", "step": 2}, {"phase": "ready", "step": 1},
                        {"phase": "blocked", "blocked_from": "ready", "step": 1}, {"phase": "blocked", "blocked_from": "judging", "step": 0},
                        {"phase": "done", "step": 0}, {"phase": "reviewing", "step": 1}):
            with self.subTest(changes):
                self.store.transition(run, "fixture_corruption", {}, **{"blocked_from": None, **changes})
                self.assertEqual(self.database()["problems"], ["invalid milestone cursor: 1"])
        self.store.transition(run, "fixture_corruption", {}, phase="thinking", step=0)
        self.assertEqual(self.database()["problems"], ["invalid run state: 1"])

    def test_doctor_checks_the_pursuit_routes_and_reports_the_claude_auth_source(self):
        spec = self.store.get(self.pursuit())["spec"]
        with patch("nightweaver.providers.user_environment", return_value=None), patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ.pop(OAUTH_VARIABLE, None)
            report = diagnose(self.store, spec)
        roles = {role for entry in report["providers"] for role in entry["roles"]}
        self.assertTrue({"pursuit judge", "pursuit judge fallback 1", "pursuit planner"} <= roles, roles)
        self.assertEqual(len(report["providers"]), 4, "worker, judge, judge fallback and planner are four distinct routes")
        self.assertIn(report["claude_auth"], ("cli_login", "none"))

    def test_a_blocked_pursuit_route_blocks_preflight(self):
        spec = self.store.get(self.configured(fixtures.fixture("good"), pursue={
            "judge": self.route("review"), "judge_fallbacks": [{"kind": "command", "argv": ["no-such-judge-tool-019.exe"]}]}))["spec"]
        report = preflight(self.store, spec)
        broken = [p for p in report["providers"] if "pursuit judge fallback 1" in p["roles"]][0]
        self.assertEqual((broken["status"], broken["reason"]), ("blocked", "missing_executable"))
        self.assertFalse(report["ready"])
        planner = [p for p in report["providers"] if "pursuit planner" in p["roles"]][0]
        self.assertIn("pursuit judge", planner["roles"], "an unset planner is the judge route")

    def test_claude_auth_values_in_the_report(self):
        token = self.root / "claude.token"
        token.write_text(FAKE_TOKEN, encoding="utf-8")
        profile = self.root / "profile"
        profile.mkdir()
        claude = {"kind": "claude", "executable": sys.executable}
        spec = self.store.get(self.configured(fixtures.fixture("good"), pursue={"judge": claude}))["spec"]
        spec_file = self.store.get(self.configured(fixtures.fixture("good"), pursue={"judge": {**claude, "oauth_token_file": str(token)}}))["spec"]
        with patch("nightweaver.providers.user_environment", return_value=None), patch.dict(os.environ):
            os.environ.pop(OAUTH_VARIABLE, None)
            os.environ["CLAUDE_CONFIG_DIR"] = str(profile)
            self.assertEqual(diagnose(self.store, spec)["claude_auth"], "none")
            (profile / ".credentials.json").write_text("{}", encoding="utf-8")
            self.assertEqual(diagnose(self.store, spec)["claude_auth"], "cli_login")
            with patch("nightweaver.providers.user_environment", return_value=FAKE_TOKEN):
                self.assertEqual(diagnose(self.store, spec)["claude_auth"], "oauth_token_user_env")
                os.environ[OAUTH_VARIABLE] = FAKE_TOKEN
                self.assertEqual(diagnose(self.store, spec)["claude_auth"], "oauth_token_env")
                report = diagnose(self.store, spec_file)
            self.assertEqual(report["claude_auth"], "oauth_token_file")
            self.assertEqual([p["auth"] for p in report["providers"] if p["provider"] == "claude"], ["oauth_token_file"])
            self.assertNotIn(FAKE_TOKEN, json.dumps(report))
            self.assertNotIn("Ab1_-Ab1_-", json.dumps(preflight(self.store, spec_file)))
            broken = self.store.get(self.configured(fixtures.fixture("good"), pursue={"judge": {**claude, "oauth_token_file": str(self.root / "gone")}}))["spec"]
            report = diagnose(self.store, broken)
            entry = [p for p in report["providers"] if p["provider"] == "claude"][0]
            self.assertEqual((entry["status"], entry["reason"], report["claude_auth"]), ("blocked", "missing_credentials", "oauth_token_file"))

    def test_the_doctor_without_a_plan_keeps_its_shape(self):
        report = diagnose(self.store)
        self.assertEqual(set(report), {"data_directory", "executables", "browser"})


if __name__ == "__main__":
    unittest.main()
