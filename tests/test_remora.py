from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("remora", ROOT / "src" / "remora.py")
assert SPEC and SPEC.loader
remora = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remora)


def launch_settings(command: list[str]) -> dict[str, object]:
    value = command[command.index("--settings") + 1]
    return json.loads(value) if value.startswith("{") else json.loads(Path(value).read_text())


class RemoraTests(unittest.TestCase):
    def setUp(self) -> None:
        with (ROOT / "config.example.toml").open("rb") as handle:
            self.config = remora.tomllib.load(handle)
        remora.validate_config(self.config)

    def test_role_map_matches_pilotfish_style_split(self) -> None:
        agents = remora.render_agents(self.config)
        self.assertEqual(
            set(agents),
            {
                "Explore",
                "scout",
                "plan-verifier",
                "security-reviewer",
                "mech-executor",
                "executor",
                "verifier",
                "security-executor",
            },
        )
        self.assertEqual(agents["scout"]["model"], "gpt-5.6-luna")
        self.assertEqual(agents["plan-verifier"]["model"], "gpt-5.6-sol")
        self.assertEqual(agents["plan-verifier"]["effort"], "medium")
        self.assertEqual(agents["plan-verifier"]["tools"], ["Read", "Glob", "Grep"])
        self.assertEqual(agents["security-reviewer"]["model"], "gpt-5.6-sol")
        self.assertEqual(agents["security-reviewer"]["effort"], "high")
        self.assertIn("WebSearch", agents["security-reviewer"]["tools"])
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
        self.assertTrue(command[command.index("--settings") + 1].startswith("{"))
        settings = launch_settings(command)
        self.assertEqual(settings["fallbackModel"], [])
        self.assertEqual(
            settings["availableModels"],
            ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
        )
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-5.6-terra")
        self.assertIn("--agents", command)
        payload = json.loads(command[command.index("--agents") + 1])
        self.assertEqual(payload["scout"]["model"], "gpt-5.6-luna")
        self.assertIn("plan-verifier", payload)
        self.assertIn("security-reviewer", payload)
        self.assertNotIn("_routing_fallback", payload["plan-verifier"])
        policy = command[command.index("--append-system-prompt") + 1]
        self.assertIn("run_in_background: true", policy)
        self.assertIn("Use foreground execution only", policy)
        self.assertIn("omit the `model` argument entirely", policy)
        self.assertIn("only for a truly ad-hoc agent", policy)
        self.assertIn("apply that phase's dispatch brake", policy)
        self.assertIn("net benefit remains positive", policy)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-secret")
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)
        self.assertNotIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", env)
        self.assertNotIn("CALICO_MODEL_CONTEXT_WINDOWS", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(os.environ["CLAUDE_CODE_SUBAGENT_MODEL"], "wrong")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_fast_mode_adds_priority_body_without_parent_mutation(self) -> None:
        _, env = remora.build_launch(self.config, [], require_token=False, fast=True)
        self.assertEqual(
            json.loads(env[remora.CLAUDE_EXTRA_BODY_ENV]),
            {"service_tier": "priority"},
        )
        self.assertNotIn(remora.CLAUDE_EXTRA_BODY_ENV, os.environ)

    @mock.patch.dict(
        os.environ,
        {remora.CLAUDE_EXTRA_BODY_ENV: " \t\n "},
        clear=True,
    )
    def test_fast_mode_treats_blank_body_as_empty(self) -> None:
        _, env = remora.build_launch(self.config, [], require_token=False, fast=True)
        self.assertEqual(
            json.loads(env[remora.CLAUDE_EXTRA_BODY_ENV]),
            {"service_tier": "priority"},
        )

    def test_fast_mode_merges_inherited_body_and_preserves_parent(self) -> None:
        inherited = json.dumps(
            {"sentinel": "keep-me", "nested": {"enabled": True}, "service_tier": "fast"}
        )
        with mock.patch.dict(
            os.environ,
            {remora.CLAUDE_EXTRA_BODY_ENV: inherited},
            clear=True,
        ):
            _, env = remora.build_launch(
                self.config, [], require_token=False, fast=True
            )
            self.assertEqual(
                json.loads(env[remora.CLAUDE_EXTRA_BODY_ENV]),
                {
                    "sentinel": "keep-me",
                    "nested": {"enabled": True},
                    "service_tier": "priority",
                },
            )
            self.assertEqual(os.environ[remora.CLAUDE_EXTRA_BODY_ENV], inherited)

    def test_fast_mode_accepts_and_normalizes_fast_or_priority(self) -> None:
        for service_tier in ("fast", "priority"):
            with self.subTest(service_tier=service_tier):
                with mock.patch.dict(
                    os.environ,
                    {
                        remora.CLAUDE_EXTRA_BODY_ENV: json.dumps(
                            {"service_tier": service_tier}
                        )
                    },
                    clear=True,
                ):
                    _, env = remora.build_launch(
                        self.config, [], require_token=False, fast=True
                    )
                self.assertEqual(
                    json.loads(env[remora.CLAUDE_EXTRA_BODY_ENV])["service_tier"],
                    "priority",
                )

    def test_fast_mode_rejects_malformed_non_object_and_conflicting_body(self) -> None:
        cases = [
            ("{malformed", "valid JSON"),
            ('{"value":NaN}', "valid JSON"),
            ('{"value":Infinity}', "valid JSON"),
            ('{"value":-Infinity}', "valid JSON"),
            ('{"nested":{"value":NaN}}', "valid JSON"),
            ('{"value":1e9999}', "valid JSON"),
            ('{"service_tier":"standard","service_tier":"fast"}', "valid JSON"),
            ('{"nested":{"value":1,"value":2}}', "valid JSON"),
            ("[]", "JSON object"),
            (json.dumps({"service_tier": "standard"}), "conflicts"),
            (json.dumps({"service_tier": 1}), "conflicts"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw):
                with mock.patch.dict(
                    os.environ,
                    {remora.CLAUDE_EXTRA_BODY_ENV: raw},
                    clear=True,
                ):
                    with self.assertRaisesRegex(remora.RemoraError, message):
                        remora.build_launch(
                            self.config, [], require_token=False, fast=True
                        )

    def test_default_mode_passes_inherited_body_through_without_parsing(self) -> None:
        inherited = "not-json-and-should-stay-opaque"
        with mock.patch.dict(
            os.environ,
            {remora.CLAUDE_EXTRA_BODY_ENV: inherited},
            clear=True,
        ):
            _, env = remora.build_launch(self.config, [], require_token=False)
        self.assertEqual(env[remora.CLAUDE_EXTRA_BODY_ENV], inherited)

    def test_live_fast_flag_is_stripped_before_forwarding(self) -> None:
        with (
            mock.patch.object(remora, "load_config", return_value=self.config),
            mock.patch.object(
                remora, "build_launch", return_value=(["claude"], {})
            ) as build,
            mock.patch.object(remora, "prepare_coralline_config"),
            mock.patch.object(remora.os, "execvpe"),
        ):
            self.assertEqual(remora.main(["--fast", "--continue", "prompt"]), 0)
        build.assert_called_once_with(
            self.config, ["--continue", "prompt"], fast=True
        )

    def test_fast_flag_rejects_leading_remora_builtin(self) -> None:
        with mock.patch.object(remora, "load_config") as load:
            self.assertEqual(remora.main(["--fast", "doctor"]), 2)
        load.assert_not_called()

    def test_dry_run_fast_flag_is_stripped_and_body_is_redacted(self) -> None:
        inherited = json.dumps({"sentinel": "do-not-print", "nested": {"x": 1}})
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {remora.CLAUDE_EXTRA_BODY_ENV: inherited},
            clear=True,
        ), redirect_stdout(output):
            remora.dry_run(self.config, ["--fast", "--continue"])
        payload = json.loads(output.getvalue())
        self.assertNotIn("--fast", payload["command"])
        self.assertEqual(payload["command"][-1], "--continue")
        self.assertEqual(
            json.loads(payload["environment"][remora.CLAUDE_EXTRA_BODY_ENV]),
            {"service_tier": "priority"},
        )
        self.assertNotIn("do-not-print", output.getvalue())
        self.assertNotIn("nested", output.getvalue())

    def test_dry_run_fast_flag_rejects_leading_remora_builtin(self) -> None:
        with self.assertRaisesRegex(remora.RemoraError, "cannot be combined"):
            remora.dry_run(self.config, ["--fast", "agents"])

    @mock.patch.dict(
        os.environ,
        {remora.CLAUDE_EXTRA_BODY_ENV: '{"value":1e9999}'},
        clear=True,
    )
    def test_main_dry_run_fast_fails_closed_on_overflowing_number(self) -> None:
        with mock.patch.object(remora, "load_config", return_value=self.config):
            self.assertEqual(remora.main(["dry-run", "--fast"]), 2)

    def test_named_roles_are_the_only_source_of_their_models(self) -> None:
        policy = remora.load_orchestration_policy()
        for role in remora.load_agent_definitions():
            self.assertIn(f"`{role}`", policy)
        self.assertIn("existing named role", policy)
        self.assertIn("invocation-level model overrides the role definition", policy)
        self.assertIn("ad-hoc agent that has no named role definition", policy)

    def test_policy_composes_with_delegation_planning_skills(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("delegation-planning skill such as Baton", policy)
        self.assertIn("may shape discovery questions", policy)
        self.assertIn("execution topology", policy)
        self.assertIn("This policy remains the source", policy)
        self.assertIn("named roles", policy)
        self.assertIn("model routing", policy)
        self.assertIn("leaf-agent boundary", policy)
        self.assertIn("approval gate", policy)
        self.assertIn("verification contract", policy)
        self.assertIn("The two layers compose", policy)

    def test_policy_has_a_plan_first_two_turn_lifecycle(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("| Discovery |", policy)
        self.assertIn("final outcome and implementation Plan may still be unknown", policy)
        self.assertIn("| Plan |", policy)
        self.assertIn("main session synthesizes one Plan", policy)
        self.assertIn("returning only `READY` or `REVISE`", policy)
        self.assertIn("tool-enforced read-only `plan-verifier`", policy)
        self.assertIn("| Approval |", policy)
        self.assertIn("wait for explicit user approval", policy)
        self.assertIn("Do not send an implementation brief or edit source", policy)
        self.assertIn("| Execution |", policy)
        self.assertIn("approved or otherwise authorized contract", policy)
        self.assertIn("| Verification |", policy)
        self.assertIn("returns only `CONFIRMED` or `REFUTED`", policy)
        self.assertIn("request only `READY` or `REVISE`", policy)
        self.assertIn("not Plan-readiness labels", policy)

    def test_plan_and_outcome_verifiers_have_separate_capabilities(self) -> None:
        plan_verifier = remora.load_agent_definitions()["plan-verifier"]
        verifier = remora.load_agent_definitions()["verifier"]
        self.assertIn("READY", plan_verifier["prompt"])
        self.assertIn("REVISE", plan_verifier["prompt"])
        self.assertEqual(plan_verifier["tools"], ["Read", "Glob", "Grep"])
        self.assertNotIn("Plan readiness", verifier["prompt"])
        self.assertIn("exactly CONFIRMED or REFUTED", verifier["prompt"])
        self.assertIn("Write", verifier["disallowedTools"])
        self.assertIn("Agent", verifier["disallowedTools"])

    def test_security_review_and_execution_have_separate_capabilities(self) -> None:
        definitions = remora.load_agent_definitions()
        reviewer = definitions["security-reviewer"]
        executor = definitions["security-executor"]
        self.assertEqual(
            reviewer["tools"], ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
        )
        self.assertIn("Never execute commands", reviewer["prompt"])
        self.assertIn("pre-approval evidence belongs to security-reviewer", executor["prompt"])

    def test_pre_eight_role_config_uses_review_role_routing_fallbacks(self) -> None:
        legacy = {
            **self.config,
            "agent_models": dict(self.config["agent_models"]),
            "agent_effort": dict(self.config["agent_effort"]),
        }
        for section in ("agent_models", "agent_effort"):
            legacy[section].pop("plan-verifier")
            legacy[section].pop("security-reviewer")
        remora.validate_config(legacy)
        agents = remora.render_agents(legacy)
        self.assertEqual(agents["plan-verifier"]["model"], agents["verifier"]["model"])
        self.assertEqual(
            agents["security-reviewer"]["model"],
            agents["security-executor"]["model"],
        )

    def test_policy_keeps_tightly_coupled_exploration_in_main_session(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("workers would repeatedly depend", policy)
        self.assertIn("main session's evolving evidence", policy)
        self.assertIn("root-cause discovery", policy)
        self.assertIn("trace-driven debugging", policy)
        self.assertIn("tightly coupled state propagation", policy)
        self.assertIn("single unknown bug", policy)
        self.assertIn("sequential `scout` → `executor` pipeline", policy)
        self.assertIn("does not own or block the main diagnosis", policy)
        self.assertIn("asking the worker to rediscover the investigation", policy)
        self.assertIn("eligible rather than mandatory", policy)

    def test_policy_uses_backend_neutral_recurrence_conditions(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("recurrence requires a stable brief", policy)
        self.assertIn("one-shot brief can completely describe", policy)
        self.assertIn("remaining items are independent and the same shape", policy)
        self.assertIn("Delegation is conditional, not mandatory", policy)
        self.assertIn("per-item triage, exceptions, integration, and acceptance", policy)
        self.assertIn("already-diagnosed review finding with a known remedy", policy)
        self.assertIn("Execution work, not an unknown-bug discovery task", policy)
        self.assertNotIn("about three times", policy)
        self.assertNotIn("plan documents", policy)

    def test_policy_verifies_at_a_coherent_boundary(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("smallest coherent integration boundary", policy)
        self.assertIn("Tests, builds, and static checks are intermediate evidence", policy)
        self.assertIn("not a universal substitute for fresh verification", policy)
        self.assertIn("cross-language or FFI seam", policy)
        self.assertIn("serialization or pre-aggregation data boundary", policy)
        self.assertIn("irreversible operation", policy)
        self.assertNotIn("feature or PR closure", policy)

    def test_policy_requires_plan_convergence_or_escalation(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("Do not resubmit a substantially unchanged Plan", policy)
        self.assertIn("material revision or new evidence", policy)
        self.assertIn("simplify it", policy)
        self.assertIn("surface the blocker to the user", policy)
        self.assertIn("defer the blocked scope", policy)
        self.assertIn("Never silently overrule", policy)
        self.assertNotIn("two `REVISE` rounds per Plan", policy)

    def test_completed_recon_output_is_collected_without_rerunning(self) -> None:
        policy = remora.load_orchestration_policy()
        agents = remora.load_agent_definitions()
        self.assertIn("final message is the deliverable for that run", policy)
        self.assertIn("result collection and continuation are separate operations", policy)
        self.assertIn("never resume or re-dispatch a finished agent", policy)
        for name in ("Explore", "scout"):
            with self.subTest(name=name):
                prompt = agents[name]["prompt"]
                self.assertIn("final message for each run", prompt)
                self.assertIn("only result the orchestrator receives", prompt)
                self.assertIn("genuinely new follow-up work", prompt)
                self.assertIn("do not repeat a completed", prompt)

    def test_policy_preserves_positive_delegation_paths(self) -> None:
        policy = remora.load_orchestration_policy()
        self.assertIn("choose by net benefit", policy)
        self.assertIn("lower model cost or quota use", policy)
        self.assertIn("preserving scarce main-session context", policy)
        self.assertIn("direct execution being slightly faster is not a veto", policy)
        self.assertIn("choose the smallest read-only structure", policy)
        self.assertIn("genuinely independent and substantial", policy)
        self.assertIn("external or tool latency can overlap", policy)
        self.assertIn("across separate directories", policy)
        self.assertIn("only duplicate startup and synthesis", policy)
        self.assertIn("stable multi-file repetition", policy)

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "0"},
        clear=False,
    )
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

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "0"},
        clear=False,
    )
    def test_explicit_append_system_prompt_file_wins(self) -> None:
        command, _ = remora.build_launch(
            self.config, ["--append-system-prompt-file", "policy.md"]
        )
        self.assertNotIn("--append-system-prompt", command)
        self.assertEqual(command.count("--append-system-prompt-file"), 1)

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "0"},
        clear=True,
    )
    def test_disabled_prompt_composition_preserves_raw_args(self) -> None:
        cases = [
            ["--append-system-prompt", "one", "--append-system-prompt", "two"],
            ["--append-system-prompt-file", "one", "--append-system-prompt-file", "two"],
            ["--append-system-prompt"],
            ["--append-system-prompt="],
            ["--append-system-prompt-file"],
            ["--append-system-prompt-file="],
            [
                "--append-system-prompt",
                "inline",
                "--append-system-prompt-file",
                "policy.md",
            ],
        ]
        for args in cases:
            with self.subTest(args=args):
                command, _ = remora.build_launch(self.config, args)
                self.assertEqual(command[-len(args) :], args)

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "1"},
        clear=True,
    )
    def test_option_extraction_stops_at_double_dash(self) -> None:
        args = ["--", "--settings", "{}", "--append-system-prompt", "literal"]
        command, _ = remora.build_launch(self.config, args)
        self.assertEqual(command[-len(args) :], args)
        self.assertEqual(command.count("--settings"), 2)
        self.assertEqual(command.count("--append-system-prompt"), 2)
        self.assertEqual(
            command[command.index("--append-system-prompt") + 1],
            remora.load_orchestration_policy(),
        )

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "1"},
        clear=True,
    )
    def test_prompt_composition_merges_inline_and_policy(self) -> None:
        policy = remora.load_orchestration_policy()
        for args, caller in (
            (["--append-system-prompt", "happy policy"], "happy policy"),
            (
                ["--append-system-prompt", "- **happy markdown**"],
                "- **happy markdown**",
            ),
            (["--append-system-prompt=happy equals"], "happy equals"),
        ):
            with self.subTest(args=args):
                command, _ = remora.build_launch(self.config, args)
                self.assertEqual(command.count("--append-system-prompt"), 1)
                self.assertEqual(
                    command[command.index("--append-system-prompt") + 1],
                    f"{caller}\n\n{policy}",
                )
                self.assertFalse(any(arg.startswith("--append-system-prompt=") for arg in command))

        empty_command, _ = remora.build_launch(
            self.config, ["--append-system-prompt", ""]
        )
        self.assertEqual(
            empty_command[empty_command.index("--append-system-prompt") + 1], policy
        )

    @mock.patch.dict(
        os.environ,
        {
            "REMORA_AUTH_TOKEN": "test-secret",
            remora.COMPOSE_SYSTEM_PROMPT_ENV: "1",
            remora.CALLER_SYSTEM_PROMPT_ENV: "happy sdk policy",
        },
        clear=True,
    )
    def test_prompt_composition_reads_sdk_bridge_and_strips_child_env(self) -> None:
        policy = remora.load_orchestration_policy()
        command, env = remora.build_launch(self.config, [])
        self.assertEqual(
            command[command.index("--append-system-prompt") + 1],
            f"happy sdk policy\n\n{policy}",
        )
        self.assertNotIn(remora.CALLER_SYSTEM_PROMPT_ENV, env)

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "1"},
        clear=True,
    )
    def test_prompt_composition_reads_file_and_treats_empty_as_no_caller(self) -> None:
        policy = remora.load_orchestration_policy()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "happy-prompt.md"
            path.write_text("happy file policy", encoding="utf-8")
            command, _ = remora.build_launch(
                self.config, ["--append-system-prompt-file", str(path)]
            )
            path.write_text("", encoding="utf-8")
            empty_command, _ = remora.build_launch(
                self.config, ["--append-system-prompt-file", str(path)]
            )
            with chdir(directory):
                hyphen_path = Path("-happy-prompt.md")
                hyphen_path.write_text("hyphen file policy", encoding="utf-8")
                hyphen_command, _ = remora.build_launch(
                    self.config, ["--append-system-prompt-file", str(hyphen_path)]
                )

        self.assertNotIn("--append-system-prompt-file", command)
        self.assertEqual(
            command[command.index("--append-system-prompt") + 1],
            f"happy file policy\n\n{policy}",
        )
        self.assertEqual(
            empty_command[empty_command.index("--append-system-prompt") + 1], policy
        )
        self.assertEqual(
            hyphen_command[hyphen_command.index("--append-system-prompt") + 1],
            f"hyphen file policy\n\n{policy}",
        )

    @mock.patch.dict(
        os.environ,
        {"REMORA_AUTH_TOKEN": "test-secret", remora.COMPOSE_SYSTEM_PROMPT_ENV: "1"},
        clear=True,
    )
    def test_prompt_composition_rejects_invalid_inputs(self) -> None:
        cases = [
            (
                [
                    "--append-system-prompt",
                    "inline",
                    "--append-system-prompt-file",
                    "policy.md",
                ],
                "cannot be combined",
            ),
            (
                ["--append-system-prompt", "one", "--append-system-prompt", "two"],
                "only be specified once",
            ),
            (
                [
                    "--append-system-prompt-file",
                    "one",
                    "--append-system-prompt-file",
                    "two",
                ],
                "only be specified once",
            ),
            (["--append-system-prompt"], "requires a value"),
            (["--append-system-prompt", "--"], "requires a value"),
            (["--append-system-prompt="], "requires a value"),
            (["--append-system-prompt-file"], "requires a value"),
            (["--append-system-prompt-file", "--"], "requires a value"),
            (["--append-system-prompt-file="], "requires a value"),
            (["--append-system-prompt-file", ""], "cannot read"),
            (
                ["--append-system-prompt-file", "/missing/remora-happy-policy.md"],
                "cannot read",
            ),
            (["--append-system-prompt-file", "\x00"], "cannot read"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                with self.assertRaisesRegex(remora.RemoraError, message):
                    remora.build_launch(self.config, args)

        with mock.patch.dict(
            os.environ,
            {remora.CALLER_SYSTEM_PROMPT_ENV: "happy sdk policy"},
            clear=False,
        ):
            for args in (
                ["--append-system-prompt", "inline"],
                ["--append-system-prompt-file", "/missing/policy.md"],
            ):
                with self.subTest(args=args):
                    with self.assertRaisesRegex(remora.RemoraError, "cannot be combined"):
                        remora.build_launch(self.config, args)

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_caller_settings_use_guarded_file_and_replace_fallback(self) -> None:
        secret = "happy-settings-secret"
        fallback_secret = "caller-fallback-secret"
        created_paths: list[str] = []
        watcher_started_before_write: list[bool] = []
        real_mkstemp = tempfile.mkstemp
        real_fdopen = os.fdopen
        real_watcher = remora.start_settings_cleanup_watcher

        def capture_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            fd, path = real_mkstemp(*args, **kwargs)
            created_paths.append(path)
            return fd, path

        def capture_watcher(path: str) -> int:
            guard_fd = real_watcher(path)
            watcher_started_before_write.append(True)
            return guard_fd

        def capture_fdopen(*args: object, **kwargs: object) -> object:
            self.assertEqual(watcher_started_before_write, [True])
            return real_fdopen(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "happy-hooks.json"
            path.write_text(
                json.dumps(
                    {
                        "hooks": {"SessionStart": [{"command": "happy hook"}]},
                        "env": {"HAPPY_UNOWNED": secret},
                        "fallbackModel": [fallback_secret, "gpt-5.6-terra"],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    remora.tempfile, "mkstemp", side_effect=capture_mkstemp
                ),
                mock.patch.object(
                    remora,
                    "start_settings_cleanup_watcher",
                    side_effect=capture_watcher,
                ),
                mock.patch.object(remora.os, "fdopen", side_effect=capture_fdopen),
            ):
                command, env = remora.build_launch(
                    self.config, ["--continue", "--settings", str(path)]
                )

        settings_arg = command[command.index("--settings") + 1]
        guard_fd = int(env[remora.SETTINGS_GUARD_FD_ENV])
        try:
            self.assertEqual(command.count("--settings"), 1)
            self.assertEqual(settings_arg, env[remora.SETTINGS_FILE_ENV])
            self.assertEqual(Path(settings_arg).stat().st_mode & 0o777, 0o600)
            self.assertTrue(os.get_inheritable(guard_fd))
            self.assertEqual(created_paths, [settings_arg])
            self.assertEqual(watcher_started_before_write, [True])
            self.assertNotIn(secret, "\n".join(command))
            self.assertNotIn(fallback_secret, "\n".join(command))
            self.assertNotIn('"hooks"', settings_arg)
            self.assertNotIn(str(path), command)

            settings = launch_settings(command)
            self.assertEqual(settings["fallbackModel"], [])
            self.assertNotIn(fallback_secret, json.dumps(settings))
            self.assertEqual(
                settings["hooks"]["SessionStart"][0]["command"], "happy hook"
            )
            self.assertEqual(settings["env"]["HAPPY_UNOWNED"], secret)
            self.assertEqual(
                settings["availableModels"],
                ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
            )
        finally:
            remora.close_launch_resources(env)
        self.assertFalse(Path(settings_arg).exists())
        with self.assertRaises(OSError):
            os.fstat(guard_fd)

    def test_settings_cleanup_watcher_removes_file_on_guard_eof(self) -> None:
        path, guard_fd = remora.temporary_settings_file('{"safe":true}')
        os.close(guard_fd)
        try:
            for _ in range(100):
                if not Path(path).exists():
                    break
                time.sleep(0.01)
            self.assertFalse(Path(path).exists())
        finally:
            Path(path).unlink(missing_ok=True)

    def test_settings_payload_is_not_written_if_watcher_fails(self) -> None:
        created_paths: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def capture_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            fd, path = real_mkstemp(*args, **kwargs)
            created_paths.append(path)
            return fd, path

        with (
            mock.patch.object(
                remora.tempfile, "mkstemp", side_effect=capture_mkstemp
            ),
            mock.patch.object(
                remora,
                "start_settings_cleanup_watcher",
                side_effect=OSError("watcher failed"),
            ),
            mock.patch.object(remora.os, "fdopen") as fdopen,
            self.assertRaisesRegex(remora.RemoraError, "secure --settings"),
        ):
            remora.temporary_settings_file('{"secret":"not-written"}')

        fdopen.assert_not_called()
        self.assertEqual(len(created_paths), 1)
        self.assertFalse(Path(created_paths[0]).exists())

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_inline_settings_equals_form_replaces_caller_fallback(self) -> None:
        command, env = remora.build_launch(
            self.config,
            [
                '--settings={"permissions":{"allow":["Read"]},'
                '"fallbackModel":[]}'
            ],
        )
        try:
            settings = launch_settings(command)
            self.assertEqual(settings["permissions"], {"allow": ["Read"]})
            self.assertEqual(settings["fallbackModel"], [])
            self.assertIn("gpt-5.6-luna", settings["availableModels"])
        finally:
            remora.close_launch_resources(env)

    def test_long_inline_settings_are_not_treated_as_a_path(self) -> None:
        value = json.dumps({"env": {"LONG_VALUE": "x" * 4096}})
        with mock.patch.object(
            remora.Path,
            "is_file",
            side_effect=OSError(remora.errno.ENAMETOOLONG, "file name too long"),
        ):
            settings = remora.load_settings(value)
        self.assertEqual(settings["env"]["LONG_VALUE"], "x" * 4096)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_caller_settings_env_cannot_override_remora_owned_keys(self) -> None:
        caller_env = {
            key: f"caller-value-{index}"
            for index, key in enumerate(sorted(remora.PROTECTED_SETTINGS_ENV))
        }
        caller_env["HAPPY_UNOWNED"] = "preserved"
        command, env = remora.build_launch(
            self.config,
            ["--settings", json.dumps({"env": caller_env})],
            require_token=False,
            fast=True,
        )
        try:
            settings = launch_settings(command)
            self.assertEqual(settings["env"], {"HAPPY_UNOWNED": "preserved"})
            self.assertEqual(env["REMORA_ACTIVE"], "1")
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317")
            self.assertEqual(
                env[remora.CLAUDE_EXTRA_BODY_ENV], '{"service_tier":"priority"}'
            )
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        finally:
            remora.close_launch_resources(env)

    def test_settings_env_requires_an_object(self) -> None:
        for value in (None, [], "invalid", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(remora.RemoraError, "env.*JSON object"):
                    remora.build_launch(
                        self.config,
                        ["--settings", json.dumps({"env": value})],
                        require_token=False,
                    )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_dry_run_hides_caller_settings_and_fallback_then_cleans_file(self) -> None:
        secret = "dry-run-settings-secret"
        fallback_secret = "dry-run-fallback-secret"
        resources: list[tuple[str, int]] = []
        real_temporary_settings_file = remora.temporary_settings_file

        def capture_resource(serialized: str) -> tuple[str, int]:
            resource = real_temporary_settings_file(serialized)
            resources.append(resource)
            return resource

        output = io.StringIO()
        with (
            mock.patch.object(
                remora, "temporary_settings_file", side_effect=capture_resource
            ),
            redirect_stdout(output),
        ):
            remora.dry_run(
                self.config,
                [
                    "--settings",
                    json.dumps(
                        {
                            "env": {"HAPPY_UNOWNED": secret},
                            "fallbackModel": [fallback_secret],
                        }
                    ),
                ],
            )

        shown = output.getvalue()
        payload = json.loads(shown)
        self.assertEqual(len(resources), 1)
        path, guard_fd = resources[0]
        self.assertNotIn(secret, shown)
        self.assertNotIn(fallback_secret, shown)
        self.assertEqual(
            payload["command"][payload["command"].index("--settings") + 1], path
        )
        self.assertNotIn(remora.SETTINGS_FILE_ENV, payload["environment"])
        self.assertNotIn(remora.SETTINGS_GUARD_FD_ENV, payload["environment"])
        self.assertFalse(Path(path).exists())
        with self.assertRaises(OSError):
            os.fstat(guard_fd)

    def test_exec_launch_strips_tracking_metadata_and_cleans_on_return(self) -> None:
        path, guard_fd = remora.temporary_settings_file('{"safe":true}')
        command = ["claude", "--settings", path]
        env = {
            remora.SETTINGS_FILE_ENV: path,
            remora.SETTINGS_GUARD_FD_ENV: str(guard_fd),
            "HAPPY_UNOWNED": "preserved",
        }

        def inspect_exec(
            executable: str, argv: list[str], child_env: dict[str, str]
        ) -> None:
            self.assertEqual(executable, "claude")
            self.assertEqual(argv, command)
            self.assertNotIn(remora.SETTINGS_FILE_ENV, child_env)
            self.assertNotIn(remora.SETTINGS_GUARD_FD_ENV, child_env)
            self.assertEqual(child_env["HAPPY_UNOWNED"], "preserved")
            self.assertTrue(os.get_inheritable(guard_fd))
            self.assertEqual(Path(path).read_text(), '{"safe":true}')

        with mock.patch.object(remora.os, "execvpe", side_effect=inspect_exec):
            remora.exec_launch(command, env)
        self.assertFalse(Path(path).exists())
        with self.assertRaises(OSError):
            os.fstat(guard_fd)

    def test_build_launch_cleans_resource_if_handoff_fails(self) -> None:
        resources: list[tuple[str, int]] = []
        real_temporary_settings_file = remora.temporary_settings_file

        def capture_resource(serialized: str) -> tuple[str, int]:
            resource = real_temporary_settings_file(serialized)
            resources.append(resource)
            return resource

        class HandoffFailureEnv(dict[str, str]):
            def __setitem__(self, key: str, value: str) -> None:
                if key == remora.SETTINGS_GUARD_FD_ENV:
                    raise RuntimeError("handoff failed")
                super().__setitem__(key, value)

        class ParentEnv(dict[str, str]):
            def copy(self) -> HandoffFailureEnv:
                return HandoffFailureEnv(self)

        parent_env = ParentEnv({"HOME": os.environ.get("HOME", str(Path.home()))})
        with (
            mock.patch.object(remora.os, "environ", parent_env),
            mock.patch.object(
                remora, "temporary_settings_file", side_effect=capture_resource
            ),
            self.assertRaisesRegex(RuntimeError, "handoff failed"),
        ):
            remora.build_launch(
                self.config, ["--settings", "{}"], require_token=False
            )

        self.assertEqual(len(resources), 1)
        path, guard_fd = resources[0]
        self.assertFalse(Path(path).exists())
        with self.assertRaises(OSError):
            os.fstat(guard_fd)

    def test_settings_merge_is_recursive(self) -> None:
        self.assertEqual(
            remora.merge_settings(
                {"hooks": {"SessionStart": ["caller"], "shared": {"caller": True}}},
                {"hooks": {"shared": {"remora": True}}},
            ),
            {
                "hooks": {
                    "SessionStart": ["caller"],
                    "shared": {"caller": True, "remora": True},
                }
            },
        )

    def test_settings_requires_one_json_object(self) -> None:
        for args in (
            ["--settings"],
            ["--settings", "--"],
            ["--settings", "--continue"],
            ["--settings", "-c"],
        ):
            with self.subTest(args=args):
                with self.assertRaisesRegex(remora.RemoraError, "requires a value"):
                    remora.build_launch(self.config, args, require_token=False)
        with self.assertRaisesRegex(remora.RemoraError, "JSON object"):
            remora.build_launch(self.config, ["--settings", "[]"], require_token=False)
        with self.assertRaisesRegex(remora.RemoraError, "valid JSON object"):
            remora.build_launch(self.config, ["--settings", "\x00"], require_token=False)
        with self.assertRaisesRegex(remora.RemoraError, "finite JSON"):
            remora.build_launch(
                self.config, ["--settings", '{"number":1e9999}'], require_token=False
            )
        with self.assertRaisesRegex(remora.RemoraError, "only be specified once"):
            remora.build_launch(
                self.config,
                ["--settings", "{}", "--settings={}"],
                require_token=False,
            )

    def test_caller_settings_reject_invalid_fallback_values_without_echo(self) -> None:
        secret = "invalid-fallback-secret"
        cases = {
            "null": None,
            "scalar-string": secret,
            "object": {"model": secret},
            "numeric": 1,
            "boolean": True,
            "mixed": ["gpt-5.6-terra", 1],
            "empty-string": [""],
            "whitespace-only": [" \t\n"],
        }
        with (
            mock.patch.object(remora, "resolve_auth_token") as resolve_auth_token,
            mock.patch.object(remora, "resolve_context_policy") as resolve_context_policy,
            mock.patch.object(remora, "temporary_settings_file") as create_settings,
        ):
            for name, value in cases.items():
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        remora.RemoraError, "fallbackModel.*non-empty strings"
                    ) as raised:
                        remora.build_launch(
                            self.config,
                            ["--settings", json.dumps({"fallbackModel": value})],
                        )
                    self.assertNotIn(secret, str(raised.exception))

        resolve_auth_token.assert_not_called()
        resolve_context_policy.assert_not_called()
        create_settings.assert_not_called()

    def test_cli_fallback_flag_rejects_all_forms_without_echo(self) -> None:
        secrets = ("separated-secret", "equals-secret", "duplicate-secret")
        cases = (
            (["--fallback-model", secrets[0]], "not supported"),
            ([f"--fallback-model={secrets[1]}"], "not supported"),
            (
                [
                    "--fallback-model",
                    secrets[0],
                    f"--fallback-model={secrets[2]}",
                ],
                "only be specified once",
            ),
            (["--fallback-model"], "requires a value"),
            (["--fallback-model="], "requires a value"),
            (["--fallback-model", "--continue"], "requires a value"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                with self.assertRaisesRegex(remora.RemoraError, message) as raised:
                    remora.build_launch(self.config, args, require_token=False)
                for secret in secrets:
                    self.assertNotIn(secret, str(raised.exception))

    def test_cli_fallback_rejection_precedes_launch_side_effects(self) -> None:
        secret = "early-rejection-secret"
        stderr = io.StringIO()
        with (
            mock.patch.object(remora, "load_config", return_value=self.config),
            mock.patch.object(remora, "load_settings") as load_settings,
            mock.patch.object(remora, "resolve_auth_token") as resolve_auth_token,
            mock.patch.object(remora, "resolve_context_policy") as resolve_context_policy,
            mock.patch.object(remora, "temporary_settings_file") as create_settings,
            mock.patch.object(remora, "prepare_coralline_config") as prepare_coralline,
            mock.patch.object(remora, "exec_launch") as exec_launch,
            redirect_stderr(stderr),
        ):
            result = remora.main(
                ["--settings", '{"hooks":{}}', "--fallback-model", secret]
            )

        self.assertEqual(result, 2)
        load_settings.assert_not_called()
        resolve_auth_token.assert_not_called()
        resolve_context_policy.assert_not_called()
        create_settings.assert_not_called()
        prepare_coralline.assert_not_called()
        exec_launch.assert_not_called()
        self.assertNotIn(secret, stderr.getvalue())

    def test_double_dash_preserves_literal_fallback_arguments(self) -> None:
        args = [
            "--",
            "--fallback-model",
            "literal-after-delimiter",
            "--fallback-model=also-literal",
        ]
        command, _ = remora.build_launch(self.config, args, require_token=False)
        self.assertEqual(command[-len(args) :], args)
        self.assertEqual(launch_settings(command)["fallbackModel"], [])

    def test_fallback_policy_preserves_explicit_terra_model_forms(self) -> None:
        cases = (
            ["--model", "sonnet"],
            ["-m", "sonnet"],
            ["--model=sonnet"],
            ["--model", "gpt-5.6-terra"],
        )
        for args in cases:
            with self.subTest(args=args):
                command, env = remora.build_launch(
                    self.config, args, require_token=False
                )
                caller_start = len(command) - len(args)
                self.assertEqual(command[caller_start:], args)
                self.assertFalse(
                    any(
                        arg == "--model" or arg.startswith("--model=")
                        for arg in command[:caller_start]
                    )
                )
                self.assertEqual(
                    env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-5.6-terra"
                )
                self.assertEqual(launch_settings(command)["fallbackModel"], [])

    def test_routing_settings_allow_models_and_disable_fallback(self) -> None:
        self.assertEqual(
            remora.routing_settings(self.config),
            {
                "availableModels": [
                    "gpt-5.6-luna",
                    "gpt-5.6-sol",
                    "gpt-5.6-terra",
                ],
                "fallbackModel": [],
            },
        )

    @mock.patch.dict(os.environ, {"REMORA_AUTH_TOKEN": "test-secret"}, clear=True)
    def test_launch_marks_only_the_child_as_remora(self) -> None:
        self.assertNotIn("REMORA_ACTIVE", os.environ)
        _, env = remora.build_launch(self.config, [])
        self.assertEqual(env["REMORA_ACTIVE"], "1")
        self.assertNotIn("REMORA_ACTIVE", os.environ)

    def test_child_isolates_coralline_stores_per_gateway(self) -> None:
        # coralline's 5h/7d segments read a cross-session high-water store, and its
        # burn segment appends a 5h sample log; both are fed by Anthropic responses.
        # A remora child talks to a GPT gateway on a different account, so it must
        # not share (and poison) the host's native stores. The child env points
        # coralline at a per-gateway subdir in remora-owned XDG state, overriding
        # any inherited host value without writing into ~/.claude.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_home = root / "state"
            native = root / ".claude"
            host_7d = str(native / "coralline" / "limit-7d.tsv")
            host_burn = str(native / "coralline" / "burn-5h.tsv")
            host_config = str(native / "coralline.conf")
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "XDG_STATE_HOME": str(state_home),
                    "CORALLINE_CONFIG": host_config,
                    "CORALLINE_RL7D_FILE": host_7d,
                    "CORALLINE_BURN_FILE": host_burn,
                },
                clear=False,
            ):
                _, env = remora.build_launch(self.config, [], require_token=False)
            store_dir = Path(env["CORALLINE_RL5H_FILE"]).parent
            # a per-gateway subdir under remora-owned state, prefixed by the host
            self.assertEqual(
                store_dir.parent,
                state_home / "remora-cc" / "coralline" / "gateways",
            )
            self.assertTrue(store_dir.name.startswith("127-0-0-1-8317-"))
            # all three stores redirected into that same dir
            self.assertEqual(Path(env["CORALLINE_RL5H_FILE"]).name, "limit-5h")
            self.assertEqual(Path(env["CORALLINE_RL7D_FILE"]).parent, store_dir)
            self.assertEqual(Path(env["CORALLINE_BURN_FILE"]).parent, store_dir)
            self.assertEqual(Path(env["CORALLINE_BURN_FILE"]).name, "burn-5h.tsv")
            # the generated config wrapper also lives in the scoped directory
            wrapper = Path(env["CORALLINE_CONFIG"])
            self.assertEqual(wrapper.parent, store_dir)
            self.assertTrue(wrapper.name.startswith("config-"))
            # inherited host paths and config are overridden, not preserved
            self.assertNotEqual(env["CORALLINE_RL7D_FILE"], host_7d)
            self.assertNotEqual(env["CORALLINE_BURN_FILE"], host_burn)
            self.assertNotEqual(env["CORALLINE_CONFIG"], host_config)
            self.assertNotIn(native, wrapper.parents)

    def test_coralline_wrapper_reapplies_scoped_paths_after_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "user config" / "coralline.conf"
            source.parent.mkdir()
            source.write_text(
                "\n".join(
                    [
                        "VL_LIMIT_SYNC=1",
                        'CONFIG_SIBLING="${CORALLINE_CONFIG%/*}/theme.conf"',
                        'VL_CONF_SIBLING="${VL_CONF%/*}/theme.conf"',
                        "RL5H_FILE=/host/custom-5h",
                        "RL7D_FILE=/host/custom-7d",
                        "BURN_FILE=/host/custom-burn",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            scoped = root / "gateway"
            env = {
                "CORALLINE_CONFIG": str(scoped / "config.conf"),
                "CORALLINE_RL5H_FILE": str(scoped / "limit-5h"),
                "CORALLINE_RL7D_FILE": str(scoped / "limit-7d"),
                "CORALLINE_BURN_FILE": str(scoped / "burn-5h.tsv"),
            }
            remora.prepare_coralline_config(env, str(source))
            output = subprocess.check_output(
                [
                    "bash",
                    "-c",
                    '. "$1"; printf "%s\\n" "$VL_LIMIT_SYNC" "$CORALLINE_CONFIG" '
                    '"$VL_CONF" "$CONFIG_SIBLING" "$VL_CONF_SIBLING" '
                    '"$RL5H_FILE" "$RL7D_FILE" "$BURN_FILE"',
                    "remora-test",
                    env["CORALLINE_CONFIG"],
                ],
                text=True,
            ).splitlines()
            self.assertEqual(
                output,
                [
                    "1",
                    str(source),
                    str(source),
                    str(source.parent / "theme.conf"),
                    str(source.parent / "theme.conf"),
                    env["CORALLINE_RL5H_FILE"],
                    env["CORALLINE_RL7D_FILE"],
                    env["CORALLINE_BURN_FILE"],
                ],
            )
            self.assertEqual(
                Path(env["CORALLINE_CONFIG"]).stat().st_mode & 0o777,
                0o600,
            )

    def test_path_routed_gateways_get_distinct_coralline_stores(self) -> None:
        # Two gateways behind the same reverse-proxy host must not collapse into
        # one store, or the cross-account poisoning this change prevents returns.
        team_a = remora.coralline_store_dir("https://gw.example.com/team-a")
        team_b = remora.coralline_store_dir("https://gw.example.com/team-b")
        self.assertNotEqual(team_a, team_b)
        self.assertTrue(team_a.name.startswith("gw-example-com-"))
        self.assertTrue(team_b.name.startswith("gw-example-com-"))
        # scheme also participates in the key
        self.assertNotEqual(
            remora.coralline_store_dir("http://gw.example.com/team-a"), team_a
        )

    def test_long_gateway_host_keeps_store_component_within_filesystem_limit(self) -> None:
        host = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
        team_a = remora.coralline_store_dir(f"https://{host}/team-a")
        team_b = remora.coralline_store_dir(f"https://{host}/team-b")
        self.assertLessEqual(
            len(team_a.name.encode("utf-8")),
            remora.CORALLINE_GATEWAY_PREFIX_MAX + 11,
        )
        self.assertNotEqual(team_a, team_b)

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
    def test_calico_mode_caps_gateway_to_codex_runtime_and_exports_exact_map(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        gateway_windows = {
            "gpt-5.6-sol": 372000,
            "gpt-5.6-terra": 372000,
            "gpt-5.6-luna": 372000,
        }
        codex_windows = {
            "gpt-5.6-sol": 272000,
            "gpt-5.6-terra": 272000,
            "gpt-5.6-luna": 272000,
        }
        with (
            mock.patch.object(
                remora, "fetch_gateway_context_windows", return_value=gateway_windows
            ),
            mock.patch.object(
                remora, "fetch_codex_context_windows", return_value=codex_windows
            ),
            mock.patch.object(remora, "calico_context_supported", return_value=True),
        ):
            _, env = remora.build_launch(config, [])
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "272000")
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
        self.assertEqual(env["CALICO_CONTEXT_DISPLAY_PERCENT"], "95")
        self.assertEqual(
            json.loads(env["CALICO_MODEL_CONTEXT_WINDOWS"]), codex_windows
        )

    def test_calico_context_policy_matches_current_codex_runtime_defaults(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        gateway_windows = {
            name: 372000 for name in remora.configured_model_names(config)
        }
        codex_windows = {
            name: 272000 for name in remora.configured_model_names(config)
        }
        with (
            mock.patch.object(
                remora, "fetch_gateway_context_windows", return_value=gateway_windows
            ),
            mock.patch.object(
                remora, "fetch_codex_context_windows", return_value=codex_windows
            ),
        ):
            policy = remora.resolve_context_policy(
                config, token="hidden", online=True
            )
        self.assertEqual(policy["source"], "gateway+codex-cache")
        self.assertEqual(policy["provider_window"], 372000)
        self.assertEqual(policy["codex_window"], 272000)
        self.assertEqual(policy["client_window"], 272000)
        self.assertEqual(policy["effective_window"], 258400)
        self.assertEqual(policy["compact_trigger"], 244800)
        self.assertIn("capped to the Codex value", policy["warning"])

    def test_calico_context_policy_restores_larger_fresh_runtime_window(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        restored_windows = {
            name: 372000 for name in remora.configured_model_names(config)
        }
        with (
            mock.patch.object(
                remora,
                "fetch_gateway_context_windows",
                return_value=restored_windows,
            ),
            mock.patch.object(
                remora,
                "fetch_codex_context_windows",
                return_value=restored_windows,
            ),
        ):
            policy = remora.resolve_context_policy(
                config, token="hidden", online=True
            )
        self.assertEqual(policy["provider_window"], 372000)
        self.assertEqual(policy["codex_window"], 372000)
        self.assertEqual(policy["client_window"], 372000)
        self.assertEqual(policy["effective_window"], 353400)
        self.assertEqual(policy["compact_trigger"], 334800)
        self.assertNotIn("capped to the Codex value", policy["warning"])

    def test_codex_context_cache_loader_rejects_stale_metadata(self) -> None:
        config = json.loads(json.dumps(self.config))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models_cache.json"
            path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2020-01-01T00:00:00Z",
                        "models": [
                            {"slug": "gpt-5.6-sol", "context_window": 999000}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config["context"]["codex_models_cache"] = str(path)
            with self.assertRaisesRegex(remora.RemoraError, "is stale"):
                remora.fetch_codex_context_windows(config)

    def test_codex_context_cache_loader_reads_fresh_metadata(self) -> None:
        config = json.loads(json.dumps(self.config))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models_cache.json"
            path.write_text(
                json.dumps(
                    {
                        "fetched_at": remora.datetime.now(
                            remora.timezone.utc
                        ).isoformat(),
                        "models": [
                            {"slug": "gpt-5.6-sol", "context_window": 272000},
                            {"slug": "ignored", "context_window": "272000"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config["context"]["codex_models_cache"] = str(path)
            self.assertEqual(
                remora.fetch_codex_context_windows(config),
                {"gpt-5.6-sol": 272000},
            )

    @mock.patch.dict(
        os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, clear=True
    )
    def test_calico_caps_explicit_compact_window_to_codex_ceiling(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["mode"] = "calico"
        gateway_windows = {
            name: 372000 for name in remora.configured_model_names(config)
        }
        codex_windows = {
            name: 272000 for name in remora.configured_model_names(config)
        }
        with (
            mock.patch.object(
                remora, "fetch_gateway_context_windows", return_value=gateway_windows
            ),
            mock.patch.object(
                remora, "fetch_codex_context_windows", return_value=codex_windows
            ),
        ):
            policy = remora.resolve_context_policy(
                config, token="hidden", online=True
            )
        self.assertEqual(policy["auto_compact_window"], 272000)
        self.assertEqual(policy["compact_trigger"], 244800)
        self.assertIn("ceiling 272000 and was capped", policy["warning"])

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
    def test_doctor_warns_when_override_exceeds_stock_window(
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
        self.assertIn("exceeds stock Claude Code custom-model window 200000", output.getvalue())

    def test_invalid_context_config_is_rejected(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["context"]["auto_compact_percent"] = 96
        with self.assertRaisesRegex(remora.RemoraError, "must not exceed"):
            remora.validate_config(config)

        config = json.loads(json.dumps(self.config))
        config["context"]["codex_fallback_window"] = 0
        with self.assertRaisesRegex(remora.RemoraError, "codex_fallback_window"):
            remora.validate_config(config)

        config = json.loads(json.dumps(self.config))
        config["context"]["codex_models_cache"] = []
        with self.assertRaisesRegex(remora.RemoraError, "codex_models_cache"):
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
