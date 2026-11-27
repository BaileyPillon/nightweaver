"""Nightweaver 0.19.2: the note that tells agy's read-only roles what is denied.

The role prompts only say "Remain read-only; do not edit anything." A Neon reviewer that finds Python gate scripts in the repository may try to
run them, be refused under the deny rules, answer anyway, and have that answer discarded (read_only_refused). invoke() now puts one short note in
front of the prompt of every read-only antigravity call, inside the stream-json user event, saying so. These tests check where the note goes and
where it does not: the worker, the other providers, agy's argv. Each builds on the capture harness of test_release_0192_agy (the real invoke()
over a stand-in agy that writes a capture); no model is called and no network is used."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from nightweaver import providers
from nightweaver.providers import SCHEMAS
from test_release_0191_gemini import Hermetic, write
from test_release_0192_agy import PROSE, READ_ONLY_MODES, ROLES, Called, raw, retargeted

# The wording, spelled out here on purpose: changing it must be a decision, not a side effect.
NOTE = ("This call is read-only. Every file write or edit and every shell or terminal command is denied, and an answer given after a denied "
        "attempt is discarded. Use only read tools: read files, list folders, search. Everything you need (the task, diffs, check results, "
        "logs) is in this prompt or in files you can read.")
PROMPT = "Review the change.\nRemain read-only; do not edit anything."


def capture(role):
    """A capture that gives `role` an answer it can finish with."""
    if role in ROLES:
        return retargeted(2, ROLES[role][0], SCHEMAS[role])
    if role in PROSE:
        payload = PROSE[role][0]
        return retargeted(1, payload, text="Here is the answer.\n```json\n" + json.dumps(payload) + "\n```\n")
    return raw(3)  # probe


def user_event(seen):
    """The one stream-json event agy was sent on stdin, checked to be exactly one line."""
    stdin = seen["prompt"]
    assert stdin.endswith("\n") and stdin.count("\n") == 1, "one event, one line"
    return json.loads(stdin)


class TheNote(Called):
    def test_the_wording(self):
        self.assertEqual(providers.AGY_READ_ONLY_NOTE, NOTE)
        self.assertNotIn("\n", NOTE, "one plain paragraph")

    def test_every_read_only_role_is_covered(self):
        self.assertEqual(set(ROLES) | set(PROSE) | {"probe"}, set(READ_ONLY_MODES))
        self.assertEqual(len(READ_ONLY_MODES), 10)

    def test_each_read_only_role_sends_the_note_once_in_front_of_its_prompt(self):
        for role in READ_ONLY_MODES:
            with self.subTest(role=role):
                (code, _, _), seen = self.call(capture(role), mode=role, prompt=PROMPT)
                self.assertEqual(code, 0)
                self.assertIn("--settings", seen["argv"], "these are the calls that get the deny rules")
                event = user_event(seen)
                self.assertEqual(set(event), {"event", "message"})
                self.assertEqual(event["event"], "user")
                self.assertEqual(set(event["message"]), {"content"})
                content = event["message"]["content"]
                self.assertEqual(content.count(NOTE), 1, "exactly once")
                self.assertEqual(content, NOTE + "\n\n" + PROMPT, "in front of the prompt, which is otherwise untouched")
                self.assertEqual(seen["executed"], 1)

    def test_a_resumed_read_only_call_carries_the_note_once_too(self):
        (code, _, _), seen = self.call(capture("reviewer"), mode="reviewer", prompt=PROMPT, session="conv-3")
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"][seen["argv"].index("--conversation") + 1], "conv-3")
        self.assertEqual(user_event(seen)["message"]["content"], NOTE + "\n\n" + PROMPT)

    def test_a_prompt_that_already_says_read_only_is_not_changed_beyond_the_note(self):
        prompt = "Remain read-only; do not edit anything.\n\nTask: add(1, 2)\nChanges:\n```diff\n+x\n```\nChecks:\nPASS"
        _, seen = self.call(capture("judge"), mode="judge", prompt=prompt)
        self.assertEqual(user_event(seen)["message"]["content"], NOTE + "\n\n" + prompt)

    def test_the_note_travels_on_stdin_only_never_in_agys_arguments(self):
        _, seen = self.call(capture("reviewer"), mode="reviewer", prompt=PROMPT)
        self.assertFalse([argument for argument in seen["argv"] if NOTE[:20] in argument])

    def test_the_worker_does_not_get_it(self):
        (code, _, _), seen = self.call(raw(7), mode="worker", prompt=PROMPT)
        self.assertEqual(code, 0)
        rules = json.loads(Path(seen["argv"][seen["argv"].index("--settings") + 1]).read_text(encoding="utf-8"))["permissions"]
        self.assertFalse(set(providers.AGY_DENY) & set(rules["deny"]), "none of the write deny rules: the worker edits")
        self.assertIn("--disable-slash-commands", seen["argv"])
        self.assertEqual(user_event(seen)["message"]["content"], PROMPT, "the worker prompt is exactly what the engine built")


class TheSlashGuard(Called):
    def test_a_read_only_prompt_that_starts_with_a_slash_cannot_run_as_a_slash_command(self):
        for role in READ_ONLY_MODES:
            with self.subTest(role=role):
                _, seen = self.call(capture(role), mode=role, prompt="/permissions please")
                content = user_event(seen)["message"]["content"]
                self.assertEqual(content, NOTE + "\n\n/permissions please")
                self.assertFalse(content.lstrip().startswith("/"), "agy takes a message that starts with / as a command")

    def test_the_guard_is_still_there_for_a_note_that_is_empty_or_moved(self):
        with patch.object(providers, "AGY_READ_ONLY_NOTE", ""):
            _, seen = self.call(capture("reviewer"), mode="reviewer", prompt="/permissions please")
        content = user_event(seen)["message"]["content"]
        self.assertTrue(content.startswith("Task:\n"), content)
        self.assertFalse(content.lstrip().startswith("/"))
        self.assertTrue(content.endswith("/permissions please"))

    def test_the_worker_is_still_left_to_its_flag(self):
        _, seen = self.call(raw(7), mode="worker", prompt="/permissions please")
        self.assertIn("--disable-slash-commands", seen["argv"])
        self.assertEqual(user_event(seen)["message"]["content"], "/permissions please")


class OtherProviders(Hermetic):
    """Claude, Codex, DeepSeek and Gemini take the prompt on stdin as plain text, as before, in every role and for a prompt that starts with /."""

    LOGS = {"claude": [{"type": "system", "session_id": "s-1"}, {"type": "result", "result": "READY"}],
            "deepseek": [{"type": "system", "session_id": "s-1"}, {"type": "result", "result": "READY"}],
            "codex": [{"type": "thread.started", "thread_id": "t-1"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}}, {"type": "turn.completed"}],
            "gemini": [{"type": "init", "session_id": "g-1", "model": "gemini-2.5-pro"},
                       {"type": "message", "role": "assistant", "content": "READY", "delta": True},
                       {"type": "result", "status": "success", "stats": {"total_tokens": 9}}]}

    def setUp(self):
        super().setUp()
        self.enterContext(patch("nightweaver.providers.default_home", return_value=self.root / "home"))
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-test-key"  # a made-up value; Hermetic restores the environment

    def call(self, kind, mode, prompt):
        seen = {}

        def fake(argv, cwd, log, timeout, **options):
            seen.update(argv=argv, prompt=options["prompt"])
            write(log, *self.LOGS[kind])
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake):
            code, _, _ = providers.invoke({"kind": kind, "executable": sys.executable}, mode, self.root, self.root / "call.jsonl", prompt, 10,
                                          lambda: False)
        self.assertEqual(code, 0)
        return seen

    def test_the_prompt_reaches_every_other_provider_exactly_as_given(self):
        for kind in ("claude", "codex", "deepseek", "gemini"):
            for mode in ("worker", "reviewer", "judge", "planner", "probe"):
                for prompt in (PROMPT, "/permissions please", "  /status"):
                    with self.subTest(kind=kind, mode=mode, prompt=prompt):
                        seen = self.call(kind, mode, prompt)
                        self.assertEqual(seen["prompt"], prompt)
                        self.assertNotIn("read-only call", seen["prompt"])
                        self.assertFalse([argument for argument in seen["argv"] if NOTE[:20] in argument])


if __name__ == "__main__":
    unittest.main()
