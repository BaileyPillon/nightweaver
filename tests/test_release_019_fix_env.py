"""Nightweaver 0.19 environment hygiene: provider credentials stay out of gates, git, suite adapters and command routes, an explicit
profile keeps its own login, Claude sessions never resume under another config directory, a usage limit is only what a provider
reported, Gemini helper models are not served models, and a rotated token wins over a stale process value."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import checks, engine, process, providers, safety, spec, suite_runner

TOKEN = "sk-ant-oat01-" + "Ab1_-" * 12 + "Zz9"
KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY")
NAMES = KEYS + tuple(name + "_FILE" for name in KEYS)
SECRETS = {name: f"secret-{name.lower()}-value" for name in NAMES}
SECRETS.update(CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY="sk-ant-api03-" + "Zz9" * 20)
LISTING = f"import json, os; print(json.dumps(sorted(k for k in os.environ if k.upper() in {NAMES!r})))"
CONFIG_VARIABLES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME", "NIGHTWEAVER_CLAUDE_SHARED_CONFIG")


def write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8")


class Hermetic(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="Nightweaver env ")))
        clean = {k: v for k, v in os.environ.items() if k.upper() not in NAMES and k not in CONFIG_VARIABLES}
        self.enterContext(patch.dict(os.environ, {**clean, "NIGHTWEAVER_HOME": str(self.root / "home")}, clear=True))
        self.registry = self.enterContext(patch("nightweaver.providers.user_environment", return_value=None))

    def leaked(self, output):
        return json.loads(output.strip().splitlines()[-1])


class Scrubbing(Hermetic):
    def test_scrub_env_drops_every_credential_and_its_file_pointer(self):
        base = {**SECRETS, "PATH": "p", "NIGHTWEAVER_HOME": "h", "claude_code_oauth_token": "lowercase spelling"}
        clean = safety.scrub_env(base)
        self.assertEqual(clean, {"PATH": "p", "NIGHTWEAVER_HOME": "h"})
        self.assertEqual(len(base), len(SECRETS) + 3, "the base environment is not modified")

    def test_scrub_env_keeps_only_the_named_variables(self):
        clean = safety.scrub_env(SECRETS, keep=("openai_api_key", "OPENAI_API_KEY_FILE"))
        self.assertEqual(sorted(clean), ["OPENAI_API_KEY", "OPENAI_API_KEY_FILE"])
        self.assertIn(providers.OAUTH_VARIABLE, safety.SECRET_VARIABLES)

    def test_the_execute_default_scrubs_instead_of_inheriting(self):
        os.environ.update(SECRETS)
        log = self.root / "direct.log"
        self.assertEqual(process.execute([sys.executable, "-c", LISTING], self.root, log, 60), 0)
        self.assertEqual(self.leaked(log.read_text(encoding="utf-8")), [])


class GateHygiene(Hermetic):
    def gate(self, code):
        path = self.root / "work"
        path.mkdir(exist_ok=True)
        folder = checks.evidence_folder(self.root / "evidence", 1)
        receipt = checks.run_gate({"name": "env", "argv": [sys.executable, "-c", code], "timeout": 60, "artifacts": []}, path, folder, "tree")
        self.assertEqual((receipt["exit"], receipt.get("artifact_error")), (0, None))
        return (folder / "gate.log").read_text(encoding="utf-8")

    def test_a_gate_sees_no_provider_credential(self):
        os.environ.update(SECRETS)
        log = self.gate(LISTING)
        self.assertEqual(self.leaked(log), [])
        for value in SECRETS.values():
            self.assertNotIn(value, log)

    def test_a_gate_still_gets_the_evidence_variables_and_the_rest_of_the_environment(self):
        os.environ.update(SECRETS)
        log = self.gate("import os; print(sorted(k for k in ('NIGHTWEAVER_EVIDENCE', 'NIGHTWEAVER_TREE', 'PATH', 'NIGHTWEAVER_HOME') if k in os.environ))")
        self.assertEqual(log.strip(), "['NIGHTWEAVER_EVIDENCE', 'NIGHTWEAVER_HOME', 'NIGHTWEAVER_TREE', 'PATH']")

    def test_gate_logs_never_keep_a_key_shaped_token(self):
        log = self.gate("print('token', 'sk-ant-oat01-' + 'Zz9' * 20, 'and', 'ghp_' + 'a' * 30); print('password: required')")
        self.assertNotIn("sk-ant", log)
        self.assertNotIn("ghp_", log)
        self.assertEqual(log.splitlines(), ["token [REDACTED] and [REDACTED]", "password: required"],
                         "only key-shaped tokens are masked; the evidence stays readable")

    def test_key_shaped_tokens_are_masked_by_redact_and_by_the_log_masker(self):
        for token in (TOKEN, "sk-ant-api03-" + "Zz9" * 20, "sk-ant-oat01-abcdefghijklmnop"):
            self.assertEqual(safety.redact(f"x {token} y"), "x [REDACTED] y")
            log = self.root / "masked.log"
            log.write_bytes(f"x {token} y\n".encode())
            safety.mask_tokens(log)
            self.assertEqual(log.read_bytes(), b"x [REDACTED] y\n")
        safety.mask_tokens(self.root / "absent.log")

    def test_git_calls_carry_no_provider_credential(self):
        os.environ.update(SECRETS)
        seen = {}

        def fake(argv, **kwargs):
            seen.update(kwargs["env"])
            return SimpleNamespace(returncode=0, stdout=b"ok\r\n", stderr=b"")

        with patch("nightweaver.engine.subprocess.run", side_effect=fake):
            self.assertEqual(engine.git(self.root, "status"), "ok")
            engine.git_bytes(self.root, "cat-file", "blob", "x", attributes_from="HEAD")
        self.assertEqual([k for k in seen if k.upper() in NAMES], [])
        self.assertEqual((seen["GIT_OPTIONAL_LOCKS"], seen["GIT_ATTR_SOURCE"]), ("0", "HEAD"))
        self.assertIn("PATH", seen)


class SuiteAdapters(Hermetic):
    def run_adapter(self, *args):
        return subprocess.run([sys.executable, "-B", suite_runner.__file__, *args], cwd=self.root, capture_output=True, text=True,
                              env={**os.environ, **SECRETS})

    def test_the_npm_adapter_child_gets_no_provider_credential(self):
        done = self.run_adapter("npm", sys.executable, "-c", LISTING)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.leaked(done.stdout), [])

    def test_the_unittest_adapter_runs_the_suite_without_provider_credentials(self):
        (self.root / "test_clean.py").write_text(
            "import os, unittest\n"
            "class T(unittest.TestCase):\n"
            f" def test_env(self): self.assertEqual([k for k in os.environ if k.upper() in {NAMES!r}], [])\n", encoding="utf-8")
        done = self.run_adapter("unittest", ".")
        self.assertEqual(done.returncode, 0, done.stderr)


class RouteCredentials(Hermetic):
    def present(self, config):
        _, env = providers.adapter(config, "worker")
        return sorted(k for k in env if k in NAMES)

    def test_every_route_keeps_only_its_own_vendors_credentials(self):
        os.environ.update(SECRETS)
        both = lambda *names: sorted(names + tuple(name + "_FILE" for name in names))
        self.assertEqual(self.present({"kind": "claude"}), both("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        self.assertEqual(self.present({"kind": "codex"}), both("OPENAI_API_KEY", "CODEX_API_KEY"))
        self.assertEqual(self.present({"kind": "gemini"}), both("GEMINI_API_KEY", "GOOGLE_API_KEY"))
        _, env = providers.adapter({"kind": "deepseek"}, "worker")
        self.assertEqual(sorted(k for k in env if k in NAMES), sorted(["DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_FILE", "ANTHROPIC_AUTH_TOKEN"]))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], SECRETS["DEEPSEEK_API_KEY"], "the DeepSeek key, not the Anthropic one")

    def test_command_routes_get_no_credentials_unless_they_inherit(self):
        os.environ.update(SECRETS)
        self.assertEqual(self.present({"kind": "command", "argv": ["tool"]}), [])
        self.assertEqual(self.present({"kind": "command", "argv": ["tool"], "inherit_credentials": False}), [])
        self.assertEqual(self.present({"kind": "command", "argv": ["tool"], "inherit_credentials": True}), sorted(NAMES))
        _, env = providers.adapter({"kind": "command", "argv": ["tool"]}, "worker")
        self.assertIn("PATH", env)

    def test_inherit_credentials_is_validated_with_the_command_route(self):
        route = spec.provider({"kind": "command", "argv": ["tool"], "inherit_credentials": True}, spec.IMPLEMENTERS, "worker")
        self.assertEqual(route, {"kind": "command", "argv": ["tool"], "inherit_credentials": True})
        self.assertEqual(spec.provider({"kind": "command", "argv": ["tool"], "inherit_credentials": False}, spec.IMPLEMENTERS, "worker"),
                         {"kind": "command", "argv": ["tool"]})
        for bad in ("yes", 1, None):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "true or false"):
                spec.provider({"kind": "command", "argv": ["tool"], "inherit_credentials": bad}, spec.IMPLEMENTERS, "worker")
        for kind in ("claude", "codex", "gemini", "deepseek"):
            with self.subTest(kind), self.assertRaisesRegex(ValueError, "Only command providers"):
                spec.provider({"kind": kind, "inherit_credentials": True}, spec.IMPLEMENTERS, "worker")


class ExplicitLogins(Hermetic):
    def test_an_explicit_profile_dir_ignores_the_ambient_token_and_the_registry(self):
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        self.registry.return_value = TOKEN + "-rotated"
        profile = self.root / "account-b"
        profile.mkdir()
        config = {"kind": "claude", "profile_dir": str(profile)}
        _, env = providers.adapter(config, "worker")
        self.assertNotIn(providers.OAUTH_VARIABLE, env, "the inherited token is removed so the profile's own login is used")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(profile))
        self.assertEqual(providers.claude_auth(config), (None, "none"))
        (profile / ".credentials.json").write_text("{}", encoding="utf-8")
        self.assertEqual(providers.claude_auth(config), (None, "cli_login"))
        _, plain = providers.adapter({"kind": "claude"}, "worker")
        self.assertEqual(plain[providers.OAUTH_VARIABLE], TOKEN + "-rotated", "a route without a profile still uses the token")

    def test_a_token_file_replaces_the_ambient_token_even_beside_a_profile(self):
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        file = self.root / "route.token"
        file.write_text("from-the-route-file\n", encoding="utf-8")
        profile = self.root / "account-b"
        profile.mkdir()
        _, env = providers.adapter({"kind": "claude", "oauth_token_file": str(file), "profile_dir": str(profile)}, "worker")
        self.assertEqual((env[providers.OAUTH_VARIABLE], env["CLAUDE_CONFIG_DIR"]), ("from-the-route-file", str(profile)))


class Rotation(Hermetic):
    def test_the_registry_wins_when_a_long_lived_process_froze_an_old_token(self):
        os.environ[providers.OAUTH_VARIABLE] = "old-token-frozen-at-logon"
        self.registry.return_value = "rotated-token"
        self.assertEqual(providers.claude_auth({"kind": "claude"}), ("rotated-token", "oauth_token_user_env"))
        _, env = providers.adapter({"kind": "claude"}, "worker")
        self.assertEqual(env[providers.OAUTH_VARIABLE], "rotated-token")
        self.assertEqual(os.environ[providers.OAUTH_VARIABLE], "old-token-frozen-at-logon", "this process is not modified")

    def test_equal_values_and_a_missing_registry_keep_the_process_source(self):
        os.environ[providers.OAUTH_VARIABLE] = "same"
        self.registry.return_value = "same"
        self.assertEqual(providers.claude_auth(), ("same", "oauth_token_env"))
        self.registry.return_value = None
        self.assertEqual(providers.claude_auth(), ("same", "oauth_token_env"))
        os.environ[providers.OAUTH_VARIABLE] = "  "
        self.registry.return_value = "registry-only"
        self.assertEqual(providers.claude_auth(), ("registry-only", "oauth_token_user_env"))

    def test_a_route_token_file_still_beats_the_registry(self):
        file = self.root / "route.token"
        file.write_text("pinned\n", encoding="utf-8")
        self.registry.return_value = "rotated-token"
        os.environ[providers.OAUTH_VARIABLE] = "frozen"
        self.assertEqual(providers.claude_auth({"kind": "claude", "oauth_token_file": str(file)}), ("pinned", "oauth_token_file"))


class SessionDirectories(Hermetic):
    """Claude Code keeps a conversation under its config directory, so a resume must stay in the directory that made it."""

    def setUp(self):
        super().setUp()
        self.log = self.root / "evidence" / "worker.jsonl"

    def resume(self, config, session):
        argv, _ = providers.adapter({"kind": "claude", **config}, "worker", session)
        return argv[argv.index("--resume") + 1] if "--resume" in argv else None

    def call(self, session=None, *, events=None, config=None):
        seen = {}

        def fake(argv, cwd, log, timeout, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            write(log, *(events if events is not None else [{"type": "system", "session_id": "sess-1"}, {"type": "result", "result": "done"}]))
            return 0

        route = {"kind": "claude", "executable": sys.executable, **(config or {})}
        with patch("nightweaver.providers.execute", side_effect=fake):
            result = providers.invoke(route, "worker", self.root, self.log, "prompt", 10, lambda: False, session)
        return result, seen

    def test_a_legacy_session_is_not_resumed_in_the_isolated_directory(self):
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        self.assertIsNone(self.resume({}, "session-from-0.18"), "the token moves the call to profiles/claude; ~/.claude is where it lives")

    def test_a_legacy_session_still_resumes_where_it_always_ran(self):
        self.assertEqual(self.resume({}, "session-from-0.18"), "session-from-0.18")
        other = self.root / "chosen"
        self.assertEqual(self.resume({"profile_dir": str(other)}, "session-from-0.18"), "session-from-0.18")
        os.environ["CLAUDE_CONFIG_DIR"] = str(other)
        self.assertEqual(self.resume({}, "session-from-0.18"), "session-from-0.18")
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        self.assertEqual(self.resume({}, "session-from-0.18"), "session-from-0.18", "an ambient CLAUDE_CONFIG_DIR already wins over isolation")

    def test_a_session_resumes_only_under_the_directory_it_was_created_in(self):
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        (_, first, _), _ = self.call()
        self.assertTrue(first.startswith("sess-1@"))
        (_, again, _), seen = self.call(first)
        self.assertEqual(seen["argv"][seen["argv"].index("--resume") + 1], "sess-1", "the tag never reaches the CLI")
        self.assertEqual(again, first)
        (self.root / "elsewhere").mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "elsewhere")
        (_, moved, _), seen = self.call(first, events=[{"type": "result", "result": "done"}])
        self.assertNotIn("--resume", seen["argv"])
        self.assertIsNone(moved, "a session this call could not resume is not handed back")
        (_, fresh, _), _ = self.call(first)
        self.assertNotEqual(fresh, first)
        self.assertTrue(fresh.startswith("sess-1@"))

    def test_the_directory_is_recorded_before_the_call_so_a_killed_runner_still_resumes(self):
        os.environ[providers.OAUTH_VARIABLE] = TOKEN
        self.call()
        recovered = providers.metadata(self.log, "claude")[0]
        self.assertTrue(recovered.startswith("sess-1@"))
        self.assertEqual(self.resume({}, recovered), "sess-1")
        self.assertEqual(providers.metadata(self.log, "codex")[0], "sess-1", "other kinds are untouched")
        self.log.with_name(self.log.name + ".config").unlink()
        self.assertEqual(providers.metadata(self.log, "claude")[0], "sess-1", "without a record the id is plain")

    def test_other_kinds_keep_plain_session_ids(self):
        write(self.log, {"type": "thread.started", "thread_id": "thread-1"})
        self.assertEqual(providers.metadata(self.log, "codex")[0], "thread-1")
        write(self.log, {"type": "init", "session_id": "gem-1"})
        self.assertEqual(providers.metadata(self.log, "gemini")[0], "gem-1")
        argv, _ = providers.adapter({"kind": "codex"}, "worker", "thread-1")
        self.assertEqual(argv[argv.index("resume") + 1], "thread-1")


class UsageLimits(Hermetic):
    PROSE = "Now writing the rate limit middleware tests; running the full suite..."

    def setUp(self):
        super().setUp()
        self.enterContext(patch("nightweaver.providers.default_home", return_value=self.root / "home"))

    def finish(self, kind, code, *lines):
        def fake(argv, cwd, log, timeout, **kwargs):
            write(log, *lines)
            return code

        with patch("nightweaver.providers.execute", side_effect=fake):
            return providers.invoke({"kind": kind, "executable": sys.executable}, "worker", self.root, self.root / "call.jsonl",
                                    "prompt", 10, lambda: False)

    def limited(self, kind, code, *lines):
        with self.assertRaises(providers.ProviderUnavailable) as caught:
            self.finish(kind, code, *lines)
        return caught.exception

    def message(self, text):
        return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}

    def test_the_controllers_own_kills_are_never_a_provider_limit(self):
        for code in (124, 125, 126):
            with self.subTest(code):
                result = self.finish("codex", code, {"type": "turn.started"}, self.message(self.PROSE))
                self.assertEqual((result[0], result[2]), (code, self.PROSE))
        for code in (124, 125, 126):
            with self.subTest(claude=code):
                tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "HTTP 429 too many requests, usage limit"}]}}
                self.assertEqual(self.finish("claude", code, {"type": "system", "session_id": "s"}, tool, "You've hit your limit")[0], code)

    def test_a_controller_kill_is_not_classified_even_when_the_cli_printed_a_real_limit(self):
        self.assertEqual(self.finish("codex", 125, {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})[0], 125)

    def test_prose_and_tool_output_that_mention_limits_do_not_classify_a_failure(self):
        tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "def rate_limit(): raise TooManyRequests  # HTTP 429"}]}}
        assistant = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Added rate limit handling for 429 and usage limit."}]}}
        self.assertEqual(self.finish("codex", 1, self.message(self.PROSE), {"type": "turn.failed", "error": {"message": "stream closed"}})[0], 1)
        self.assertEqual(self.finish("claude", 1, {"type": "system", "session_id": "s"}, assistant, tool,
                                     {"type": "result", "is_error": True, "result": "Execution error"})[0], 1)

    def test_a_limit_in_the_providers_error_text_still_parks(self):
        error = self.limited("codex", 1, self.message(self.PROSE), {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        self.assertEqual(error.kind, "usage_limit")
        self.assertIn("Provider said: You've hit your usage limit.", str(error))
        self.assertNotIn("middleware", str(error))

    def test_the_cli_stderr_lines_in_the_log_are_the_final_error_text(self):
        error = self.limited("codex", 1, {"type": "turn.started"}, "ERROR: You've hit your usage limit. Try again in 2 hours.")
        self.assertEqual(error.kind, "usage_limit")
        self.assertTrue(timedelta(minutes=118) < error.reset_at - datetime.now(timezone.utc) <= timedelta(minutes=120))

    def test_a_flagged_assistant_error_and_a_rejected_rate_limit_event_classify(self):
        flagged = {"type": "assistant", "error": "rate_limit",
                   "message": {"content": [{"type": "text", "text": "You've hit your limit · resets 7pm (UTC)"}]}}
        error = self.limited("claude", 1, {"type": "system", "session_id": "s"}, flagged)
        self.assertEqual((error.kind, error.reset_at.hour, error.reset_at.tzinfo), ("usage_limit", 19, timezone.utc))
        target = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
        error = self.limited("claude", 1, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": target}})
        self.assertEqual((error.kind, int(error.reset_at.timestamp())), ("usage_limit", target))
        ignored = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": target}}
        self.assertEqual(self.finish("claude", 1, ignored)[0], 1, "a warning is not a failure")

    def test_the_reset_time_is_never_read_from_the_assistants_prose(self):
        said = {"type": "assistant", "message": {"content": [{"type": "text", "text": "I will retry in 20 minutes, after the 3pm build."}]}}
        error = self.limited("claude", 1, {"type": "system", "session_id": "s"}, said,
                             {"type": "result", "is_error": True, "result": "You've hit your usage limit"})
        self.assertEqual((error.kind, error.reset_at), ("usage_limit", None))

    def test_other_error_classes_ignore_prose_too(self):
        said = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed the token expired and please log in handling."}]}}
        self.assertEqual(self.finish("claude", 1, said)[0], 1)
        self.assertEqual(self.finish("codex", 1, self.message("please log in again, the service unavailable page works"))[0], 1)


class GeminiServedModels(Hermetic):
    def served(self, *events):
        write(self.root / "call.jsonl", *events)
        return providers.served_models({"kind": "gemini"}, self.root / "call.jsonl")

    @staticmethod
    def stats(**tokens):
        return {"type": "result", "stats": {"models": {name.replace("_", "-").replace("25", "2.5"): {"total_tokens": count}
                                                       for name, count in tokens.items()}}}

    def test_helper_models_beside_the_main_model_are_not_served_models(self):
        init = {"type": "init", "model": "gemini-2.5-pro"}
        result = self.served(init, self.stats(gemini_25_pro=9000, gemini_25_flash_lite=700))
        self.assertEqual(result["models"], ["gemini-2.5-pro"])
        pending = {"role": "worker", "provider": "gemini", "model": "gemini-2.5-pro", "call": 1}
        self.assertEqual(engine.model_drift(pending, result, [])[0], [], "no false model_mismatch")
        no_weights = {"type": "result", "stats": {"models": {"gemini-2.5-pro": {}, "gemini-2.5-flash-lite": {}}}}
        self.assertEqual(self.served(init, no_weights)["models"], ["gemini-2.5-pro"], "unknown weights fall back to the init model")
        split = self.stats(gemini_25_pro=500, gemini_25_flash=400, gemini_25_flash_lite=300)
        self.assertEqual(self.served(init, split)["models"], ["gemini-2.5-pro"], "no model carries most of the tokens")

    def test_a_model_that_clearly_carried_the_call_is_reported_so_a_fallback_still_shows(self):
        result = self.served({"type": "init", "model": "gemini-2.5-pro"}, self.stats(gemini_25_pro=100, gemini_25_flash=9000))
        self.assertEqual(result["models"], ["gemini-2.5-flash"])
        pending = {"role": "worker", "provider": "gemini", "model": "gemini-2.5-pro", "call": 1}
        self.assertEqual(engine.model_drift(pending, result, [])[0][0][1]["served"], "gemini-2.5-flash")

    def test_aliases_and_routing_modes_name_no_model(self):
        self.assertEqual(self.served({"type": "init", "model": "auto"})["models"], [])
        self.assertEqual(self.served({"type": "init", "model": "auto-gemini-2.5"}, {"type": "result", "stats": {"models": {"pro": {}}}})["models"], [])

    def test_gemini_aliases_match_the_ids_they_stand_for(self):
        same = [("pro", "gemini-2.5-pro"), ("flash", "gemini-2.5-flash"), ("flash-lite", "gemini-2.5-flash-lite"), ("pro", "gemini-3-pro-preview"),
                ("flash", "models/gemini-2.5-flash-preview-09-2025"), ("flash", "gemini-2.0-flash-001"), ("auto", "gemini-2.5-pro"),
                ("auto-gemini-2.5", "gemini-2.5-flash"), ("gemini-2.5-pro", "gemini-2.5-pro")]
        different = [("flash", "gemini-2.5-flash-lite"), ("pro", "gemini-2.5-flash"), ("flash-lite", "gemini-2.5-flash"), ("pro", "claude-pro-5"),
                     ("gemini-2.5-pro", "gemini-2.5-flash")]
        for asked, got in same:
            self.assertTrue(providers.same_model(asked, got), (asked, got))
        for asked, got in different:
            self.assertFalse(providers.same_model(asked, got), (asked, got))
        self.assertTrue(providers.same_model("sonnet", "claude-sonnet-5-5"), "Claude families are unchanged")
        self.assertFalse(providers.same_model("pro", "claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
