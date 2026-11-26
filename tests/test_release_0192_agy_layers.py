"""Nightweaver 0.19.2, review fixes: the layers behind agy's deny rules.

A skeptic probed what stands between agy's permission rules and an edit, with an offline harness (the real invoke() over a stand-in agy that
writes agy-shaped streams) and four live read-only calls, and reported four gaps. Every test here builds its stream from a capture in
tests/agy_streams (exp-02 for the shape of a read-only review, exp-10 for the subagent step and the refusals the skeptic's live call wrote),
swapping in the tool steps under test with `tooled`; no model is called and no network is used."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import providers, readiness
from nightweaver.engine import Run
from nightweaver.providers import SCHEMAS, ProviderUnavailable
from nightweaver.reports import metrics
from nightweaver.verdicts import extract_verdict
import test_windows as fixtures
from test_release_0191_gemini import Hermetic
from test_release_0192_agy import BARE, LOADED, READ_ONLY_MODES, Called, Replayed, events, part, put, raw, retargeted, rules_output, tooled
from test_windows import fixture

REPO = "C:\\work\\repo"  # the repository the captures name
DENIED = 'permission check failed for write_file "x": Permission denied for write_file(x). Matches user-configured deny rule.'
INIT_TOOLS = events(2)[0]["init"]["tools"]
# Every tool in the list agy 1.5.17 announces, sorted by what a read-only call may do with it. A new agy tool breaks the totals below on
# purpose: someone has to decide which side it is on.
WRITERS = {"call_mcp_tool", "delete_knowledge", "generate_image", "multi_replace_file_content", "notebook_edit", "notebook_execution",
           "replace_file_content", "run_command", "run_workflow", "sed_file", "send_command_input", "write_to_file"}
DELEGATES = {"browser_subagent", "invoke_subagent"}
OTHERS = {"ask_custom_permission", "ask_permission", "ask_question", "browser_click_element", "browser_drag_pixel_to_pixel", "browser_get_dom",
          "browser_get_network_request", "browser_input", "browser_list_network_requests", "browser_mouse_down", "browser_mouse_up",
          "browser_move_mouse", "browser_press_key", "browser_refresh_page", "browser_resize_window", "browser_scroll", "browser_scroll_dom",
          "browser_select_option", "capture_browser_console_logs", "capture_browser_screenshot", "click_browser_pixel", "command_status",
          "define_subagent", "execute_browser_javascript", "find_by_name", "finish", "grep_search", "list_browser_pages", "list_dir",
          "list_permissions", "list_plugin_accounts", "list_resources", "manage_inbox", "manage_subagents", "manage_task", "open_browser_url",
          "read_browser_page", "read_resource", "read_url_content", "schedule", "search_marketplace", "search_web", "send_message",
          "view_file", "wait", "wait_5_seconds"}


class Layered(Called, Replayed):
    @property
    def brain(self):
        return self.root / "user" / ".gemini" / "antigravity-cli" / "brain"

    def refused_call(self, items, mode="reviewer", **kwargs):
        with self.assertRaises(ProviderUnavailable) as caught:
            self.call(items, mode=mode, **kwargs)
        return caught.exception


class AgysOwnFolder(Layered):
    """Gap 1: the exemption for agy's conversation folder must not be a text match anyone can satisfy."""

    def cases(self):
        brain = str(self.brain)
        return [
            (brain + "\\00000000-0000-4000-8000-000000000005\\implementation_plan.md", True),  # the shape exp-05 wrote
            (brain.upper() + "\\conversation\\notes.md", True),  # Windows paths do not care about case
            (brain.replace("\\", "/") + "/conversation/notes.md", True),
            (brain + "\\conversation\\..\\other\\notes.md", True),  # resolved, and still inside
            (brain + "\\..\\..\\..\\..\\work\\repo\\calc.py", False),  # the skeptic's bypass: climbs out of the folder
            (brain + "\\conversation\\..\\..\\..\\settings.json", False),
            (REPO + "\\.gemini\\antigravity-cli\\brain\\x.md", False),  # a repository with a folder of that name (the old text match)
            ("C:\\Users\\other\\.gemini\\antigravity-cli\\brain\\x.md", False),  # another home
            (brain, False),
            (brain + "x\\y.md", False),
            ("brain\\x.md", False),
            ("", False)]

    def test_a_file_tool_is_exempt_only_when_its_path_really_is_inside_the_folder(self):
        for path, own in self.cases():
            with self.subTest(path=path):
                found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": path})))
                self.assertEqual(found["wrote"] == [], own)

    def test_the_skeptics_bypasses_are_refused_by_invoke(self):
        brain = str(self.brain)
        for name, params in (("write_file", {"Path": brain + "\\..\\..\\..\\..\\work\\repo\\calc.py"}),
                             ("run_command", {"CommandLine": "Set-Content calc.py 'x' # " + brain}),
                             ("run_command", {"CommandLine": brain + "\\conversation\\run.cmd"})):
            for mode in ("reviewer", "judge"):
                with self.subTest(name=name, mode=mode):
                    error = self.refused_call(tooled(2, (name, params)), mode)
                    self.assertEqual(error.kind, "read_only_violation")
                    self.assertIn(name, str(error))

    def test_a_command_is_never_a_path(self):
        brain = str(self.brain)
        for line in (brain + "\\conversation\\tool.exe", "echo x # " + brain, "type nul > " + brain + "\\x"):
            with self.subTest(line=line):
                self.assertEqual(len(self.stream(2, "reviewer", tooled(2, ("run_command", {"CommandLine": line})))["wrote"]), 1)

    def test_every_path_a_step_names_must_be_agys_own_and_a_step_naming_none_proves_nothing(self):
        brain = str(self.brain)
        for params in ({"Path": brain + "\\x.md", "TargetFile": REPO + "\\calc.py"}, {"Where": brain + "\\x.md"}, {}):
            with self.subTest(params=params):
                self.assertEqual(len(self.stream(2, "reviewer", tooled(2, ("write_file", params)))["wrote"]), 1)
        self.assertEqual(self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": brain + "\\x.md", "TargetFile": brain + "\\y.md"})))["wrote"], [])

    def test_the_real_plan_write_of_exp_05_is_still_agys_own(self):
        found = self.stream(5, "reviewer")
        self.assertEqual((found["wrote"], found["refused"]), ([], []))
        self.assertTrue(found["plan_file"].endswith("implementation_plan.md"))
        (code, _, _), _ = self.call(raw(5), mode="reviewer")
        self.assertEqual(code, 0)


class EveryWritingTool(Layered):
    """Gap 2, first half: more tools write than the three of the live runs."""

    def test_the_real_tool_list_is_sorted_into_writers_delegates_and_the_rest(self):
        self.assertEqual(len(INIT_TOOLS), 60)
        self.assertEqual(set(INIT_TOOLS), WRITERS | DELEGATES | OTHERS)
        self.assertFalse(WRITERS & DELEGATES | WRITERS & OTHERS | DELEGATES & OTHERS)
        self.assertEqual({name for name in INIT_TOOLS if providers.agy_writes(name)}, WRITERS)
        for name in ("write_file", "edit_file", "delete_file", "apply_patch", "create_directory", "append_to_file", "save_artifact", "exec_script"):
            self.assertTrue(providers.agy_writes(name), name)
        for name in ("read_file", "view_file", "list_dir", "grep_search", "execute_browser_javascript"):
            self.assertFalse(providers.agy_writes(name), name)

    def test_every_writing_tool_that_completes_is_refused_in_a_read_only_call(self):
        for name in sorted(WRITERS | {"write_file", "edit_file", "apply_patch"}):
            for mode in ("reviewer", "judge"):
                with self.subTest(name=name, mode=mode):
                    error = self.refused_call(tooled(2, (name, {"TargetFile": REPO + "\\calc.py"})), mode)
                    self.assertEqual(error.kind, "read_only_violation")
                    self.assertIn(name + " ", str(error))
                    self.assertIn("its answer is discarded", str(error))

    def test_a_writing_tool_with_parameters_nobody_knows_is_still_refused(self):
        for name in sorted(WRITERS | {"edit_file"}):
            with self.subTest(name=name):
                self.assertEqual(self.refused_call(tooled(2, (name, {"Unheard": "of"}))).kind, "read_only_violation")
                self.assertEqual(self.refused_call(tooled(2, (name, {}))).kind, "read_only_violation")

    def test_a_tool_step_is_read_by_its_tool_name_whatever_its_step_type(self):
        self.assertEqual(self.refused_call(tooled(2, ("sed_file", {"Path": REPO + "\\calc.py"}), step_type="command")).kind, "read_only_violation")

    def test_the_other_tools_and_read_file_complete_without_a_complaint(self):
        for name in sorted(OTHERS | {"read_file"}):
            with self.subTest(name=name):
                (code, _, answer), _ = self.call(tooled(2, (name, {"Path": REPO + "\\calc.py"})), mode="reviewer")
                self.assertEqual((code, extract_verdict(answer, ("PASS", "REVISE"))["verdict"]), (0, "REVISE"))

    def test_a_step_that_did_not_finish_wrote_nothing_yet(self):
        items = [e for e in tooled(2, ("sed_file", {"TargetFile": REPO + "\\calc.py"})) if not (e["event"] == "step_update" and part(e)["state"] == "DONE"
                                                                                                  and part(e).get("tool_name") == "sed_file")]
        self.assertEqual(self.stream(2, "reviewer", items)["wrote"], [])


class Subagents(Layered):
    """Gap 2, second half: agy writes a subagent as one step and none of its own, so its work is read from the files."""

    def quiet(self):
        """exp-10 without its refused writes: a reviewer that asked a subagent and answered."""
        return [e for e in events(10) if not (e["event"] == "step_update" and part(e)["step_type"] == "tool")]

    def test_the_real_capture_has_one_subagent_step_and_none_of_the_subagents_own(self):
        steps = [part(e) for e in events(10) if e["event"] == "step_update"]
        found = self.stream(10, "reviewer")
        subagent = [s for s in steps if s["step_type"] == "subagent"]
        self.assertEqual([(s["state"], s["tool_name"]) for s in subagent], [("ACTIVE", "invoke_subagent"), ("DONE", "invoke_subagent")])
        self.assertEqual(found["delegated"], ["invoke_subagent"])
        own = subagent[-1]["subagent_info"]["subagents"][0]["conversation_id"]
        self.assertNotEqual(own, found["session"])
        self.assertEqual({s["conversation_id"] for s in steps}, {found["session"]}, "no step of the stream belongs to the subagent")
        self.assertTrue(own.startswith("00000000-0000-4000-8000-"))

    def test_a_subagent_step_is_recognised_by_its_type_even_without_a_tool_name(self):
        items = [({"event": "step_update", "step_update": {k: v for k, v in part(e).items() if k != "tool_name"}}
                  if e["event"] == "step_update" and part(e)["step_type"] == "subagent" else e) for e in self.quiet()]
        self.assertEqual(self.stream(10, "reviewer", items)["delegated"], ["subagent"])

    def test_a_subagent_that_changed_a_file_is_caught_from_the_files(self):
        (self.root / "calc.py").write_text("def sub(a, b):\n    return a + b\n", encoding="utf-8")
        error = self.refused_call(self.quiet(), during=lambda: (self.root / "calc.py").write_text("def sub(a, b):\n    return a - b  # fixed\n",
                                                                                                encoding="utf-8"))
        self.assertEqual(error.kind, "read_only_violation")
        self.assertIn("invoke_subagent (its steps are not in the stream; the files changed: calc.py)", str(error))
        self.assertIn("its answer is discarded", str(error))

    def test_a_subagent_that_created_or_deleted_a_file_is_caught_too(self):
        (self.root / "old.txt").write_text("old", encoding="utf-8")

        def create():
            (self.root / "sub").mkdir()
            (self.root / "sub" / "sub_agent.txt").write_text("y", encoding="utf-8")

        self.assertIn(os.path.join("sub", "sub_agent.txt"), str(self.refused_call(self.quiet(), during=create)))
        self.assertIn("old.txt", str(self.refused_call(self.quiet(), during=lambda: (self.root / "old.txt").unlink())))

    def test_a_subagent_that_changed_nothing_is_no_violation_and_the_calls_own_log_is_not_a_change(self):
        (code, _, answer), _ = self.call(self.quiet(), mode="reviewer")
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "call.jsonl").is_file(), "the log is in the folder the call was made in")
        self.assertEqual(extract_verdict(answer, ("PASS", "REVISE"))["verdict"], "REVISE")

    def test_git_and_node_modules_are_not_looked_at(self):
        def touch():
            for folder in (".git", "node_modules"):
                (self.root / folder).mkdir(exist_ok=True)
                (self.root / folder / "x").write_text("changed", encoding="utf-8")

        (code, _, _), _ = self.call(self.quiet(), mode="reviewer", during=touch)
        self.assertEqual(code, 0)

    def test_the_other_read_only_roles_are_checked_the_same_way(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                error = self.refused_call(self.quiet(), mode, during=lambda: (self.root / f"{mode}.txt").write_text("x", encoding="utf-8"))
                self.assertEqual(error.kind, "read_only_violation")

    def test_a_tree_that_cannot_be_listed_proves_nothing_either_way(self):
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        with patch("nightweaver.providers.AGY_TREE_LIMIT", 0):
            self.assertIsNone(providers.agy_tree(self.root))
            (code, _, _), _ = self.call(self.quiet(), mode="reviewer", during=lambda: (self.root / "a.txt").write_text("changed", encoding="utf-8"))
        self.assertEqual(code, 0, "the run's own snapshot check is what is left")
        self.assertEqual(providers.agy_changes(None, {"a": (1, 1)}), [])

    def test_the_files_are_listed_only_for_a_read_only_antigravity_call_and_read_again_only_after_a_subagent(self):
        with patch("nightweaver.providers.agy_tree", wraps=providers.agy_tree) as listed:
            self.call(raw(2), mode="reviewer")
            self.assertEqual(listed.call_count, 1, "before the call; a call that ran no subagent is not read again")
            listed.reset_mock()
            self.call(self.quiet(), mode="judge")
            self.assertEqual(listed.call_count, 2)
            listed.reset_mock()
            self.call(raw(7), mode="worker")
            listed.assert_not_called()

    def test_agy_tree_lists_files_with_size_and_time_and_skips_what_it_is_told_to(self):
        (self.root / "keep").mkdir()
        (self.root / "keep" / "a.txt").write_text("aa", encoding="utf-8")
        (self.root / "skipped.log").write_text("x", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        tree = providers.agy_tree(self.root, [self.root / "skipped.log"])
        self.assertEqual([k for k in tree if not k.startswith(("home", "user", "local"))], [os.path.join("keep", "a.txt")])
        self.assertEqual(tree[os.path.join("keep", "a.txt")][0], 2)
        self.assertEqual(providers.agy_tree(self.root / "nowhere"), {})
        self.assertEqual(providers.agy_changes({"a": (1, 1), "b": (2, 2)}, {"a": (1, 1), "b": (3, 2), "c": (1, 1)}), ["b", "c"])


class RefusedThenAnswered(Layered):
    """Gap 4: a role that went for an edit or a command, was refused and answered anyway is not a review."""

    def test_the_real_capture_is_a_refusal_then_a_revise_the_engine_would_have_sent_to_the_worker(self):
        found = self.stream(10, "reviewer")
        self.assertEqual((len(found["denied"]), found["refused"], found["wrote"], found["failed"]), (2, found["denied"], [], False))
        verdict = extract_verdict(found["answer"], ("PASS", "REVISE"))
        self.assertEqual(verdict["verdict"], "REVISE")
        self.assertIn("Matches user-configured deny rule", verdict["reason"])

    def test_every_read_only_role_refuses_to_take_that_answer(self):
        for mode in READ_ONLY_MODES:
            with self.subTest(mode=mode):
                error = self.refused_call(raw(10), mode)
                self.assertEqual(error.kind, "read_only_refused")
                message = str(error)
                self.assertIn(f"The Antigravity {mode} call is read-only", message)
                self.assertIn("refused under the permission rules (write_file C:\\work\\repo\\sub\\deep\\nested.txt; "
                              "write_file C:\\work\\evidence\\evidence_note.txt)", message)
                self.assertIn(f"so it is not taken as a {mode} result", message)
                self.assertIn("retries this as a transient failure", message)
                self.assertIn("call.jsonl", message)

    def test_the_captures_of_a_refused_edit_and_a_refused_command_are_the_same(self):
        for number in (6, 8):
            self.assertEqual(self.refused_call(raw(number)).kind, "read_only_refused", number)

    def test_a_refusal_inside_agys_own_folder_is_not_a_reason(self):
        plan = str(self.brain / "conversation" / "implementation_plan.md")
        found = self.stream(2, "reviewer", tooled(2, ("write_file", {"Path": plan}, DENIED)))
        self.assertEqual((len(found["denied"]), found["refused"]), (1, []))
        (code, _, answer), _ = self.call(tooled(2, ("write_file", {"Path": plan}, DENIED)), mode="reviewer")
        self.assertEqual((code, extract_verdict(answer, ("PASS", "REVISE"))["verdict"]), (0, "REVISE"))

    def test_the_worker_is_never_asked_about_refusals_and_a_failed_call_is_classified_by_its_failure(self):
        (code, _, _), _ = self.call(raw(6), mode="worker")
        self.assertEqual(code, 0)
        (code, _, _), _ = self.call(raw(6), mode="reviewer", code=3)
        self.assertEqual(code, 3)

    def test_nothing_to_read_after_a_refusal_is_still_the_no_answer_that_names_the_refusals(self):
        blank = [e for e in tooled(2, ("write_file", {"Path": REPO + "\\x.txt"}, DENIED)) if not (e["event"] == "step_update" and
                                                                                                   part(e)["step_type"] == "agent_response")]
        blank = [({"event": "result", "result": {k: v for k, v in part(e).items() if k not in ("response", "structured_output")}}
                  if e["event"] == "result" else e) for e in blank]
        error = self.refused_call(blank)
        self.assertEqual(error.kind, "no_answer")
        self.assertIn("refused 1 tool action", str(error))


class RefusedReviewerRuns(unittest.TestCase):
    """The engine: a reviewer whose REVISE only describes its refusals is retried, and the worker is not charged for it."""
    setUp = fixtures.WindowsRunTest.setUp
    configured = fixtures.WindowsRunTest.configured

    def agy(self, answers, calls):
        from nightweaver import process
        real = process.execute

        def execute(argv, cwd, log, timeout, **options):
            if "--input-format" not in argv:
                return real(argv, cwd, log, timeout, **options)
            calls.append(argv)
            put(log, answers[min(len(calls), len(answers)) - 1])
            return 0

        return execute

    def test_a_refused_review_is_retried_as_transient_and_never_reaches_the_worker_as_a_revise(self):
        calls = []
        passing = retargeted(2, {"verdict": "PASS", "reason": "Looks right."}, SCHEMAS["reviewer"])
        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1,
                              retry={"initial_seconds": 1, "max_seconds": 1})
        with patch("nightweaver.providers.execute", side_effect=self.agy([raw(10), passing], calls)), \
                patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "done")
        events_ = self.store.events(run)
        retry = [e for e in events_ if e["kind"] == "provider_retry_scheduled"]
        self.assertEqual([e["details"]["reason"] for e in retry], ["read_only_refused"])
        self.assertIn("refused under the permission rules", retry[0]["details"]["issue"])
        self.assertEqual(metrics(self.store, run)["worker_attempts"], 1, "the worker was not asked to change anything")
        self.assertNotIn("Independent review requested changes", json.dumps(events_))
        self.assertEqual(len(calls), 2)

    def test_a_reviewer_that_keeps_going_for_edits_blocks_the_run_with_the_message(self):
        calls = []
        run = self.configured(fixture("good"), reviewer={"kind": "antigravity", "executable": sys.executable}, attempts=1,
                              retry={"initial_seconds": 1, "max_seconds": 1, "transient_attempts": 1})
        with patch("nightweaver.providers.execute", side_effect=self.agy([raw(10)], calls)), patch("nightweaver.providers.agy_run", side_effect=rules_output(LOADED)):
            self.assertEqual(Run(self.store, run).run(), "blocked")
        state = self.store.get(run)["state"]
        self.assertEqual(state["provider_issue"]["reason"], "read_only_refused")
        self.assertIn("refused under the permission rules", state["last_error"])
        self.assertNotIn("Independent review requested changes", state["last_error"])


