from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "orchestration_runtime", ROOT / "src" / "orchestration_runtime.py"
)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    path.chmod(0o600)


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.state = root / "state" / "orchestration"
        self.projects = root / "claude" / "projects"
        self.projects.mkdir(mode=0o700, parents=True)
        bindings = {
            "plan-verifier": {"model": "gpt-5.6-sol", "effort": "high"},
            "executor": {"model": "gpt-5.6-luna", "effort": "max"},
        }
        hashes = {
            "policy": runtime.source_hash(ROOT / "agents" / "orchestration.md"),
            "roles": runtime.source_hash(ROOT / "agents" / "agents.json"),
            "runtime": runtime.source_hash(ROOT / "src" / "orchestration_runtime.py"),
        }
        self.environment = {
            runtime.ENV_STATE_ROOT: str(self.state),
            runtime.ENV_PROJECTS_ROOT: str(self.projects),
            runtime.ENV_BINDINGS: json.dumps(bindings),
            runtime.ENV_SOURCE_HASHES: json.dumps(hashes),
        }
        self.env_patch = mock.patch.dict(os.environ, self.environment, clear=False)
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temporary.cleanup()

    def prompt(self, text: str = "Plan a secure database migration.") -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "root-session",
            "prompt_id": "native-prompt",
            "prompt": text,
        }

    def child_transcript(
        self, agent_id: str = "child-one", *, model: object = "gpt-5.6-sol", effort: object = "high", text: str = "READY"
    ) -> Path:
        path = (
            self.projects
            / "project"
            / "root-session"
            / "subagents"
            / f"agent-{agent_id}.jsonl"
        )
        event: dict[str, object] = {
            "type": "assistant",
            "agentId": agent_id,
            "sessionId": "root-session",
            "message": {"model": model, "content": [{"type": "text", "text": text}]},
        }
        if effort is not None:
            event["effort"] = effort
        write_jsonl(path, [event])
        return path

    def root_transcript(self, *, async_launch: bool = False, tag: bool = True) -> Path:
        prompt = runtime.review_request("native-prompt") if tag else (
            "semantic_adjudication\nquoted " + runtime.review_tag("native-prompt")
        )
        agent_result: dict[str, object] = {
            "status": "async_launched" if async_launch else "completed",
            "agentId": "child-one",
            "resolvedModel": "gpt-5.6-sol",
        }
        events: list[dict[str, object]] = [
            {
                "type": "assistant",
                "sessionId": "root-session",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-agent",
                            "name": "Agent",
                            "input": {"subagent_type": "plan-verifier", "prompt": prompt},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "sessionId": "root-session",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-agent"}]},
                "toolUseResult": agent_result,
            },
        ]
        if async_launch:
            events.extend(
                [
                    {
                        "type": "assistant",
                        "sessionId": "root-session",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool-output",
                                    "name": "TaskOutput",
                                    "input": {"task_id": "child-one"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "root-session",
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-output"}]},
                        "toolUseResult": {
                            "retrieval_status": "success",
                            "task": {
                                "status": "completed",
                                "task_type": "local_agent",
                                "task_id": "child-one",
                            },
                        },
                    },
                ]
            )
        path = self.projects / "project" / "root-session.jsonl"
        write_jsonl(path, events)
        return path

    def start_and_stop_child(self, child_path: Path, *, verdict: str = "READY") -> None:
        runtime.handle(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
                "effort": {"level": "high"},
            }
        )
        runtime.handle(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
                "effort": {"level": "high"},
                "transcript_path": str(child_path.parent.parent.with_suffix(".jsonl")),
                "agent_transcript_path": str(child_path),
                "last_assistant_message": verdict,
            }
        )

    def latest(self) -> dict[str, object]:
        return json.loads(
            (self.state / "latest" / "plan-verifier-automatic_plan_review.json").read_text()
        )

    def test_prompt_uses_native_identity_and_never_stores_prompt(self) -> None:
        response = runtime.handle(self.prompt())
        self.assertIn(runtime.review_tag("native-prompt"), json.dumps(response))
        state = next((self.state / "sessions").glob("*.json")).read_text()
        self.assertNotIn("native-prompt", state)
        self.assertNotIn("secure database migration", state)
        self.assertIn(runtime.hash_prompt_id("native-prompt"), state)

    def test_missing_native_prompt_id_is_skipped_without_fallback(self) -> None:
        payload = self.prompt()
        payload.pop("prompt_id")
        self.assertIsNone(runtime.handle(payload))
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertEqual(state["status"]["reason"], "native_prompt_id_missing")
        self.assertIsNone(state["active"])

    def test_ordinary_prompt_adds_no_review_requirement(self) -> None:
        self.assertIsNone(runtime.handle(self.prompt("Refactor this local helper.")))
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertFalse(state["active"]["required"])

    def test_matching_sync_runtime_evidence_verifies_ready(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        response = runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertIsNone(response)
        receipt = self.latest()
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertTrue(receipt["readiness_granted"])
        rendered = json.dumps(receipt)
        for forbidden in ("root-session", "child-one", str(self.projects)):
            self.assertNotIn(forbidden, rendered)

    def test_async_launch_requires_completed_task_output(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript(async_launch=True)),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "VERIFIED")

    def test_async_launch_alone_is_not_completion(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        path = self.root_transcript(async_launch=True)
        events = [json.loads(line) for line in path.read_text().splitlines()][:2]
        write_jsonl(path, events)
        response = runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(path),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(response["decision"], "block")
        self.assertEqual(self.latest()["reason"], "async_completion_missing")

    def test_missing_actual_effort_never_verifies(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript(effort=None))
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "SKIPPED")
        self.assertEqual(self.latest()["reason"], "observed_model_or_effort_missing")

    def test_subagent_stop_retries_lagging_transcript(self) -> None:
        runtime.handle(self.prompt())
        child = self.child_transcript()
        settled = runtime._jsonl(child, projects_root=self.projects)
        self.assertIsNotNone(settled)
        with (
            mock.patch.object(runtime, "_jsonl", side_effect=[None, settled]),
            mock.patch.object(runtime.time, "sleep") as sleep,
        ):
            self.start_and_stop_child(child)
        sleep.assert_called_once_with(runtime.TRANSCRIPT_SETTLE_SECONDS)

        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "VERIFIED")

    def test_missing_resolved_model_never_verifies(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        root = self.root_transcript()
        events = [json.loads(line) for line in root.read_text().splitlines()]
        events[1]["toolUseResult"].pop("resolvedModel")
        write_jsonl(root, events)
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(root),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "SKIPPED")
        self.assertEqual(self.latest()["reason"], "resolved_model_missing")

    def test_one_assistant_record_missing_model_never_verifies(self) -> None:
        child = self.child_transcript()
        events = [json.loads(line) for line in child.read_text().splitlines()]
        events.append(
            {
                "type": "assistant",
                "agentId": "child-one",
                "sessionId": "root-session",
                "effort": "high",
                "message": {"content": [{"type": "text", "text": "READY"}]},
            }
        )
        write_jsonl(child, events)
        runtime.handle(self.prompt())
        self.start_and_stop_child(child)
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["reason"], "observed_model_or_effort_missing")

    def test_revise_is_evidence_but_never_grants_readiness(self) -> None:
        revise = "REVISE\nBlocker: x\nEvidence: y\nMinimum revision: z\nAcceptance check: q"
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript(text=revise), verdict=revise)
        response = runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertIsNone(response)
        self.assertEqual(self.latest()["verdict_status"], "REVISE")
        self.assertFalse(self.latest()["readiness_granted"])

    def test_semantic_adjudication_or_missing_tag_cannot_verify(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        response = runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript(tag=False)),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(response["decision"], "block")
        self.assertEqual(self.latest()["reason"], "readiness_contract_missing")

    def test_same_blocker_is_blocked_only_once(self) -> None:
        runtime.handle(self.prompt())
        payload = {
            "hook_event_name": "Stop",
            "session_id": "root-session",
            "prompt_id": "native-prompt",
            "transcript_path": str(self.root_transcript()),
            "stop_hook_active": False,
        }
        self.assertEqual(runtime.handle(payload)["decision"], "block")
        self.assertIsNone(runtime.handle(payload))

    def test_same_blocker_new_prompt_keeps_retry_used_and_rejects_stale_events(self) -> None:
        runtime.handle(self.prompt())
        first_stop = {
            "hook_event_name": "Stop",
            "session_id": "root-session",
            "prompt_id": "native-prompt",
            "transcript_path": str(self.root_transcript()),
            "stop_hook_active": False,
        }
        self.assertEqual(runtime.handle(first_stop)["decision"], "block")
        second = self.prompt()
        second["prompt_id"] = "native-prompt-two"
        runtime.handle(second)
        self.start_and_stop_child(self.child_transcript())
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertTrue(state["active"]["attempted"])
        self.assertEqual(state["active"]["children"], [])

    def test_duplicate_native_prompt_callback_preserves_completed_child(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        self.assertIsNone(runtime.handle(self.prompt("different replay payload")))
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertTrue(state["active"]["children"][0]["completed"])
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "VERIFIED")

    def test_symlink_child_evidence_is_rejected(self) -> None:
        real = self.child_transcript(agent_id="real")
        link = real.parent / "agent-child-one.jsonl"
        link.symlink_to(real.name)
        runtime.handle(self.prompt())
        self.start_and_stop_child(link)
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertFalse(state["active"]["children"][0]["completed"])

    def test_intermediate_symlink_evidence_is_rejected(self) -> None:
        outside = self.projects.parent / "outside"
        target = outside / "root-session" / "subagents" / "agent-child-one.jsonl"
        write_jsonl(
            target,
            [
                {
                    "type": "assistant",
                    "agentId": "child-one",
                    "sessionId": "root-session",
                    "effort": "high",
                    "message": {"model": "gpt-5.6-sol", "content": [{"type": "text", "text": "READY"}]},
                }
            ],
        )
        (self.projects / "linked").symlink_to(outside, target_is_directory=True)
        runtime.handle(self.prompt())
        runtime.handle(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
            }
        )
        runtime.handle(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
                "transcript_path": str(self.projects / "linked" / "root-session.jsonl"),
                "agent_transcript_path": str(self.projects / "linked" / "root-session" / "subagents" / "agent-child-one.jsonl"),
                "last_assistant_message": "READY",
            }
        )
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertFalse(state["active"]["children"][0]["completed"])

    def test_prior_sibling_subagents_layout_is_rejected(self) -> None:
        child = self.projects / "project" / "subagents" / "agent-child-one.jsonl"
        write_jsonl(
            child,
            [
                {
                    "type": "assistant",
                    "agentId": "child-one",
                    "sessionId": "root-session",
                    "effort": "high",
                    "message": {
                        "model": "gpt-5.6-sol",
                        "content": [{"type": "text", "text": "READY"}],
                    },
                }
            ],
        )
        runtime.handle(self.prompt())
        runtime.handle(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
            }
        )
        runtime.handle(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "agent_id": "child-one",
                "agent_type": "plan-verifier",
                "transcript_path": str(self.projects / "project" / "root-session.jsonl"),
                "agent_transcript_path": str(child),
                "last_assistant_message": "READY",
            }
        )
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertFalse(state["active"]["children"][0]["completed"])

    def test_intermediate_symlink_state_root_is_rejected_without_outside_write(self) -> None:
        outside = self.projects.parent / "outside-state"
        outside.mkdir(mode=0o700)
        link = self.projects.parent / "state-link"
        link.symlink_to(outside, target_is_directory=True)
        with mock.patch.dict(os.environ, {runtime.ENV_STATE_ROOT: str(link / "orchestration")}):
            self.assertIsNone(runtime.handle(self.prompt()))
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_existing_state_is_preserved(self) -> None:
        runtime.handle(self.prompt())
        path = next((self.state / "sessions").glob("*.json"))
        path.write_text("not-json")
        path.chmod(0o600)
        before = path.read_bytes()
        runtime.handle(self.prompt("Plan secure database migration with strict review."))
        self.assertEqual(path.read_bytes(), before)

    def test_schema_shaped_state_with_unallowlisted_field_is_preserved(self) -> None:
        runtime.handle(self.prompt())
        path = next((self.state / "sessions").glob("*.json"))
        state = json.loads(path.read_text())
        state["raw_prompt"] = "must-not-be-accepted"
        path.write_text(json.dumps(state))
        path.chmod(0o600)
        before = path.read_bytes()
        runtime.handle(self.prompt("Plan secure database migration with strict review."))
        self.assertEqual(path.read_bytes(), before)

    def test_child_transcript_identity_mismatch_fails_receipt(self) -> None:
        child = self.child_transcript()
        events = [json.loads(line) for line in child.read_text().splitlines()]
        events[0]["agentId"] = "different-child"
        write_jsonl(child, events)
        runtime.handle(self.prompt())
        self.start_and_stop_child(child)
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        self.assertEqual(self.latest()["status"], "FAILED")
        self.assertEqual(self.latest()["reason"], "child_session_identity_mismatch")

    def test_status_rejects_schema_shaped_receipt_with_unsafe_value(self) -> None:
        directory = self.state / "latest"
        directory.mkdir(mode=0o700, parents=True)
        path = directory / "plan-verifier-automatic_plan_review.json"
        path.write_text(json.dumps({"schema": 1, "observed_model": "/private/secret"}))
        path.chmod(0o600)
        self.assertEqual(runtime.status(self.state)["receipts"], [])

    def test_status_rejects_missing_or_unknown_source_hash_keys(self) -> None:
        runtime.handle(self.prompt())
        self.start_and_stop_child(self.child_transcript())
        runtime.handle(
            {
                "hook_event_name": "Stop",
                "session_id": "root-session",
                "prompt_id": "native-prompt",
                "transcript_path": str(self.root_transcript()),
                "stop_hook_active": False,
            }
        )
        path = self.state / "latest" / "plan-verifier-automatic_plan_review.json"
        valid = json.loads(path.read_text())
        cases = (
            {"policy": valid["source_hashes"]["policy"]},
            {**valid["source_hashes"], "unknown": "0" * 64},
        )
        for source_hashes in cases:
            with self.subTest(keys=sorted(source_hashes)):
                receipt = dict(valid)
                receipt["source_hashes"] = source_hashes
                path.write_text(json.dumps(receipt))
                path.chmod(0o600)
                self.assertEqual(runtime.status(self.state)["receipts"], [])

    def test_concurrent_updates_remain_bounded_valid_json(self) -> None:
        runtime.handle(self.prompt())

        def start(index: int) -> None:
            runtime.handle(
                {
                    "hook_event_name": "SubagentStart",
                    "session_id": "root-session",
                    "prompt_id": "native-prompt",
                    "agent_id": f"child-{index}",
                    "agent_type": "executor",
                }
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(start, range(80)))
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertTrue(runtime._valid_state(state, state["session_hash"]))
        self.assertLessEqual(len(state["active"]["children"]), runtime.MAX_CHILDREN)

    def test_session_state_capacity_fails_soft(self) -> None:
        with mock.patch.object(runtime, "MAX_SESSIONS", 2):
            for index in range(3):
                payload = self.prompt()
                payload["session_id"] = f"session-{index}"
                payload["prompt_id"] = f"prompt-{index}"
                runtime.handle(payload)
        self.assertEqual(len(list((self.state / "sessions").glob("*.json"))), 2)

    def test_group_writable_child_evidence_is_rejected(self) -> None:
        child = self.child_transcript()
        child.chmod(0o660)
        runtime.handle(self.prompt())
        self.start_and_stop_child(child)
        state = json.loads(next((self.state / "sessions").glob("*.json")).read_text())
        self.assertFalse(state["active"]["children"][0]["completed"])

    def test_offline_named_verification_does_not_write_live_state(self) -> None:
        receipt = runtime.verify_named_files(
            self.root_transcript(),
            self.child_transcript(),
            projects_root=self.projects,
            role="plan-verifier",
            expected_model="gpt-5.6-sol",
            expected_effort="high",
            prompt_id="native-prompt",
        )
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertFalse(self.state.exists())

    def test_offline_verification_requires_async_completion(self) -> None:
        root = self.root_transcript(async_launch=True)
        write_jsonl(root, [json.loads(line) for line in root.read_text().splitlines()][:2])
        receipt = runtime.verify_named_files(
            root,
            self.child_transcript(),
            projects_root=self.projects,
            role="plan-verifier",
            expected_model="gpt-5.6-sol",
            expected_effort="high",
            prompt_id="native-prompt",
        )
        self.assertEqual(receipt["status"], "SKIPPED")
        self.assertEqual(receipt["reason"], "async_completion_missing")

    def test_session_end_removes_only_its_state(self) -> None:
        runtime.handle(self.prompt())
        path = next((self.state / "sessions").glob("*.json"))
        runtime.handle({"hook_event_name": "SessionEnd", "session_id": "root-session"})
        self.assertFalse(path.exists())

    def test_selftest_is_launchable(self) -> None:
        self.assertEqual(runtime.main(["--selftest"]), 0)


if __name__ == "__main__":
    unittest.main()
