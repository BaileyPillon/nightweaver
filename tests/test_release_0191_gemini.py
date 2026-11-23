"""Nightweaver 0.19.1 Gemini group: the Gemini CLI route made to work on this machine (the npm package's real entrypoint, folder trust for
headless worktrees, Gemini's own error texts classified so a login or quota problem parks or blocks instead of burning attempts) and the
new antigravity kind for Google's agy CLI (argv for every mode, effort per model, a stream-json parser that is still pending live
verification, sign-in detection, credential scrubbing). No model is called and no network is used."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nightweaver import cli, engine, providers, readiness, safety, spec
from nightweaver.cli import main

HOME_VARIABLES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME")
ALL_MODES = ("worker", *providers.READ_ONLY, "planner", "probe")
NEON_LEVELS = ("low", "medium", "high", "xhigh")


def write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines), encoding="utf-8")


class Hermetic(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nwgem")))
        keep = {k: v for k, v in os.environ.items() if not safety.is_secret(k) and k.upper() not in HOME_VARIABLES
                and not k.upper().startswith(("GEMINI_", "GOOGLE_", "AGY_", "NIGHTWEAVER_"))}
        self.enterContext(patch.dict(os.environ, {**keep, "NIGHTWEAVER_HOME": str(self.root / "home"), "LOCALAPPDATA": str(self.root / "local")},
                                     clear=True))
        self.enterContext(patch("nightweaver.providers.user_environment", return_value=None))
        providers.AGY_SIGNED_IN.clear()
        self.addCleanup(providers.AGY_SIGNED_IN.clear)

    def fake_agy(self, name, *, code=0, output=""):
        """A stand-in agy.cmd that answers --version with 1.5.15, `models` with the given output and exit code (on stderr when it fails),
        and anything else with exit 2. Windows runs a .cmd as a program."""
        folder = self.root / name
        folder.mkdir()
        redirect = " 1>&2" if code else ""
        echoes = "".join(f"echo {line}{redirect}& " for line in output.splitlines())
        script = folder / "agy.cmd"
        script.write_text("\r\n".join(["@echo off", 'if "%1"=="--version" (echo 1.5.15& exit /b 0)',
                                       f'if "%1"=="models" ({echoes}exit /b {code})', "exit /b 2"]) + "\r\n", encoding="ascii")
        return script


class GeminiArgv(Hermetic):
    def test_every_mode_is_read_only_plan_except_the_worker(self):
        for mode in ALL_MODES:
            argv, env = providers.adapter({"kind": "gemini", "model": "gemini-2.5-pro"}, mode)
            self.assertEqual(argv[argv.index("--approval-mode") + 1], "auto_edit" if mode == "worker" else "plan", mode)
            self.assertEqual(argv[argv.index("--model") + 1], "gemini-2.5-pro")
            self.assertEqual(argv[:3], ["gemini", "--output-format", "stream-json"])
            self.assertNotIn("--resume", argv)
        argv, _ = providers.adapter({"kind": "gemini"}, "reviewer", "prior-session")
        self.assertEqual(argv[argv.index("--resume") + 1], "prior-session")
        self.assertNotIn("--model", argv)

    def test_the_workspace_is_trusted_by_environment_not_by_a_flag_older_versions_reject(self):
        os.environ["GEMINI_RESTRICTED_MODE"] = "true"
        for operator in (None, "false", "true"):
            if operator:
                os.environ["GEMINI_CLI_TRUST_WORKSPACE"] = operator
            argv, env = providers.adapter({"kind": "gemini"}, "worker")
            self.assertEqual(env["GEMINI_CLI_TRUST_WORKSPACE"], "true", "a Nightweaver worktree is never in the trusted-folders list")
            self.assertEqual(env["GEMINI_RESTRICTED_MODE"], "true", "an explicit restricted mode is the operator's to lift")
            self.assertNotIn("--skip-trust", argv)
        del os.environ["GEMINI_CLI_TRUST_WORKSPACE"]
        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", providers.adapter({"kind": "codex"}, "worker")[1])
        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", providers.adapter({"kind": "command", "argv": ["x"]}, "worker")[1])
        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", providers.adapter({"kind": "antigravity"}, "worker")[1])

    def test_a_profile_dir_becomes_the_cli_home(self):
        _, env = providers.adapter({"kind": "gemini", "profile_dir": str(self.root)}, "worker")
        self.assertEqual(env["GEMINI_CLI_HOME"], str(self.root))


class AntigravityArgv(Hermetic):
    def test_the_default_executable_is_the_installer_location_when_it_exists_else_agy(self):
        self.assertEqual(providers.provider_executable({"kind": "antigravity"}), "agy")
        installed = self.root / "local" / "agy" / "bin" / "agy.exe"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"")
        self.assertEqual(providers.provider_executable({"kind": "antigravity"}), str(installed))
        self.assertEqual(providers.provider_executable({"kind": "antigravity", "executable": "D:/tools/agy.exe"}), "D:/tools/agy.exe")
        self.assertEqual(providers.adapter({"kind": "antigravity"}, "worker")[0][0], str(installed))

    def test_every_mode_reads_the_task_from_stdin_as_stream_json_and_only_the_worker_may_edit(self):
        for mode in ALL_MODES:
            argv, env = providers.adapter({"kind": "antigravity"}, mode)
            self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits" if mode == "worker" else "plan", mode)
            self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
            self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
            self.assertIn("--disable-slash-commands", argv)
            for flag in ("--print", "-p", "--prompt", "--dangerously-skip-permissions", "--sandbox", "--effort", "--model", "--conversation"):
                self.assertNotIn(flag, argv, f"{flag} in {mode}: --print takes its prompt as a flag value and ignores stdin, and "
                                 "--input-format stream-json rejects a flag prompt")

    def test_effort_model_and_resume_follow_the_route(self):
        config = {"kind": "antigravity", "model": "gemini-4-neon", "effort": "xhigh", "executable": "agy"}
        argv, _ = providers.adapter(config, "worker", "conv-7")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-neon")
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-7")
        self.assertNotIn("--resume", argv)

    def test_structured_modes_pass_their_schema_as_a_file_path(self):
        for mode in ALL_MODES:
            argv, _ = providers.adapter({"kind": "antigravity"}, mode)
            if mode not in providers.SCHEMAS:
                self.assertNotIn("--json-schema", argv, mode)
                continue
            target = Path(argv[argv.index("--json-schema") + 1])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), providers.SCHEMAS[mode], mode)
            self.assertTrue(target.is_relative_to(self.root / "home"), "the schema lives in Nightweaver's own folder, outside any worktree")
        target.write_text("{}", encoding="utf-8")
        argv, _ = providers.adapter({"kind": "antigravity"}, "judge")
        self.assertEqual(json.loads(Path(argv[argv.index("--json-schema") + 1]).read_text(encoding="utf-8")), providers.JUDGE_SCHEMA,
                         "a stale schema file is rewritten")

    def test_the_agy_sandbox_is_opt_in_because_windows_needs_24h2(self):
        os.environ["NIGHTWEAVER_ANTIGRAVITY_SANDBOX"] = "1"
        self.assertIn("--sandbox", providers.adapter({"kind": "antigravity"}, "worker")[0])

    def test_a_profile_dir_never_leaks_into_claudes_variable(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root)
        _, env = providers.adapter({"kind": "antigravity"}, "worker")
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), str(self.root), "inherited, not rewritten")
        self.assertIsNone(providers.profile_override({"kind": "antigravity"}))
        description = providers.describe_provider({"kind": "antigravity"})
        self.assertTrue(description["profile_dir"].endswith(os.path.join(".gemini", "antigravity-cli")))
        self.assertEqual((description["executable"], description["model"]), ("agy", "CLI default"))


class EffortValidation(Hermetic):
    def route(self, **fields):
        return spec.provider({"kind": "antigravity", **fields}, spec.IMPLEMENTERS, "worker", self.root)

    def test_every_level_is_accepted_for_a_model_with_no_stated_cap(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(self.route(effort=level)["effort"], level)
            self.assertEqual(self.route(effort=level, model="claude-opus-5-5")["effort"], level)
        self.assertNotIn("effort", self.route(model="gemini-4-neon"))

    def test_neon_takes_low_to_xhigh_and_rejects_max_with_the_levels_it_does_accept(self):
        for model in ("gemini-4-neon", "models/gemini-4-neon", "gemini-4-hydrogen"):
            for level in NEON_LEVELS:
                self.assertEqual(self.route(model=model, effort=level)["effort"], level, (model, level))
            with self.assertRaisesRegex(ValueError, r"Unsupported effort for antigravity: max for model .*accepts low, medium, high, xhigh"):
                self.route(model=model, effort="max")
        with self.assertRaisesRegex(ValueError, "Unsupported effort for antigravity: huge"):
            self.route(effort="huge")
        for model in ("gemini-4-carbon", "gemini-4-argon"):
            self.assertEqual(self.route(model=model, effort="high")["effort"], "high")
            with self.assertRaisesRegex(ValueError, "accepts low, medium, high$"):
                self.route(model=model, effort="xhigh")

    def test_a_slug_that_names_its_own_level_cannot_be_given_another(self):
        self.assertEqual(self.route(model="gemini-4-neon-xhigh")["model"], "gemini-4-neon-xhigh")
        self.assertEqual(self.route(model="gemini-4-neon-xhigh", effort="xhigh")["effort"], "xhigh")
        with self.assertRaisesRegex(ValueError, "already names effort xhigh"):
            self.route(model="gemini-4-neon-xhigh", effort="high")

    def test_the_gemini_cli_still_has_no_effort_and_says_where_to_get_one(self):
        for level in ("low", "high", "xhigh"):
            with self.assertRaisesRegex(ValueError, "Unsupported effort for gemini: .*antigravity kind"):
                spec.provider({"kind": "gemini", "effort": level}, spec.IMPLEMENTERS, "worker", self.root)

    def test_antigravity_is_a_worker_reviewer_judge_and_planner_kind_with_no_profile_dir(self):
        self.assertIn("antigravity", spec.IMPLEMENTERS)
        self.assertIn("antigravity", spec.REVIEWERS)
        self.assertEqual(self.route(executable="D:/t/agy.exe")["executable"], "D:/t/agy.exe")
        with self.assertRaisesRegex(ValueError, "no profile_dir"):
            self.route(profile_dir=str(self.root))
        with self.assertRaisesRegex(ValueError, "api_key_file"):
            self.route(api_key_file="key.txt")

    def test_the_command_line_offers_antigravity_wherever_it_offers_gemini(self):
        for flag in ("--worker", "--judge", "--reviewer", "--planner-kind", "--rescue"):
            with redirect_stderr(StringIO()) as err, self.assertRaises(SystemExit):
                main(["--home", str(self.root / "home"), "oneshot", str(self.root), "goal", flag, "nonsense"])
            self.assertIn("antigravity", err.getvalue(), flag)
        with redirect_stderr(StringIO()) as err, self.assertRaises(SystemExit):
            main(["--home", str(self.root / "home"), "doctor", "--planner", "nonsense"])
        self.assertIn("antigravity", err.getvalue())
        with redirect_stderr(StringIO()) as err, self.assertRaises(SystemExit):
            main(["--home", str(self.root / "home"), "oneshot", str(self.root), "goal", "--pursue", "--explore", "2",
                  "--explore-workers", "gemini,antigravity,nonsense"])
        self.assertIn("antigravity", err.getvalue(), "the explore-workers refusal lists the kinds it accepts")
        self.assertIn("antigravity", cli.EXPLORERS)

    def test_doctor_refuses_max_for_neon_at_the_command_line(self):
        err = StringIO()
        with redirect_stderr(err), redirect_stdout(StringIO()):
            code = main(["--home", str(self.root / "home"), "doctor", "--planner", "antigravity", "--model", "gemini-4-neon", "--effort", "max"])
        self.assertEqual(code, 1)
        self.assertIn("Unsupported effort for antigravity: max", err.getvalue())


class NpmEntrypoint(Hermetic):
    def tree(self, *, bin_entry="bundle/gemini.js", files=("bundle/gemini.js",), node=True, launcher="gemini.cmd", folder="npm"):
        npm = self.root / folder
        package = npm / "node_modules" / "@google" / "gemini-cli"
        package.mkdir(parents=True)
        manifest = {"name": "@google/gemini-cli", "version": "0.70.0"}
        if bin_entry is not None:
            manifest["bin"] = {"gemini": bin_entry}
        (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        for name in files:
            (package / name).parent.mkdir(parents=True, exist_ok=True)
            (package / name).write_text("// entry\n", encoding="utf-8")
        (npm / launcher).write_text("@echo off\r\n", encoding="ascii")
        if node:
            (npm / "node.exe").write_bytes(b"")
        return npm, package

    def call(self, config, **kwargs):
        seen = {}

        def fake(argv, cwd, log, timeout, **options):
            seen.update(argv=argv, prompt=options["prompt"], env=options["env"])
            write(log, {"type": "init", "session_id": "g-1", "model": "gemini-2.5-pro"},
                  {"type": "message", "role": "assistant", "content": "READY", "delta": True},
                  {"type": "result", "status": "success", "stats": {"total_tokens": 9}})
            return 0

        with patch("nightweaver.providers.execute", side_effect=fake):
            outcome = providers.invoke(config, kwargs.pop("mode", "reviewer"), self.root, self.root / "call.jsonl", "do it", 10, lambda: False, **kwargs)
        return outcome, seen

    def test_the_current_package_layout_resolves_to_bundle_gemini_js(self):
        npm, package = self.tree()
        self.assertEqual(providers.gemini_entrypoint(npm / "gemini.cmd"), package / "bundle" / "gemini.js")

    def test_older_packages_with_dist_index_js_still_resolve(self):
        npm, package = self.tree(bin_entry="dist/index.js", files=("dist/index.js",))
        self.assertEqual(providers.gemini_entrypoint(npm / "gemini.cmd"), package / "dist" / "index.js")
        npm, package = self.tree(bin_entry=None, files=("dist/index.js",), folder="npm2")  # a manifest that names no bin
        self.assertEqual(providers.gemini_entrypoint(npm / "gemini.cmd"), package / "dist" / "index.js")

    def test_a_manifest_that_points_at_a_missing_file_falls_back_to_the_known_names(self):
        npm, package = self.tree(bin_entry="build/gone.js", files=("bundle/gemini.js",))
        self.assertEqual(providers.gemini_entrypoint(npm / "gemini.cmd"), package / "bundle" / "gemini.js")

    def test_an_entry_outside_the_package_is_never_used(self):
        npm, package = self.tree(bin_entry="../../../outside.js", files=())
        (npm / "outside.js").write_text("// not part of the package\n", encoding="utf-8")
        self.assertIsNone(providers.gemini_entrypoint(npm / "gemini.cmd"))

    def test_no_package_beside_the_launcher_is_none(self):
        (self.root / "bare").mkdir()
        (self.root / "bare" / "gemini.cmd").write_text("@echo off\r\n", encoding="ascii")
        self.assertIsNone(providers.gemini_entrypoint(self.root / "bare" / "gemini.cmd"))

    def test_invoke_runs_node_on_the_entry_without_a_shell_and_keeps_the_arguments(self):
        npm, package = self.tree()
        (outcome, seen) = self.call({"kind": "gemini", "executable": str(npm / "gemini.cmd"), "model": "gemini-2.5-pro"}, session="prior")
        self.assertEqual(outcome, (0, "g-1", "READY"))
        argv = seen["argv"]
        self.assertEqual(argv[:2], [str(npm / "node.exe"), str(package / "bundle" / "gemini.js")])
        self.assertEqual(argv[2:5], ["--output-format", "stream-json", "--approval-mode"])
        self.assertEqual(argv[argv.index("--resume") + 1], "prior")
        self.assertEqual(seen["env"]["GEMINI_CLI_TRUST_WORKSPACE"], "true")
        self.assertEqual(seen["prompt"], "do it", "the task still travels on stdin as plain text for the Gemini CLI")

    def test_node_from_path_is_used_when_none_sits_beside_the_launcher(self):
        npm, package = self.tree(node=False)
        real = shutil.which
        with patch("nightweaver.providers.shutil.which", side_effect=lambda name, *a, **k: "C:/node/node.exe" if name == "node" else real(name, *a, **k)):
            _, seen = self.call({"kind": "gemini", "executable": str(npm / "gemini.cmd")})
        self.assertEqual(seen["argv"][:2], ["C:/node/node.exe", str(package / "bundle" / "gemini.js")])

    def test_a_missing_node_or_package_blocks_with_the_cause_named(self):
        npm, _ = self.tree(node=False)
        real = shutil.which
        with patch("nightweaver.providers.shutil.which", side_effect=lambda name, *a, **k: None if name == "node" else real(name, *a, **k)):
            with self.assertRaisesRegex(providers.ProviderUnavailable, "node is not installed") as caught:
                self.call({"kind": "gemini", "executable": str(npm / "gemini.cmd")})
        self.assertEqual(caught.exception.kind, "missing_executable")
        (self.root / "empty").mkdir()
        (self.root / "empty" / "gemini.cmd").write_text("@echo off\r\n", encoding="ascii")
        (self.root / "empty" / "node.exe").write_bytes(b"")
        with self.assertRaisesRegex(providers.ProviderUnavailable, "npm install -g @google/gemini-cli") as caught:
            self.call({"kind": "gemini", "executable": str(self.root / "empty" / "gemini.cmd")})
        self.assertEqual(caught.exception.kind, "missing_executable")

    def test_other_kinds_behind_a_cmd_launcher_are_still_refused(self):
        npm, _ = self.tree(launcher="claude.cmd")
        with self.assertRaisesRegex(providers.ProviderUnavailable, "native executable"):
            self.call({"kind": "claude", "executable": str(npm / "claude.cmd")})

    def test_the_real_install_on_this_machine_resolves_when_present(self):
        launcher = shutil.which("gemini")
        if not launcher or Path(launcher).suffix.casefold() != ".cmd":
            self.skipTest("no npm Gemini launcher installed")
        entry = providers.gemini_entrypoint(launcher)
        self.assertTrue(entry is None or entry.is_file())


class Classification(Hermetic):
    AUTH = ("Please set an Auth method in your C:\\Users\\x\\.gemini\\settings.json or specify one of the following environment variables "
            "before running: GEMINI_API_KEY, GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA")
    TRUST = ("Gemini CLI is not running in a trusted directory. To proceed, either use `--skip-trust`, set the `GEMINI_CLI_TRUST_WORKSPACE=true` "
             "environment variable, or trust this directory in interactive mode.")
    TABLE = (
        # kind, exit code, provider's words, expected ProviderUnavailable.kind (None: an ordinary failed attempt)
        ("gemini", 41, AUTH, "authentication"),
        ("gemini", 41, "Invalid auth method selected.", "authentication"),
        ("gemini", 55, TRUST, "configuration"),
        ("gemini", 55, "", "configuration"),
        ("gemini", 1, TRUST, "configuration"),
        ("gemini", 52, "", "configuration"),
        ("gemini", 1, "You have exhausted your capacity on this model. Your quota will reset after 3h52m49s.", "usage_limit"),
        ("gemini", 1, "You have exhausted your daily quota on this model.", "usage_limit"),
        ("gemini", 1, '[API Error: {"error":{"code":429,"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}]',
         "usage_limit"),
        ("gemini", 1, "Please retry in 36.2s. exhausted your capacity", "usage_limit"),
        ("gemini", 1, "Requested entity was not found.", "model_access"),
        ("gemini", 1, '[API Error: {"error":{"code":404,"message":"models/gemini-9 is not found for API version v1beta","status":"NOT_FOUND"}}]',
         "model_access"),
        ("gemini", 1, "API key not valid. Please pass a valid API key.", "authentication"),
        ("gemini", 1, '{"error":{"code":400,"message":"x","status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}', "authentication"),
        ("gemini", 1, "The model is overloaded. Please try again later.", "transient"),
        ("gemini", 1, '{"error":{"code":503,"status":"UNAVAILABLE"}}', "transient"),
        ("gemini", 1, "tests did not pass", None),
        ("gemini", 42, "Error resuming session: no such session", None),
        ("antigravity", 1, "Error: authentication required. Run 'agy' to log in, then retry.", "authentication"),
        ("antigravity", 1, "Error: Please sign in to view available models. Launch the CLI without arguments to sign in.", "authentication"),
        ("antigravity", 1, 'Error: modelProvider is set to "gemini" in settings.json, but the GEMINI_API_KEY environment variable is not set.',
         "authentication"),
        ("antigravity", 3, 'AGY_ERROR: {"short_error":"You have exhausted your capacity on this model. Your quota will reset after 1h2m3s."}',
         "usage_limit"),
        ("antigravity", 3, 'AGY_ERROR: {"canonical_status":"RESOURCE_EXHAUSTED","code":429,"retryable":false}', "usage_limit"),
        ("antigravity", 1, 'Error: failed to resolve model "gemini-9": model not found', "model_access"),
        ("antigravity", 1, 'Error: unknown model "nonsense"', "model_access"),
        ("antigravity", 3, 'AGY_ERROR: {"canonical_status":"UNAVAILABLE","code":503,"retryable":true}', "transient"),
        ("antigravity", 2, "Error: --input-format stream-json reads prompts from stdin, so a prompt given on the command line would be ignored", None),
        ("antigravity", 41, "an ordinary failure", None),
    )

    def test_every_gemini_and_agy_failure_text_maps_to_the_right_kind(self):
        for kind, code, detail, expected in self.TABLE:
            problem = providers.google_problem({"kind": kind}, code, detail)
            self.assertEqual(problem.kind if problem else None, expected, (kind, code, detail[:70]))

    def test_a_quota_wait_carries_the_reset_the_message_names(self):
        problem = providers.google_problem({"kind": "gemini"}, 1, "You have exhausted your capacity on this model. Your quota will reset after 3h52m49s.")
        wait = problem.reset_at - datetime.now(timezone.utc)
        self.assertLess(abs(wait - timedelta(hours=3, minutes=52, seconds=49)), timedelta(seconds=30))
        self.assertIn("Provider said", str(problem))
        self.assertIsNone(providers.google_problem({"kind": "gemini"}, 1, "You have exhausted your daily quota on this model.").reset_at)
        short = providers.google_problem({"kind": "gemini"}, 1, '{"error":{"code":429,"details":[{"retryDelay":"36s"}],"status":"RESOURCE_EXHAUSTED"}}')
        self.assertLess(abs(short.reset_at - datetime.now(timezone.utc) - timedelta(seconds=36)), timedelta(seconds=30))

    def test_the_messages_tell_the_operator_what_to_do(self):
        gemini = providers.google_problem({"kind": "gemini"}, 41, "")
        self.assertIn("GEMINI_API_KEY", str(gemini))
        self.assertIn("Enterprise-only", str(gemini))
        self.assertIn("--retry", str(gemini))
        agy = providers.google_problem({"kind": "antigravity"}, 1, "authentication required")
        self.assertIn("agy", str(agy))
        self.assertIn("modelProvider", str(agy))

    def finish(self, kind, code, *lines):
        def fake(argv, cwd, log, timeout, **options):
            write(log, *lines)
            return code

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", return_value=(0, "models")):
            return providers.invoke({"kind": kind, "executable": sys.executable}, "worker", self.root, self.root / "call.jsonl", "task", 10, lambda: False)

    def blocked(self, kind, code, *lines):
        with self.assertRaises(providers.ProviderUnavailable) as caught:
            self.finish(kind, code, *lines)
        return caught.exception

    def test_a_failed_call_parks_or_blocks_instead_of_burning_an_attempt(self):
        self.assertEqual(self.blocked("gemini", 41, self.AUTH).kind, "authentication")
        self.assertEqual(self.blocked("gemini", 55, self.TRUST).kind, "configuration")
        limit = self.blocked("gemini", 1, {"type": "init", "session_id": "s"},
                             {"type": "error", "severity": "error", "message": "You have exhausted your capacity on this model. Your quota will reset after 2h."},
                             {"type": "result", "status": "error", "error": {"message": "quota"}})
        self.assertEqual(limit.kind, "usage_limit")
        self.assertIsNotNone(limit.reset_at)
        self.assertEqual(self.blocked("gemini", 1, "[API Error: Requested entity was not found.]").kind, "model_access")
        self.assertEqual(self.blocked("antigravity", 1, "Error: authentication required. Run 'agy' to log in, then retry.").kind, "authentication")
        self.assertEqual(self.blocked("antigravity", 3, 'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED"}').kind, "usage_limit")

    def test_a_clean_exit_with_a_failed_agy_result_is_still_a_failure(self):
        problem = self.blocked("antigravity", 0, {"event": "init", "conversation_id": "c"},
                               {"event": "result", "result": {"conversation_id": "c", "status": "ERROR",
                                                              "error": "You have exhausted your capacity on this model. Your quota will reset after 2h."}})
        self.assertEqual(problem.kind, "usage_limit")
        code, session, _ = self.finish("antigravity", 1, {"event": "result", "result": {"conversation_id": "c", "status": "ERROR", "error": "tool failed"}})
        self.assertEqual((code, session), (1, "c"), "an unrecognised failure stays an ordinary failed attempt")

    def test_words_in_the_assistants_prose_or_tool_output_never_classify(self):
        words = "RESOURCE_EXHAUSTED exhausted your capacity authentication required"
        events = ({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": words}},
                  {"event": "step_update", "step_update": {"step_type": "tool", "tool_info": {"error": {"type": "x", "message": words}}}},
                  {"event": "result", "result": {"status": "SUCCESS", "response": words}})
        self.assertEqual(self.finish("antigravity", 1, *events)[0], 1)
        prose = {"type": "message", "role": "assistant", "content": words, "delta": True}
        self.assertEqual(self.finish("gemini", 1, prose)[0], 1)

    def test_the_google_wording_means_nothing_to_the_other_vendors(self):
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-test-key"
        for kind in ("codex", "claude", "deepseek"):
            code, _, _ = self.finish(kind, 1, "You have exhausted your capacity on this model.", "Requested entity was not found.")
            self.assertEqual(code, 1, kind)


STEP = {"event": "step_update", "step_update": {"conversation_id": "conv-1", "step_index": 0, "state": "DONE", "step_type": "user_input"}}


def delta(index, text, conversation="conv-1"):
    return {"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index, "state": "ACTIVE",
                                                   "step_type": "agent_response", "text_delta": text}}


INIT = {"event": "init", "conversation_id": "conv-1", "init": {"cwd": "C:/w", "tools": ["read_file"], "permission_mode": "request-review",
                                                               "model": "gemini-4-neon-xhigh"}}
USAGE = {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 300, "cache_read_tokens": 40, "total_tokens": 420}


def result(**fields):
    return {"event": "result", "result": {"conversation_id": "conv-1", "status": "SUCCESS", "duration_seconds": 3.2, "num_turns": 1,
                                          "usage": USAGE, **fields}}


class AntigravityStream(Hermetic):
    """The stream-json parser is PENDING LIVE VERIFICATION: these fixtures come from agy's docs, not from a signed-in run."""

    def log(self, *events):
        write(self.root / "call.jsonl", *events)
        return self.root / "call.jsonl"

    def test_a_successful_call_yields_session_answer_usage_and_model(self):
        log = self.log(INIT, STEP, delta(1, "Looking at "), delta(1, "the repo."), delta(3, "RE"), delta(3, "ADY"), result(response="READY"))
        self.assertEqual(providers.metadata(log, "antigravity"), ("conv-1", "READY"))
        found = providers.agy_stream(log)
        self.assertEqual((found["session"], found["model"], found["status"], found["failed"]), ("conv-1", "gemini-4-neon-xhigh", "SUCCESS", False))
        self.assertEqual(found["usage"], USAGE)
        receipt = providers.usage(log, "antigravity")
        self.assertEqual(receipt["observed_tokens"], 420)
        self.assertEqual(receipt["token_channels"], {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 300, "cache_read_tokens": 40})
        self.assertEqual(receipt["billing"], "not_measured")

    def test_without_a_response_the_answer_is_the_last_agent_step_not_the_narration(self):
        log = self.log(INIT, delta(1, "Let me look."), delta(2, "Now the "), delta(2, "final answer."), result())
        self.assertEqual(providers.metadata(log, "antigravity")[1], "Now the final answer.")
        self.assertEqual(providers.metadata(self.log(INIT, delta(1, "partial "), delta(1, "text")), "antigravity")[1], "partial text",
                         "a stream cut off before its result still gives what was said")

    def test_a_schema_mode_prefers_the_structured_output(self):
        verdict = {"verdict": "PASS", "reason": "fine"}
        log = self.log(INIT, result(response="Here you go.", structured_output=verdict))
        self.assertEqual(json.loads(providers.metadata(log, "antigravity", "reviewer")[1]), verdict)
        self.assertEqual(providers.metadata(log, "antigravity", "worker")[1], "Here you go.", "a worker's answer is its prose")
        text = self.log(INIT, result(response="x", structured_output=json.dumps(verdict)))
        self.assertEqual(json.loads(providers.metadata(text, "antigravity", "judge")[1]), verdict)

    def test_a_subagent_step_cannot_replace_the_conversation_id(self):
        log = self.log(INIT, delta(1, "ok", conversation="conv-sub"), result(response="done"))
        self.assertEqual(providers.metadata(log, "antigravity")[0], "conv-1")
        self.assertEqual(providers.metadata(self.log(delta(1, "ok", conversation="conv-sub")), "antigravity")[0], "conv-sub",
                         "a stream with no init or result falls back to the step's id")

    def test_fields_beside_the_event_name_are_read_too(self):
        log = self.log({"event": "init", "conversation_id": "flat-1", "model": "gemini-4-neon"},
                       {"event": "result", "conversation_id": "flat-1", "status": "SUCCESS", "response": "ok", "usage": {"total_tokens": 5}})
        self.assertEqual(providers.metadata(log, "antigravity"), ("flat-1", "ok"))
        self.assertEqual(providers.usage(log, "antigravity")["observed_tokens"], 5)
        self.assertEqual(providers.served_models({"kind": "antigravity"}, log)["models"], ["gemini-4-neon"])

    def test_failure_is_any_status_but_success_or_a_finished_reason(self):
        for fields, failed in (({}, False), ({"status": "ERROR", "error": "x"}, True), ({"status": "INVALID"}, True), ({"status": "CANCELED"}, True),
                               ({"status": "INTERRUPTED"}, True), ({"status": "success"}, False), ({"finished_reason": "print_timeout"}, True),
                               ({"finished_reason": "interrupted"}, True)):
            self.assertIs(providers.agy_stream(self.log(INIT, result(**fields)))["failed"], failed, fields)

    def test_the_providers_error_comes_from_the_result_and_the_stderr_lines(self):
        log = self.log("warning: unrecognized --mode value", INIT, delta(1, "RESOURCE_EXHAUSTED in prose"),
                       result(status="ERROR", error={"code": 429, "message": "quota gone"}), 'AGY_ERROR: {"short_error":"boom"}')
        errors = providers.provider_errors(log)
        self.assertIn("quota gone", errors)
        self.assertIn('"code": 429', errors)
        self.assertIn("AGY_ERROR", errors)
        self.assertNotIn("in prose", errors)
        self.assertEqual(providers.provider_errors(self.log(INIT, result(response="fine"))), "")

    def test_malformed_and_oversized_input_is_skipped(self):
        log = self.log("not json", "[]", "null", {"event": 7}, {"event": "result", "result": "text"}, {"event": "init", "init": []}, INIT,
                       result(usage="many", response=["x"]))
        found = providers.agy_stream(log)
        self.assertEqual((found["session"], found["model"], found["usage"], found["answer"]), ("conv-1", "gemini-4-neon-xhigh", None, ""))
        self.assertEqual(providers.usage(log, "antigravity")["observed_tokens"], None, "missing telemetry stays unknown, not zero")
        self.assertEqual(providers.agy_stream(self.root / "missing.jsonl")["session"], None)

    def test_a_later_result_replaces_the_earlier_cumulative_usage(self):
        log = self.log(INIT, result(usage={"total_tokens": 100, "input_tokens": 60, "output_tokens": 40}),
                       result(usage={"total_tokens": 250, "input_tokens": 150, "output_tokens": 100}))
        receipt = providers.usage(log, "antigravity")
        self.assertEqual((receipt["observed_tokens"], receipt["token_channels"]["input_tokens"]), (250, 150))
        no_total = self.log(INIT, result(usage={"input_tokens": 7, "output_tokens": 3}))
        self.assertEqual(providers.usage(no_total, "antigravity")["observed_tokens"], 10)
        negative = self.log(INIT, result(usage={"input_tokens": -1, "total_tokens": 9}))
        self.assertIsNone(providers.usage(negative, "antigravity")["observed_tokens"])

    def test_the_served_model_is_the_init_model_and_a_slug_with_a_level_is_the_asked_model(self):
        log = self.log(INIT, result(response="x"))
        served = providers.served_models({"kind": "antigravity"}, log)
        self.assertEqual(served, {"models": ["gemini-4-neon-xhigh"], "source": "init"})
        asked = {"role": "worker", "provider": "antigravity", "model": "gemini-4-neon", "call": 1}
        self.assertEqual(engine.model_drift(asked, served, [])[0], [], "gemini-4-neon at xhigh is served as gemini-4-neon-xhigh")
        other = {**asked, "model": "gemini-4-carbon"}
        self.assertEqual(engine.model_drift(other, served, [])[0][0][1]["served"], "gemini-4-neon-xhigh")
        self.assertEqual(providers.served_models({"kind": "antigravity"}, self.log(result(response="x")))["models"], [], "no --model, no init model")

    def test_canonical_matching_of_gemini_4_slugs(self):
        same = (("gemini-4-neon", "gemini-4-neon-xhigh"), ("gemini-4-neon-xhigh", "gemini-4-neon"), ("gemini-4-neon", "gemini-4-neon"),
                ("gemini-4-neon-xhigh", "gemini-4-neon-xhigh"), ("gemini-4-neon", "models/gemini-4-neon-high"), ("Gemini-4-Neon", "gemini-4-neon-low"))
        different = (("gemini-4-neon-xhigh", "gemini-4-neon-low"), ("gemini-4-neon", "gemini-4-hydrogen-xhigh"), ("gemini-4-neon", "gemini-4-carbon"),
                     ("gemini-4-neon", "gemini-3-neon-xhigh"), ("claude-opus-5-5", "claude-opus-5-5-high-x"))
        for asked, got in same:
            self.assertTrue(providers.same_model(asked, got), (asked, got))
        for asked, got in different:
            self.assertFalse(providers.same_model(asked, got), (asked, got))
        self.assertEqual(providers.effortless("gemini-4-neon-xhigh"), "gemini-4-neon")
        self.assertEqual(providers.effortless("gpt-5-high"), "gpt-5-high", "other vendors' ids keep their suffix")


