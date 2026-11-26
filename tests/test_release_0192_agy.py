"""Nightweaver 0.19.2: the antigravity provider made to work for every role.

The streams in tests/agy_streams are what agy 1.5.17 really wrote in nine signed-in runs on 2026-11-25 (a scratch repository with one seeded
bug, `agy --input-format stream-json --output-format stream-json`, the task on stdin as Nightweaver sends it), with conversation ids and local
paths replaced. A tenth capture (exp-10) is a review run by the skeptic who tested the read-only layers: a read-only reviewer asked to try six
writing tools, one of them a subagent. Two more files hold what `agy -p /permissions` printed, which costs no model turn. Every test replays
those captures or builds a variation of one (the answer of another role swapped in, an answer blanked, a tool step swapped for another tool's,
as `tooled` does); no model is called and no network is used."""

import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import explore, providers, pursuit, readiness, rescue, wish
from nightweaver.engine import Run, git
from nightweaver.providers import SCHEMAS, ProviderUnavailable
from nightweaver.reports import metrics
from nightweaver.verdicts import extract_object, extract_verdict
import test_windows as fixtures
from test_release_0191_gemini import ALL_MODES, Hermetic, write
from test_windows import fixture

STREAMS = Path(__file__).parent / "agy_streams"
READ_ONLY_MODES = tuple(mode for mode in ALL_MODES if mode != "worker")


def raw(number):
    """The lines of one capture, as agy wrote them (a stderr line, where agy printed one, comes first)."""
    return next(STREAMS.glob(f"exp-{number:02d}-*.jsonl")).read_text(encoding="utf-8").splitlines()


def events(number):
    return [json.loads(line) for line in raw(number) if line.startswith("{")]


def part(event):
    return event[event["event"]]


def steps(number, kind=None, state=None):
    return [part(e) for e in events(number) if e["event"] == "step_update" and (kind is None or part(e)["step_type"] == kind)
            and (state is None or part(e)["state"] == state)]


def result_of(number):
    return next(part(e) for e in events(number) if e["event"] == "result")


def put(path, items):
    write(path, *items)
    return path


TESTER = "C:\\Users\\tester"  # the made-up home folder the captures name (agy keeps its conversation folder under the home)


def at_home(items, home):
    """The captures with their made-up home folder replaced by the temporary one that Path.home() returns in a test."""
    old, new = json.dumps(TESTER)[1:-1], json.dumps(str(home))[1:-1]
    return [(line if isinstance(line, str) else json.dumps(line)).replace(old, new) for line in items]


def tooled(number, *calls, step_type="tool"):
    """A capture whose tool steps are replaced by these calls, in the shape agy wrote them: ACTIVE, then DONE (or ERROR with the refusal agy
    wrote when a call is (name, parameters, message)). Init, messages, usage and result stay, so the answer is the capture's."""
    kept = [e for e in events(number) if not (e["event"] == "step_update" and part(e)["step_type"] in ("tool", "subagent"))]
    conversation = kept[0]["conversation_id"]
    made = []
    for index, (name, params, *refusal) in enumerate(calls, start=2):
        info = {"name": name, "parameters": params}
        made.append({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index, "state": "ACTIVE",
                                                              "step_type": step_type, "tool_name": name, "tool_info": info}})
        end = {**info, "error": {"type": "TOOL_ERROR", "message": refusal[0]}} if refusal else info
        made.append({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index,
                                                              "state": "ERROR" if refusal else "DONE", "step_type": step_type, "tool_name": name,
                                                              "duration_seconds": 0.5, "tool_info": end}})
    at = next(i for i, e in enumerate(kept) if e["event"] == "result")
    return kept[:at] + made + kept[at:]


def blanked(number):
    """A capture whose agent said nothing: every message text and the result's response and structured_output are gone, as in the empty
    answer Nightweaver was reported to receive. Tool steps, usage and everything else stay."""
    out = []
    for event in events(number):
        body = part(event)
        if event["event"] == "step_update" and body["step_type"] == "agent_response":
            body = {k: v for k, v in body.items() if k != "text_delta"}
            event = {"event": "step_update", "step_update": body}
        if event["event"] == "result":
            body = {**body, "response": ""}
            body.pop("structured_output", None)
            event = {"event": "result", "result": body}
        out.append(event)
    return out


def retargeted(number, payload, schema=None, text=None):
    """A capture with the answer of another role swapped in: agy's own init, tool steps, ids, usage and event order, but one agent message
    holding `payload` (or `text`) and a result whose response and structured_output say the same, as agy writes them."""
    said = text if text is not None else json.dumps(payload) + "\n"
    kept = [e for e in events(number) if not (e["event"] == "step_update" and part(e)["step_type"] == "agent_response" and "text_delta" in part(e))]
    last = max(part(e)["step_index"] for e in kept if e["event"] == "step_update")
    conversation = part(kept[0]).get("conversation_id") or kept[0]["conversation_id"]
    spoken = {"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": last + 1, "state": "DONE", "step_type": "agent_response",
                                                      "text_delta": said, "duration_seconds": 2.0,
                                                      "usage": {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 5, "cache_read_tokens": 0,
                                                                "total_tokens": 110}}}
    out = []
    for event in kept:
        body = part(event)
        if event["event"] == "init":
            body = {**body, **({"json_schema": schema} if schema else {})}
            if not schema:
                body.pop("json_schema", None)
            event = {"event": "init", "conversation_id": event["conversation_id"], "init": body}
        if event["event"] == "result":
            out.append(spoken)
            body = {**body, "response": said}
            body.pop("structured_output", None)
            body.pop("json_schema", None)
            if schema:
                body.update(structured_output=payload, json_schema=schema)
            event = {"event": "result", "result": body}
        out.append(event)
    return out


