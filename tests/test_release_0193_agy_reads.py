"""Nightweaver 0.19.3: agy's roles may read outside the repository, and a call agy ends silently after an auto-denied prompt is resumed once.

A real run on 0.19.2, in a research repository whose objective names files outside it, had a goal planner (antigravity, gemini-4-neon, cwd a repository on drive D:) read six files in the repository and three
outside it, in another folder on C:, in parallel. The first outside read was auto-denied ("user denied permission for read_file"): a headless agy cannot answer a permission
prompt, so it finishes the batch, makes no further model call and returns status SUCCESS with an empty response. 0.19.2 saw no answer, no
refusal and no write, raised no_answer without a word about the denial, and the run never launched.

The captures in tests/agy_streams named reads-01 to reads-07 are what agy 1.5.18 wrote in the lab runs of 2026-11-26 (a scratch repository
and a scratch folder beside it, reads-07 the structure of the real planner stream of that run), with ids and paths replaced; permissions-read-allow and
permissions-worker-allow are what `agy -p /permissions --settings FILE` printed for the two files Nightweaver now writes (the read-only one was
taken again in round 3 for the final rules, the two network deny rules included; the project and shared scopes, which hold the machine's own
rules, are emptied). No test here calls a model or uses the network."""

import argparse
import json
import os
import re
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nightweaver import providers, readiness
from nightweaver.cli import plan_goal
from nightweaver.engine import Run
from nightweaver.providers import AGY_ALLOW, AGY_DENY, AGY_READ_ONLY_NOTE, AGY_RESUME, AGY_RESUME_FLOOR, SCHEMAS, ProviderUnavailable
from nightweaver.store import Store
import test_windows as fixtures
from test_release_0191_gemini import ALL_MODES, Hermetic
from test_release_0192_agy import BARE, LOADED, PROSE, READ_ONLY_MODES, ROLES, blanked, part, put, raw, rules_output, tooled
from test_windows import fixture

STREAMS = Path(__file__).parent / "agy_streams"
ALLOWED = (STREAMS / "permissions-read-allow.jsonl").read_text(encoding="utf-8")  # the read-only file, as agy listed it
WORKER_ALLOWED = (STREAMS / "permissions-worker-allow.jsonl").read_text(encoding="utf-8")
AUTO = ('permission check failed for read_file "C:/work/outside/x.txt": user denied permission for read_file(C:/work/outside/x.txt)\n'
        "Do not attempt to circumvent this denial by rephrasing the command, using alternative tools/scripts (e.g. python, sh, curl), or accessing "
        "the same target resource. Proceed without performing this action.")
RULE = 'permission check failed for write_file "x": Permission denied for write_file(x). Matches user-configured deny rule.'
FIRST_READ = "C:/work/outside/alpha/notes_one.txt"  # the read reads-01 had denied
FIRST_USAGE, RESUMED_USAGE = 12854, 2806  # the own usage of reads-01 and of its resume (reads-02), whose result.usage says 15660 for both


def reads(number):
    """The lines of one capture, as the log held them (agy's stderr line sits before the result, where Nightweaver's merged log has it)."""
    return next(STREAMS.glob(f"reads-{number:02d}-*.jsonl")).read_text(encoding="utf-8").splitlines()


def read_events(number):
    return [json.loads(line) for line in reads(number) if line.startswith("{")]


def answered(text=None, payload=None, schema=None):
    """The resume capture (reads-02) with another answer in it: the model's words and the result's response, and with a schema agy's
    structured_output too. Its init, ids, steps and usage stay, so the resumed call counts the 2806 tokens the real one did."""
    said = text if text is not None else json.dumps(payload) + "\n"
    out = []
    for event in read_events(2):
        body = dict(part(event))
        if event["event"] == "step_update" and body["step_type"] == "agent_response":
            if body["state"] != "DONE":
                continue
            body["text_delta"] = said
        elif event["event"] == "init" and schema:
            body["json_schema"] = schema
        elif event["event"] == "result":
            body["response"] = said
            body.pop("structured_output", None)
            if schema:
                body.update(structured_output=payload, json_schema=schema)
        out.append({"event": event["event"], **({"conversation_id": event["conversation_id"]} if "conversation_id" in event else {}),
                    event["event"]: body})
    return out


def lines_of(items):
    return [item if isinstance(item, str) else json.dumps(item) for item in items]


def with_tools(items, *calls, step_type="tool"):
    """A capture with more tool steps in front of its stderr line and result: (name, parameters) completes, (name, parameters, message) ends in
    state ERROR with that message, as agy writes a refused or a denied step."""
    lines = lines_of(items)
    conversation = next(json.loads(line)["conversation_id"] for line in lines if line.startswith("{"))
    made = []
    for index, (name, params, *refusal) in enumerate(calls, start=90):
        info = {"name": name, "parameters": params}
        end = {**info, "error": {"type": "TOOL_ERROR", "message": refusal[0]}} if refusal else info
        for state, tool_info in (("ACTIVE", info), ("ERROR" if refusal else "DONE", end)):
            made.append(json.dumps({"event": "step_update", "step_update": {
                "conversation_id": conversation, "step_index": index, "state": state, "step_type": step_type, "tool_name": name,
                "tool_info": tool_info}}))
    at = next(i for i, line in enumerate(lines) if not line.startswith("{") or json.loads(line).get("event") == "result")
    return [*lines[:at], *made, *lines[at:]]


def narrated(items, *said, response=None):
    """A capture whose agent messages say what is given: every text in it is removed first, then each (step index, text) is set on that
    agent_response step (a step the capture lacks is added at the end), and the result's response joins the texts in order, as agy's does
    (every agent message of the run, narration included), unless `response` says otherwise."""
    lines, texts, present, out = lines_of(items), dict(said), set(), []
    conversation = next(json.loads(line)["conversation_id"] for line in lines if line.startswith("{"))
    for line in lines:
        if line.startswith("{"):
            event = json.loads(line)
            body = event[event["event"]]
            if event["event"] == "step_update" and body["step_type"] == "agent_response":
                body.pop("text_delta", None)
                if body["state"] == "DONE" and body["step_index"] in texts:
                    body["text_delta"] = texts[body["step_index"]]
                    present.add(body["step_index"])
            if event["event"] == "result":
                body["response"] = response if response is not None else "".join(text for _, text in sorted(texts.items()))
                body.pop("structured_output", None)
            line = json.dumps(event)
        out.append(line)
    added = [json.dumps({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index, "state": "DONE",
                                                                 "step_type": "agent_response", "text_delta": text}})
             for index, text in sorted(texts.items()) if index not in present]
    at = next(i for i, line in enumerate(out) if not line.startswith("{") or json.loads(line).get("event") == "result")
    return [*out[:at], *added, *out[at:]]


def with_line(items, line):
    """A capture with one more line that is not JSON (agy's stderr, which Nightweaver's merged log puts before the result)."""
    lines = lines_of(items)
    at = next(i for i, text in enumerate(lines) if not text.startswith("{") or json.loads(text).get("event") == "result")
    return [*lines[:at], line, *lines[at:]]


def altered(items, **result):
    """A capture whose result event has these fields changed."""
    out = []
    for line in lines_of(items):
        if line.startswith("{") and json.loads(line)["event"] == "result":
            event = json.loads(line)
            event["result"].update(result)
            line = json.dumps(event)
        out.append(line)
    return out


class Resumed(Hermetic):
    """invoke() with agy replaced by one that writes the next of a list of streams into whatever log it is asked to write."""

    def setUp(self):
        super().setUp()
        providers.AGY_READ_ALLOWED.clear()
        self.addCleanup(providers.AGY_READ_ALLOWED.clear)

    def agy(self, streams, calls, codes=None, agy=None, took=None):
        """`took` maps a call's index to the seconds it "runs": providers.monotonic is a fake clock that only moves by that, so what the first
        call leaves of its timeout is exact."""
        clock = [1000.0]

        def fake(argv, cwd, path, timeout, **options):
            if len(calls) >= len(streams):
                raise AssertionError(f"agy was called a {len(calls) + 1}th time; {len(streams)} stream(s) were expected")
            calls.append({"argv": list(argv), "log": Path(path), "timeout": timeout, "cwd": cwd,
                          "options": {key: value for key, value in options.items() if key not in ("prompt", "env")},
                          "prompt": json.loads(options["prompt"])["message"]["content"]})
            put(Path(path), streams[len(calls) - 1])
            clock[0] += (took or {}).get(len(calls) - 1, 0)
            return (codes or {}).get(len(calls) - 1, 0)

        stack = ExitStack()
        stack.enter_context(patch("nightweaver.providers.monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(patch("nightweaver.providers.execute", side_effect=fake))
        stack.enter_context(patch("nightweaver.providers.agy_run", side_effect=agy or rules_output(ALLOWED)))
        return stack

    def asked(self, streams, *, mode="planner", calls=None, codes=None, agy=None, prompt="Plan it.", config=None, log=None, took=None,
              timeout=600, paused=None, **kwargs):
        """invoke()'s outcome for these streams; `calls` (a list, when given) collects what each agy call was started with. The timeout is
        long enough for a resume (see AGY_RESUME_FLOOR) unless a test says otherwise."""
        calls = [] if calls is None else calls
        config = {"kind": "antigravity", "executable": sys.executable, **(config or {})}
        with self.agy(streams, calls, codes, agy, took):
            return providers.invoke(config, mode, self.root, log or self.root / "call.jsonl", prompt, timeout, paused or (lambda: False), **kwargs)

    def failed(self, streams, *, calls=None, **kwargs):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.asked(streams, calls=calls, **kwargs)
        return caught.exception

    def parsed(self, items, mode="planner"):
        path = self.root / "parsed.jsonl"
        put(path, lines_of(items))
        return providers.agy_stream(path, mode)


class Captures(unittest.TestCase):
    def test_seven_captures_and_two_permission_listings_were_added(self):
        self.assertEqual(sorted(path.name[:8] for path in STREAMS.glob("reads-*.jsonl")), [f"reads-0{n}" for n in range(1, 8)])
        self.assertEqual(len(list(STREAMS.glob("exp-*.jsonl"))), 10, "the 0.19.2 captures are untouched")
        self.assertEqual(providers.agy_permissions(ALLOWED)["allow"], set(AGY_ALLOW))
        self.assertEqual(providers.agy_permissions(WORKER_ALLOWED)["deny"], set())

    def test_they_hold_no_account_name_local_path_email_or_real_conversation_id(self):
        ids = set()
        for path in sorted(STREAMS.glob("reads-*.jsonl")) + [STREAMS / "permissions-read-allow.jsonl", STREAMS / "permissions-worker-allow.jsonl"]:
            text = path.read_text(encoding="utf-8")
            for word in ("Administrator", "Users", "Nightweaver-trials", "agy-reads-lab", "agy-lab", "@", "AIza", "ya29.", "D:/", "D:" + chr(92)):
                self.assertNotIn(word, text, path.name)
            ids |= set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text))
        self.assertTrue(ids)
        self.assertTrue(all(found.startswith("00000000-0000-4000-8000-0000000193") for found in ids), ids)

    def test_the_denied_captures_are_what_the_lab_runs_showed(self):
        for number in (1, 4, 6, 7):
            with self.subTest(capture=number):
                items = reads(number)
                events = [json.loads(line) for line in items if line.startswith("{")]
                result = part(events[-1])
                self.assertEqual((events[-1]["event"], result["status"], result["response"], result["num_turns"]), ("result", "SUCCESS", "", 1))
                self.assertEqual(result["denied_actions"], [{"action": "read_file", "display_name": "ReadFile"}])
                stray = [line for line in items if not line.startswith("{")]
                self.assertEqual(len(stray), 1)
                self.assertTrue(stray[0].startswith("jetski: no output produced"))
                self.assertLess(items.index(stray[0]), len(items) - 1, "Nightweaver's merged log puts agy's stderr line before the result")
                failing = [part(e) for e in events if e["event"] == "step_update" and part(e)["state"] == "ERROR"]
                self.assertEqual(len(failing), 1)
                self.assertIn("user denied permission for read_file(", failing[0]["tool_info"]["error"]["message"])
                self.assertNotIn("finished_reason", result)

    def test_the_allowed_captures_read_everything_and_answer(self):
        for number in (3, 5):
            with self.subTest(capture=number):
                events = read_events(number)
                result = part(events[-1])
                self.assertEqual((result["status"], result["response"].strip()), ("SUCCESS", "AMBER-11; CORAL-44; ALPHA-LOADER-33; BETA-BARS-66"))
                self.assertNotIn("denied_actions", result)
                self.assertFalse([e for e in events if e["event"] == "step_update" and part(e)["state"] == "ERROR"])
                self.assertEqual([line for line in reads(number) if not line.startswith("{")], [])

    def test_the_resume_capture_is_the_same_conversation_with_its_own_usage_inside_a_cumulative_total(self):
        first, resumed = read_events(1), read_events(2)
        self.assertEqual(first[0]["conversation_id"], resumed[0]["conversation_id"])
        own = sum(part(e)["usage"]["total_tokens"] for e in resumed if e["event"] == "step_update" and "usage" in part(e))
        self.assertEqual((own, part(resumed[-1])["usage"]["total_tokens"]), (RESUMED_USAGE, FIRST_USAGE + RESUMED_USAGE))
        self.assertEqual(part(first[-1])["usage"]["total_tokens"], FIRST_USAGE)
        self.assertEqual(part(resumed[-1])["num_turns"], 2, "cumulative, as usage is")