class Doctor(Hermetic):
    """Gap 3: doctor --live says so when the read-only rules could not be confirmed, instead of ready."""

    def check(self, items, *, agy=None, live=True):
        calls = []

        def fake(argv, cwd, log, timeout, **options):
            calls.append(argv)
            put(log, items)
            return 0

        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir(exist_ok=True)
        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", side_effect=agy or rules_output(LOADED)):
            report = readiness.check_route(store, {"kind": "antigravity", "model": "gemini-4-neon", "executable": sys.executable}, self.root, live)
        return report, calls

    def test_a_ready_probe_is_not_ready_when_agys_permissions_answer_is_not_recognised(self):
        for output in ((0, "gemini-4-neon"), (None, ""), (1, "boom"), (0, '{"event":"command_result","command":{"name":"permissions","data":{}}}\n'),
                       (0, '{"event":"command_result","command":{"name":"permissions","data":{"rules":["x"]}}}\n')):
            with self.subTest(output=output):
                providers.AGY_READ_ONLY.clear()
                report, calls = self.check(raw(3), agy=lambda executable, env, *args, timeout=30: output)
                self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("unverified", "read_only_unverified", None))
                self.assertIn("agy -p /permissions printed nothing Nightweaver recognises", report["action"])
                self.assertIn("only the stream check and the run's snapshot check", report["action"])
                self.assertTrue(Path(report["log"]).name.endswith(".jsonl"))
                self.assertEqual(len(calls), 1, "the probe itself did run, read-only")
                self.assertIn("--settings", calls[0])

    def test_agys_own_settings_that_turn_slash_commands_off_are_named_as_the_reason(self):
        self.agy_settings(disableSlashCommands=True)
        asked = []
        report, calls = self.check(raw(3), agy=lambda executable, env, *args, timeout=30: (asked.append(args), (0, "x"))[1])
        self.assertEqual((report["status"], report["reason"], report["read_only_rules"]), ("unverified", "read_only_unverified", None))
        self.assertIn("turns slash commands off", report["action"])
        self.assertFalse([a for a in asked if "/permissions" in a], "still no model turn spent on asking")

    def test_the_controls_enforced_is_ready_missing_is_blocked_and_a_wrong_answer_stays_unexpected(self):
        report, _ = self.check(raw(3))
        self.assertEqual((report["status"], report["read_only_rules"]), ("ready", "enforced"))
        report, calls = self.check(raw(3), agy=rules_output(BARE))
        self.assertEqual((report["status"], report["reason"], calls), ("blocked", "configuration", []))
        report, _ = self.check(retargeted(3, None, text="Sure, I can help with that.\n"), agy=lambda executable, env, *args, timeout=30: (0, "x"))
        self.assertEqual((report["status"], report["reason"]), ("unverified", "unexpected_response"))

    def test_the_plain_report_warns_before_any_model_call_when_the_rules_cannot_be_confirmed(self):
        report, calls = self.check(raw(3), agy=lambda executable, env, *args, timeout=30: (0, "x"), live=False)
        self.assertEqual((report["status"], report["read_only_rules"], calls), ("configured", None, []))
        self.assertIn("could not confirm that it loads Nightweaver's read-only permission rules", report["action"])
        report, calls = self.check(raw(3), live=False)
        self.assertEqual((report["read_only_rules"], calls), ("enforced", []))
        self.assertNotIn("could not confirm", report["action"])

    def test_other_providers_are_not_asked_about_agys_rules(self):
        store = SimpleNamespace(root=self.root / "store")
        store.root.mkdir()
        with patch("nightweaver.readiness.check_provider", return_value="x"), patch("nightweaver.readiness.invoke", return_value=(0, None, "READY")):
            self.assertEqual(readiness.check_route(store, {"kind": "codex"}, self.root, True)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