# What `agy -p /permissions` printed (stdout), with no settings and with Nightweaver's --settings file. Zero turns, zero tokens.
BARE = (STREAMS / "permissions-bare.jsonl").read_text(encoding="utf-8")
LOADED = (STREAMS / "permissions-readonly.jsonl").read_text(encoding="utf-8")


class Replayed(Hermetic):
    def log(self, items, name="call.jsonl"):
        return put(self.root / name, at_home(items, self.root / "user"))

    def stream(self, number, mode=None, items=None):
        return providers.agy_stream(self.log(items if items is not None else raw(number)), mode)


class RealStreams(Replayed):
    """The parser against every success stream agy 1.5.17 wrote: conversation, model, answer and where it came from, usage, how it ended."""

    def test_every_capture_names_its_conversation_model_and_a_clean_finish(self):
        for number in range(1, 11):
            found = self.stream(number, "reviewer")
            self.assertRegex(found["session"], r"^00000000-0000-4000-8000-\d{12}$", number)
            self.assertEqual((found["model"], found["status"], found["failed"], found["reason"], found["error"]),
                             ("gemini-4-neon", "SUCCESS", False, None, None), number)
            self.assertEqual(found["session"], result_of(number)["conversation_id"], "the id of init, of every step and of the result agree")

    def test_a_plan_mode_review_without_a_schema_answers_with_the_response(self):
        found = self.stream(1)
        self.assertEqual(found["source"], "response")
        verdict = extract_verdict(found["answer"], ("PASS", "REVISE"))
        self.assertEqual(verdict["verdict"], "REVISE")
        self.assertIn("a + b", verdict["reason"])
        self.assertEqual(found["messages"], 1, "the first agent step only called tools and has no text")
        self.assertEqual(providers.metadata(self.log(raw(1)), "antigravity"), (found["session"], found["answer"]))

    def test_a_schema_role_answers_with_the_structured_output_never_the_doubled_response(self):
        """After agy's own re-ask (num_turns 2) `response` holds the JSON twice, in two key orders; structured_output is the one answer."""
        real = result_of(2)
        self.assertEqual(real["num_turns"], 2)
        self.assertEqual(len(real["response"].strip().splitlines()), 2, "the capture really has the answer twice")
        with self.assertRaises(ValueError):
            json.loads(real["response"])
        found = self.stream(2, "reviewer")
        self.assertEqual((found["source"], json.loads(found["answer"])), ("structured_output", real["structured_output"]))
        self.assertEqual(set(json.loads(found["answer"])), {"verdict", "reason"})
        self.assertEqual(extract_verdict(found["answer"], ("PASS", "REVISE"))["verdict"], "REVISE")

    def test_without_a_schema_the_doubled_response_is_still_read_by_the_tolerant_extractor(self):
        found = self.stream(2, None)
        self.assertEqual(found["source"], "response")
        self.assertEqual(extract_verdict(found["answer"], ("PASS", "REVISE"))["verdict"], "REVISE", "two agreeing objects are one answer")

    def test_a_ready_probe_answers_ready(self):
        found = self.stream(3, "probe")
        self.assertEqual((found["answer"].strip(), found["source"], found["messages"]), ("READY", "response", 1))

    def test_a_message_that_arrives_whole_in_its_done_event_is_read(self):
        """At xhigh agy streamed no chunk (exp-04): the DONE event holds the whole text. With the result's own fields gone, that text is the answer."""
        done = [s for s in steps(4, "agent_response", "DONE") if "text_delta" in s]
        self.assertEqual(len(done), 1)
        self.assertEqual(len([s for s in steps(4, "agent_response", "ACTIVE")]), 0)
        items = [e for e in events(4) if e["event"] != "result"]
        found = self.stream(4, "reviewer", items)
        self.assertEqual((found["answer"], found["source"]), (done[0]["text_delta"].strip(), "agent_response"))
        self.assertEqual(json.loads(found["answer"])["verdict"], "REVISE")

    def test_the_last_message_with_text_is_the_answer_when_nothing_else_is(self):
        items = [e for e in events(2) if e["event"] != "result"]
        last = part(items[-2])
        blank = {"event": "step_update", "step_update": {**last, "step_index": 40, "text_delta": "\n", "state": "DONE"}}
        found = self.stream(2, "reviewer", [*items[:-1], blank, items[-1]])
        self.assertEqual(found["source"], "agent_response")
        self.assertEqual(extract_verdict(found["answer"], ("PASS", "REVISE"))["verdict"], "REVISE", "a final blank message does not hide the answer")
        cut = self.stream(2, "reviewer", items[:-3])
        self.assertEqual((cut["status"], cut["failed"]), (None, False), "a stream cut off before its result is not a failure of its own")
        self.assertTrue(cut["answer"], "what was said before the cut is still there")

    def test_a_subagents_words_are_not_the_answer(self):
        main = events(2)[0]["conversation_id"]
        items = [events(2)[0], {"event": "step_update", "step_update": {"conversation_id": main, "step_index": 1, "state": "DONE",
                                                                      "step_type": "agent_response", "text_delta": "main answer"}},
                 {"event": "step_update", "step_update": {"conversation_id": "other-conversation", "step_index": 2, "state": "DONE",
                                                          "step_type": "agent_response", "text_delta": "subagent chatter"}}]
        self.assertEqual(self.stream(2, None, items)["answer"], "main answer")

    def test_a_plan_file_agy_wrote_is_named_from_the_stream_and_is_not_the_answer(self):
        found = self.stream(5, "reviewer")
        self.assertTrue(found["plan_file"].endswith("implementation_plan.md"))
        self.assertIn(found["session"], found["plan_file"], "the path is found by the conversation id in the stream, not by the newest file")
        self.assertEqual(found["wrote"], [], "agy's own conversation folder is not the repository")
        self.assertEqual(json.loads(found["answer"])["verdict"], "REVISE")
        self.assertNotIn("Implementation Plan", found["answer"])
        self.assertIsNone(self.stream(1)["plan_file"], "a review that only reads never writes a plan")

    def test_the_actions_agy_refused_under_the_deny_rules_are_expected_not_failures(self):
        found = self.stream(6, "reviewer")
        self.assertEqual(found["failed"], False)
        self.assertEqual([entry.split()[0] for entry in found["denied"]], ["edit_file", "write_file"])
        self.assertEqual(found["wrote"], [])
        for error in [s["tool_info"]["error"] for s in steps(6, "tool", "ERROR")]:
            self.assertEqual(error["type"], "TOOL_ERROR")
            self.assertIn("Matches user-configured deny rule", error["message"])
        shell = self.stream(8, "reviewer")
        self.assertEqual([entry.split()[0] for entry in shell["denied"]], ["run_command"])
        self.assertEqual(shell["wrote"], [])

    def test_an_edit_that_completed_is_seen_in_the_stream(self):
        """exp-07 is the control: the same task with no deny rules went through (git showed M calc.py and ?? probe.txt)."""
        found = self.stream(7, "reviewer")
        self.assertEqual([entry.split()[0] for entry in found["wrote"]], ["edit_file", "write_file"])
        self.assertTrue(found["wrote"][0].endswith("calc.py") and found["wrote"][1].endswith("probe.txt"))
        self.assertEqual(found["denied"], [])
        self.assertEqual([entry for entry in self.stream(1)["wrote"] + self.stream(6)["wrote"]], [])

    def test_a_warning_agy_printed_on_stderr_does_not_disturb_the_parse(self):
        self.assertTrue(raw(8)[0].startswith("warning: --mode auto is not available"))
        found = self.stream(8, "reviewer")
        self.assertEqual((found["model"], found["failed"], found["source"]), ("gemini-4-neon", False, "structured_output"))
        self.assertEqual(json.loads(found["answer"])["verdict"], "REVISE")
        self.assertEqual(providers.provider_errors(self.log(raw(8))), raw(8)[0], "the stderr line is all a classifier would see")

    def test_the_served_model_is_the_init_model_for_every_capture(self):
        for number in range(1, 11):
            self.assertEqual(providers.served_models({"kind": "antigravity"}, self.log(raw(number))), {"models": ["gemini-4-neon"], "source": "init"})