class SettingsFiles(Hermetic):
    def test_the_read_only_file_allows_local_reads_and_keeps_every_deny_rule(self):
        target = providers.settings_file()
        rules = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(list(rules), ["permissions"])
        self.assertEqual(rules["permissions"]["allow"], ["read_file(*)", "view_file(*)", "list_dir(*)", "grep_search(*)", "find_by_name(*)"])
        self.assertEqual(rules["permissions"]["deny"][:7], ["write_file(*)", "write_file(/)", "write_file(.)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"])
        self.assertEqual(rules["permissions"]["deny"][7:], providers.agy_secret_rules(), "then the reads of what holds a login, and with no user file nothing else")
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(target.is_relative_to(self.root / "home"), "Nightweaver's own folder, never a worktree")

    def test_the_worker_file_allows_local_reads_denies_no_write_and_only_the_login_reads(self):
        target = providers.worker_settings_file()
        rules = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(rules, {"permissions": {"allow": list(AGY_ALLOW), "deny": providers.agy_secret_rules()}})
        self.assertFalse(set(AGY_DENY) & set(rules["permissions"]["deny"]), "it may edit")
        self.assertNotEqual(target, providers.settings_file())
        self.assertTrue(target.is_relative_to(self.root / "home"))

    def test_a_stale_copy_of_either_file_is_rewritten(self):
        secrets = providers.agy_secret_rules()
        for make, expected in ((providers.settings_file, {"allow": list(AGY_ALLOW), "deny": [*AGY_DENY, *secrets]}),
                               (providers.worker_settings_file, {"allow": list(AGY_ALLOW), "deny": secrets})):
            target = make()
            target.write_text('{"permissions": {"deny": []}}', encoding="utf-8")
            self.assertEqual(json.loads(make().read_text(encoding="utf-8"))["permissions"], expected)

    def test_the_allow_rules_are_agys_local_read_tools_and_nothing_that_reaches_out_or_changes_anything(self):
        names = [rule.split("(")[0] for rule in AGY_ALLOW]
        self.assertEqual(names, ["read_file", "view_file", "list_dir", "grep_search", "find_by_name"])
        self.assertTrue(set(names) <= set(providers.AGY_READS))
        for network in ("read_url_content", "search_web", "read_resource", "list_resources", "read_browser_page"):
            self.assertNotIn(network, names)
        self.assertFalse([name for name in names if providers.agy_writes(name)])
        self.assertEqual({rule.split("(", 1)[1] for rule in AGY_ALLOW}, {"*)"}, "the one form the live runs matched; a glob scope did not (outside/**)")
        self.assertFalse(set(AGY_ALLOW) & set(AGY_DENY))

    def test_every_role_passes_one_settings_file_and_only_the_worker_has_none_of_the_write_deny_rules(self):
        for mode in ALL_MODES:
            argv, _ = providers.adapter({"kind": "antigravity", "model": "gemini-4-neon", "effort": "xhigh"}, mode)
            self.assertEqual(argv.count("--settings"), 1, mode)
            target = Path(argv[argv.index("--settings") + 1])
            self.assertEqual(target, providers.worker_settings_file() if mode == "worker" else providers.settings_file(), mode)
            rules = json.loads(target.read_text(encoding="utf-8"))["permissions"]
            self.assertEqual(rules["allow"], list(AGY_ALLOW), mode)
            self.assertEqual(rules["deny"], providers.agy_secret_rules() if mode == "worker" else [*AGY_DENY, *providers.agy_secret_rules()], mode)

    def test_the_worker_keeps_its_flag_and_may_edit_and_read_only_roles_do_not_get_the_flag(self):
        for mode in ALL_MODES:
            argv, _ = providers.adapter({"kind": "antigravity"}, mode, "conv-1")
            self.assertEqual("--disable-slash-commands" in argv, mode == "worker", mode)
            self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits", mode)
            self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1", mode)
            self.assertNotIn("--dangerously-skip-permissions", argv, mode)
            self.assertNotIn("--sandbox", argv, mode)

    def test_the_files_hold_exactly_what_agy_listed_as_loaded(self):
        read_only, worker = providers.agy_permissions(ALLOWED), providers.agy_permissions(WORKER_ALLOWED)
        self.assertEqual(read_only, {"allow": set(AGY_ALLOW), "deny": set(AGY_DENY)})
        self.assertEqual(worker, {"allow": set(AGY_ALLOW), "deny": set()})
        self.assertEqual(set(json.loads(providers.settings_file().read_text(encoding="utf-8"))["permissions"]["allow"]), read_only["allow"])
        self.assertEqual(json.loads(ALLOWED.splitlines()[0])["command"]["data"]["permissions"][2]["scope"], "global", "agy files them under the global scope")


READ_TOOLS = [rule.split("(")[0] for rule in AGY_ALLOW]


def rule_place(rule):
    return rule[rule.index("(") + 1:-1]


def where(path):
    """A path the way a rule writes it."""
    return Path(path).resolve().as_posix()


class UserRules(Hermetic):
    """Review finding 1. agy replaces permissions.allow and permissions.deny of the user's own settings.json with the --settings file's (agy 1.5.18,
    free /permissions call with the home pointed at a scratch folder: the user's file alone listed the user's rules, the same file beside the
    generated one listed only the generated rules), so with read_file(*) allowed, a generated file that left out the user's deny rules turned a
    read the user had refused (read_file(**/.env)) into one every read-only role could make, and the allow rules a worker had run under before
    0.19.3 were dropped too."""

    USER = {"allow": ["command(git status)"], "deny": ["read_file(**/.env)", "read_file(D:/private)"]}

    def rules(self, make):
        return json.loads(make().read_text(encoding="utf-8"))["permissions"]

    def test_a_read_only_role_keeps_the_users_own_deny_rules_beside_its_own(self):
        self.agy_settings(permissions=self.USER)
        deny = self.rules(providers.settings_file)["deny"]
        self.assertEqual(deny, [*AGY_DENY, *providers.agy_secret_rules(), *self.USER["deny"]])
        self.assertLessEqual(set(AGY_DENY) | set(self.USER["deny"]), set(deny))

    def test_a_read_only_role_does_not_gain_the_users_allow_rules(self):
        """The user's grants (a network tool among them) must not widen a role that reads files a prompt-injected repository controls."""
        self.agy_settings(permissions={"allow": ["command(git status)", "read_url_content(*)"], "deny": []})
        self.assertEqual(self.rules(providers.settings_file)["allow"], list(AGY_ALLOW))

    def test_the_worker_keeps_the_users_allow_and_deny_rules_and_none_of_the_write_deny_rules(self):
        self.agy_settings(permissions=self.USER)
        rules = self.rules(providers.worker_settings_file)
        self.assertEqual(rules["allow"], [*AGY_ALLOW, "command(git status)"])
        self.assertEqual(rules["deny"], [*providers.agy_secret_rules(), *self.USER["deny"]])
        self.assertFalse(set(AGY_DENY) & set(rules["deny"]))

    def test_a_rule_the_user_repeats_from_nightweavers_own_is_listed_once(self):
        self.agy_settings(permissions={"allow": ["read_file(*)", "command(git status)", "command(git status)"],
                                       "deny": ["command(*)", "read_file(**/.env)", "read_file(**/.env)"]})
        self.assertEqual(self.rules(providers.settings_file)["deny"].count("command(*)"), 1)
        self.assertEqual(self.rules(providers.settings_file)["deny"].count("read_file(**/.env)"), 1)
        self.assertEqual(self.rules(providers.worker_settings_file)["allow"], [*AGY_ALLOW, "command(git status)"])

    def test_a_settings_file_that_says_nothing_usable_adds_nothing_and_never_stops_a_call(self):
        plain = [self.rules(providers.settings_file), self.rules(providers.worker_settings_file)]
        for fields in ({}, {"permissions": None}, {"permissions": "deny everything"}, {"permissions": []},
                       {"permissions": {"allow": "command(*)", "deny": {"read_file(*)": True}}},
                       {"permissions": {"allow": [1, None, ["x"]], "deny": [{"rule": "read_file(*)"}, 7]}}):
            self.agy_settings(**fields)
            self.assertEqual([self.rules(providers.settings_file), self.rules(providers.worker_settings_file)], plain, fields)
        target = self.root / "user" / ".gemini" / "antigravity-cli" / "settings.json"
        for text in ("{not json", "[]"):
            target.write_text(text, encoding="utf-8")
            self.assertEqual([self.rules(providers.settings_file), self.rules(providers.worker_settings_file)], plain, text)

    def test_every_role_passes_a_file_that_holds_the_users_deny_rules(self):
        self.agy_settings(permissions=self.USER)
        for mode in ALL_MODES:
            argv, _ = providers.adapter({"kind": "antigravity"}, mode)
            deny = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))["permissions"].get("deny", [])
            for rule in self.USER["deny"]:
                self.assertIn(rule, deny, mode)

    def test_a_change_in_the_users_rules_is_another_file_so_a_call_under_way_keeps_the_rules_it_started_with(self):
        before = providers.settings_file()
        text = before.read_text(encoding="utf-8")
        self.agy_settings(permissions=self.USER)
        after = providers.settings_file()
        self.assertNotEqual(before, after)
        self.assertEqual(before.read_text(encoding="utf-8"), text, "the file of the call already started is not rewritten")
        self.assertEqual(providers.settings_file(), after, "the same rules, the same file")
        self.assertNotEqual(providers.worker_settings_file(), after)

    def test_the_guard_asks_agy_about_the_file_the_calls_are_started_with(self):
        self.agy_settings(permissions=self.USER)
        asked = []
        with patch("nightweaver.providers.agy_run", side_effect=lambda *args, **kwargs: asked.append(args) or (0, ALLOWED)):
            providers.antigravity_permissions(sys.executable, {}, self.root)
        argv, _ = providers.adapter({"kind": "antigravity"}, "reviewer")
        self.assertEqual(asked[0][asked[0].index("--settings") + 1], argv[argv.index("--settings") + 1])
        self.assertIn(self.USER["deny"][0], Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))


class Logins(Hermetic):
    """Review finding 2. read_file(*) opens whatever the account can read, and a deny rule wins over an allow rule in the same call, so the
    files that hold a login are denied to every read tool (live runs proved the rule forms and the precedence; that a deny rule on a
    plain absolute path matches the files under it was not run, since the review allowed no model call)."""

    def deny(self, make=None):
        return json.loads((make or providers.settings_file)().read_text(encoding="utf-8"))["permissions"]["deny"]

    def denied(self, path, make=None):
        return {tool for tool in READ_TOOLS if f"{tool}({where(path)})" in self.deny(make)}

    def test_every_read_tool_is_denied_at_providers_json_and_the_profiles_folder_for_every_role(self):
        for make in (providers.settings_file, providers.worker_settings_file):
            for place in (self.root / "home" / "providers.json", self.root / "home" / "profiles"):
                self.assertEqual(self.denied(place, make), set(READ_TOOLS), (make.__name__, place.name))

    def test_nightweavers_home_is_never_denied_whole_because_the_worktrees_and_evidence_a_review_reads_are_in_it(self):
        worktree = self.root / "home" / "runs" / "0123456789ab" / "worktrees" / "01"
        evidence = self.root / "home" / "runs" / "0123456789ab" / "evidence" / "01" / "01"
        for make in (providers.settings_file, providers.worker_settings_file):
            for rule in self.deny(make):
                if rule.split("(")[0] in READ_TOOLS:
                    for read in (worktree, evidence, self.root / "home", self.root / "home" / "planning", self.root / "home" / "runs"):
                        self.assertFalse(Path(where(read)).is_relative_to(rule_place(rule)), (rule, read))

    def test_the_deepseek_key_file_is_denied_whether_providers_json_or_the_environment_names_it(self):
        named, other = self.root / "keys" / "named.txt", self.root / "keys" / "from-environment.txt"
        named.parent.mkdir(parents=True)
        named.write_text("SENTINEL-NAMED-KEY", encoding="utf-8")
        other.write_text("SENTINEL-ENVIRONMENT-KEY", encoding="utf-8")
        (self.root / "home").mkdir()
        (self.root / "home" / "providers.json").write_text(json.dumps({"deepseek": {"api_key_file": str(named)}}), encoding="utf-8")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY_FILE": str(other)}):
            for make in (providers.settings_file, providers.worker_settings_file):
                self.assertEqual(self.denied(named, make), set(READ_TOOLS))
                self.assertEqual(self.denied(other, make), set(READ_TOOLS))
                self.assertNotRegex(make().read_text(encoding="utf-8"), "SENTINEL", "the location goes in the file, never the key")

    def test_a_providers_json_that_is_not_what_it_should_be_adds_nothing_and_never_stops_a_call(self):
        (self.root / "home").mkdir()
        plain = providers.agy_secret_rules()
        for text in ("{nope", "[]", '{"deepseek": "x"}', '{"deepseek": {"api_key_file": 5}}', '{"deepseek": {"api_key_file": "  "}}'):
            (self.root / "home" / "providers.json").write_text(text, encoding="utf-8")
            self.assertEqual(providers.agy_secret_rules(), plain, text)

    def test_the_login_file_of_each_cli_is_denied_in_its_default_home_or_the_one_the_environment_chooses(self):
        user = self.root / "user"
        files = [user / ".claude" / ".credentials.json", user / ".codex" / "auth.json", user / ".gemini" / "oauth_creds.json"]
        for path in files:
            self.assertEqual(self.denied(path), set(READ_TOOLS), path)
        chosen = {"CLAUDE_CONFIG_DIR": self.root / "claude-here", "CODEX_HOME": self.root / "codex-here", "GEMINI_CLI_HOME": self.root / "gemini-here"}
        with patch.dict(os.environ, {name: str(path) for name, path in chosen.items()}):
            self.assertEqual(self.denied(chosen["CLAUDE_CONFIG_DIR"] / ".credentials.json"), set(READ_TOOLS))
            self.assertEqual(self.denied(chosen["CODEX_HOME"] / "auth.json"), set(READ_TOOLS))
            self.assertEqual(self.denied(chosen["GEMINI_CLI_HOME"] / ".gemini" / "oauth_creds.json"), set(READ_TOOLS))
            self.assertEqual(self.denied(files[0]), set(), "the default home is not what this process uses any more")

    def test_only_the_login_files_are_denied_in_those_homes_not_the_folders_agy_and_the_clis_keep_their_work_in(self):
        places = {rule_place(rule) for rule in self.deny()} - {rule_place(rule) for rule in AGY_DENY}
        self.assertFalse([place for place in places if Path(place).name in (".claude", ".codex", ".gemini", "antigravity-cli", "brain")])

    def test_the_profile_key_and_token_files_of_a_plan_are_found_wherever_its_route_sits(self):
        plan = {"worker": {"kind": "deepseek", "api_key_file": "a-key.txt"},
                "pursue": {"judge": {"kind": "claude", "profile_dir": "judge-profile"}, "explore": {"workers": [{"kind": "codex", "profile_dir": "w1"}]}},
                "milestones": [{"worker": {"kind": "claude", "oauth_token_file": "tok.txt"}, "reviewer_fallbacks": [{"kind": "claude", "profile_dir": "rf"}]}],
                "title": "profile_dir", "other": {"profile_dir": 5, "api_key_file": ""}}
        self.assertEqual(sorted(providers.credential_paths(plan)), ["a-key.txt", "judge-profile", "rf", "tok.txt", "w1"])
        self.assertEqual(providers.credential_paths({}), [])
        self.assertEqual(providers.credential_paths("profile_dir"), [])

    def test_protected_places_are_denied_to_every_read_tool_for_every_role_once_a_plan_names_them(self):
        profile, key, token = self.root / "profiles-of-the-plan" / "judge", self.root / "plan-keys" / "deepseek.key", self.root / "plan-keys" / "claude.token"
        providers.protect([str(profile), str(key), str(token)], keep=(self.root / "repository", self.root / "home" / "runs" / "0123456789ab"))
        for make in (providers.settings_file, providers.worker_settings_file):
            for place in (profile, key, token):
                self.assertEqual(self.denied(place, make), set(READ_TOOLS), (make.__name__, place.name))

    def test_a_place_that_holds_the_repository_or_the_run_folder_is_not_protected_because_denying_it_would_deny_the_review(self):
        repository, run = self.root / "work" / "repository", self.root / "home" / "runs" / "0123456789ab"
        providers.protect([str(self.root / "work"), str(repository), str(self.root / "home"), str(self.root), str(self.root / "elsewhere-profile")],
                          keep=(repository, run))
        self.assertEqual(providers.AGY_PROTECTED, {where(self.root / "elsewhere-profile")}, "only the place that holds neither")
        self.assertEqual(self.denied(self.root / "work"), set())

    def test_a_profile_that_is_nightweavers_home_or_a_parent_of_it_is_never_denied_either(self):
        providers.protect([str(self.root / "home"), str(self.root)])
        self.assertEqual({rule_place(rule) for rule in providers.agy_secret_rules()} & {where(self.root / "home"), where(self.root)}, set())
        self.assertEqual(self.denied(self.root / "home" / "providers.json"), set(READ_TOOLS), "its own files are denied as before")

    def test_a_place_that_cannot_be_a_path_is_skipped(self):
        before = set(providers.AGY_PROTECTED)
        providers.protect(["\0bad", "", "   ", None, 5])
        providers.agy_secret_rules()
        self.assertEqual(providers.AGY_PROTECTED - before, set(), "nothing usable was named")

    def test_the_rules_are_in_the_form_the_live_runs_matched_a_plain_absolute_path_with_forward_slashes(self):
        providers.protect([str(self.root / "pr of ile (x86)")])
        for rule in self.deny():
            tool = rule.split("(")[0]
            self.assertIn(tool, [*READ_TOOLS, "write_file", "command", "mcp", "read_url", "execute_url"], rule)
            if tool in READ_TOOLS:
                self.assertNotIn("\\", rule)
                self.assertNotIn("*", rule)
                self.assertTrue(Path(rule_place(rule)).is_absolute(), rule)
        self.assertTrue(self.denied(self.root / "pr of ile (x86)"))

    def test_the_rules_come_out_in_the_same_order_so_the_same_places_are_the_same_file(self):
        providers.protect([str(self.root / "b"), str(self.root / "a")])
        first = providers.settings_file()
        providers.AGY_PROTECTED.clear()
        providers.protect([str(self.root / "a"), str(self.root / "b")])
        self.assertEqual(providers.settings_file(), first)

    def test_a_file_with_other_places_is_another_file_so_two_plans_never_rewrite_each_others(self):
        base = providers.settings_file()
        providers.protect([str(self.root / "first-plan-profile")])
        first = providers.settings_file()
        providers.AGY_PROTECTED.clear()
        providers.protect([str(self.root / "second-plan-profile")])
        second = providers.settings_file()
        self.assertEqual(len({base, first, second}), 3)
        self.assertIn("first-plan-profile", first.read_text(encoding="utf-8"), "the first plan's file keeps its rules")
        self.assertNotIn("first-plan-profile", second.read_text(encoding="utf-8"))

    def test_the_guard_still_needs_only_the_write_deny_rules_so_a_listing_without_the_login_rules_does_not_stop_a_call(self):
        self.assertEqual(providers.agy_permissions(ALLOWED)["deny"], set(AGY_DENY))
        with patch("nightweaver.providers.agy_run", return_value=(0, ALLOWED)):
            self.assertEqual(providers.antigravity_permissions(sys.executable, {}, self.root), ("enforced", "loaded"))


