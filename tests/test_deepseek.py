import argparse
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nightweaver.cli import plan_goal
from nightweaver.providers import ProviderUnavailable, adapter, advisory, configure_deepseek_key, deepseek_key, invoke, metadata
from nightweaver.readiness import check_route
from nightweaver.spec import IMPLEMENTERS, provider
from nightweaver.store import Store


class DeepSeekTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="Nightweaver provider ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key_file = self.root / "model credential.txt"
        self.key_file.write_text("test-credential-value\n", encoding="utf-8-sig")
        self.environment = patch.dict(os.environ, {"NIGHTWEAVER_HOME": str(self.root / "runtime")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_configuration_stores_reference_and_resolves_bom_key(self):
        result = configure_deepseek_key(self.key_file)
        settings = self.root / "runtime" / "providers.json"
        self.assertNotIn("test-credential-value", settings.read_text(encoding="utf-8"))
        self.assertEqual(result["api_key_file"], str(self.key_file.resolve()))
        self.assertEqual(deepseek_key({}), "test-credential-value")

    def test_explicit_effort_on_new_and_resumed_model_routes(self):
        for kind, effort in (("claude", "xhigh"), ("codex", "xhigh"), ("deepseek", "max")):
            with self.subTest(kind=kind):
                route = {"kind": kind, "effort": effort}
                if kind == "deepseek":
                    route["api_key_file"] = str(self.key_file)
                validated = provider(route, IMPLEMENTERS, "worker")
                for session in (None, "prior-session"):
                    argv, env = adapter(validated, "worker", session)
                    if kind == "codex":
                        self.assertIn('model_reasoning_effort="xhigh"', argv)
                    else:
                        self.assertEqual(argv[argv.index("--effort") + 1], effort)
                        self.assertEqual(env["CLAUDE_CODE_EFFORT_LEVEL"], effort)
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            provider({"kind": "deepseek", "effort": "xhigh"}, IMPLEMENTERS, "reviewer")

    def test_reviewer_reads_structured_verdict_with_prose_summary(self):
        path = self.root / "review.jsonl"
        verdict = {"verdict": "REVISE", "reason": "Phone screenshot is missing"}
        path.write_text(json.dumps({"type": "result", "result": "Review complete.", "structured_output": verdict}),
                        encoding="utf-8")
        self.assertEqual(json.loads(metadata(path, "deepseek", "reviewer")[1]), verdict)
        self.assertEqual(metadata(path, "deepseek", "worker")[1], "Review complete.")

    @unittest.skipUnless(os.name == "nt", "Windows temp roots")
    def test_codex_worker_uses_a_private_temp_inside_its_normal_sandbox(self):
        scratch = []
        def finish(argv, cwd, log, timeout, **kwargs):
            self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
            env = kwargs["env"]
            self.assertEqual(env["TMP"], env["TEMP"])
            self.assertEqual(env["TMPDIR"], env["TEMP"])
            scratch.append(Path(env["TEMP"]))
            self.assertTrue(scratch[0].is_dir())
            log.write_text('{"type":"turn.completed"}', encoding="utf-8")
            return 0
        with patch("nightweaver.providers.execute", side_effect=finish):
            invoke({"kind": "codex", "executable": sys.executable}, "worker", self.root, self.root/"work.jsonl",
                   "verify", 30, lambda: False)
        self.assertFalse(scratch[0].exists())

    def test_local_thinking_settings_and_inference_receipt(self):
        cfg = {"model": "test-model", "port": 11435, "think": "xhigh", "max_tokens": 4096}
        result = {"model": "test-model:latest", "done_reason": "stop", "eval_count": 8,
                  "message": {"content": "READY", "thinking": "synthetic trace"}}
        receipt = {}
        show = b'{"thinking":{"values":[false,"low","medium","xhigh"]}}'
        with patch("nightweaver.providers.urlopen", side_effect=[io.BytesIO(show), io.BytesIO(json.dumps(result).encode())]) as post:
            self.assertEqual(advisory(cfg, "test", receipt=receipt), "READY")
        payload = json.loads(post.call_args.args[0].data)
        self.assertEqual(payload["think"], "xhigh")
        self.assertGreater(payload["options"]["num_ctx"], payload["options"]["num_predict"])
        self.assertTrue(receipt["thinking_observed"])
        self.assertNotIn("synthetic trace", json.dumps(receipt))
        with patch("nightweaver.providers.urlopen", return_value=io.BytesIO(show)), self.assertRaisesRegex(ValueError, "supports thinking"):
            advisory({**cfg, "think": True}, "test")

    def test_adapter_keeps_routing_credentials_and_profile_process_local(self):
        os.environ.update({"ANTHROPIC_BASE_URL": "https://existing.invalid", "ANTHROPIC_API_KEY": "existing-key",
                           "CLAUDE_CONFIG_DIR": "existing-profile", "CLAUDE_CODE_OAUTH_TOKEN": "existing-login",
                           "CLAUDE_CODE_USE_VERTEX": "1"})
        before = dict(os.environ)
        argv, env = adapter({"kind": "deepseek", "api_key_file": str(self.key_file)}, "worker", "prior-session")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-flash")
        self.assertEqual(argv[argv.index("--resume") + 1], "prior-session")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-credential-value")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.root / "runtime" / "profiles" / "deepseek"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", env)
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn("test-credential-value", json.dumps(argv))

    def test_missing_key_blocks_before_starting_worker(self):
        with self.assertRaises(ProviderUnavailable):
            adapter({"kind": "deepseek"}, "worker")

    def test_file_override_and_environment_credentials(self):
        os.environ["DEEPSEEK_API_KEY"] = "environment-credential"
        self.assertEqual(deepseek_key({}), "environment-credential")
        self.assertEqual(deepseek_key({"api_key_file": str(self.key_file)}), "test-credential-value")
        with self.assertRaisesRegex(ValueError, "api_key_file"):
            provider({"kind": "deepseek", "api_key": "inline-value"}, IMPLEMENTERS, "worker")

    def test_reviewer_receives_read_tools_and_evidence_directory(self):
        log = self.root / "review.jsonl"
        evidence = self.root / "evidence"

        def finished(argv, cwd, output, timeout, **kwargs):
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--add-dir") + 1], str(evidence))
            output.write_text('{"type":"system","session_id":"review-session"}\n'
                              '{"type":"result","result":"verified"}\n', encoding="utf-8")
            return 0

        with patch("nightweaver.providers.execute", side_effect=finished):
            result = invoke({"kind": "deepseek", "api_key_file": str(self.key_file), "executable": sys.executable}, "reviewer",
                            self.root, log, "Review the candidate", 30, lambda: False, evidence=evidence)
        self.assertEqual(result, (0, "review-session", "verified"))

    def test_api_error_blocks_with_recovery_message(self):
        log = self.root / "worker.jsonl"
        log.write_text('{"type":"result","result":"Authentication Fails (invalid API key)"}\n', encoding="utf-8")
        with patch("nightweaver.providers.execute", return_value=1):
            with self.assertRaisesRegex(ProviderUnavailable, "credential or balance"):
                invoke({"kind": "deepseek", "api_key_file": str(self.key_file), "executable": sys.executable}, "worker",
                       self.root, log, "Implement", 30, lambda: False)

    def test_oneshot_routes_to_deepseek_and_preserves_checks(self):
        template = self.root / "plan.json"
        check = {"name": "Existing contract", "argv": [sys.executable, "check.py"]}
        final = {"name": "Delivery contract", "argv": [sys.executable, "check.py", "--final"]}
        template.write_text(json.dumps({"repository": str(self.root), "title": "A project",
                                        "worker": {"kind": "deepseek"}, "gates": [check],
                                        "final_gates": [final], "acceptance": ["Deliver the final artifact."]}),
                            encoding="utf-8")
        args = argparse.Namespace(template=str(template), objective="Deliver the project", planner="deepseek",
                                  model="deepseek-v4-pro", profile_dir=None, start=False, enqueue=False)
        answer = json.dumps({"milestones": [{"title": "Deliver", "task": "Implement the full objective"}]})
        with patch("nightweaver.cli.invoke", return_value=(0, "planner-session", answer)) as planner:
            result = plan_goal(Store(self.root / "runtime"), args)
        self.assertEqual(planner.call_args.args[0], {"kind": "deepseek", "model": "deepseek-v4-pro"})
        self.assertEqual(result["gates"], [check])
        self.assertEqual(result["final_gates"], [final])
        self.assertIn("Deliver the final artifact.", planner.call_args.args[4])
        self.assertEqual(result["worker"], {"kind": "deepseek"})

    def test_readiness_checks_the_selected_profile_before_probing(self):
        store = Store(self.root / "runtime")
        config = {"kind": "claude", "executable": sys.executable,
                  "profile_dir": str(self.root / "deleted profile")}
        with patch("nightweaver.readiness.invoke") as probe:
            result = check_route(store, config, self.root, live=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "missing_profile")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