class UsageAccounting(Replayed):
    def test_a_fresh_call_reports_what_its_steps_and_its_result_both_say(self):
        for number in (1, 2, 3, 4, 5, 6, 7, 8):
            expected = sum(s["usage"]["total_tokens"] for s in steps(number) if "usage" in s)
            self.assertEqual(result_of(number)["usage"]["total_tokens"], expected, f"in the capture {number} the two agree")
            receipt = providers.usage(self.log(raw(number)), "antigravity")
            self.assertEqual(receipt["observed_tokens"], expected, number)
            self.assertEqual(receipt["token_channels"]["input_tokens"], result_of(number)["usage"]["input_tokens"], number)
            self.assertEqual(receipt["billing"], "not_measured")

    def test_a_resumed_conversation_is_counted_once_not_cumulatively(self):
        """exp-09 resumed the conversation of exp-06: agy's result.usage is the whole conversation (20759), this call's own steps are 5735."""
        total, own = result_of(9)["usage"]["total_tokens"], sum(s["usage"]["total_tokens"] for s in steps(9) if "usage" in s)
        self.assertEqual((total, own), (20759, 5735))
        self.assertEqual(total, result_of(6)["usage"]["total_tokens"] + own, "the earlier call's tokens are inside the cumulative figure")
        log = self.log(raw(9))
        receipt = providers.usage(log, "antigravity")
        self.assertEqual(receipt["observed_tokens"], 5735)
        self.assertEqual(receipt["token_channels"], {"input_tokens": 5330, "output_tokens": 405, "thinking_tokens": 214, "cache_read_tokens": 17118})
        found = providers.agy_stream(log)
        self.assertEqual((found["usage"]["total_tokens"], found["call_usage"]["total_tokens"]), (20759, 5735))
        # the first call and its resume, added up as the engine adds them, are the conversation's total and not more
        both = providers.usage(self.log(raw(6), "first.jsonl"), "antigravity")["observed_tokens"] + receipt["observed_tokens"]
        self.assertEqual(both, total)

    def test_a_stream_cut_off_before_its_result_still_counts_the_steps_it_finished(self):
        items = [e for e in events(1) if e["event"] != "result"]
        self.assertEqual(providers.usage(self.log(items), "antigravity")["observed_tokens"], 13446)

    def test_without_step_usage_the_results_figure_is_used_and_missing_telemetry_stays_unknown(self):
        bare = [{"event": "result", "result": {"conversation_id": "c", "status": "SUCCESS", "response": "x", "usage": {"total_tokens": 9}}}]
        self.assertEqual(providers.usage(self.log(bare), "antigravity")["observed_tokens"], 9)
        self.assertIsNone(providers.usage(self.log([{"event": "init", "conversation_id": "c", "init": {}}]), "antigravity")["observed_tokens"])