class LoginsOfAPlan(unittest.TestCase):
    """The places a plan's routes keep a login reach agy's deny rules from the engine (every role of a run) and from the goal planner."""
    configured = fixtures.WindowsRunTest.configured

    def setUp(self):
        fixtures.WindowsRunTest.setUp(self)
        providers.AGY_PROTECTED.clear()
        self.addCleanup(providers.AGY_PROTECTED.clear)

    def test_a_run_remembers_the_logins_of_every_route_of_its_plan(self):
        profile, token, key = self.root / "claude profile", self.root / "claude.token", self.root / "deepseek.key"
        run = self.configured(fixture("good"), reviewer={"kind": "claude", "profile_dir": str(profile)},
                              reviewer_fallbacks=[{"kind": "claude", "oauth_token_file": str(token)}],
                              milestones=[{"title": "Prepare fixture", "task": "Set the value",
                                           "worker": {"kind": "deepseek", "api_key_file": str(key)}}])
        self.assertEqual(providers.AGY_PROTECTED, set(), "nothing is remembered before a run is opened")
        Run(self.store, run)
        self.assertEqual(providers.AGY_PROTECTED, {where(profile), where(token), where(key)})

    def test_a_run_whose_route_profile_holds_the_repository_does_not_protect_that_profile(self):
        run = self.configured(fixture("good"), reviewer={"kind": "claude", "profile_dir": str(self.root)})
        Run(self.store, run)
        self.assertEqual(providers.AGY_PROTECTED, set())

    def test_the_goal_planner_remembers_the_logins_of_the_plan_before_it_calls_agy(self):
        repository = self.root / "planning-repository"
        repository.mkdir()
        template = self.root / "plan.json"
        template.write_text(json.dumps({"repository": str(repository), "title": "A project",
                                        "worker": {"kind": "deepseek", "api_key_file": "keys/deepseek.key"},
                                        "reviewer": {"kind": "claude", "profile_dir": "profiles/reviewer"},
                                        "gates": [{"name": "Contract", "argv": [sys.executable, "check.py"]}], "acceptance": ["Deliver."]}),
                            encoding="utf-8")
        args = argparse.Namespace(template=str(template), objective="Deliver it", planner="antigravity", model=None, profile_dir=None,
                                  start=False, enqueue=False, prompt_only=True)
        plan_goal(Store(self.root / "runtime"), args)
        self.assertEqual(providers.AGY_PROTECTED, {where(self.root / "keys" / "deepseek.key"), where(self.root / "profiles" / "reviewer")},
                         "paths in a template are relative to the template's folder")


class Guard(Resumed):
    def test_the_listing_is_read_as_allow_and_deny_rules_in_every_scope(self):
        self.assertEqual(providers.agy_permissions(BARE), {"allow": set(), "deny": set()})
        self.assertEqual(providers.agy_permissions(LOADED)["allow"], set(), "the 0.19.2 listing has deny rules only")
        self.assertEqual(providers.agy_rules(ALLOWED), set(AGY_DENY), "agy_rules is still the deny rules")
        self.assertIsNone(providers.agy_permissions("warning: x\nnot json\n"))
        self.assertIsNone(providers.agy_rules(""))
        scopes = {"event": "command_result", "command": {"name": "permissions", "data": {"permissions": [
            {"scope": "project", "allow": ["read_file(*)"]}, {"scope": "global", "allow": ["list_dir(*)", 7], "deny": ["command(*)"]}, "junk"]}}}
        self.assertEqual(providers.agy_permissions(json.dumps(scopes)), {"allow": {"read_file(*)", "list_dir(*)"}, "deny": {"command(*)"}})

    def test_the_guard_reports_the_allow_rules_as_loaded(self):
        with patch("nightweaver.providers.agy_run", side_effect=rules_output(ALLOWED)):
            self.assertEqual(providers.antigravity_guard(sys.executable, self.root), "loaded")
            self.assertEqual(providers.antigravity_permissions(sys.executable, {}, self.root), ("enforced", "loaded"))
            self.assertEqual(providers.antigravity_rules(sys.executable, {}, self.root), "enforced")

    def test_a_missing_allow_rule_is_reported_and_never_stops_a_call(self):
        for output, why in ((LOADED, "no allow rule at all (the 0.19.2 listing)"), (ALLOWED.replace(',"find_by_name(*)"', ""), "one rule short")):
            with self.subTest(why=why):
                providers.AGY_READ_ONLY.clear()
                with patch("nightweaver.providers.agy_run", side_effect=rules_output(output)):
                    self.assertEqual(providers.antigravity_guard(sys.executable, self.root), "missing")
                providers.AGY_READ_ONLY.clear()
                code, _, answer = self.asked([reads(3)], agy=rules_output(output))
                self.assertEqual((code, answer.strip()), (0, "AMBER-11; CORAL-44; ALPHA-LOADER-33; BETA-BARS-66"))

    def test_a_missing_deny_rule_still_stops_the_call_whatever_the_allow_rules_say(self):
        for output in (BARE, ALLOWED.replace('"command(*)",', ""), ALLOWED.replace(',"mcp(*)"', "")):
            with self.subTest(output=output[:60]):
                calls = []
                error = self.failed([reads(3)], calls=calls, agy=rules_output(output))
                self.assertEqual(error.kind, "configuration")
                self.assertIn("did not load the read-only permission rules", str(error))
                self.assertEqual(calls, [], "no model call was made")
        self.assertEqual(providers.AGY_READ_ALLOWED, {}, "nothing is remembered for a call that was stopped")

    def test_what_the_guard_learned_is_remembered_with_the_five_minutes_check(self):
        asked = []

        def agy(executable, env, *args, timeout=30, cwd=None):
            asked.append(args)
            return (0, ALLOWED) if "/permissions" in args else (0, "models")

        with patch("nightweaver.providers.agy_run", side_effect=agy):
            self.assertEqual(providers.antigravity_guard(sys.executable, self.root), "loaded")
            self.assertEqual(providers.antigravity_guard(sys.executable, self.root), "loaded")
            self.assertEqual(len([a for a in asked if "/permissions" in a]), 1)
            key = providers.read_only_key(sys.executable, self.root)
            providers.AGY_READ_ONLY[(sys.executable, "long gone")] = providers.monotonic() - 301
            providers.AGY_READ_ALLOWED[(sys.executable, "long gone")] = "missing"
            providers.AGY_READ_ONLY.pop(key)
            providers.antigravity_guard(sys.executable, self.root)
        self.assertNotIn((sys.executable, "long gone"), providers.AGY_READ_ALLOWED, "pruned together with the five-minute memory")

    def test_when_agy_cannot_say_the_guard_says_nothing_and_remembers_nothing(self):
        for output in ((None, ""), (1, "boom"), (0, "gemini-4-neon"), (0, '{"event":"command_result","command":{"name":"permissions","data":{}}}\n')):
            with self.subTest(output=output):
                with patch("nightweaver.providers.agy_run", return_value=output):
                    self.assertIsNone(providers.antigravity_guard(sys.executable, self.root))
        self.assertEqual((providers.AGY_READ_ONLY, providers.AGY_READ_ALLOWED), ({}, {}))

    def test_agys_own_settings_that_turn_slash_commands_off_skip_the_free_check(self):
        self.agy_settings(disableSlashCommands=True)
        with patch("nightweaver.providers.agy_run") as asked:
            self.assertIsNone(providers.antigravity_guard(sys.executable, self.root))
        asked.assert_not_called()

    def test_the_worker_is_never_asked_and_gets_no_note_though_it_passes_a_settings_file(self):
        asked, calls = [], []

        def agy(executable, env, *args, timeout=30, cwd=None):
            asked.append(args)
            return 0, "gemini-4-neon-xhigh"

        self.asked([raw(7)], mode="worker", calls=calls, agy=agy, prompt="Fix sub.")
        self.assertFalse([a for a in asked if "/permissions" in a])
        self.assertEqual(calls[0]["prompt"], "Fix sub.", "the worker's prompt is exactly what the engine built")
        self.assertIn("--settings", calls[0]["argv"])
        self.assertIn("--disable-slash-commands", calls[0]["argv"])
        self.assertEqual(providers.AGY_READ_ONLY, {})

    def test_a_read_only_call_gets_the_guard_and_the_note_by_its_mode_not_by_a_flag(self):
        real, asked, calls = providers.adapter, [], []

        def without_settings(config, mode, session=None, images=()):
            argv, env = real(config, mode, session, images)
            index = argv.index("--settings")
            return argv[:index] + argv[index + 2:], env  # an adapter that leaves the flag out

        def agy(executable, env, *args, timeout=30, cwd=None):
            asked.append(args)
            return (0, ALLOWED) if "/permissions" in args else (0, "models")

        with patch("nightweaver.providers.adapter", side_effect=without_settings):
            self.asked([reads(3)], mode="reviewer", calls=calls, agy=agy, prompt="Review it.")
        self.assertTrue([a for a in asked if "/permissions" in a], "asked although argv has no --settings")
        self.assertEqual(calls[0]["prompt"], AGY_READ_ONLY_NOTE + "\n\nReview it.")