class AntigravityCall(Hermetic):
    def invoke(self, *lines, config=None, mode="worker", code=0, **kwargs):
        seen = {}
        prompt = kwargs.pop("prompt", "Build it.\nSecond line \u00e9")

        def fake(argv, cwd, log, timeout, **options):
            seen.update(argv=argv, prompt=options["prompt"], env=options["env"])
            write(log, *(lines or (INIT, delta(1, "done"), result(response="DONE"))))
            return code

        with patch("nightweaver.providers.execute", side_effect=fake), patch("nightweaver.providers.agy_run", return_value=(0, "gemini-4-neon-xhigh")):
            outcome = providers.invoke({"kind": "antigravity", "executable": sys.executable, **(config or {})}, mode, self.root,
                                       self.root / "call.jsonl", prompt, 10, lambda: False, **kwargs)
        return outcome, seen

    def test_the_task_goes_in_on_stdin_as_one_stream_json_user_message(self):
        outcome, seen = self.invoke(prompt='Say "hi"\nand\\or more \u00e9')
        self.assertEqual(outcome, (0, "conv-1", "DONE"))
        self.assertEqual(seen["argv"][0], sys.executable)
        lines = seen["prompt"].splitlines()
        self.assertEqual(len(lines), 1, "one NDJSON line, however many lines the task has")
        self.assertEqual(json.loads(lines[0]), {"event": "user", "message": {"content": 'Say "hi"\nand\\or more \u00e9'}})
        self.assertTrue(seen["prompt"].isascii() and seen["prompt"].endswith("\n"))
        self.assertNotIn("--print", seen["argv"])

    def test_evidence_and_image_folders_are_added_and_images_stay_paths_in_the_prompt(self):
        evidence = self.root / "evidence"
        shots = self.root / "outside" / "shots"
        inside = evidence / "inner"
        for folder in (evidence, shots, inside):
            folder.mkdir(parents=True)
        images = [shots / "a.png", inside / "b.png"]
        _, seen = self.invoke(config={"model": "gemini-4-neon"}, mode="judge", evidence=evidence, images=images)
        argv = seen["argv"]
        added = [argv[i + 1] for i, item in enumerate(argv) if item == "--add-dir"]
        self.assertEqual(added, [str(evidence), str(shots)], "a folder already under the evidence folder is not added twice")
        self.assertNotIn("--image", argv)
        self.assertEqual(seen["env"]["NIGHTWEAVER_EVIDENCE"], str(evidence))
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")

    def test_a_judge_gets_the_structured_answer(self):
        verdict = {"verdict": "MET", "score": 90, "criteria": [], "gaps": [], "reason": "ok"}
        (code, session, answer), seen = self.invoke(INIT, result(response="prose", structured_output=verdict), mode="judge")
        self.assertEqual((code, session), (0, "conv-1"))
        self.assertEqual(json.loads(answer), verdict)
        self.assertIn("--json-schema", seen["argv"])

    def test_resume_passes_the_conversation_id_and_a_new_one_is_returned(self):
        (_, session, _), seen = self.invoke(session="conv-old")
        self.assertEqual(seen["argv"][seen["argv"].index("--conversation") + 1], "conv-old")
        self.assertEqual(session, "conv-1")

    def test_agy_reads_only_its_vendors_credentials(self):
        os.environ.update(GEMINI_API_KEY="g-secret", GOOGLE_API_KEY="k-secret", OPENAI_API_KEY="o-secret", ANTHROPIC_API_KEY="a-secret",
                          DEEPSEEK_API_KEY="d-secret", GITHUB_TOKEN="t-secret", GOOGLE_APPLICATION_CREDENTIALS="adc.json",
                          GOOGLE_CLOUD_PROJECT="proj", AGY_ADC_AUTH="true", CLAUDE_CODE_OAUTH_TOKEN="c-secret", GEMINI_ACCESS_TOKEN="x-secret")
        _, seen = self.invoke()
        env = seen["env"]
        for kept in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "AGY_ADC_AUTH", "GEMINI_ACCESS_TOKEN"):
            self.assertIn(kept, env, kept)
        for dropped in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertNotIn(dropped, env, dropped)
        self.assertEqual(safety.own_credentials("antigravity", os.environ), safety.own_credentials("gemini", os.environ))
        for gate in (safety.scrub_env(os.environ), providers.adapter({"kind": "command", "argv": ["x"]}, "worker")[1]):
            self.assertFalse({"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_ACCESS_TOKEN"} & set(gate))
        self.assertIn("AGY_ADC_AUTH", safety.scrub_env(os.environ), "a switch whose name is not credential-shaped is not a secret")

    def test_a_gemini_route_keeps_only_its_own_key(self):
        os.environ.update(GEMINI_API_KEY="g-secret", OPENAI_API_KEY="o-secret")
        _, env = providers.adapter({"kind": "gemini"}, "worker")
        self.assertEqual((env["GEMINI_API_KEY"], "OPENAI_API_KEY" in env), ("g-secret", False))