class ReadOnlyArgv(Hermetic):
    def test_every_read_only_role_runs_accept_edits_behind_the_deny_rules_and_never_plan_mode(self):
        for mode in READ_ONLY_MODES:
            argv, _ = providers.adapter({"kind": "antigravity", "model": "gemini-4-neon", "effort": "xhigh"}, mode)
            self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits", mode)
            self.assertNotIn("plan", argv, mode)
            self.assertNotIn("--disable-slash-commands", argv, "plan mode is gone, and the flag is untested beside --settings")
            self.assertNotIn("--sandbox", argv, "the sandbox restricts terminal commands only (an edit went through under it)")
            self.assertEqual(argv.count("--settings"), 1, mode)
            self.assertEqual(("--json-schema" in argv), mode in SCHEMAS, mode)
            self.assertEqual(argv[argv.index("--effort") + 1:argv.index("--effort") + 2], ["xhigh"])

    def test_the_worker_alone_may_edit_and_keeps_its_flag_and_no_settings(self):
        argv, _ = providers.adapter({"kind": "antigravity"}, "worker")
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--json-schema", argv)

    def test_the_settings_file_holds_exactly_the_deny_rules_the_live_runs_proved(self):
        argv, _ = providers.adapter({"kind": "antigravity"}, "reviewer")
        target = Path(argv[argv.index("--settings") + 1])
        self.assertTrue(target.is_relative_to(self.root / "home"), "Nightweaver's own folder, never a worktree")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"permissions": {"deny": list(providers.AGY_DENY)}})
        self.assertEqual(providers.AGY_DENY[:4], ("write_file(*)", "write_file(/)", "write_file(.)", "command(*)"),
                         "the rules that exp-06 and exp-08 showed blocking edit_file, write_file and run_command")
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        target.write_text('{"permissions": {"deny": []}}', encoding="utf-8")
        providers.adapter({"kind": "antigravity"}, "judge")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["permissions"]["deny"], list(providers.AGY_DENY), "a stale file is rewritten")
        self.assertEqual({providers.adapter({"kind": "antigravity"}, mode)[0][providers.adapter({"kind": "antigravity"}, mode)[0].index("--settings") + 1]
                          for mode in READ_ONLY_MODES}, {str(target)}, "one file for every read-only role")

    def test_a_probe_and_a_reviewer_differ_only_by_the_schema(self):
        probe, _ = providers.adapter({"kind": "antigravity", "model": "gemini-4-neon"}, "probe")
        reviewer, _ = providers.adapter({"kind": "antigravity", "model": "gemini-4-neon"}, "reviewer")
        index = reviewer.index("--json-schema")
        self.assertEqual([*reviewer[:index], *reviewer[index + 2:]], probe)

    def test_resume_keeps_every_flag(self):
        argv, _ = providers.adapter({"kind": "antigravity"}, "reviewer", "conv-3")
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-3")
        for flag in ("--settings", "--json-schema", "--mode"):
            self.assertIn(flag, argv, "agy was only seen honouring the flags that were repeated on the resumed call")

    def test_the_version_is_0_19_2_everywhere(self):
        import nightweaver
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(nightweaver.__version__, "0.19.2")
        self.assertIn('version = "0.19.2"', (root / "pyproject.toml").read_text(encoding="utf-8"))


def rules_output(text):
    return lambda executable, env, *args, timeout=30, cwd=None: (0, text) if "/permissions" in args else (0, "gemini-4-neon-xhigh")


class Called(Hermetic):
    """Each role's path through invoke(), with agy replaced by one that writes a capture."""

    def call(self, items, *, mode, config=None, code=0, agy=None, prompt="Review it.", during=None, **kwargs):
        """`during` runs while agy "runs": whatever a subagent or a tool does to the worktree (the folder the call is made in, self.root)."""
        seen = {"executed": 0}

        def fake(argv, cwd, log, timeout, **options):
            seen.update(argv=argv, prompt=options["prompt"], env=options["env"], executed=seen["executed"] + 1)
            put(log, at_home(items, self.root / "user"))
            if during:
                during()
            return code

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=agy or rules_output(LOADED)):
            outcome = providers.invoke({"kind": "antigravity", "executable": sys.executable, **(config or {})}, mode, self.root,
                                       self.root / "call.jsonl", prompt, 10, lambda: False, **kwargs)
        return outcome, seen


JUDGMENT = {"verdict": "MET", "score": 91, "criteria": [{"criterion": "works", "met": True, "note": "seen"}], "gaps": [], "reason": "done"}
WAVE = {"milestones": [{"title": "Fix sub", "task": "Make sub subtract."}], "conflicts": []}
DIRECTIONS = {"directions": [{"name": "Plain", "brief": "Keep it plain."}, {"name": "Bold", "brief": "Make it bold."}]}
DIAGNOSIS = {"diagnosis": "The build never changes sub.", "strategies": [{"name": "Rewrite", "approach": "Rewrite sub."},
                                                                        {"name": "Patch", "approach": "Patch the operator."}]}