class Doctor(Resumed):
    def probe(self, agy_output, live):
        from types import SimpleNamespace
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir(exist_ok=True)
        calls = []

        def fake(argv, cwd, log, timeout, **options):
            calls.append(argv)
            put(Path(log), raw(3))
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=rules_output(agy_output)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}, self.root, live)
        return report, calls

    def test_the_report_names_the_allow_rules_agy_loaded(self):
        report, calls = self.probe(ALLOWED, False)
        self.assertEqual((report["status"], report["read_only_rules"], report["read_allow_rules"]), ("configured", "enforced", "loaded"))
        self.assertNotIn("allow-rules", report["action"])
        self.assertEqual(calls, [], "the plain report costs no model turn")

    def test_a_report_without_them_warns_and_does_not_block(self):
        report, _ = self.probe(LOADED, False)
        self.assertEqual((report["status"], report["read_only_rules"], report["read_allow_rules"]), ("configured", "enforced", "missing"))
        self.assertIn("did not report Nightweaver's read allow-rules as loaded", report["action"])
        self.assertIn("no answer", report["action"])

    def test_a_live_probe_with_the_allow_rules_loaded_is_ready_without_a_warning(self):
        report, calls = self.probe(ALLOWED, True)
        self.assertEqual((report["status"], report["read_only_rules"], report["read_allow_rules"]), ("ready", "enforced", "loaded"))
        self.assertNotIn("warning", report)
        self.assertEqual(len(calls), 1)

    def test_a_live_probe_with_one_allow_rule_missing_is_ready_with_a_warning(self):
        report, calls = self.probe(ALLOWED.replace(',"list_dir(*)"', ""), True)
        self.assertEqual((report["status"], report["read_only_rules"], report["read_allow_rules"]), ("ready", "enforced", "missing"))
        self.assertIn("read allow-rules", report["warning"])
        self.assertEqual(len(calls), 1, "a missing allow rule is not a block")

    def test_a_missing_deny_rule_is_still_a_block_before_any_model_call(self):
        report, calls = self.probe(BARE, True)
        self.assertEqual((report["status"], report["reason"]), ("blocked", "configuration"))
        self.assertEqual(calls, [])


class Parsing(Resumed):
    """The stream: an auto-denied permission is its own thing, apart from a refusal under a deny rule, a write and a read that worked."""

    def test_the_auto_denied_read_is_not_a_refusal_not_a_write_and_not_a_rule_denial(self):
        found = self.parsed(reads(1))
        self.assertEqual(found["auto_denied"], ["read_file " + FIRST_READ])
        self.assertEqual(found["denied_actions"], ["read_file"])
        self.assertTrue(found["notice"].startswith("jetski: no output produced"))
        self.assertEqual((found["denied"], found["refused"], found["wrote"]), ([], [], []))
        self.assertEqual((found["answer"], found["status"], found["failed"], found["messages"]), ("", "SUCCESS", False, 0))
        self.assertTrue(providers.agy_auto_denied(found))

    def test_every_denied_capture_is_seen_as_a_reviewer_a_planner_and_a_worker_would_parse_it(self):
        for number, tool in ((1, FIRST_READ), (4, FIRST_READ), (6, "C:/work/outside/beta/notes_two.txt"), (7, "C:/work/other/run-a/loader.py")):
            for mode in (None, "planner", "reviewer", "worker"):
                with self.subTest(capture=number, mode=mode):
                    found = self.parsed(reads(number), mode)
                    self.assertEqual(found["auto_denied"], ["read_file " + tool])
                    self.assertEqual((found["denied"], found["refused"], found["answer"]), ([], [], ""))
                    self.assertTrue(providers.agy_auto_denied(found))

    def test_no_signal_in_a_call_that_read_everything_and_none_in_the_refusals_of_0192(self):
        for found in (self.parsed(reads(3)), self.parsed(reads(5), "worker"), self.parsed(raw(6), "reviewer"), self.parsed(raw(8), "reviewer"),
                      self.parsed(raw(10), "reviewer")):
            self.assertEqual((found["auto_denied"], found["denied_actions"], found["notice"]), ([], [], None))
            self.assertFalse(providers.agy_auto_denied(found))
        self.assertTrue(self.parsed(raw(6), "reviewer")["denied"], "a rule refusal is in denied, as before")

    def test_the_two_messages_are_told_apart(self):
        self.assertTrue(providers.AGY_AUTO_DENIED.search(AUTO))
        self.assertFalse(providers.agy_denied(AUTO), "agy_denied is the wording of a deny rule only")
        self.assertTrue(providers.agy_denied(RULE))
        self.assertFalse(providers.AGY_AUTO_DENIED.search(RULE))

    def test_a_rule_refusal_that_echoes_the_phrase_in_its_path_stays_a_rule_refusal(self):
        message = ('permission check failed for write_file "C:/x/user denied permission for/y": Permission denied for '
                   "write_file(C:/x/user denied permission for/y). Matches user-configured deny rule.")
        found = self.parsed(tooled(2, ("write_file", {"Path": "C:/x/user denied permission for/y"}, message)), "reviewer")
        self.assertEqual((len(found["denied"]), len(found["refused"]), found["auto_denied"]), (1, 1, []))

    def test_a_denied_read_is_not_an_attempt_to_act_and_any_other_denied_tool_is(self):
        for name, refused in (("read_file", False), ("view_file", False), ("list_dir", False), ("read_url_content", False), ("search_web", False),
                              ("call_mcp_tool", True), ("write_to_file", True), ("run_command", True), ("send_command_input", True),
                              ("some_tool_nobody_has_seen", True)):
            with self.subTest(name=name):
                found = self.parsed(tooled(2, (name, {"Path": "C:/work/outside/x.txt"}, AUTO)), "reviewer")
                self.assertEqual(len(found["auto_denied"]), 1)
                self.assertEqual(bool(found["refused"]), refused)
                self.assertEqual((found["denied"], found["wrote"]), ([], []))

    def test_denied_actions_are_read_from_the_result_whatever_their_shape(self):
        shapes = [{"action": "read_file", "display_name": "ReadFile"}, "view_file", {}, 5, {"display_name": "x"}, {"action": "  "}, {"action": "list_dir"}]
        found = self.parsed(altered(reads(3), denied_actions=shapes))
        self.assertEqual(found["denied_actions"], ["read_file", "view_file", "list_dir"])
        self.assertEqual(self.parsed(altered(reads(3), denied_actions="not a list"))["denied_actions"], [])
        self.assertEqual(self.parsed(altered(reads(3), denied_actions=[]))["denied_actions"], [])

    def test_agys_own_line_counts_only_when_it_is_not_a_json_event(self):
        first = next(line for line in reads(1) if not line.startswith("{"))
        second = ("the 2 tool(s) required approval that headless mode cannot prompt for, so they were auto-denied. Settings allow-rules do not "
                  "apply; re-run with --dangerously-skip-permissions to auto-approve all tools.")
        said = json.dumps({"event": "step_update", "step_update": {"conversation_id": "c", "step_index": 1, "state": "DONE",
                                                                  "step_type": "agent_response", "text_delta": first}})
        for name, lines, expected in (("first wording", [first], first), ("second wording", [second], second), ("neither", ["warning: slow disk"], None),
                                      ("only inside an event", [said], None), ("indented", ["   " + first + "   "], first), ("blank", ["", "  "], None)):
            with self.subTest(name=name):
                path = self.root / "notice.jsonl"
                put(path, lines)
                self.assertEqual(providers.agy_notice(path), expected)
        path = self.root / "long.jsonl"
        put(path, ["x" * 20000 + " " + first, first])
        self.assertEqual(providers.agy_notice(path), first, "a long line is judged by its beginning and the next line is still read")
        self.assertIsNone(providers.agy_notice(self.root / "absent.jsonl"))
        path = self.root / "cut.jsonl"
        path.write_bytes(first.encode("utf-8")[:60])  # a log cut mid-line, no newline
        self.assertEqual(providers.agy_notice(path), first.encode("utf-8")[:60].decode("utf-8", "replace").strip())

    def test_any_one_signal_is_enough_because_the_wording_of_each_can_change(self):
        base = {"auto_denied": [], "denied_actions": [], "notice": None}
        self.assertFalse(providers.agy_auto_denied(base))
        for field, value in (("auto_denied", ["read_file x"]), ("denied_actions", ["read_file"]),
                             ("notice", "jetski: no output produced - a tool required the permission that headless mode cannot prompt for")):
            self.assertTrue(providers.agy_auto_denied({**base, field: value}), field)
        self.assertFalse(providers.agy_auto_denied({**base, "notice": "agy said so"}), "a line that names no permission is not one (round 2)")

    def test_a_denial_does_not_disturb_the_answer_the_usage_or_the_session(self):
        found = self.parsed(reads(1))
        self.assertEqual((found["call_usage"]["total_tokens"], found["usage"]["total_tokens"]), (FIRST_USAGE, FIRST_USAGE))
        self.assertEqual(found["session"], read_events(1)[0]["conversation_id"])
        self.assertEqual(found["model"], "gemini-4-neon")


