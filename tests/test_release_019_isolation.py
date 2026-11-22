import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from nightweaver import providers


class ClaudeIsolation(unittest.TestCase):
    """Unattended Claude routes must not inherit the operator's interactive CLAUDE.md, skills or settings."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        base = {k: v for k, v in os.environ.items()
                if k not in ("CLAUDE_CONFIG_DIR", "NIGHTWEAVER_CLAUDE_SHARED_CONFIG", providers.OAUTH_VARIABLE)}
        self.env = mock.patch.dict(os.environ, {**base, "NIGHTWEAVER_HOME": str(self.home)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registry = mock.patch.object(providers, "user_environment", return_value=None)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def config_dir(self, config):
        return providers.adapter({"kind": "claude", **config}, "worker")[1].get("CLAUDE_CONFIG_DIR")

    def test_token_isolates_into_nightweaver_profile(self):
        os.environ[providers.OAUTH_VARIABLE] = "sk-ant-oat01-" + "x" * 40
        expected = self.home / "profiles" / "claude"
        self.assertEqual(self.config_dir({}), str(expected))
        self.assertTrue(expected.is_dir())
        self.assertEqual(providers.describe_provider({"kind": "claude"})["profile_dir"], str(expected.resolve()))

    def test_explicit_choices_win(self):
        os.environ[providers.OAUTH_VARIABLE] = "sk-ant-oat01-" + "x" * 40
        chosen = str(self.home / "chosen")
        self.assertEqual(self.config_dir({"profile_dir": chosen}), chosen)
        with mock.patch.dict(os.environ, {"NIGHTWEAVER_CLAUDE_SHARED_CONFIG": "1"}):
            self.assertIsNone(self.config_dir({}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": chosen}):
            self.assertEqual(self.config_dir({}), chosen)

    def test_cli_login_without_token_keeps_the_shared_profile(self):
        self.assertIsNone(self.config_dir({}))

    def test_other_providers_are_untouched(self):
        os.environ[providers.OAUTH_VARIABLE] = "sk-ant-oat01-" + "x" * 40
        codex = providers.adapter({"kind": "codex"}, "worker")[1]
        self.assertNotIn("CLAUDE_CONFIG_DIR", codex)
        self.assertNotIn(providers.OAUTH_VARIABLE, codex)


if __name__ == "__main__":
    unittest.main()