BRIEF = {"title": "Fix sub", "objective": "Make sub subtract.", "acceptance": ["sub(5, 3) is 2", "add is unchanged", "tests pass", "README lists sub"],
         "visual": False, "conflicts": []}
# role -> (answer swapped into a capture, what the role's own parser makes of what invoke() returns)
ROLES = {
    "reviewer": ({"verdict": "PASS", "reason": "The change is correct."}, lambda a: extract_verdict(a, ("PASS", "REVISE"))["verdict"] == "PASS"),
    "judge": (JUDGMENT, lambda a: pursuit.parse_judgment(a)["score"] == 91),
    "replanner": (WAVE, lambda a: pursuit.parse_wave(a, 3)[0] == [{"title": "Fix sub", "task": "Make sub subtract."}]),
    "directions": (DIRECTIONS, lambda a: [d["name"] for d in explore.parse_directions(a, 2)] == ["Plain", "Bold"]),
    "comparison": ({"winner": "tie", "reason": "Equal."}, lambda a: explore.parse_comparison(a)[0] == "TIE"),
    "ranking": ({"ranking": ["B", "A"], "reason": "B is better."}, lambda a: explore.parse_ranking(a, ["A", "B"])[0] == ["B", "A"]),
    "diagnosis": (DIAGNOSIS, lambda a: [s["name"] for s in rescue.parse_diagnosis(a, 2)[1]] == ["Rewrite", "Patch"]),
}
# The roles whose prompt asks for JSON in prose (no schema): agy narrates, then answers.
PROSE = {"planner": (WAVE, lambda a: [m["title"] for m in extract_object(a, "milestones")["milestones"]] == ["Fix sub"]),
         "brief": (BRIEF, lambda a: wish.read_brief(a)["title"] == "Fix sub")}


class EveryRole(Called):
    def test_each_schema_role_gets_its_structured_answer_in_read_only_accept_edits(self):
        self.assertEqual(set(ROLES), set(SCHEMAS), "every role with a schema is covered")
        for role, (payload, parse) in ROLES.items():
            with self.subTest(role=role):
                (code, session, answer), seen = self.call(retargeted(2, payload, SCHEMAS[role]), mode=role)
                self.assertEqual((code, session), (0, events(2)[0]["conversation_id"]))
                self.assertEqual(json.loads(answer), payload, "agy's structured_output, as one JSON object")
                self.assertTrue(parse(answer), "the role's own parser reads it")
                argv = seen["argv"]
                self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
                self.assertEqual(json.loads(Path(argv[argv.index("--json-schema") + 1]).read_text(encoding="utf-8")), SCHEMAS[role])
                self.assertEqual(json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))["permissions"]["deny"][3], "command(*)")

    def test_each_prose_role_gets_its_answer_out_of_agys_narration(self):
        for role, (payload, parse) in PROSE.items():
            with self.subTest(role=role):
                text = "I'll read the repository first.\n\nHere is the answer.\n```json\n" + json.dumps(payload) + "\n```\n"
                (code, _, answer), seen = self.call(retargeted(1, payload, text=text), mode=role)
                self.assertEqual(code, 0)
                self.assertTrue(parse(answer))
                self.assertNotIn("--json-schema", seen["argv"])
                self.assertIn("--settings", seen["argv"])

    def test_the_probe_and_the_real_ready_capture(self):
        (code, _, answer), seen = self.call(raw(3), mode="probe", prompt="Reply with the single word READY.")
        self.assertEqual((code, answer.strip().casefold()), (0, "ready"))
        self.assertEqual(json.loads(seen["prompt"])["message"]["content"], providers.AGY_READ_ONLY_NOTE + "\n\nReply with the single word READY.",
                         "a read-only call's prompt follows the note that says edits and commands are denied (see test_release_0192_agy_note)")

    def test_the_reviewer_over_the_real_captures_with_and_without_a_schema(self):
        for number, mode in ((1, "brief"), (2, "reviewer"), (4, "reviewer"), (9, "reviewer")):
            with self.subTest(capture=number):
                (code, _, answer), _ = self.call(raw(number), mode=mode)
                self.assertEqual((code, extract_verdict(answer, ("PASS", "REVISE"))["verdict"]), (0, "REVISE"))

    def test_a_plan_mode_capture_that_wrote_a_plan_still_yields_the_verdict_not_the_plan(self):
        (code, _, answer), _ = self.call(raw(5), mode="reviewer")
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(answer)), {"verdict", "reason"})

    def test_the_worker_is_unchanged_and_its_prose_is_its_answer(self):
        (code, session, answer), seen = self.call(raw(7), mode="worker", prompt="Fix sub.")
        self.assertEqual(code, 0)
        self.assertIn("--disable-slash-commands", seen["argv"])
        self.assertNotIn("--settings", seen["argv"])
        self.assertIn("Fixed sub()", answer, "exp-07 is the worker's shape: edit_file and write_file completed, and that is allowed")

    def test_a_read_only_prompt_that_starts_with_a_slash_is_not_run_as_a_command(self):
        note = providers.AGY_READ_ONLY_NOTE + "\n\n"
        _, seen = self.call(raw(3), mode="probe", prompt="/permissions please")
        content = json.loads(seen["prompt"])["message"]["content"]
        self.assertEqual(content, note + "/permissions please", "the note comes first, so the message no longer starts with a slash")
        self.assertFalse(content.lstrip().startswith("/"))
        _, seen = self.call(raw(3), mode="worker", prompt="/permissions please")
        self.assertEqual(json.loads(seen["prompt"])["message"]["content"], "/permissions please", "the worker has --disable-slash-commands instead")
        _, seen = self.call(raw(3), mode="probe", prompt="  Review /permissions")
        self.assertEqual(json.loads(seen["prompt"])["message"]["content"], note + "  Review /permissions")