class SignIn(Hermetic):
    def setUp(self):
        super().setUp()
        self.signed_out = self.fake_agy("out", code=1, output="Fetching available models...\nError: Please sign in to view available models.")
        self.signed_in = self.fake_agy("in", code=0, output="Available models:\ngemini-4-neon-xhigh\ngemini-3.8-flash-medium")
        self.broken = self.fake_agy("broken", code=1, output="Error: could not reach the model service")

    def route(self, script, **fields):
        return {"kind": "antigravity", "executable": str(script), **fields}

    def test_doctor_reports_version_and_that_agy_is_signed_out(self):
        report = readiness.check_route(None, self.route(self.signed_out, model="gemini-4-neon", effort="xhigh"), self.root, False)
        self.assertEqual((report["status"], report["reason"]), ("blocked", "authentication"))
        self.assertEqual((report["version"], report["signed_in"], report["auth"]), ("1.5.15", False, "cli_login"))
        self.assertEqual((report["provider"], report["model"], report["effort"]), ("antigravity", "gemini-4-neon", "xhigh"))
        self.assertIn("Run agy once and sign in", report["action"])
        self.assertNotIn("model_listed", report)

    def test_doctor_reports_a_signed_in_agy_and_whether_it_lists_the_model(self):
        report = readiness.check_route(None, self.route(self.signed_in, model="gemini-4-neon"), self.root, False)
        self.assertEqual((report["status"], report["signed_in"], report["version"], report["model_listed"]), ("configured", True, "1.5.15", True))
        self.assertIs(readiness.check_route(None, self.route(self.signed_in, model="gemini-9-nope"), self.root, False)["model_listed"], False)
        self.assertNotIn("model_listed", readiness.check_route(None, self.route(self.signed_in), self.root, False))

    def test_an_unreadable_answer_is_unknown_not_signed_out(self):
        report = readiness.check_route(None, self.route(self.broken), self.root, False)
        self.assertEqual((report["status"], report["signed_in"]), ("configured", None))

    def test_a_missing_agy_is_a_missing_executable(self):
        report = readiness.check_route(None, {"kind": "antigravity", "executable": str(self.root / "nowhere" / "agy.exe")}, self.root, False)
        self.assertEqual((report["status"], report["reason"]), ("blocked", "missing_executable"))
        self.assertNotIn("signed_in", report)

    def test_an_api_key_route_names_its_source_and_is_not_blocked_by_a_missing_login(self):
        os.environ["GEMINI_API_KEY"] = "sk-never-printed-0123456789"
        report = readiness.check_route(None, self.route(self.signed_out), self.root, False)
        self.assertEqual((report["status"], report["auth"], report["signed_in"]), ("configured", "api_key", False))
        self.assertNotIn("never-printed", json.dumps(report))
        del os.environ["GEMINI_API_KEY"]
        os.environ["AGY_ADC_AUTH"] = "true"
        self.assertEqual(readiness.check_route(None, self.route(self.signed_out), self.root, False)["auth"], "adc")

    def test_a_signed_out_agy_stops_the_call_before_it_can_wait_for_a_login(self):
        with patch("nightweaver.providers.execute", side_effect=AssertionError("agy must not be started signed out")):
            with self.assertRaises(providers.ProviderUnavailable) as caught:
                providers.invoke(self.route(self.signed_out), "worker", self.root, self.root / "call.jsonl", "task", 10, lambda: False)
        self.assertEqual(caught.exception.kind, "authentication")

    def test_a_signed_in_check_is_remembered_and_a_signed_out_one_is_not(self):
        with patch("nightweaver.providers.agy_run", return_value=(0, "models")) as ran:
            for _ in range(3):
                providers.antigravity_login("agy")
        self.assertEqual(ran.call_count, 1)
        providers.AGY_SIGNED_IN.clear()
        with patch("nightweaver.providers.agy_run", return_value=(1, "Please sign in")) as ran:
            for _ in range(2):
                with self.assertRaises(providers.ProviderUnavailable):
                    providers.antigravity_login("agy")
        self.assertEqual(ran.call_count, 2, "signing in must take effect on the next call")

    def test_a_route_with_a_key_is_not_asked_whether_it_is_signed_in(self):
        os.environ["GOOGLE_API_KEY"] = "k"
        with patch("nightweaver.providers.agy_run", side_effect=AssertionError("not asked")):
            providers.antigravity_login("agy")

    def test_the_plain_doctor_report_keeps_its_shape_and_the_route_report_finds_the_unlisted_agy(self):
        report = readiness.diagnose(SimpleNamespace(root=self.root / "data"))
        self.assertEqual(set(report["executables"]), {"git", "claude", "codex", "gemini", "ollama"}, "the report's documented shape is unchanged")
        installed = self.root / "local" / "agy" / "bin" / "agy.exe"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"")
        found = readiness.check_route(None, {"kind": "antigravity"}, self.root, False)
        self.assertEqual(found["executable"], str(installed), "agy is not on PATH, so the route finds the installer's folder")

    def test_the_command_line_doctor_checks_an_agy_planner_without_a_model_call(self):
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.signed_out.parent) + os.pathsep + os.environ["PATH"]}))
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--home", str(self.root / "home"), "doctor", "--planner", "antigravity", "--model", "gemini-4-neon", "--effort", "xhigh"])
        provider = json.loads(out.getvalue())["providers"][0]
        self.assertEqual(code, 3, "a blocked required route exits 3")
        self.assertEqual((provider["provider"], provider["status"], provider["reason"], provider["signed_in"], provider["version"]),
                         ("antigravity", "blocked", "authentication", False, "1.5.15"))
        self.assertEqual(provider["roles"], ["planner"])
        self.enterContext(patch.dict(os.environ, {"PATH": str(self.signed_in.parent) + os.pathsep + os.environ["PATH"]}))
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--home", str(self.root / "home"), "doctor", "--planner", "antigravity", "--model", "gemini-4-neon"])
        provider = json.loads(out.getvalue())["providers"][0]
        self.assertEqual((code, provider["status"], provider["signed_in"], provider["model_listed"]), (0, "configured", True, True))


class GeminiDoctor(Hermetic):
    def test_doctor_names_how_a_gemini_route_will_sign_in_without_printing_it(self):
        fake = self.root / "bin" / "gemini.exe"
        fake.parent.mkdir()
        fake.write_bytes(b"")
        config = {"kind": "gemini", "executable": str(fake), "profile_dir": str(self.root / "profile")}
        (self.root / "profile").mkdir()
        report = readiness.check_route(None, config, self.root, False)
        self.assertEqual((report["status"], report["auth"]), ("configured", "none_found"))
        self.assertIn("GEMINI_API_KEY", report["action"])
        (self.root / "profile" / ".gemini").mkdir()
        (self.root / "profile" / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        self.assertEqual(readiness.check_route(None, config, self.root, False)["auth"], "oauth_login")
        os.environ["GEMINI_API_KEY"] = "AIza-never-printed"
        report = readiness.check_route(None, config, self.root, False)
        self.assertEqual(report["auth"], "api_key")
        self.assertNotIn("never-printed", json.dumps(report))
        del os.environ["GEMINI_API_KEY"]
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        self.assertEqual(readiness.check_route(None, config, self.root, False)["auth"], "google_cloud")


if __name__ == "__main__":
    unittest.main()
