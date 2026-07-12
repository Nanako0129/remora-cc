from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
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
        with mock.patch.object(
            remora,
            "fetch_gateway_context_windows",
            return_value={
                "gpt-5.6-sol": 372000,
                "gpt-5.6-terra": 372000,
                "gpt-5.6-luna": 372000,
            },
        ):
            command, env = remora.build_launch(self.config, ["--continue"])
        self.assertEqual(command[0], "claude")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(
            settings["availableModels"],
            ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
        )
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-5.6-terra")
        self.assertIn("--agents", command)
        payload = json.loads(command[command.index("--agents") + 1])
        self.assertEqual(payload["scout"]["model"], "gpt-5.6-luna")
        policy = command[command.index("--append-system-prompt") + 1]
        self.assertIn("run_in_background: true", policy)
        self.assertIn("Use foreground execution only", policy)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-secret")
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)
        self.assertNotIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", env)
        self.assertNotIn("CALICO_MODEL_CONTEXT_WINDOWS", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(os.environ["CLAUDE_CODE_SUBAGENT_MODEL"], "wrong")

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=False)
    def test_explicit_claude_flags_win(self) -> None:
        custom = '{"mine":{"description":"x","prompt":"y"}}'
        command, _ = remora.build_launch(
            self.config,
            [
                "--model",
                "custom-main",
                "--agents",
                custom,
                "--append-system-prompt",
                "custom policy",
            ],
        )
        self.assertEqual(command.count("--model"), 1)
        self.assertEqual(command.count("--agents"), 1)
        self.assertIn("custom-main", command)
        self.assertIn(custom, command)
        self.assertEqual(command.count("--append-system-prompt"), 1)
        self.assertIn("custom policy", command)

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=False)
    def test_explicit_append_system_prompt_file_wins(self) -> None:
        command, _ = remora.build_launch(
            self.config, ["--append-system-prompt-file", "policy.md"]
        )
        self.assertNotIn("--append-system-prompt", command)
        self.assertEqual(command.count("--append-system-prompt-file"), 1)

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_explicit_settings_fail_closed_instead_of_disabling_routing(self) -> None:
        with self.assertRaisesRegex(remora.RemoraError, "cannot be combined"):
            remora.build_launch(self.config, ["--settings", "custom.json"])

    def test_routing_settings_allow_every_configured_model(self) -> None:
        self.assertEqual(
            remora.routing_settings(self.config),
            {
                "availableModels": [
                    "gpt-5.6-luna",
                    "gpt-5.6-sol",
                    "gpt-5.6-terra",
                ]
            },
        )

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_launch_marks_only_the_child_as_remora(self) -> None:
        self.assertNotIn("REMORA_ACTIVE", os.environ)
        _, env = remora.build_launch(self.config, [])
        self.assertEqual(env["REMORA_ACTIVE"], "1")
        self.assertNotIn("REMORA_ACTIVE", os.environ)

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

    def test_context_policy_uses_safe_fallback_offline(self) -> None:
        policy = remora.resolve_context_policy(self.config)
        self.assertEqual(policy["source"], "fallback")
        self.assertEqual(policy["provider_window"], 372000)
        self.assertEqual(policy["client_window"], 200000)
        self.assertEqual(policy["effective_window"], 200000)
        self.assertIsNone(policy["auto_compact_window"])
        self.assertIsNone(policy["auto_compact_percent"])
        self.assertIsNone(policy["compact_trigger"])

    def test_context_policy_uses_smallest_configured_gateway_window(self) -> None:
        windows = {
            "gpt-5.6-sol": 1050000,
            "gpt-5.6-terra": 500000,
            "gpt-5.6-luna": 372000,
        }
        with mock.patch.object(
            remora, "fetch_gateway_context_windows", return_value=windows
        ):
            policy = remora.resolve_context_policy(
                self.config, token="hidden", online=True
            )
        self.assertEqual(policy["source"], "gateway")
        self.assertEqual(policy["provider_window"], 372000)
        self.assertEqual(policy["effective_window"], 200000)
        self.assertIsNone(policy["compact_trigger"])

    def test_context_policy_falls_back_when_discovery_fails(self) -> None:
        with mock.patch.object(
            remora, "fetch_gateway_context_windows", side_effect=OSError("offline")
        ):
            policy = remora.resolve_context_policy(
                self.config, token="hidden", online=True
            )
        self.assertEqual(policy["source"], "fallback")
        self.assertIsNone(policy["auto_compact_window"])
        self.assertIsNone(policy["compact_trigger"])
        self.assertIn("discovery failed", policy["warning"])

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_calico_mode_uses_provider_window_and_exports_exact_map(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        windows = {
            "gpt-5.6-sol": 372000,
            "gpt-5.6-terra": 372000,
            "gpt-5.6-luna": 372000,
        }
        with (
            mock.patch.object(
                remora, "fetch_gateway_context_windows", return_value=windows
            ),
            mock.patch.object(remora, "calico_context_supported", return_value=True),
        ):
            _, env = remora.build_launch(config, [])
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "372000")
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
        self.assertEqual(env["CALICO_CONTEXT_DISPLAY_PERCENT"], "95")
        self.assertEqual(
            json.loads(env["CALICO_MODEL_CONTEXT_WINDOWS"]), windows
        )

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_calico_mode_fails_closed_without_context_patch(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        with mock.patch.object(remora, "calico_context_supported", return_value=False):
            with self.assertRaisesRegex(remora.RemoraError, "requires a Calico"):
                remora.build_launch(config, [])

    def test_calico_active_turn_marker_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "claude"
            binary.write_bytes(
                b"calico-active-turn-adapter:v1 x-calico-prompt-id "
                b"x-calico-active-turn-version"
            )
            binary.chmod(0o755)
            with mock.patch.object(remora.shutil, "which", return_value=str(binary)):
                self.assertTrue(remora.calico_active_turn_supported("claude"))

    def test_calico_active_turn_marker_is_unavailable_when_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "claude"
            binary.write_bytes(b"stock-claude")
            binary.chmod(0o755)
            with mock.patch.object(remora.shutil, "which", return_value=str(binary)):
                self.assertFalse(remora.calico_active_turn_supported("claude"))

    def test_gateway_active_turn_capability_header(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {remora.GATEWAY_ACTIVE_TURN_HEADER: "1"}
        with mock.patch.object(remora.urllib.request, "urlopen", return_value=response):
            self.assertTrue(
                remora.gateway_active_turn_supported(self.config, "hidden-token")
            )

    def test_context_policy_uses_fallback_for_missing_model_metadata(self) -> None:
        with mock.patch.object(
            remora,
            "fetch_gateway_context_windows",
            return_value={"gpt-5.6-sol": 1050000},
        ):
            policy = remora.resolve_context_policy(
                self.config, token="hidden", online=True
            )
        self.assertEqual(policy["source"], "gateway+fallback")
        self.assertEqual(policy["provider_window"], 372000)
        self.assertIn("gpt-5.6-luna", policy["warning"])

    @mock.patch.dict(
        os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"}, clear=False
    )
    def test_explicit_auto_compact_environment_wins(self) -> None:
        with mock.patch.object(
            remora,
            "fetch_gateway_context_windows",
            return_value={
                "gpt-5.6-sol": 372000,
                "gpt-5.6-terra": 372000,
                "gpt-5.6-luna": 372000,
            },
        ):
            policy = remora.resolve_context_policy(
                self.config, token="hidden", online=True
            )
        self.assertEqual(policy["source"], "gateway+environment")
        self.assertEqual(policy["auto_compact_window"], 300000)
        self.assertIsNone(policy["compact_trigger"])

    @mock.patch.dict(
        os.environ, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "85"}, clear=False
    )
    def test_explicit_auto_compact_percentage_wins(self) -> None:
        policy = remora.resolve_context_policy(self.config)
        self.assertEqual(policy["source"], "fallback+environment")
        self.assertEqual(policy["auto_compact_percent"], 85)
        self.assertIsNone(policy["compact_trigger"])

    @mock.patch.dict(
        os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, clear=False
    )
    @mock.patch.object(remora, "resolve_auth_token", return_value="hidden")
    @mock.patch.object(remora.shutil, "which", return_value="/usr/bin/claude")
    @mock.patch.object(
        remora,
        "fetch_gateway_context_windows",
        return_value={
            "gpt-5.6-sol": 372000,
            "gpt-5.6-terra": 372000,
            "gpt-5.6-luna": 372000,
        },
    )
    @mock.patch.object(remora.urllib.request, "urlopen")
    def test_doctor_warns_when_override_exceeds_gateway(
        self,
        urlopen: mock.Mock,
        _fetch: mock.Mock,
        _which: mock.Mock,
        _token: mock.Mock,
    ) -> None:
        response = mock.MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response
        output = io.StringIO()
        with redirect_stdout(output):
            result = remora.doctor(self.config, online=True)
        self.assertEqual(result, 0)
        self.assertIn("exceeds detected gateway ceiling 372000", output.getvalue())

    def test_invalid_context_config_is_rejected(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["auto_compact_percent"] = 96
        with self.assertRaisesRegex(remora.RemoraError, "must not exceed"):
            remora.validate_config(config)

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "do-not-print"}, clear=False)
    def test_dry_run_never_prints_gateway_token(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            remora.dry_run(self.config, [])
        self.assertNotIn("do-not-print", output.getvalue())
        self.assertNotIn('"CLAUDE_CODE_AUTO_COMPACT_WINDOW"', output.getvalue())
        self.assertNotIn('"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