class AutoDenialIsNeverARefusalOfARead(Resumed):
    def test_a_denied_read_in_a_call_that_still_answered_is_not_a_refusal(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                code, _, answer = self.asked([tooled(2, ("read_file", {"Path": "C:/work/outside/x.txt"}, AUTO))], mode=mode)
                self.assertEqual(code, 0)
                self.assertTrue(answer.strip())

    def test_a_denied_read_never_raises_read_only_refused_not_even_when_the_call_is_resumed(self):
        error = self.failed([reads(1), answered(text="")])
        self.assertEqual(error.kind, "no_answer")
        code, _, answer = self.asked([reads(1), reads(2)])
        self.assertEqual(code, 0)
        self.assertTrue(answer.strip())

    def test_any_other_tool_a_read_only_role_was_denied_is_read_only_refused_when_it_answered(self):
        for name in ("call_mcp_tool", "write_to_file", "notebook_edit"):
            with self.subTest(name=name):
                error = self.failed([tooled(2, (name, {"Path": "C:/work/outside/x.txt"}, AUTO))], mode="reviewer")
                self.assertEqual(error.kind, "read_only_refused")
                self.assertIn(name, str(error))


class ResumeOnAutoDenial(Resumed):
    def test_a_prose_role_is_resumed_once_and_the_resumed_answer_is_the_call_s_answer(self):
        calls = []
        code, session, answer = self.asked([reads(1), reads(2)], calls=calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual((code, session), (0, read_events(1)[0]["conversation_id"]))
        self.assertEqual(answer.strip(), part(read_events(2)[-1])["response"].strip())
        self.assertTrue(answer.startswith("unreadable (permission denied)"), "what the real resume said")

    def test_the_resume_continues_the_conversation_with_the_same_flags_and_the_note_and_asks_for_the_answer(self):
        calls = []
        self.asked([reads(1), reads(2)], calls=calls, config={"model": "gemini-4-neon", "effort": "xhigh"}, evidence=self.root / "evidence")
        first, second = calls
        conversation = read_events(1)[0]["conversation_id"]
        self.assertNotIn("--conversation", first["argv"])
        self.assertEqual(second["argv"], first["argv"] + ["--conversation", conversation], "every flag of the first call, and the conversation")
        self.assertEqual(second["argv"][second["argv"].index("--settings") + 1], str(providers.settings_file()))
        self.assertEqual(second["argv"][second["argv"].index("--mode") + 1], "accept-edits")
        self.assertIn("--add-dir", second["argv"])
        self.assertNotIn("--json-schema", second["argv"], "a prose role has none")
        self.assertEqual(first["prompt"], AGY_READ_ONLY_NOTE + "\n\nPlan it.")
        self.assertEqual(second["prompt"], AGY_READ_ONLY_NOTE + "\n\n" + AGY_RESUME)
        self.assertEqual((second["timeout"], second["cwd"]), (first["timeout"], first["cwd"]))

    def test_a_conversation_the_call_was_given_is_replaced_by_the_one_agy_reports(self):
        calls = []
        self.asked([reads(1), reads(2)], calls=calls, session="an-older-conversation")
        first, second = calls
        self.assertEqual(first["argv"][first["argv"].index("--conversation") + 1], "an-older-conversation")
        self.assertEqual(second["argv"].count("--conversation"), 1)
        self.assertEqual(second["argv"][second["argv"].index("--conversation") + 1], read_events(1)[0]["conversation_id"])

    def test_the_prompt_allows_no_tool_and_asks_for_the_answer_in_the_format_the_task_asked_for(self):
        for phrase in ("Do not call any tools", "exactly the format the task asked for", "from what you have already read", "say so inside the answer"):
            self.assertIn(phrase, AGY_RESUME)
        self.assertFalse(AGY_RESUME.lstrip().startswith("/"))
        self.assertLess(len(AGY_RESUME), 600, "short")

    def test_the_first_log_is_kept_and_the_resumed_call_writes_its_own_beside_it(self):
        calls = []
        self.asked([reads(1), reads(2)], calls=calls)
        first, second = calls
        self.assertEqual(first["log"], self.root / "call.jsonl")
        self.assertEqual(second["log"], self.root / "call.jsonl.resume.jsonl")
        self.assertEqual(providers.agy_resume_log(first["log"]), second["log"])
        self.assertEqual(providers.agy_logs(first["log"]), [first["log"], second["log"]])
        self.assertEqual(first["log"].read_text(encoding="utf-8").splitlines(), reads(1), "the first log is exactly the denied stream")
        self.assertEqual(second["log"].read_text(encoding="utf-8").splitlines(), reads(2))
        self.assertTrue(providers.agy_auto_denied(providers.agy_stream(first["log"])))
        self.assertFalse(providers.agy_auto_denied(providers.agy_stream(second["log"])))

    def test_usage_is_the_sum_of_the_two_calls_own_usage_not_the_cumulative_figure_twice(self):
        calls = []
        self.asked([reads(1), reads(2)], calls=calls)
        log = calls[0]["log"]
        receipt = providers.usage(log, "antigravity")
        self.assertEqual(receipt["observed_tokens"], FIRST_USAGE + RESUMED_USAGE)
        self.assertEqual(receipt["observed_tokens"], part(read_events(2)[-1])["usage"]["total_tokens"], "the cumulative figure of the resumed result is that sum")
        self.assertEqual(receipt["token_channels"], {"input_tokens": 12326 + 2713, "output_tokens": 528 + 93, "thinking_tokens": 300 + 74,
                                                       "cache_read_tokens": 7645 + 8220})
        self.assertEqual(receipt["billing"], "not_measured")
        self.assertEqual(receipt["resume_log"], str(calls[1]["log"]), "the evidence names the second log")
        self.assertEqual(providers.usage(calls[1]["log"], "antigravity")["observed_tokens"], RESUMED_USAGE, "the resume log alone is its own call")
        self.assertNotIn("resume_log", providers.usage(self.write_alone(reads(1)), "antigravity"))

    def write_alone(self, items):
        path = self.root / "alone.jsonl"
        put(path, lines_of(items))
        return path

    def test_every_read_only_role_is_resumed_and_a_schema_role_gets_its_schema_again(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                if mode in ROLES:
                    payload, parse = ROLES[mode]
                    stream = answered(payload=payload, schema=SCHEMAS[mode])
                elif mode in PROSE:
                    payload, parse = PROSE[mode]
                    stream = answered(text="Here is the answer.\n```json\n" + json.dumps(payload) + "\n```\n")
                else:
                    payload, parse, stream = None, None, answered(text="READY\n")
                calls = []
                (code, _, answer) = self.asked([reads(1), stream], mode=mode, calls=calls, log=self.root / f"{mode}.jsonl")
                self.assertEqual((code, len(calls)), (0, 2))
                second = calls[1]["argv"]
                self.assertEqual(("--json-schema" in second), mode in SCHEMAS)
                if mode in SCHEMAS:
                    self.assertEqual(json.loads(Path(second[second.index("--json-schema") + 1]).read_text(encoding="utf-8")), SCHEMAS[mode])
                    self.assertEqual(json.loads(answer), payload, "structured_output of the resumed call")
                if parse:
                    self.assertTrue(parse(answer), "the role's own parser reads the resumed answer")
                self.assertEqual(second[second.index("--conversation") + 1], read_events(1)[0]["conversation_id"])
                self.assertEqual(providers.usage(calls[0]["log"], "antigravity")["observed_tokens"], FIRST_USAGE + RESUMED_USAGE)

    def test_the_goal_planner_succeeds_on_the_stream_of_a_repository_whose_objective_names_outside_files(self):
        """cli.plan_goal makes one planner call, so 0.19.2 stopped the run with no_answer. The first call is the stream of that run (nine reads, the
        ninth outside the repository and denied); the resumed call gives the milestones."""
        template = self.root / "plan.json"
        template.write_text(json.dumps({"repository": str(self.root), "title": "A project", "worker": {"kind": "deepseek"},
                                        "gates": [{"name": "Contract", "argv": [sys.executable, "check.py"]}], "acceptance": ["Deliver."]}),
                            encoding="utf-8")
        args = argparse.Namespace(template=str(template), objective="Deliver it", planner="antigravity", model="gemini-4-neon", profile_dir=None,
                                  start=False, enqueue=False)
        plan = {"milestones": [{"title": "Build the loader", "task": "Write the loader and its tests, then run them."}]}
        calls = []
        streams = [reads(7), answered(text="Here is the plan.\n```json\n" + json.dumps(plan) + "\n```\n")]
        with self.agy(streams, calls), patch("nightweaver.providers.check_provider", return_value=sys.executable):
            planned = plan_goal(Store(self.root / "runtime"), args)
        self.assertEqual(planned["milestones"], plan["milestones"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--model") + 1], "gemini-4-neon")
        self.assertIn("Deliver it", calls[0]["prompt"])
        self.assertEqual(calls[1]["log"], calls[0]["log"].with_name(calls[0]["log"].name + ".resume.jsonl"))
        self.assertEqual(calls[0]["log"].parent.name, "planning", "the planner's logs are where plan_goal keeps them, both of them")
        self.assertTrue(calls[0]["log"].is_file() and calls[1]["log"].is_file())

    def test_the_goal_planner_still_fails_with_the_denial_named_when_the_resume_gives_nothing(self):
        template = self.root / "plan.json"
        template.write_text(json.dumps({"repository": str(self.root), "title": "A project", "worker": {"kind": "deepseek"},
                                        "gates": [{"name": "Contract", "argv": [sys.executable, "check.py"]}], "acceptance": ["Deliver."]}),
                            encoding="utf-8")
        args = argparse.Namespace(template=str(template), objective="Deliver it", planner="antigravity", model="gemini-4-neon", profile_dir=None,
                                  start=False, enqueue=False)
        calls = []
        with self.agy([reads(7), answered(text="")], calls), patch("nightweaver.providers.check_provider", return_value=sys.executable):
            with self.assertRaises(ProviderUnavailable) as caught:
                plan_goal(Store(self.root / "runtime"), args)
        self.assertEqual(caught.exception.kind, "no_answer")
        self.assertIn("auto-denied 1 tool action (read_file C:/work/other/run-a/loader.py)", str(caught.exception))
        self.assertEqual(len(calls), 2)


class NoResume(Resumed):
    def test_a_worker_is_never_resumed(self):
        calls = []
        code, session, answer = self.asked([reads(4)], mode="worker", calls=calls, prompt="Fix it.")
        self.assertEqual((code, answer.strip(), len(calls)), (0, "", 1))
        self.assertEqual(session, read_events(4)[0]["conversation_id"])
        self.assertFalse(providers.agy_logs(calls[0]["log"])[1:], "no resume log")
        self.assertEqual(calls[0]["prompt"], "Fix it.")

    def test_a_plain_silence_with_no_auto_denial_fails_as_it_did(self):
        """Nothing in the facts shows a resume rescues a call that was silent for another reason, so it is not tried."""
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                calls = []
                error = self.failed([blanked(2)], mode=mode, calls=calls, log=self.root / f"{mode}.jsonl")
                self.assertEqual((error.kind, len(calls)), ("no_answer", 1))
                self.assertNotIn("auto-denied", str(error))
                self.assertNotIn("resumed", str(error))
                self.assertIn(f"finished the {mode} call (status SUCCESS) but returned no answer", str(error))

    def test_a_call_that_failed_or_ended_in_another_exit_code_is_not_resumed(self):
        for name, streams, codes in (("status ERROR: a failed call, not a silent success", [altered(reads(1), status="ERROR", finished_reason="error",
                                                                                                   error="boom")], {}),
                                     ("exit 3", [reads(1)], {0: 3})):
            with self.subTest(name=name):
                calls = []
                try:
                    self.asked(streams, calls=calls, codes=codes)
                except ProviderUnavailable as error:
                    self.assertNotEqual(error.kind, "no_answer", "the failure is classified from the log, not called a silence")
                self.assertEqual(len(calls), 1)

    def test_an_answer_that_exists_is_never_replaced_by_a_resume(self):
        calls = []
        code, _, answer = self.asked([altered(reads(3), denied_actions=[{"action": "read_file", "display_name": "ReadFile"}])], calls=calls)
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertTrue(answer.startswith("AMBER-11"))

    def test_a_write_the_first_call_completed_is_a_violation_and_nothing_is_resumed(self):
        calls = []
        error = self.failed([with_tools(reads(1), ("write_file", {"Path": "C:/work/repo/calc.py"}))], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("read_only_violation", 1))
        self.assertIn("write_file", str(error))

    def test_a_write_the_resumed_call_completed_is_a_violation_naming_the_resumed_log(self):
        calls = []
        error = self.failed([reads(1), with_tools(reads(2), ("edit_file", {"Path": "C:/work/repo/calc.py"}))], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("read_only_violation", 2))
        self.assertIn("edit_file", str(error))
        self.assertIn("call.jsonl.resume.jsonl", str(error))
        self.assertIn("its answer is discarded", str(error))

    def test_a_first_call_that_also_tried_to_write_is_not_resumed_for_an_answer_that_would_be_discarded(self):
        calls = []
        error = self.failed([with_tools(reads(1), ("write_file", {"Path": "C:/work/repo/calc.py"}, RULE))], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("no_answer", 1))
        self.assertIn("refused 1 tool action under the read-only rules", str(error))
        self.assertIn("auto-denied 1 tool action", str(error))

    def test_an_answer_the_resumed_call_gave_after_a_refused_action_is_not_taken(self):
        calls = []
        error = self.failed([reads(1), with_tools(reads(2), ("write_file", {"Path": "C:/work/repo/calc.py"}, RULE))], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("read_only_refused", 2))
        self.assertIn("call.jsonl.resume.jsonl", str(error))

    def test_a_resume_that_is_still_empty_fails_as_no_answer_naming_the_denial_agys_line_and_both_logs(self):
        calls = []
        error = self.failed([reads(1), answered(text="")], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("no_answer", 2))
        message = str(error)
        self.assertIn("finished the planner call (status SUCCESS) but returned no answer", message)
        self.assertIn("auto-denied 1 tool action (read_file " + FIRST_READ + ")", message)
        self.assertIn('agy said: "jetski: no output produced', message)
        self.assertIn("resumed once", message)
        self.assertIn(str(self.root / "call.jsonl.resume.jsonl"), message)
        self.assertIn("See " + str(self.root / "call.jsonl"), message)
        self.assertIn("retries this as a transient failure", message)

    def test_a_resume_that_is_denied_again_is_not_resumed_a_second_time(self):
        calls = []
        error = self.failed([reads(1), reads(1)], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("no_answer", 2), "a third call would have raised in the stand-in agy")
        self.assertIn("auto-denied 2 tool actions", str(error))

    def test_the_resumed_call_that_fails_is_classified_from_its_own_log_and_a_controller_exit_is_not_classified(self):
        error = self.failed([reads(1), ["read ECONNRESET while calling the model"]], codes={1: 1})
        self.assertEqual(error.kind, "transient", "the first log has no such text: the resume log is the one that was read")
        calls = []
        code, _, _ = self.asked([reads(1), ["still thinking"]], calls=calls, codes={1: 124})
        self.assertEqual((code, len(calls)), (124, 2), "the controller ended the resume (time): not a provider failure")

    def test_a_resume_log_left_by_an_earlier_attempt_is_removed_and_never_counted(self):
        stale = self.root / "call.jsonl.resume.jsonl"
        put(stale, lines_of(reads(2)))
        calls = []
        self.asked([reads(3)], mode="reviewer", calls=calls)
        self.assertFalse(stale.exists())
        self.assertEqual(providers.usage(calls[0]["log"], "antigravity")["observed_tokens"], 15672, "reads-03 alone")

    def test_a_delegated_change_in_the_resumed_call_is_a_violation_too(self):
        subagent = {"event": "step_update", "step_update": {"conversation_id": read_events(1)[0]["conversation_id"], "step_index": 99, "state": "DONE",
                                                            "step_type": "subagent", "tool_name": "invoke_subagent", "subagent_info": {"prompt": "x"}}}
        seen = []
        stack = self.agy([reads(1), [*lines_of(answered(text="done\n"))[:-1], json.dumps(subagent), lines_of(answered(text="done\n"))[-1]]], seen)
        with stack:
            real = providers.execute

            def edits(argv, cwd, path, timeout, **options):
                code = real(argv, cwd, path, timeout, **options)
                if len(seen) == 2:
                    (Path(cwd) / "changed.txt").write_text("a subagent wrote this", encoding="utf-8")
                return code

            with patch("nightweaver.providers.execute", side_effect=edits), self.assertRaises(ProviderUnavailable) as caught:
                providers.invoke({"kind": "antigravity", "executable": sys.executable}, "planner", self.root, self.root / "call.jsonl", "Plan it.", 600, lambda: False)
        self.assertEqual(caught.exception.kind, "read_only_violation")
        self.assertIn("changed.txt", str(caught.exception))


class Silence(Resumed):
    def test_the_message_names_the_auto_denied_action_agys_line_and_what_nightweaver_allows(self):
        found = self.parsed(reads(1))
        message = providers.agy_silence(found, "planner", "call.jsonl")
        self.assertIn("returned no answer: no agent message with text.", message)
        self.assertIn("agy auto-denied 1 tool action (read_file " + FIRST_READ + "): a headless call cannot answer a permission prompt", message)
        self.assertIn(found["notice"][:120], message)
        self.assertIn(", ".join(AGY_ALLOW), message)
        self.assertIn("never passes --dangerously-skip-permissions", message)
        self.assertNotIn("resumed", message)

    def test_a_missing_allow_rule_is_named_as_the_likely_cause(self):
        message = providers.agy_silence(self.parsed(reads(1)), "reviewer", "call.jsonl", reads="missing")
        self.assertIn("agy did not list Nightweaver's read allow-rules as loaded", message)
        self.assertIn("no structured output", message)
        self.assertNotIn("never passes", message)

    def test_each_signal_alone_is_enough_to_name_the_cause(self):
        line_only = self.parsed(reads(1))
        line_only.update(auto_denied=[], denied_actions=[])
        message = providers.agy_silence(line_only, "planner", "call.jsonl")
        self.assertIn("agy auto-denied a tool action:", message)
        self.assertIn("jetski: no output produced", message)
        actions_only = self.parsed(reads(1))
        actions_only.update(auto_denied=[], notice=None)
        self.assertIn("agy auto-denied 1 tool action (read_file):", providers.agy_silence(actions_only, "planner", "call.jsonl"))
        step_only = self.parsed(reads(1))
        step_only.update(denied_actions=[], notice=None)
        message = providers.agy_silence(step_only, "planner", "call.jsonl")
        self.assertIn("(read_file " + FIRST_READ + ")", message)
        self.assertNotIn("agy said", message)

    def test_a_call_with_no_denial_keeps_the_message_of_0192(self):
        message = providers.agy_silence(self.parsed(blanked(2), "reviewer"), "reviewer", "call.jsonl")
        self.assertEqual(message, "The Antigravity CLI finished the reviewer call (status SUCCESS) but returned no answer: no agent message with text, "
                                  "no structured output. See call.jsonl. A run retries this as a transient failure before it blocks; resume with "
                                  "--retry once agy answers.")

    def test_a_secret_in_agys_line_is_not_repeated(self):
        found = self.parsed(reads(1))
        found["notice"] = "jetski: no output produced - auto-denied. token sk-ant-api03-" + "a" * 40
        self.assertNotIn("sk-ant-api03", providers.agy_silence(found, "planner", "call.jsonl"))


class ResumeTime(Resumed):
    """Round 2, finding 1. The call's timeout is the whole call's, so the resume gets only what the first call left of it (timeout minus the
    first call's elapsed time) and is skipped when less than AGY_RESUME_FLOOR is left. 0.19.3 gave the resume the full timeout again, so
    a first call that took most of it let the pair run up to twice the limit."""

    def test_the_floor_is_a_small_number_of_seconds(self):
        self.assertEqual(AGY_RESUME_FLOOR, 20)

    def test_the_resume_gets_only_what_is_left_of_the_calls_timeout(self):
        calls = []
        _, _, answer = self.asked([reads(1), reads(2)], calls=calls, timeout=600, took={0: 250.0})
        self.assertEqual([call["timeout"] for call in calls], [600, 350.0])
        self.assertTrue(answer.strip(), "the resume ran and answered")

    def test_a_first_call_that_took_no_time_leaves_the_resume_the_whole_timeout(self):
        calls = []
        self.asked([reads(1), reads(2)], calls=calls, timeout=600)
        self.assertEqual([call["timeout"] for call in calls], [600, 600])

    def test_exactly_the_floor_left_still_starts_the_resume(self):
        calls = []
        _, _, answer = self.asked([reads(1), reads(2)], calls=calls, timeout=600, took={0: 600 - AGY_RESUME_FLOOR})
        self.assertEqual([call["timeout"] for call in calls], [600, AGY_RESUME_FLOOR])
        self.assertTrue(answer.strip())

    def test_with_less_than_the_floor_left_the_resume_is_skipped_and_the_call_fails_as_no_answer_saying_so(self):
        for took, left in ((581, 19), (590, 10), (600, 0), (900, 0)):
            with self.subTest(took=took):
                calls = []
                error = self.failed([reads(1)], calls=calls, timeout=600, took={0: took})  # a second call would raise in the stand-in agy
                self.assertEqual((error.kind, len(calls)), ("no_answer", 1))
                message = str(error)
                self.assertIn("skipped for lack of time", message)
                self.assertIn(f"only {left} s of its 600 s timeout were left", message)
                self.assertIn(f"a resume needs at least {AGY_RESUME_FLOOR} s", message)
                self.assertIn("auto-denied 1 tool action (read_file " + FIRST_READ + ")", message, "the denial is still named")
                self.assertIn('agy said: "jetski: no output produced', message, "and agy's own line quoted")
                self.assertNotIn("resumed once", message)
                self.assertFalse(providers.agy_resume_log(self.root / "call.jsonl").exists(), "no resume log: no resume")
                self.assertIn("retries this as a transient failure", message)

    def test_a_call_that_answered_never_looks_at_the_clock(self):
        calls = []
        code, _, answer = self.asked([reads(3)], calls=calls, timeout=600, took={0: 599})
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertTrue(answer.startswith("AMBER-11"))

    def test_the_pause_check_and_the_health_limits_still_reach_the_resume(self):
        calls, pause = [], lambda: False
        self.asked([reads(1), reads(2)], calls=calls, paused=pause, health={"idle_timeout": 900}, took={0: 100})
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIs(call["options"]["paused"], pause)
            self.assertEqual(call["options"]["idle_timeout"], 900)
        self.assertEqual([call["timeout"] for call in calls], [600, 500])

    def test_the_message_of_a_skipped_resume_names_nothing_about_a_resume_that_did_not_happen(self):
        found = self.parsed(reads(1))
        message = providers.agy_silence(found, "planner", "call.jsonl", skipped=(12.4, 600))
        self.assertIn("only 12 s of its 600 s timeout were left after the first call", message)
        self.assertNotIn("resumed once", message)
        self.assertNotIn("skipped", providers.agy_silence(found, "planner", "call.jsonl"))


NARRATION = [(1, "I will start by reading the files.\n"), (4, "Now the three outside files, in parallel.\n")]
DENIED_RESULT = [{"action": "read_file", "display_name": "ReadFile"}]


class Narration(Resumed):
    """Round 2, finding 2. An agent message that came before the denied tool batch is narration ("I will read the files"): the turn ended at
    the batch, and `response` joins that text, so 0.19.3 took it for the answer and never resumed. reads-01 has no such text (the model
    said nothing before its reads in the lab), so these captures are reads-01 and reads-03 with messages written into them."""

    def test_text_before_the_denied_batch_is_not_the_answer(self):
        found = self.parsed(narrated(reads(1), *NARRATION))
        self.assertEqual((found["answer"], found["source"]), ("", None))
        self.assertEqual((found["messages"], found["narration"]), (2, 2))
        self.assertTrue(providers.agy_auto_denied(found))
        self.assertEqual(found["auto_denied"], ["read_file " + FIRST_READ])

    def test_the_same_text_in_a_call_with_no_denial_is_the_answer_as_before(self):
        found = self.parsed(narrated(reads(3), *NARRATION, (9, "AMBER-11; CORAL-44; ALPHA-LOADER-33; BETA-BARS-66\n")))
        self.assertFalse(providers.agy_auto_denied(found))
        self.assertEqual((found["source"], found["narration"]), ("response", 0))
        self.assertIn("AMBER-11", found["answer"])

    def test_a_call_with_only_narration_before_a_denied_batch_is_resumed_and_the_resumed_answer_is_the_answer(self):
        for mode in ("planner", "brief", "probe"):
            with self.subTest(mode=mode):
                calls = []
                code, _, answer = self.asked([narrated(reads(1), *NARRATION), reads(2)], mode=mode, calls=calls, log=self.root / f"{mode}.jsonl")
                self.assertEqual((code, len(calls)), (0, 2))
                self.assertTrue(answer.startswith("unreadable (permission denied)"), "what the resume said, not the narration")
                self.assertNotIn("I will start", answer)

    def test_a_schema_role_is_resumed_too_and_gets_its_structured_answer_from_the_resume(self):
        payload = {"verdict": "PASS", "reason": "The change is correct."}
        calls = []
        code, _, answer = self.asked([narrated(reads(1), *NARRATION), answered(payload=payload, schema=SCHEMAS["reviewer"])], mode="reviewer", calls=calls)
        self.assertEqual((code, len(calls)), (0, 2))
        self.assertEqual(json.loads(answer), payload)

    def test_a_structured_output_in_the_denied_call_still_wins_and_nothing_is_resumed(self):
        payload = {"verdict": "REVISE", "reason": "calc.py has a bug."}
        stream = altered(narrated(reads(1), *NARRATION), structured_output=payload)
        calls = []
        code, _, answer = self.asked([stream], mode="reviewer", calls=calls)
        self.assertEqual((code, len(calls), json.loads(answer)), (0, 1, payload))
        self.assertEqual(self.parsed(stream, "reviewer")["source"], "structured_output")

    def test_a_message_after_the_denied_batch_is_a_real_answer_and_alone_the_answer(self):
        stream = narrated(reads(1), *NARRATION, (9, "Not every outside file could be read.\n"))
        found = self.parsed(stream)
        self.assertEqual((found["answer"], found["source"], found["messages"], found["narration"]),
                         ("Not every outside file could be read.", "agent_response", 3, 2))
        calls = []
        code, _, answer = self.asked([stream], calls=calls)
        self.assertEqual((code, len(calls), answer), (0, 1, "Not every outside file could be read."))

    def test_when_only_the_result_shows_the_denial_the_batch_ends_at_the_last_tool_step(self):
        """reads-03 read everything (steps 2 to 8 DONE); a denied_actions in its result with no failing step marks it denied."""
        silent = altered(narrated(reads(3), *NARRATION), denied_actions=DENIED_RESULT)
        found = self.parsed(silent)
        self.assertEqual((found["answer"], found["narration"], found["auto_denied"]), ("", 2, []))
        calls = []
        code, _, answer = self.asked([silent, reads(2)], calls=calls)
        self.assertEqual((code, len(calls)), (0, 2))
        said = altered(narrated(reads(3), *NARRATION, (9, "AMBER-11; CORAL-44; ALPHA-LOADER-33; BETA-BARS-66\n")), denied_actions=DENIED_RESULT)
        self.assertEqual(self.parsed(said)["answer"], "AMBER-11; CORAL-44; ALPHA-LOADER-33; BETA-BARS-66", "a message after the last tool step is the answer")

    def test_when_only_agys_line_shows_the_denial_the_narration_is_dropped_too(self):
        line = next(text for text in reads(1) if not text.startswith("{"))
        found = self.parsed(with_line(narrated(reads(3), *NARRATION), line))
        self.assertEqual((found["answer"], found["narration"], found["denied_actions"], found["auto_denied"]), ("", 2, [], []))
        self.assertTrue(providers.agy_auto_denied(found))

    def test_a_message_whose_place_is_unknown_is_never_discarded_and_nor_is_one_when_no_tool_step_shows_where_the_batch_was(self):
        unplaced = json.dumps({"event": "step_update", "step_update": {"conversation_id": read_events(1)[0]["conversation_id"], "state": "DONE",
                                                                      "step_type": "agent_response", "text_delta": "words with no step index"}})
        found = self.parsed([*lines_of(narrated(reads(1), *NARRATION))[:-1], unplaced, lines_of(reads(1))[-1]])
        self.assertEqual((found["answer"], found["narration"]), ("words with no step index", 2))
        bare = altered(narrated([line for line in lines_of(reads(1)) if '"tool"' not in line], (1, "talk\n")), denied_actions=DENIED_RESULT)
        self.assertEqual(self.parsed(bare)["narration"], 0, "without a tool step there is no batch to be before")

    def test_the_failure_after_a_resume_that_is_empty_says_the_text_was_narration(self):
        error = self.failed([narrated(reads(1), *NARRATION), answered(text="")])
        self.assertEqual(error.kind, "no_answer")
        message = str(error)
        self.assertIn("Its 2 agent messages with text came before the denied tool batch, so they are narration and not an answer.", message)
        self.assertIn("auto-denied 1 tool action (read_file " + FIRST_READ + ")", message)
        found = self.parsed(narrated(reads(1), (1, "I will read it.\n")))
        self.assertIn("Its 1 agent message with text came before the denied tool batch, so it is narration and not an answer.",
                      providers.agy_silence(found, "planner", "call.jsonl"))
        self.assertNotIn("narration", providers.agy_silence(self.parsed(reads(1)), "planner", "call.jsonl"))

    def test_a_worker_is_not_resumed_and_its_answer_is_not_the_narration_either(self):
        calls = []
        code, _, answer = self.asked([narrated(reads(4), *NARRATION)], mode="worker", calls=calls, prompt="Fix it.")
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertEqual(answer, "")


class Trigger(Resumed):
    """Round 2, finding 3. The resume and the "auto-denied" wording fire on a real denial signal only: result.denied_actions, a tool step
    that ended "user denied permission", or agy's own line when it names a permission. 0.19.3 took any of agy's phrases as one, so a call
    that ended with no output for another reason was resumed and described as an auto-denial."""

    BARE = "jetski: no output produced - the model returned an empty response and called no tool."
    UNRELATED = "warning: cannot prompt for an API key in a headless call; using the one in the environment."
    FIRST = next(text for text in reads(1) if not text.startswith("{"))
    SECOND = ("the 2 tool(s) required approval that headless mode cannot prompt for, so they were auto-denied. Settings allow-rules do not "
              "apply; re-run with --dangerously-skip-permissions to auto-approve all tools.")

    def silent(self, line):
        """A call that read everything and said nothing, with one line from agy: no denied_actions, no failing step."""
        return with_line(narrated(reads(3)), line)

    def test_a_bare_no_output_line_for_another_reason_is_not_a_denial_and_is_quoted(self):
        for name, line in (("no output produced", self.BARE), ("cannot prompt for", self.UNRELATED)):
            with self.subTest(name=name):
                found = self.parsed(self.silent(line))
                self.assertEqual(found["notice"], line, "agy's line is still kept to be quoted")
                self.assertFalse(providers.agy_auto_denied(found))
                calls = []
                error = self.failed([self.silent(line)], calls=calls)
                self.assertEqual((error.kind, len(calls)), ("no_answer", 1), "not resumed")
                message = str(error)
                self.assertIn(f'agy printed: "{line}"', message)
                self.assertIn("does not name a permission", message)
                self.assertNotIn("auto-denied", message)
                self.assertNotIn("a headless call cannot answer a permission prompt", message)
                self.assertNotIn("Nightweaver allows these local reads", message)

    def test_a_line_that_names_a_permission_is_a_denial_in_either_wording_and_the_call_is_resumed(self):
        for name, line in (("first wording", self.FIRST), ("second wording", self.SECOND),
                           ("other words", "jetski: no output produced - the read_file permission was not granted")):
            with self.subTest(name=name):
                found = self.parsed(self.silent(line))
                self.assertEqual((found["auto_denied"], found["denied_actions"]), ([], []), "the line alone")
                self.assertTrue(providers.agy_auto_denied(found))
                calls = []
                code, _, answer = self.asked([self.silent(line), reads(2)], calls=calls)
                self.assertEqual((code, len(calls)), (0, 2))
                self.assertTrue(answer.strip())

    def test_the_real_line_is_found_behind_an_unrelated_warning_in_front_of_it(self):
        stream = with_line(with_line(narrated(reads(3)), self.FIRST), self.UNRELATED)  # the unrelated one ends up first
        lines = lines_of(stream)
        self.assertLess(lines.index(self.UNRELATED), lines.index(self.FIRST))
        found = self.parsed(stream)
        self.assertEqual(found["notice"], self.FIRST)
        self.assertTrue(providers.agy_auto_denied(found))
        self.assertEqual(self.parsed(with_line(with_line(narrated(reads(3)), self.UNRELATED), self.BARE))["notice"], self.BARE,
                         "with no line that names a permission the first line that matches is the one quoted")

    def test_the_tool_step_and_denied_actions_fire_whatever_the_line_says(self):
        stray = [text for text in reads(1) if text.startswith("{")]
        stream = with_line(stray, self.UNRELATED)
        found = self.parsed(stream)
        self.assertEqual((found["notice"], found["auto_denied"], found["denied_actions"]), (self.UNRELATED, ["read_file " + FIRST_READ], ["read_file"]))
        self.assertTrue(providers.agy_auto_denied(found))
        calls = []
        code, _, _ = self.asked([stream, reads(2)], calls=calls)
        self.assertEqual((code, len(calls)), (0, 2))
        error = self.failed([stream, answered(text="")])
        self.assertIn("auto-denied 1 tool action (read_file " + FIRST_READ + ")", str(error))
        step_only = self.parsed(altered(stray, denied_actions=[]))
        self.assertEqual((step_only["denied_actions"], step_only["notice"]), ([], None))
        self.assertTrue(providers.agy_auto_denied(step_only))
        result_only = self.parsed(altered([text for text in stray if "ERROR" not in text], denied_actions=DENIED_RESULT))
        self.assertEqual((result_only["auto_denied"], result_only["notice"]), ([], None))
        self.assertTrue(providers.agy_auto_denied(result_only))

    def test_the_trigger_needs_a_permission_in_the_line_and_nothing_else_in_it(self):
        base = {"auto_denied": [], "denied_actions": [], "notice": None}
        for notice, expected in ((self.BARE, False), (self.UNRELATED, False), ("agy said so", False), ("", False), (None, False),
                                 (self.FIRST, True), (self.SECOND, True), ("no output produced: Permission denied", True)):
            with self.subTest(notice=notice):
                self.assertEqual(bool(providers.agy_auto_denied({**base, "notice": notice})), expected)

    def test_a_line_inside_a_json_event_is_still_no_signal(self):
        said = json.dumps({"event": "step_update", "step_update": {"conversation_id": "c", "step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                                  "text_delta": self.FIRST}})
        found = self.parsed([*lines_of(narrated(reads(3)))[:-1], said, lines_of(narrated(reads(3)))[-1]])
        self.assertIsNone(found["notice"])
        self.assertFalse(providers.agy_auto_denied(found))

    def test_a_denial_message_without_a_line_that_quotes_none_is_unchanged(self):
        message = providers.agy_silence(self.parsed(reads(1)), "planner", "call.jsonl")
        self.assertIn("agy auto-denied 1 tool action (read_file " + FIRST_READ + "): a headless call cannot answer a permission prompt", message)
        self.assertNotIn("agy printed", message)


CONVERSATION = "00000000-0000-4000-8000-000000019399"
URL_DENIED = 'permission check failed for read_url "https://example.com": Permission denied for read_url(https://example.com). Matches user-configured deny rule.'


def shaped(*steps, denied_actions=(), line=None, response=""):
    """A stream built step by step, for the shapes no lab call had. ("text", index, words) is an agent message; ("tool", index, name, parameters)
    a tool step that completes and ("tool", index, name, parameters, message) one that ends in ERROR with that message, as agy writes them."""
    def event(kind, **body):
        return json.dumps({"event": kind, kind: {"conversation_id": CONVERSATION, **body}})

    lines = [json.dumps({"event": "init", "conversation_id": CONVERSATION, "init": {"model": "gemini-4-neon", "tools": ["read_file"]}}),
             event("step_update", step_index=0, state="DONE", step_type="user_input")]
    for kind, index, *rest in steps:
        if kind == "text":
            lines.append(event("step_update", step_index=index, state="DONE", step_type="agent_response", text_delta=rest[0]))
            continue
        name, params, *refusal = rest
        info = {"name": name, "parameters": params}
        end = {**info, "error": {"type": "TOOL_ERROR", "message": refusal[0]}} if refusal else info
        lines.append(event("step_update", step_index=index, state="ACTIVE", step_type="tool", tool_name=name, tool_info=info))
        lines.append(event("step_update", step_index=index, state="ERROR" if refusal else "DONE", step_type="tool", tool_name=name, tool_info=end))
    if line:
        lines.append(line)
    result = {"conversation_id": CONVERSATION, "status": "SUCCESS", "response": response, "num_turns": 1,
              "usage": {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 110},
              **({"denied_actions": list(denied_actions)} if denied_actions else {})}
    return [*lines, json.dumps({"event": "result", "result": result})]


def read_step(index, name="read_file", path=FIRST_READ, message=None):
    return ("tool", index, name, {"Path": path}, *([message] if message else []))


# The skeptic's shape: narration, a read, the denied read, narration again, then two more reads of the same batch, and nothing after them.
SKEPTIC = [("text", 1, "I will read the files.\n"), read_step(2, path="C:/work/repo/a.txt"), read_step(3, message=AUTO),
           ("text", 4, "Reading the rest now.\n"), read_step(5, path="C:/work/outside/b.txt"), read_step(6, path="C:/work/outside/c.txt")]
SAID = "I will read the files.\nReading the rest now.\n"


class DeniedBatch(Resumed):
    """Round 3, finding B. The denied batch ends at the last tool step of the call, whichever step was denied. Round 2 ended it at the denied
    step, so text between that step and a later tool step of the same batch was kept as the answer and nothing was resumed."""

    def test_text_between_the_denied_step_and_a_later_tool_step_is_narration_and_the_call_is_resumed(self):
        for why, extra in (("with denied_actions", DENIED_RESULT), ("the step error alone", ())):
            with self.subTest(why=why):
                stream = shaped(*SKEPTIC, denied_actions=extra, response=SAID)
                found = self.parsed(stream)
                self.assertEqual((found["answer"], found["source"]), ("", None))
                self.assertEqual((found["messages"], found["narration"]), (2, 2))
                self.assertEqual(found["auto_denied"], ["read_file " + FIRST_READ])
                self.assertTrue(providers.agy_auto_denied(found))
                calls = []
                code, _, answer = self.asked([stream, reads(2)], calls=calls)
                self.assertEqual((code, len(calls)), (0, 2), "resumed")
                self.assertTrue(answer.startswith("unreadable (permission denied)"), "what the resume said, not the narration")
                self.assertNotIn("Reading the rest", answer)

    def test_a_message_after_the_last_tool_step_is_still_the_answer_alone_and_nothing_is_resumed(self):
        stream = shaped(*SKEPTIC, ("text", 7, "Two of the three files could be read.\n"), denied_actions=DENIED_RESULT, response=SAID + "Two of the three files could be read.\n")
        found = self.parsed(stream)
        self.assertEqual((found["answer"], found["source"], found["messages"], found["narration"]),
                         ("Two of the three files could be read.", "agent_response", 3, 2))
        calls = []
        code, _, answer = self.asked([stream], calls=calls)
        self.assertEqual((code, len(calls), answer), (0, 1, "Two of the three files could be read."))

    def test_the_text_at_step_4_is_before_the_last_tool_step_6_so_it_is_narration(self):
        """Round 2 ended the batch at the denied step 3 and counted one narration message (step 1); step 4 was kept as the answer."""
        found = self.parsed(shaped(*SKEPTIC, denied_actions=DENIED_RESULT, response=SAID))
        self.assertEqual(found["narration"], 2)

    def test_a_denied_step_that_is_the_last_tool_step_ends_the_batch_there_as_before(self):
        stream = shaped(("text", 1, "I will read the files.\n"), read_step(2, path="C:/work/repo/a.txt"), read_step(3, message=AUTO),
                        ("text", 4, "The denied file could not be read.\n"), denied_actions=DENIED_RESULT, response="I will read the files.\nThe denied file could not be read.\n")
        found = self.parsed(stream)
        self.assertEqual((found["answer"], found["narration"]), ("The denied file could not be read.", 1))


class FullLine(Resumed):
    """Round 3, finding C. Whether agy's line names a permission is judged on the whole line, not on the 600 characters kept for quoting."""

    LONG = "jetski: no output produced - " + "the model produced nothing and " * 24 + "a tool required the read_file permission that headless mode cannot prompt for."

    def silent(self, line):
        return with_line(narrated(reads(3)), line)

    def test_a_line_that_names_the_permission_after_character_600_is_a_denial(self):
        self.assertGreater(len(self.LONG), 650)
        self.assertNotIn("permission", self.LONG[:600])
        found = self.parsed(self.silent(self.LONG))
        self.assertEqual(found["notice"], self.LONG[:600], "the quote is still cut")
        self.assertIs(found["notice_permission"], True)
        self.assertTrue(providers.agy_auto_denied(found))
        self.assertEqual((found["auto_denied"], found["denied_actions"]), ([], []), "the line alone")
        calls = []
        code, _, answer = self.asked([self.silent(self.LONG), reads(2)], calls=calls)
        self.assertEqual((code, len(calls)), (0, 2), "resumed")
        self.assertTrue(answer.strip())

    def test_a_long_line_that_never_names_a_permission_is_not_one(self):
        line = "jetski: no output produced - " + "the model produced nothing and " * 30
        found = self.parsed(self.silent(line))
        self.assertEqual((found["notice"], found["notice_permission"]), (line[:600], False))
        self.assertFalse(providers.agy_auto_denied(found))
        calls = []
        error = self.failed([self.silent(line)], calls=calls)
        self.assertEqual((error.kind, len(calls)), ("no_answer", 1))
        self.assertIn("does not name a permission", str(error))

    def test_agy_line_gives_the_quote_and_whether_the_line_names_a_permission(self):
        path = self.root / "line.jsonl"
        put(path, lines_of(self.silent(self.LONG)))
        self.assertEqual(providers.agy_line(path), (self.LONG[:600], True))
        self.assertEqual(providers.agy_notice(path), self.LONG[:600], "agy_notice is the first of the two")
        self.assertEqual(providers.agy_line(self.root / "absent.jsonl"), (None, False))
        put(path, lines_of(narrated(reads(3))))
        self.assertEqual(providers.agy_line(path), (None, False))

    def test_a_permission_past_the_first_4096_bytes_of_a_line_is_not_looked_for(self):
        """The one limit of the whole-line test: a line is read to its first 4096 bytes, so a huge line costs one read."""
        line = "jetski: no output produced - " + "x" * 5000 + " permission"
        found = self.parsed(self.silent(line))
        self.assertEqual((found["notice"], found["notice_permission"]), (line[:600], False))

    def test_a_dict_without_the_flag_is_judged_on_the_quote(self):
        base = {"auto_denied": [], "denied_actions": [], "notice": "no output produced - permission"}
        self.assertTrue(providers.agy_auto_denied(base))
        self.assertFalse(providers.agy_auto_denied({**base, "notice_permission": False}), "the stored judgement wins")
        self.assertTrue(providers.agy_auto_denied({**base, "notice": "no output produced", "notice_permission": True}))
        self.assertFalse(providers.agy_auto_denied({**base, "notice": None, "notice_permission": True}), "no line, no signal")


class ResumedLine(Resumed):
    """Round 3, finding D. After a resume the resumed call's own line is quoted only when it names a permission; otherwise the first call's stays."""

    FIRST = Trigger.FIRST
    SECOND = Trigger.SECOND
    OTHER = "jetski: no output produced - the model returned an empty response and called no tool."

    def message(self, first, resumed):
        error = self.failed([first, resumed])
        self.assertEqual(error.kind, "no_answer")
        return str(error)

    def test_a_resumed_line_that_names_no_permission_does_not_replace_the_first_calls(self):
        message = self.message(reads(1), with_line(answered(text=""), self.OTHER))
        self.assertIn('agy said: "' + self.FIRST[:80], message)
        self.assertNotIn("the model returned an empty response", message)

    def test_a_resumed_line_that_names_a_permission_is_the_one_quoted(self):
        message = self.message(reads(1), with_line(answered(text=""), self.SECOND))
        self.assertIn('agy said: "the 2 tool(s) required approval', message)
        self.assertNotIn(self.FIRST[:80], message)

    def test_the_resumed_line_is_kept_when_the_first_call_printed_none(self):
        stray = [text for text in reads(1) if text.startswith("{")]
        message = self.message(stray, with_line(answered(text=""), self.OTHER))
        self.assertIn('agy said: "' + self.OTHER, message)

    def test_when_neither_call_printed_a_line_none_is_quoted(self):
        stray = [text for text in reads(1) if text.startswith("{")]
        self.assertNotIn("agy said", self.message(stray, answered(text="")))

    def test_the_first_calls_line_stays_when_the_resumed_call_prints_one_of_its_own_that_names_nothing_either(self):
        message = self.message(reads(1), with_line(answered(text=""), "jetski: no output produced - nothing to say."))
        self.assertIn('agy said: "' + self.FIRST[:80], message)


class NetworkRules(Resumed):
    """Round 3, finding A. A read-only role may read any local file, so it must not also be able to send what it read out through a URL tool.
    The two URL classes agy documents are denied for every domain; search_web cannot be denied by any rule that was tried, and a completed
    search or URL read is reported."""

    def test_the_read_only_file_denies_the_url_classes_and_the_worker_file_does_not(self):
        self.assertEqual(providers.AGY_NO_NETWORK, ("read_url(*)", "execute_url(*)"))
        self.assertEqual(AGY_DENY[-2:], providers.AGY_NO_NETWORK)
        read_only = json.loads(providers.settings_file().read_text(encoding="utf-8"))["permissions"]
        self.assertEqual(read_only["deny"][:7], list(AGY_DENY))
        self.assertFalse({"read_url(*)", "execute_url(*)"} & set(json.loads(providers.worker_settings_file().read_text(encoding="utf-8"))["permissions"]["deny"]),
                         "the worker may use the network")
        self.assertFalse({rule for rule in read_only["allow"] if rule.split("(")[0] in ("read_url", "execute_url", "read_url_content", "search_web")})

    def test_the_rules_that_did_not_gate_search_web_are_not_passed(self):
        """search_web completed under search_web(*) and web_search(*) (two live calls with them and one without, 2026-11-26): a rule that never matches only looks like a barrier."""
        for rule in ("search_web(*)", "web_search(*)"):
            self.assertNotIn(rule, AGY_DENY)
            self.assertNotIn(rule, json.loads(providers.settings_file().read_text(encoding="utf-8"))["permissions"]["deny"])

    def test_the_guard_stops_a_call_when_agy_did_not_list_a_network_rule(self):
        for rule in providers.AGY_NO_NETWORK:
            with self.subTest(rule=rule):
                providers.AGY_READ_ONLY.clear()
                error = self.failed([reads(3)], agy=rules_output(ALLOWED.replace(f',"{rule}"', "")))
                self.assertEqual(error.kind, "configuration")
                self.assertIn(rule, str(error))
        providers.AGY_READ_ONLY.clear()
        self.assertEqual(self.asked([reads(3)], mode="reviewer")[0], 0, "with the real listing the call goes on")

    def test_a_denied_url_read_or_search_is_a_harmless_read_that_did_not_run(self):
        """The 0.19.2 round 4 classification stands: a read-only role refused a read (a user's rule, or now ours) did not act."""
        stream = tooled(2, ("read_url_content", {"Url": "https://example.com"}, URL_DENIED),
                        ("search_web", {"query": "example domain"}, 'permission check failed for search_web: Permission denied for search_web(example domain). Matches user-configured deny rule.'))
        found = self.parsed(stream)
        self.assertEqual([entry.split()[0] for entry in found["denied"]], ["read_url_content", "search_web"])
        self.assertEqual((found["refused"], found["wrote"], found["network"]), ([], [], []), "denied steps did not run")
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                code, _, answer = self.asked([stream], mode=mode)
                self.assertEqual(code, 0)
                self.assertTrue(answer.strip())

    def test_a_completed_search_or_url_read_is_reported_and_never_costs_the_answer(self):
        stream = tooled(2, ("search_web", {"query": "example domain"}), ("read_url_content", {"Url": "https://example.com"}))
        found = self.parsed(stream)
        self.assertEqual(found["network"], ["search_web example domain", "read_url_content https://example.com"])
        self.assertEqual((found["denied"], found["refused"], found["wrote"]), ([], [], []))
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                code, _, answer = self.asked([stream], mode=mode)
                self.assertEqual(code, 0, "a search alone never discards the answer")
                self.assertTrue(answer.strip())

    def test_only_a_step_that_completed_counts_and_a_step_is_counted_once(self):
        found = self.parsed(shaped(("tool", 2, "search_web", {"query": "a"}), ("tool", 3, "search_web", {"query": "b"}, "failed: no network"),
                                   ("tool", 4, "read_file", {"Path": FIRST_READ}), ("text", 5, "done\n"), response="done\n"))
        self.assertEqual(found["network"], ["search_web a"], "an ERROR step did not complete, and the ACTIVE line of a step is not a second step")

    def test_the_query_is_cut_and_the_tool_is_matched_whatever_its_case(self):
        found = self.parsed(shaped(("tool", 2, "Search_Web", {"query": "q" * 500}), ("text", 3, "done\n"), response="done\n"))
        self.assertEqual(len(found["network"][0]), 300)
        self.assertTrue(found["network"][0].startswith("Search_Web qqq"))

    def test_agy_network_reads_the_resumed_call_as_well(self):
        first = shaped(("tool", 2, "search_web", {"query": "first"}), read_step(3, message=AUTO), denied_actions=DENIED_RESULT)
        resumed = shaped(("tool", 2, "read_url_content", {"Url": "https://example.com"}), ("text", 3, "Answer.\n"), response="Answer.\n")
        calls = []
        code, _, answer = self.asked([first, resumed], calls=calls)
        self.assertEqual((code, len(calls)), (0, 2))
        self.assertEqual(providers.agy_network(self.root / "call.jsonl"), ["search_web first", "read_url_content https://example.com"])
        self.assertEqual(providers.agy_network(self.root / "absent.jsonl"), [])
        put(self.root / "plain.jsonl", lines_of(reads(3)))
        self.assertEqual(providers.agy_network(self.root / "plain.jsonl"), [])

    def test_a_blank_query_is_still_named(self):
        found = self.parsed(shaped(("tool", 2, "search_web", {}), ("text", 3, "done\n"), response="done\n"))
        self.assertEqual(found["network"], ["search_web"])


AGY = {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}


class NetworkEvent(unittest.TestCase):
    """The engine records read_only_network for a read-only agy call whose stream shows a completed search or URL read, and keeps the answer."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def run_review(self, stream):
        from nightweaver import process
        from tempfile import TemporaryDirectory

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                return process.execute(argv, cwd, log, timeout, **options)
            put(Path(log), lines_of(stream))
            return 0

        home = TemporaryDirectory(prefix="nw-home-")
        self.addCleanup(home.cleanup)
        providers.AGY_READ_ONLY.clear()
        providers.AGY_READ_ALLOWED.clear()
        self.addCleanup(providers.AGY_READ_ONLY.clear)
        self.addCleanup(providers.AGY_READ_ALLOWED.clear)
        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1)
        with patch.dict("os.environ", {"NIGHTWEAVER_HOME": home.name}), patch("nightweaver.providers.execute", side_effect=execute), \
                patch("nightweaver.providers.agy_run", side_effect=rules_output(ALLOWED)):
            self.assertEqual(Run(self.store, run).run(), "done")
        return run, self.store.events(run)

    def test_a_reviewer_that_searched_the_web_passes_the_run_and_the_warning_names_the_search(self):
        verdict = answered(payload={"verdict": "PASS", "reason": "The change is correct."}, schema=SCHEMAS["reviewer"])
        run, events = self.run_review(with_tools(verdict, ("search_web", {"query": "example domain"})))
        self.assertIn("review_passed", [e["kind"] for e in events], "the answer was kept")
        warned = [e["details"] for e in events if e["kind"] == "read_only_network"]
        self.assertEqual(len(warned), 1)
        self.assertEqual((warned[0]["role"], warned[0]["steps"], warned[0]["count"], warned[0]["provider"]), ("reviewer", ["search_web example domain"], 1, "antigravity"))
        self.assertIn("search_web example domain", warned[0]["warning"])
        self.assertIn("cannot stop a web search", warned[0]["warning"])
        self.assertTrue(Path(warned[0]["log"]).is_file())
        kinds = [e["kind"] for e in events]
        self.assertLess(kinds.index("provider_call_finished"), kinds.index("read_only_network"))

    def test_a_reviewer_that_did_not_use_the_network_records_no_warning(self):
        verdict = answered(payload={"verdict": "PASS", "reason": "The change is correct."}, schema=SCHEMAS["reviewer"])
        _, events = self.run_review(verdict)
        self.assertNotIn("read_only_network", [e["kind"] for e in events])

    def test_a_denied_url_read_is_no_warning_because_it_did_not_run(self):
        verdict = answered(payload={"verdict": "PASS", "reason": "The change is correct."}, schema=SCHEMAS["reviewer"])
        _, events = self.run_review(with_tools(verdict, ("read_url_content", {"Url": "https://example.com"}, URL_DENIED)))
        self.assertNotIn("read_only_network", [e["kind"] for e in events])
        self.assertIn("review_passed", [e["kind"] for e in events])

    def test_the_worker_may_use_the_network_and_is_never_flagged(self):
        run = self.configured(fixture("good"), attempts=1)
        log = Path(self.store.root) / "worker.jsonl"
        put(log, lines_of(with_tools(reads(3), ("search_web", {"query": "example domain"}))))
        Run(self.store, run).flag_network(AGY, "worker", "worker", log)
        self.assertNotIn("read_only_network", [e["kind"] for e in self.store.events(run)])
        Run(self.store, run).flag_network(AGY, "reviewer", "reviewer", log)
        self.assertIn("read_only_network", [e["kind"] for e in self.store.events(run)], "the same log under a read-only mode is flagged")
        Run(self.store, run).flag_network({"kind": "claude"}, "reviewer", "reviewer", log)
        self.assertEqual([e["kind"] for e in self.store.events(run)].count("read_only_network"), 1, "only agy's streams are read")

    def test_a_query_that_holds_a_token_is_redacted_in_the_event(self):
        verdict = answered(payload={"verdict": "PASS", "reason": "The change is correct."}, schema=SCHEMAS["reviewer"])
        token = "sk-ant-api03-" + "a" * 40
        _, events = self.run_review(with_tools(verdict, ("search_web", {"query": "what is " + token})))
        warned = next(e["details"] for e in events if e["kind"] == "read_only_network")
        self.assertNotIn(token, json.dumps(warned))
        self.assertIn("search_web what is", warned["steps"][0])


class EngineEvidence(unittest.TestCase):
    """A run whose agy reviewer was auto-denied and resumed: the run goes on, and its evidence holds both calls."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def test_a_reviewer_resumed_after_a_denied_read_passes_the_run_and_the_event_counts_both_calls(self):
        from nightweaver import process
        from tempfile import TemporaryDirectory
        agy_calls = []
        verdict = answered(payload={"verdict": "PASS", "reason": "The change is correct."}, schema=SCHEMAS["reviewer"])

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                return process.execute(argv, cwd, log, timeout, **options)
            agy_calls.append({"argv": list(argv), "log": Path(log)})
            put(Path(log), lines_of(verdict if "--conversation" in argv else reads(1)))
            return 0

        home = TemporaryDirectory(prefix="nw-home-")
        self.addCleanup(home.cleanup)
        providers.AGY_READ_ONLY.clear()
        providers.AGY_READ_ALLOWED.clear()
        self.addCleanup(providers.AGY_READ_ONLY.clear)
        self.addCleanup(providers.AGY_READ_ALLOWED.clear)
        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1)
        with patch.dict("os.environ", {"NIGHTWEAVER_HOME": home.name}), patch("nightweaver.providers.execute", side_effect=execute), \
                patch("nightweaver.providers.agy_run", side_effect=rules_output(ALLOWED)):
            self.assertEqual(Run(self.store, run).run(), "done")
        self.assertEqual(len(agy_calls), 2, "one reviewer call, resumed once")
        self.assertNotIn("--conversation", agy_calls[0]["argv"])
        self.assertIn("--conversation", agy_calls[1]["argv"])
        events = self.store.events(run)
        finished = [e["details"] for e in events if e["kind"] == "provider_call_finished" and e["details"].get("role") == "reviewer"]
        self.assertEqual(len(finished), 1, "the engine counts one provider call")
        self.assertEqual(finished[0]["observed_tokens"], FIRST_USAGE + RESUMED_USAGE)
        self.assertEqual(finished[0]["log"], str(agy_calls[0]["log"]))
        self.assertEqual(finished[0]["resume_log"], str(agy_calls[1]["log"]))
        self.assertTrue(Path(finished[0]["log"]).is_file() and Path(finished[0]["resume_log"]).is_file())
        self.assertIn("review_passed", [e["kind"] for e in events])
        self.assertNotIn("provider_retry_scheduled", [e["kind"] for e in events], "the denial no longer costs a retry")


class Release(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_version_is_0_19_3_everywhere(self):
        import nightweaver
        self.assertEqual(nightweaver.__version__, "0.19.3")
        self.assertIn('version = "0.19.3"', (self.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## What's new in 0.19.3", readme)
        self.assertLess(readme.index("## What's new in 0.19.3"), readme.index("## What's new in 0.19.2"))
        self.assertIn("docs/v0.19.3-notes.md", readme)

    def test_the_readme_counts_the_suite_by_its_test_methods_and_lists_this_module(self):
        counts = {path.name: len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"), re.M)) for path in Path(__file__).parent.glob("test_*.py")}
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"The suite has {sum(counts.values()):,} tests.", readme)
        mine = counts["test_release_0193_agy_reads.py"]
        self.assertIn(f"Version 0.19.3 added {mine} in `test_release_0193_agy_reads`", readme)

    def test_the_notes_say_what_failed_why_what_changed_and_where_it_stops(self):
        notes = (self.ROOT / "docs" / "v0.19.3-notes.md").read_text(encoding="utf-8")
        for phrase in ("research repository", "user denied permission for read_file", "denied_actions", "no output produced", "read_file(*)", "worker",
                       "resumed once", "<log>.resume.jsonl", "Limits", "allowNonWorkspaceAccess", "read_url_content"):
            self.assertIn(phrase, notes)
        self.assertFalse(re.search(r"Administrator|C:\\Users|@gmail", notes))

    def test_the_notes_and_the_readme_say_what_the_review_found_and_what_the_fixes_do_not_prove(self):
        notes = (self.ROOT / "docs" / "v0.19.3-notes.md").read_text(encoding="utf-8")
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        review = notes[notes.index("## Review fixes"):notes.index("## Tests")]
        for phrase in ("replaces `permissions.allow` and `permissions.deny`", "does not merge", "read_file(**/.env)", "providers.json", "profiles",
                       "DEEPSEEK_API_KEY_FILE", ".credentials.json", "auth.json", "oauth_creds.json", "profile_dir", "api_key_file", "oauth_token_file",
                       "runs/<id>", "was not run", "not tested", "digest", "protect()", "project and shared scopes", "Round 2"):
            self.assertIn(phrase, review)
        for phrase in ("Your own `agy` rules and your logins", "docs/v0.19.3-notes.md#review-fixes", "A live read-only call with the real generated file"):
            self.assertIn(phrase, readme)
        self.assertNotIn("was not run live", readme, "round 2 ran it")
        self.assertNotIn("and no deny rule, because", readme, "the worker's file has the login-read deny rules and the user's own")

    def test_the_notes_say_what_round_two_changed_and_what_the_live_calls_proved_and_did_not(self):
        notes = (self.ROOT / "docs" / "v0.19.3-notes.md").read_text(encoding="utf-8")
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        round_two = notes[notes.index("## Round 2 review fixes"):notes.index("## Round 3 review fixes")]
        for phrase in ("AGY_RESUME_FLOOR", "20 seconds", "skipped for lack of time", "narration", "last auto-denied tool step", "structured_output",
                       "denied_actions", "`permission` in the line", "Matches user-configured deny rule", "backslashes", "junction",
                       "No rule form had to change", "What was not run", "Git Bash", "`687f4c6`"):
            self.assertIn(phrase, round_two)
        self.assertEqual(providers.AGY_RESUME_FLOOR, 20)
        self.assertFalse(re.search(r"Administrator|C:\\Users|@gmail|Nightweaver-trials|agy-reads-lab", notes + readme))
        for phrase in ("only the time the first call left", "narration and not an answer", "names a permission"):
            self.assertIn(phrase, readme)

    def test_the_notes_say_what_round_three_found_changed_proved_and_did_not(self):
        notes = (self.ROOT / "docs" / "v0.19.3-notes.md").read_text(encoding="utf-8")
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        round_three = notes[notes.index("## Round 3 review fixes"):notes.index("## Tests")]
        for phrase in ("`read_url(*)` and `execute_url(*)`", "`search_web` cannot be gated", "known limit", "completed under `search_web(*)` and under `web_search(*)`",
                       "never matched live", "not in its tool list", "Not proven", "`read_only_network`", "The answer is kept", "three of `agy`'s own help files",
                       "last tool step of the call", "first 4096 bytes", "`notice_permission`", "only when that line names a permission", "Turn 3 (control)"):
            self.assertIn(phrase, round_three)
        self.assertNotIn("the network tools are not allowed", notes, "round 3 found search_web running")
        self.assertIn("it read three of its own help files", notes, "the round 2 account of r2-exp-01 is corrected")
        for phrase in ("`search_web` still runs in a read-only role", "`read_only_network`", "Review round 3", "never matched live"):
            self.assertIn(phrase, readme)
        self.assertEqual(providers.AGY_NO_NETWORK, ("read_url(*)", "execute_url(*)"))
        self.assertFalse(re.search(r"Administrator|C:\\Users|@gmail|Nightweaver-trials|agy-reads-lab", notes + readme))


if __name__ == "__main__":
    unittest.main()
