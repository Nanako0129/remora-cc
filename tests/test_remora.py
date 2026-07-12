from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("remora", ROOT / "src" / "remora.py")
assert SPEC and SPEC.loader
remora = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remora)


class RemoraTests(unittest.TestCase):
    def setUp(self) -> None:
        with (ROOT / "config.example.toml").open("rb") as handle:
            self.config = remora.tomllib.load(handle)
        remora.validate_config(self.config)

    def test_role_map_matches_pilotfish_style_split(self) -> None:
        agents = remora.render_agents(self.config)
        self.assertEqual(agents["scout"]["model"], "gpt-5.6-luna")
        self.assertEqual(agents["mech-executor"]["model"], "gpt-5.6-luna")
        self.assertEqual(agents["executor"]["model"], "gpt-5.6-luna")
        self.assertEqual(agents["executor"]["effort"], "max")
        self.assertEqual(agents["verifier"]["effort"], "high")
        self.assertIn("Agent", agents["executor"]["disallowedTools"])

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret", "CLAUDE_CODE_SUBAGENT_MODEL": "wrong"}, clear=False)
    def test_launch_is_session_scoped_and_clears_global_override(self) -> None:
        command, env = remora.build_launch(self.config, ["--continue"])
        self.assertEqual(command[0], "claude")
        self.assertEqual(command[1:3], ["--model", "gpt-5.6-sol"])
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-5.6-terra")
        self.assertIn("--agents", command)
        payload = json.loads(command[command.index("--agents") + 1])
        self.assertEqual(payload["scout"]["model"], "gpt-5.6-luna")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-secret")
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(os.environ["CLAUDE_CODE_SUBAGENT_MODEL"], "wrong")

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=False)
    def test_explicit_claude_flags_win(self) -> None:
        custom = '{"mine":{"description":"x","prompt":"y"}}'
        command, _ = remora.build_launch(
            self.config, ["--model", "custom-main", "--agents", custom]
        )
        self.assertEqual(command.count("--model"), 1)
        self.assertEqual(command.count("--agents"), 1)
        self.assertIn("custom-main", command)
        self.assertIn(custom, command)

    def test_token_command_is_executed_without_a_shell(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["proxy"]["auth_token_env"] = "REMORA_TEST_MISSING"
        config["proxy"]["auth_token_command"] = ["printf", "command-secret"]
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(remora.resolve_auth_token(config), "command-secret")

    def test_config_override_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text((ROOT / "config.example.toml").read_text(), encoding="utf-8")
            with mock.patch.dict(os.environ, {"REMORA_CONFIG": str(path)}):
                self.assertEqual(remora.config_path(), path)
                self.assertEqual(remora.load_config()["models"]["main"], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