class EmptyAnswer(Called):
    def test_a_read_only_role_that_gets_nothing_fails_with_a_message_that_says_why(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                with self.assertRaises(ProviderUnavailable) as caught:
                    self.call(blanked(2), mode=mode)
                self.assertEqual(caught.exception.kind, "no_answer")
                message = str(caught.exception)
                self.assertIn(f"finished the {mode} call (status SUCCESS) but returned no answer", message)
                self.assertIn("call.jsonl", message)
                self.assertIn("retries this as a transient failure", message)
                self.assertEqual("no structured output" in message, mode in SCHEMAS, mode)

    def test_the_worker_may_answer_nothing(self):
        (code, session, answer), _ = self.call(blanked(2), mode="worker")
        self.assertEqual((code, answer.strip()), (0, ""))

    def test_a_plan_written_instead_of_the_answer_is_named_and_never_read_as_one(self):
        plan = self.root / "user" / ".gemini" / "antigravity-cli" / "brain" / "conversation" / "implementation_plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text('# Implementation Plan\n{"verdict":"PASS","reason":"read me instead"}\n', encoding="utf-8")
        items = [({**e, "step_update": {**part(e), "tool_info": {**part(e)["tool_info"], "parameters": {"Path": str(plan)}}}}
                  if e["event"] == "step_update" and part(e).get("tool_name") == "write_file" else e) for e in blanked(5)]
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call(items, mode="reviewer")
        self.assertEqual(caught.exception.kind, "no_answer")
        self.assertIn(str(plan), str(caught.exception))
        self.assertIn("does not read that file as the answer", str(caught.exception))
        self.assertNotIn("read me instead", str(caught.exception))

    def test_refused_actions_are_named_in_the_message(self):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call(blanked(6), mode="judge")
        self.assertIn("refused 2 tool actions under the read-only rules (edit_file ", str(caught.exception))

    def test_a_failed_result_is_still_a_failed_call_not_an_empty_answer(self):
        items = [({"event": "result", "result": {**part(e), "status": "ERROR", "finished_reason": "error", "error": "boom"}} if e["event"] == "result" else e)
                 for e in blanked(2)]
        (code, _, answer), _ = self.call(items, mode="reviewer")
        self.assertEqual(code, 1)


class ReadOnlyHolds(Called):
    """The deny rules are the barrier; these are the layers behind it."""

    def test_a_read_only_call_that_completed_an_edit_is_refused_whatever_it_answered(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                with self.assertRaises(ProviderUnavailable) as caught:
                    self.call(raw(7), mode=mode)
                self.assertEqual(caught.exception.kind, "read_only_violation")
                self.assertIn("edit_file", str(caught.exception))
                self.assertIn("write_file", str(caught.exception))
                self.assertIn("its answer is discarded", str(caught.exception))
                self.assertNotIn("PASS", str(caught.exception).split("(")[0])

    def test_the_refusals_under_the_deny_rules_are_not_violations(self):
        """The rules held, so nothing was written: the call is not a read_only_violation. (Whether its answer can be trusted is
        RefusedThenAnswered's question (tests/test_release_0192_agy_layers.py).)"""
        for number in (6, 8, 10):
            with self.assertRaises(ProviderUnavailable) as caught:
                self.call(raw(number), mode="reviewer")
            self.assertEqual(caught.exception.kind, "read_only_refused", number)

    def test_a_violation_is_reported_even_when_the_call_failed_afterwards(self):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call(raw(7), mode="reviewer", code=3)
        self.assertEqual(caught.exception.kind, "read_only_violation")

    def test_a_read_only_role_is_not_started_when_agy_did_not_load_the_rules(self):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call(raw(2), mode="reviewer", agy=rules_output(BARE))
        self.assertEqual(caught.exception.kind, "configuration")
        self.assertIn("did not load the read-only permission rules", str(caught.exception))
        self.assertIn("write_file(*)", str(caught.exception))
        with patch("nightweaver.providers.execute") as model, patch("nightweaver.providers.agy_run", side_effect=rules_output(BARE)):
            with self.assertRaises(ProviderUnavailable):
                providers.invoke({"kind": "antigravity", "executable": sys.executable}, "judge", self.root, self.root / "call.jsonl", "Judge it.", 10,
                                 lambda: False)
        model.assert_not_called()

    def test_the_worker_is_never_asked_about_the_rules(self):
        asked = []

        def agy(executable, env, *args, timeout=30, cwd=None):
            asked.append(args)
            return 0, "gemini-4-neon-xhigh"

        self.call(raw(7), mode="worker", agy=agy)
        self.assertFalse([a for a in asked if "/permissions" in a])

    def test_the_rules_are_asked_for_with_the_file_the_call_uses_and_remembered_for_five_minutes(self):
        asked = []

        def agy(executable, env, *args, timeout=30, cwd=None):
            asked.append(args)
            return (0, LOADED) if "/permissions" in args else (0, "models")

        _, seen = self.call(raw(2), mode="reviewer", agy=agy)
        question = next(a for a in asked if "/permissions" in a)
        self.assertEqual(question[:4], ("-p", "/permissions", "--output-format", "stream-json"))
        self.assertEqual(question[question.index("--settings") + 1], seen["argv"][seen["argv"].index("--settings") + 1])
        self.call(raw(2), mode="judge", agy=agy)
        self.assertEqual(len([a for a in asked if "/permissions" in a]), 1, "one question per agy per five minutes")
        providers.AGY_READ_ONLY.clear()
        self.call(raw(2), mode="judge", agy=agy)
        self.assertEqual(len([a for a in asked if "/permissions" in a]), 2)

    def test_a_missing_answer_from_agy_about_its_rules_does_not_stop_the_call(self):
        for output in ((None, ""), (1, "boom"), (0, "gemini-4-neon"), (0, '{"event":"command_result","command":{"name":"permissions","data":{}}}\n')):
            with self.subTest(output=output):
                (code, _, _), _ = self.call(raw(2), mode="reviewer", agy=lambda executable, env, *args, timeout=30, cwd=None: output)
                self.assertEqual(code, 0)
        self.assertEqual(providers.AGY_READ_ONLY, {}, "nothing was learned, so nothing is remembered")

    def test_agys_own_settings_that_turn_slash_commands_off_skip_the_free_check(self):
        """/permissions would then run as a chat prompt, which is a model turn."""
        self.agy_settings(disableSlashCommands=True)
        asked = []
        self.call(raw(2), mode="reviewer", agy=lambda executable, env, *args, timeout=30, cwd=None: (asked.append(args), (0, "x"))[1])
        self.assertFalse([a for a in asked if "/permissions" in a])

    def test_the_captured_permissions_output_is_read_as_the_rules_it_lists(self):
        self.assertEqual(providers.agy_rules(BARE), set(), "no settings: three scopes and no rule")
        self.assertEqual(providers.agy_rules(LOADED), {"write_file(*)", "write_file(/)", "write_file(.)", "command(*)", "mcp(*)", "mcp(*/*)"},
                         "the capture was taken with a wider candidate set")
        self.assertIsNone(providers.agy_rules("warning: x\nnot json\n"))
        self.assertIsNone(providers.agy_rules(""))
        with patch("nightweaver.providers.agy_run", return_value=(0, LOADED)):
            self.assertEqual(providers.antigravity_rules("agy", {}), "enforced")
        with patch("nightweaver.providers.agy_run", return_value=(0, BARE)):
            self.assertEqual(providers.antigravity_rules("agy", {}), "missing")
        with patch("nightweaver.providers.agy_run", return_value=(0, LOADED.replace('"command(*)",', ""))):
            self.assertEqual(providers.antigravity_rules("agy", {}), "missing", "one rule short is missing")


class ReviewerEditsAreCaught(unittest.TestCase):
    """Nightweaver's own protection behind agy's: the run compares the worktree's tree before and after a review. Here an agy reviewer, the
    way exp-07 showed agy editing a file, changes result.txt during its review."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def agy_reviews(self, calls):
        """An execute that is agy for an agy call (a capture, after `calls[n]` has done its side effect in the worktree) and the real one else."""
        from nightweaver import process
        real = process.execute

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                return real(argv, cwd, log, timeout, **options)
            number = len(calls)
            calls.append(argv)
            if number == 0:
                (Path(cwd) / "result.txt").write_text("ready, and edited by the reviewer", encoding="utf-8")  # what agy did in exp-07
            put(log, retargeted(2, {"verdict": "PASS", "reason": "Looks right."}, SCHEMAS["reviewer"]))
            return 0

        return execute

    def test_an_edit_made_during_a_review_sends_the_run_back_to_its_checks_and_is_never_promoted(self):
        calls = []
        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=3)
        with patch("nightweaver.providers.execute", side_effect=self.agy_reviews(calls)), \
                patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "done")
        kinds = [event["kind"] for event in self.store.events(run)]
        self.assertIn("snapshot_changed", kinds, "the first PASS came from a tree that was no longer the one checked")
        self.assertEqual(kinds.count("review_passed"), 2)
        self.assertLess(kinds.index("review_passed"), kinds.index("snapshot_changed"))
        self.assertEqual(kinds.count("milestone_accepted"), 1)
        self.assertEqual(git(self.repo, "show", f"nightweaver/{run}:result.txt"), "ready", "the reviewer's edit is not in the accepted commit")
        self.assertEqual(len(calls), 2, "the review was asked again for the tree that was checked")
        for argv in calls:
            self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
            self.assertIn("--settings", argv)

    def test_a_silent_agy_reviewer_is_retried_as_transient_and_the_worker_is_not_charged(self):
        calls = []
        real_calls = []

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                from nightweaver import process
                return process.execute(argv, cwd, log, timeout, **options)
            real_calls.append(argv)
            put(log, blanked(2) if len(real_calls) == 1 else retargeted(2, {"verdict": "PASS", "reason": "Looks right."}, SCHEMAS["reviewer"]))
            return 0

        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1,
                              retry={"initial_seconds": 1, "max_seconds": 1})
        with patch("nightweaver.providers.execute", side_effect=execute), patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "done")
        events = self.store.events(run)
        retry = [e for e in events if e["kind"] == "provider_retry_scheduled"]
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0]["details"]["reason"], "no_answer")
        self.assertIn("returned no answer", retry[0]["details"]["issue"])
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 1)

    def test_a_reviewer_that_keeps_saying_nothing_blocks_the_run_with_the_message(self):
        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                from nightweaver import process
                return process.execute(argv, cwd, log, timeout, **options)
            put(log, blanked(2))
            return 0

        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1,
                              retry={"initial_seconds": 1, "max_seconds": 1, "transient_attempts": 1})
        with patch("nightweaver.providers.execute", side_effect=execute), patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        error = self.store.get(run)["state"]["last_error"]
        self.assertIn("finished the reviewer call (status SUCCESS) but returned no answer", error)
        self.assertEqual(self.store.get(run)["state"]["provider_issue"]["reason"], "no_answer")

    def test_a_reviewer_that_edited_a_file_is_refused_at_the_call_before_its_verdict_is_looked_at(self):
        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                from nightweaver import process
                return process.execute(argv, cwd, log, timeout, **options)
            put(log, raw(7))
            return 0

        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1)
        with patch("nightweaver.providers.execute", side_effect=execute), patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual(state["provider_issue"]["reason"], "read_only_violation")
        self.assertIn("its answer is discarded", state["last_error"])
        self.assertEqual(git(self.repo, "branch", "--list", "nightweaver/*"), "", "nothing was promoted")


class Doctor(Hermetic):
    """doctor --live probes the route in the configuration the roles use and asserts an answer."""

    def check(self, items, *, agy=None, config=None):
        calls = []

        def fake(argv, cwd, log, timeout, **options):
            calls.append(argv)
            put(log, items)
            return 0

        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=agy or rules_output(LOADED)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable, **(config or {})},
                                           self.root, True)
        return report, calls

    def test_a_good_probe_is_ready_and_ran_in_the_read_only_configuration(self):
        report, calls = self.check(raw(3))
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual((argv[argv.index("--mode") + 1], "--settings" in argv, "--json-schema" in argv), ("accept-edits", True, False))
        reviewer, _ = providers.adapter({"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}, "reviewer")
        self.assertEqual(sorted(set(reviewer) - set(argv) - {"--json-schema"} - {a for a in reviewer if a.endswith("agy-schema-reviewer.json")}), [])

    def test_a_probe_that_returns_nothing_is_not_ready_and_says_so(self):
        report, _ = self.check(blanked(3))
        self.assertEqual((report["status"], report["reason"]), ("blocked", "no_answer"))
        self.assertIn("returned no answer", report["action"])
        self.assertTrue(Path(report["log"]).name.endswith(".jsonl"), "the probe's log is named, so the operator can open it")

    def test_a_probe_that_answers_something_else_is_unverified(self):
        report, _ = self.check(retargeted(3, None, text="Sure, I can help with that.\n"))
        self.assertEqual((report["status"], report["reason"]), ("unverified", "unexpected_response"))

    def test_a_probe_whose_agy_did_not_load_the_rules_is_blocked_before_any_model_call(self):
        report, calls = self.check(raw(3), agy=rules_output(BARE))
        self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("blocked", "configuration", "missing"))
        self.assertEqual(calls, [])

    def test_the_plain_report_says_whether_agy_loads_the_rules_without_a_model_call(self):
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        with patch("nightweaver.providers.agy_run", side_effect=rules_output(BARE)), patch("nightweaver.providers.execute") as model:
            report = readiness.check_route(store, {"kind": "antigravity", "executable": sys.executable}, self.root, False)
        self.assertEqual(report["status"], "configured")
        self.assertEqual(report["read_only_rules"], "missing")
        self.assertIn("read-only permission rules", report["action"])
        model.assert_not_called()

    def test_any_provider_that_exits_clean_with_nothing_is_called_empty_not_unexpected(self):
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        with patch("nightweaver.readiness.check_provider", return_value="x"), patch("nightweaver.readiness.invoke", return_value=(0, None, "  ")):
            report = readiness.check_route(store, {"kind": "codex"}, self.root, True)
        self.assertEqual((report["status"], report["reason"]), ("unverified", "empty_answer"))
        with patch("nightweaver.readiness.check_provider", return_value="x"), patch("nightweaver.readiness.invoke", return_value=(0, None, "NOPE")):
            self.assertEqual(readiness.check_route(store, {"kind": "codex"}, self.root, True)["reason"], "unexpected_response")
        with patch("nightweaver.readiness.check_provider", return_value="x"), patch("nightweaver.readiness.invoke", return_value=(124, None, "")):
            self.assertEqual(readiness.check_route(store, {"kind": "codex"}, self.root, True)["reason"], "timeout")


class Fixtures(unittest.TestCase):
    def test_the_captures_hold_no_account_name_local_path_or_real_conversation_id(self):
        import re
        ids = set()
        for path in sorted(STREAMS.glob("*")):
            text = path.read_text(encoding="utf-8")
            for word in ("Administrator", "Nightweaver-trials", "agy-lab", "@gmail", "AIza", "ya29."):
                self.assertNotIn(word, text, path.name)
            ids |= set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text))
        self.assertTrue(ids)
        self.assertTrue(all(i.startswith("00000000-0000-4000-8000-") for i in ids), ids)

    def test_ten_captures_and_two_permission_listings(self):
        self.assertEqual(len(list(STREAMS.glob("exp-*.jsonl"))), 10)
        self.assertEqual(json.loads(BARE.splitlines()[0])["command"]["name"], "permissions")
        self.assertEqual(json.loads(LOADED.splitlines()[-1])["result"]["num_turns"], 0)


if __name__ == "__main__":
    unittest.main()
