#!/usr/bin/env python3
"""Bounded Claude Code hook runtime for Remora orchestration evidence.

The prompt classifier and readiness-verdict grammar are derived from
Pilotfish commit 71d3cb546e1e94a42c689c249c3c820e3e4a0b30.
Copyright (c) 2026 Nanako0129 and Miyago; used under the MIT License.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = 1
REQUIRED_TASK = "automatic_plan_review"
REVIEW_MODE = "readiness_review"
MAX_HOOK_INPUT_BYTES = 1_048_576
MAX_PROMPT_CHARS = 65_536
MAX_STATE_BYTES = 65_536
MAX_TRANSCRIPT_BYTES = 16 * 1_048_576
MAX_CHILDREN = 64
MAX_SESSIONS = 128
HOOK_TIMEOUT_SECONDS = 10
TRANSCRIPT_SETTLE_ATTEMPTS = 20
TRANSCRIPT_SETTLE_SECONDS = 0.05
ENV_STATE_ROOT = "REMORA_ORCHESTRATION_STATE_ROOT"
ENV_PROJECTS_ROOT = "REMORA_ORCHESTRATION_PROJECTS_ROOT"
ENV_BINDINGS = "REMORA_ORCHESTRATION_ROLE_BINDINGS"
ENV_SOURCE_HASHES = "REMORA_ORCHESTRATION_SOURCE_HASHES"
ENV_NAMES = frozenset(
    {ENV_STATE_ROOT, ENV_PROJECTS_ROOT, ENV_BINDINGS, ENV_SOURCE_HASHES}
)
KNOWN_ROLES = frozenset(
    {
        "Explore",
        "scout",
        "plan-verifier",
        "security-reviewer",
        "mech-executor",
        "executor",
        "verifier",
        "security-executor",
    }
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/+@-]{1,128}$")
_PLAN_RE = re.compile(
    r"(?:\b(?:plan|planning|pre-approval|approval|approve|readiness|proposal)\b|"
    r"計畫|規劃|方案|核准|批准|審核)",
    re.IGNORECASE,
)
_CATEGORY_PATTERNS = {
    "data": re.compile(
        r"\b(?:data|database|schema|serialization|migration|pii|personal data|"
        r"backup|restore)\b|資料|數據|資料庫|結構描述|序列化|遷移|移轉",
        re.IGNORECASE,
    ),
    "external": re.compile(
        r"\b(?:external|third[- ]party|remote system|send (?:email|message)|"
        r"external mutation|external action)\b|外部系統|第三方|對外",
        re.IGNORECASE,
    ),
    "irreversible": re.compile(
        r"\b(?:destructive|irreversible|delete|drop|truncate|purge|overwrite|"
        r"force[- ]push)\b|破壞性|不可逆|刪除|清除|覆寫",
        re.IGNORECASE,
    ),
    "release": re.compile(
        r"\b(?:release|deploy|deployment|production|rollout|publish|shipping)\b|"
        r"發布|發佈|部署|上線|正式環境",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\b(?:security|secure|trust boundary|authentication|authorization|"
        r"authn|authz|credential|secret|permission|iam|cryptography|crypto|"
        r"encryption|vulnerabilit(?:y|ies))\b|安全|信任邊界|身分驗證|身份驗證|"
        r"認證|授權|憑證|密鑰|祕密|秘密|權限|加密|漏洞",
        re.IGNORECASE,
    ),
}
_REVIEW_INTENT_PATTERNS = {
    "fast": re.compile(
        r"(?:快一點|快點|省時間|省錢|節省(?:時間|成本|token)|"
        r"不要額外(?:審查|review|思考)|先不要額外(?:審查|review)|"
        r"as fast as possible|save (?:time|money|tokens)|"
        r"minimi[sz]e cost|skip (?:the )?extra review)",
        re.IGNORECASE,
    ),
    "strict": re.compile(
        r"(?:嚴格(?:審查|review)|完整(?:驗證|測試|審查)|全面(?:審查|驗證)|"
        r"thorough(?:ly)? review|strict review|full verification|"
        r"test thoroughly|be rigorous)",
        re.IGNORECASE,
    ),
    "default": re.compile(
        r"(?:照預設|按預設模式|依照預設|use the default|default mode)",
        re.IGNORECASE,
    ),
}
_REVIEW_INTENT_NEGATION = re.compile(
    r"(?:不要|別|不必|do not|don't|never)\s*"
    r"(?:快|快速|quick|strict|嚴格|完整|thorough|rigorous)",
    re.IGNORECASE,
)
_QUOTED_SEGMENT = re.compile(
    r'"[^"\n]*"|\'[^\'\n]*\'|`[^`\n]*`|「[^」\n]*」|『[^』\n]*』'
)
BLOCK_REASON = (
    "Status: WAITING_FOR_REVIEW. Required independent Plan review evidence is "
    "missing for this native prompt. Call the plan-verifier role with "
    "mode=readiness_review and the automatic_plan_review tag already supplied "
    "in context, collect its completed result, then continue. This is an "
    "internal review dependency, not a user decision."
)


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER_RE.fullmatch(value))


def _safe_runtime_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(SAFE_VALUE_RE.fullmatch(value))
        and not value.startswith("/")
        and ".." not in value
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_prompt_id(prompt_id: str) -> str:
    """Return the receipt-safe native prompt identity."""
    if not _valid_identifier(prompt_id):
        raise ValueError("invalid native prompt_id")
    return _hash_text(prompt_id)


def review_tag(prompt_id: str) -> str:
    """Return the exact task tag a root Agent prompt must contain."""
    return f"{REQUIRED_TASK}:{hash_prompt_id(prompt_id)}"


def review_request(prompt_id: str) -> str:
    """Return the unambiguous prefix required in the reviewer task prompt."""
    return f"{REVIEW_MODE}\n{review_tag(prompt_id)}"


def classify_prompt(prompt: object) -> tuple[str, ...]:
    """Return bounded material-risk labels without retaining prompt text."""
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        return ()
    if _PLAN_RE.search(prompt) is None:
        return ()
    return tuple(
        sorted(
            category
            for category, pattern in _CATEGORY_PATTERNS.items()
            if pattern.search(prompt) is not None
        )
    )


def classify_review_intent(prompt: object) -> str | None:
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        return None
    text = _QUOTED_SEGMENT.sub(" ", prompt)
    if _REVIEW_INTENT_NEGATION.search(text):
        return None
    matches = [
        intent
        for intent, pattern in _REVIEW_INTENT_PATTERNS.items()
        if pattern.search(text) is not None
    ]
    return matches[0] if len(matches) == 1 else None


def requires_review(categories: tuple[str, ...]) -> bool:
    return bool(categories) and ("security" in categories or len(categories) >= 2)


def blocker_fingerprint(prompt: object, categories: tuple[str, ...]) -> str | None:
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        return None
    normalized = re.sub(r"\s+", " ", prompt.strip()).casefold()
    return _hash_text(
        json.dumps(
            {"categories": list(categories), "prompt": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def classify_verdict(text: object) -> str:
    if text == "READY":
        return "READY"
    if not isinstance(text, str) or not text.startswith("REVISE\n"):
        return "MALFORMED"
    if text != text.strip():
        return "MALFORMED"
    lines = [line for line in text.splitlines()[1:] if line]
    fields = ("Blocker:", "Evidence:", "Minimum revision:", "Acceptance check:")
    if not lines or len(lines) % len(fields):
        return "MALFORMED"
    for index, line in enumerate(lines):
        field = fields[index % len(fields)]
        if not line.startswith(field) or not line[len(field) :].strip():
            return "MALFORMED"
    return "REVISE"


def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _secure_mode(info: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return (
        stat.S_ISREG(info.st_mode)
        and (getuid is None or info.st_uid == getuid())
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _secure_directory(path: Path, *, create: bool) -> bool:
    try:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                component = current.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                os.mkdir(current, 0o700)
                component = current.lstat()
            if stat.S_ISLNK(component.st_mode) or not stat.S_ISDIR(component.st_mode):
                return False
        info = path.lstat()
        getuid = getattr(os, "getuid", None)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (getuid is not None and info.st_uid != getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700:
            if not create:
                return False
            os.chmod(path, 0o700)
        return True
    except OSError:
        return False


def _read_file(path: Path, limit: int, *, beneath: Path) -> tuple[bytes, str] | None:
    descriptor: int | None = None
    try:
        base_info = beneath.lstat()
        getuid = getattr(os, "getuid", None)
        if (
            stat.S_ISLNK(base_info.st_mode)
            or not stat.S_ISDIR(base_info.st_mode)
            or (getuid is not None and base_info.st_uid != getuid())
            or base_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return None
        base = beneath.resolve(strict=True)
        lexical = path.relative_to(beneath)
        if ".." in lexical.parts:
            return None
        current = beneath
        for part in lexical.parts:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(base)
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not _secure_mode(before) or before.st_size > limit:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if _fingerprint(opened) != _fingerprint(before):
            return None
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        if (
            len(payload) > limit
            or _fingerprint(after_fd) != _fingerprint(before)
            or _fingerprint(after_path) != _fingerprint(before)
        ):
            return None
        return payload, hashlib.sha256(payload).hexdigest()
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _jsonl(path: Path, *, projects_root: Path) -> tuple[list[dict[str, Any]], str] | None:
    result = _read_file(path, MAX_TRANSCRIPT_BYTES, beneath=projects_root)
    if result is None:
        return None
    payload, digest = result
    try:
        events = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not events or not all(isinstance(event, dict) for event in events):
        return None
    return events, digest


def _state_paths(state_root: Path, session_id: str) -> tuple[Path, Path] | None:
    root = state_root / "sessions"
    if not _secure_directory(state_root, create=True) or not _secure_directory(root, create=True):
        return None
    digest = _hash_text(session_id)
    # ponytail: one lock serializes tiny hook updates; split per session only if
    # measured hook contention becomes material.
    return root / f"{digest}.json", state_root / ".state.lock"


_INVALID = object()


def _valid_state(value: object, session_hash: str) -> bool:
    if not isinstance(value, dict) or set(value) != {"schema", "session_hash", "active", "status"}:
        return False
    status = value.get("status")
    if (
        value.get("schema") != SCHEMA
        or value.get("session_hash") != session_hash
        or not isinstance(status, dict)
        or set(status) != {"status", "reason"}
        or status.get("status") not in {"SKIPPED", "CONFIGURED", "WAITING_FOR_REVIEW", "VERIFIED", "OBSERVED"}
        or not isinstance(status.get("reason"), str)
        or not SAFE_VALUE_RE.fullmatch(status["reason"])
    ):
        return False
    active = value.get("active")
    if active is None:
        return True
    active_keys = {
        "prompt_hash", "blocker_fingerprint", "categories", "intent", "required",
        "attempted", "children", "started_at",
    }
    if not isinstance(active, dict) or set(active) != active_keys:
        return False
    categories = active.get("categories")
    if (
        not isinstance(active.get("prompt_hash"), str)
        or not HASH_RE.fullmatch(active["prompt_hash"])
        or (
            active.get("blocker_fingerprint") is not None
            and (
                not isinstance(active["blocker_fingerprint"], str)
                or not HASH_RE.fullmatch(active["blocker_fingerprint"])
            )
        )
        or not isinstance(categories, list)
        or categories != sorted(set(categories))
        or any(category not in _CATEGORY_PATTERNS for category in categories)
        or active.get("intent") not in {None, "fast", "default", "strict"}
        or not isinstance(active.get("required"), bool)
        or not isinstance(active.get("attempted"), bool)
        or not isinstance(active.get("started_at"), str)
        or not SAFE_VALUE_RE.fullmatch(active["started_at"])
    ):
        return False
    children = active.get("children")
    return (
        isinstance(children, list)
        and len(children) <= MAX_CHILDREN
        and all(_valid_child_state(child) for child in children)
    )


def _valid_child_state(child: object) -> bool:
    required = {"agent_hash", "parent_hash", "role", "expected", "hook_effort", "completed"}
    optional = {
        "observed", "transcript_hash", "hook_effort_stop", "correlated",
        "correlation_reason",
    }
    if not isinstance(child, dict) or not required.issubset(child) or set(child) - required - optional:
        return False
    expected = child.get("expected")
    if (
        child.get("role") not in KNOWN_ROLES
        or not all(isinstance(child.get(key), str) and HASH_RE.fullmatch(child[key]) for key in ("agent_hash", "parent_hash"))
        or not isinstance(expected, dict)
        or set(expected) != {"model", "effort"}
        or not all(_safe_runtime_value(item) for item in expected.values())
        or child.get("hook_effort") is not None
        and not _safe_runtime_value(child["hook_effort"])
        or not isinstance(child.get("completed"), bool)
    ):
        return False
    if "transcript_hash" in child and (
        not isinstance(child["transcript_hash"], str) or not HASH_RE.fullmatch(child["transcript_hash"])
    ):
        return False
    if "hook_effort_stop" in child and child["hook_effort_stop"] is not None and (
        not _safe_runtime_value(child["hook_effort_stop"])
    ):
        return False
    if "correlated" in child and not isinstance(child["correlated"], bool):
        return False
    if "correlation_reason" in child and (
        not isinstance(child["correlation_reason"], str) or not SAFE_VALUE_RE.fullmatch(child["correlation_reason"])
    ):
        return False
    observed = child.get("observed")
    if observed is None:
        return not child["completed"]
    if not isinstance(observed, dict) or set(observed) != {
        "model", "effort", "mixed_model", "mixed_effort", "missing_model",
        "missing_effort", "identity_match", "verdict",
    }:
        return False
    return (
        all(
            observed.get(key) is None or _safe_runtime_value(observed[key])
            for key in ("model", "effort")
        )
        and all(isinstance(observed.get(key), bool) for key in ("mixed_model", "mixed_effort", "missing_model", "missing_effort", "identity_match"))
        and observed.get("verdict") in {"READY", "REVISE", "MALFORMED"}
    )


def _load_state(path: Path, session_hash: str) -> dict[str, Any] | object | None:
    if not path.exists():
        return None
    result = _read_file(path, MAX_STATE_BYTES, beneath=path.parent)
    if result is None:
        return _INVALID
    try:
        state = json.loads(result[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID
    if not _valid_state(state, session_hash):
        return _INVALID
    return state


def _atomic_json(path: Path, value: dict[str, Any], *, limit: int) -> bool:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(payload) > limit:
        return False
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        if path.exists() and not _secure_mode(path.lstat()):
            return False
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and path.is_symlink():
            return False
        os.replace(temporary, path)
        temporary = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _with_state(
    session_id: str,
    state_root: Path,
    update: Any,
) -> Any:
    paths = _state_paths(state_root, session_id)
    if paths is None:
        return None
    path, lock_path = paths
    session_hash = _hash_text(session_id)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        lock_info = os.fstat(lock_fd)
        if not _secure_mode(lock_info):
            return None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        loaded = _load_state(path, session_hash)
        if loaded is _INVALID:
            return None
        if loaded is None and len(list(path.parent.glob("*.json"))) >= MAX_SESSIONS:
            return None
        state = loaded or {
            "schema": SCHEMA,
            "session_hash": session_hash,
            "active": None,
            "status": {"status": "SKIPPED", "reason": "no_prompt"},
        }
        result = update(state)
        if result is _DELETE:
            path.unlink(missing_ok=True)
            return True
        if not _atomic_json(path, state, limit=MAX_STATE_BYTES):
            return None
        return result if result is not None else True
    except OSError:
        return None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


_DELETE = object()


def _bindings() -> dict[str, dict[str, str]] | None:
    try:
        value = json.loads(os.environ[ENV_BINDINGS])
    except (KeyError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) - KNOWN_ROLES:
        return None
    result: dict[str, dict[str, str]] = {}
    for role, binding in value.items():
        if not isinstance(binding, dict) or set(binding) != {"model", "effort"}:
            return None
        if not all(_safe_runtime_value(item) for item in binding.values()):
            return None
        result[role] = dict(binding)
    return result


def _source_hashes() -> dict[str, str] | None:
    try:
        value = json.loads(os.environ[ENV_SOURCE_HASHES])
    except (KeyError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"policy", "roles", "runtime"}
        or not all(isinstance(item, str) and HASH_RE.fullmatch(item) for item in value.values())
    ):
        return None
    return value


def _current_sources(expected: dict[str, str]) -> bool:
    root = Path(__file__).resolve().parent.parent
    paths = {
        "policy": root / "agents" / "orchestration.md",
        "roles": root / "agents" / "agents.json",
        "runtime": Path(__file__).resolve(),
    }
    try:
        return all(source_hash(path) == expected[name] for name, path in paths.items())
    except OSError:
        return False


def _context(payload: dict[str, Any], categories: tuple[str, ...], intent: str | None) -> dict[str, Any] | None:
    prompt_id = payload.get("prompt_id")
    if not _valid_identifier(prompt_id):
        return None
    prompt_hash = hash_prompt_id(prompt_id)
    signal: dict[str, Any] = {
        "schema": SCHEMA,
        "prompt_hash": prompt_hash,
        "risk_categories": list(categories),
    }
    if intent:
        signal.update(
            {
                "review_intent": intent,
                "review_intent_source": "explicit",
                "review_intent_scope": "prompt",
            }
        )
    if requires_review(categories):
        signal.update(
            {
                "required_role": "plan-verifier",
                "mode": REVIEW_MODE,
                "contract_tag": review_tag(prompt_id),
                "request_prefix": review_request(prompt_id),
            }
        )
    if len(signal) == 3 and not intent:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "Remora orchestration signal: "
            + json.dumps(signal, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        }
    }


def _effort(payload: dict[str, Any]) -> str | None:
    value = payload.get("effort")
    if isinstance(value, dict):
        value = value.get("level")
    return value if _safe_runtime_value(value) else None


def _message_content(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) and all(isinstance(x, dict) for x in content) else []


def _child_observation(
    events: list[dict[str, Any]],
    verdict_text: object,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    models: set[str] = set()
    efforts: set[str] = set()
    texts: list[str] = []
    missing_model = False
    missing_effort = False
    identity_match = True
    for event in events:
        if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
            continue
        if agent_id is not None and (
            event.get("agentId") != agent_id or event.get("sessionId") != session_id
        ):
            identity_match = False
        model = event["message"].get("model")
        effort = event.get("effort")
        if isinstance(effort, dict):
            effort = effort.get("level")
        if _safe_runtime_value(model):
            models.add(model)
        else:
            missing_model = True
        if _safe_runtime_value(effort):
            efforts.add(effort)
        else:
            missing_effort = True
        for item in _message_content(event):
            if item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str):
                texts.append(item["text"])
    transcript_text = texts[-1] if texts else None
    verdict = classify_verdict(transcript_text)
    if verdict_text is not None and verdict_text != transcript_text:
        verdict = "MALFORMED"
    return {
        "model": next(iter(models)) if len(models) == 1 else None,
        "effort": next(iter(efforts)) if len(efforts) == 1 else None,
        "mixed_model": len(models) > 1,
        "mixed_effort": len(efforts) > 1,
        "missing_model": missing_model,
        "missing_effort": missing_effort,
        "identity_match": identity_match,
        "verdict": verdict,
    }


def _tool_pairs(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    uses: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        for item in _message_content(event):
            if item.get("type") == "tool_use" and _valid_identifier(item.get("id")):
                uses[item["id"]] = item
            elif item.get("type") == "tool_result" and _valid_identifier(item.get("tool_use_id")):
                result = event.get("toolUseResult")
                if isinstance(result, dict):
                    results[item["tool_use_id"]] = result
    return uses, results


def _correlate_root(
    events: list[dict[str, Any]],
    child: dict[str, Any],
    active: dict[str, Any],
    *,
    session_id: str,
) -> tuple[bool, str]:
    message_events = [
        event
        for event in events
        if event.get("type") in {"assistant", "user"}
        and isinstance(event.get("message"), dict)
    ]
    if not message_events or any(event.get("sessionId") != session_id for event in message_events):
        return False, "root_session_identity_mismatch"
    uses, results = _tool_pairs(events)
    launches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for tool_id, use in uses.items():
        if use.get("name") not in {"Agent", "Task"}:
            continue
        inputs = use.get("input")
        result = results.get(tool_id)
        if not isinstance(inputs, dict) or not isinstance(result, dict):
            continue
        agent_id = result.get("agentId")
        role = inputs.get("subagent_type", inputs.get("agent_type"))
        if (
            isinstance(agent_id, str)
            and _hash_text(agent_id) == child.get("agent_hash")
            and role == child.get("role")
        ):
            launches.append((tool_id, inputs, result))
    if len(launches) != 1:
        return False, "parent_child_link_missing"
    _, inputs, result = launches[0]
    if "model" in inputs:
        return False, "invocation_model_override"
    resolved_model = result.get("resolvedModel")
    if not _safe_runtime_value(resolved_model):
        return False, "resolved_model_missing"
    if resolved_model != child["expected"]["model"]:
        return False, "resolved_model_mismatch"
    if child.get("role") == "plan-verifier" and active.get("required"):
        prompt = inputs.get("prompt")
        expected_tag = f"{REQUIRED_TASK}:{active['prompt_hash']}"
        lines = [line.strip() for line in prompt.splitlines() if line.strip()] if isinstance(prompt, str) else []
        if lines[:2] != [REVIEW_MODE, expected_tag]:
            return False, "readiness_contract_missing"
    if result.get("status") == "async_launched":
        raw_agent = result.get("agentId")
        completed = False
        for tool_id, use in uses.items():
            if use.get("name") != "TaskOutput" or not isinstance(use.get("input"), dict):
                continue
            if use["input"].get("task_id") != raw_agent:
                continue
            output = results.get(tool_id)
            task = output.get("task") if isinstance(output, dict) else None
            if (
                isinstance(output, dict)
                and output.get("retrieval_status") in {"success", "completed"}
                and isinstance(task, dict)
                and task.get("status") == "completed"
                and task.get("task_type") == "local_agent"
                and task.get("task_id") == raw_agent
            ):
                completed = True
        if not completed:
            return False, "async_completion_missing"
    elif result.get("status") not in {"completed", "success"}:
        return False, "completion_missing"
    return True, "complete"


def _receipt(
    *,
    role: str,
    contract: str,
    active: dict[str, Any],
    child: dict[str, Any],
    source_hashes: dict[str, str],
    root_hash: str,
) -> dict[str, Any]:
    expected = child["expected"]
    observed = child["observed"]
    status = "VERIFIED"
    reason = "matching_runtime_evidence"
    if not child.get("correlated"):
        status, reason = "SKIPPED", child.get("correlation_reason", "uncorrelated")
    elif not child.get("completed"):
        status, reason = "SKIPPED", "child_completion_missing"
    elif observed.get("mixed_model") or observed.get("mixed_effort"):
        status, reason = "FAILED", "mixed_runtime_observation"
    elif (
        not observed.get("model")
        or not observed.get("effort")
        or observed.get("missing_model")
        or observed.get("missing_effort")
    ):
        status, reason = "SKIPPED", "observed_model_or_effort_missing"
    elif not observed.get("identity_match"):
        status, reason = "FAILED", "child_session_identity_mismatch"
    elif observed["model"] != expected["model"] or observed["effort"] != expected["effort"]:
        status, reason = "FAILED", "model_or_effort_mismatch"
    elif (
        (child.get("hook_effort") and child["hook_effort"] != observed["effort"])
        or (
            child.get("hook_effort_stop")
            and child["hook_effort_stop"] != observed["effort"]
        )
    ):
        status, reason = "FAILED", "hook_transcript_effort_mismatch"
    elif contract == REQUIRED_TASK and observed.get("verdict") == "MALFORMED":
        status, reason = "FAILED", "invalid_readiness_verdict"
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "reason": reason,
        "role": role,
        "contract": contract,
        "completion_status": "completed" if child.get("completed") else "unknown",
        "verdict_status": observed.get("verdict", "MALFORMED"),
        "readiness_granted": contract == REQUIRED_TASK and observed.get("verdict") == "READY" and status == "VERIFIED",
        "expected_model": expected["model"],
        "expected_effort": expected["effort"],
        "source_hashes": source_hashes,
        "evidence_hashes": {
            "prompt": active["prompt_hash"],
            "parent": child["parent_hash"],
            "child": child["agent_hash"],
            "root_transcript": root_hash,
            "child_transcript": child.get("transcript_hash", ""),
        },
        "recorded_at": _now(),
    }
    if observed.get("model"):
        receipt["observed_model"] = observed["model"]
    if observed.get("effort"):
        receipt["observed_effort"] = observed["effort"]
    return receipt


def _write_receipt(state_root: Path, receipt: dict[str, Any]) -> bool:
    directory = state_root / "latest"
    if not _secure_directory(state_root, create=True) or not _secure_directory(directory, create=True):
        return False
    role = receipt.get("role")
    contract = receipt.get("contract")
    if role not in KNOWN_ROLES or contract not in {REQUIRED_TASK, "typed_dispatch"}:
        return False
    path = directory / f"{role}-{contract}.json"
    if path.exists():
        existing = _read_file(path, MAX_STATE_BYTES, beneath=directory)
        if existing is None:
            return False
        try:
            parsed = json.loads(existing[0])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not _valid_receipt(parsed):
            return False
    return _atomic_json(path, receipt, limit=MAX_STATE_BYTES)


def _valid_receipt(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "schema", "status", "reason", "role", "contract", "completion_status",
        "verdict_status", "readiness_granted", "expected_model", "expected_effort",
        "source_hashes", "evidence_hashes", "recorded_at",
    }
    if not required.issubset(value) or set(value) - required - {"observed_model", "observed_effort"}:
        return False
    if (
        value.get("schema") != SCHEMA
        or value.get("status") not in {"VERIFIED", "SKIPPED", "FAILED"}
        or value.get("role") not in KNOWN_ROLES
        or value.get("contract") not in {REQUIRED_TASK, "typed_dispatch"}
        or value.get("completion_status") not in {"completed", "unknown"}
        or value.get("verdict_status") not in {"READY", "REVISE", "MALFORMED"}
        or not isinstance(value.get("readiness_granted"), bool)
    ):
        return False
    for key in ("reason", "recorded_at"):
        if not isinstance(value.get(key), str) or not SAFE_VALUE_RE.fullmatch(value[key]):
            return False
    for key in ("expected_model", "expected_effort", "observed_model", "observed_effort"):
        if key in value and not _safe_runtime_value(value[key]):
            return False
    for group in ("source_hashes", "evidence_hashes"):
        hashes = value.get(group)
        if not isinstance(hashes, dict) or not hashes or not all(isinstance(item, str) and HASH_RE.fullmatch(item) for item in hashes.values()):
            return False
        if group == "source_hashes" and set(hashes) != {
            "policy",
            "roles",
            "runtime",
        }:
            return False
    return True


def _roots() -> tuple[Path, Path] | None:
    state = os.environ.get(ENV_STATE_ROOT)
    projects = os.environ.get(ENV_PROJECTS_ROOT)
    if not state or not projects:
        return None
    return Path(state), Path(projects)


def _handle_prompt(payload: dict[str, Any], state_root: Path) -> dict[str, Any] | None:
    session_id = payload.get("session_id")
    if not _valid_identifier(session_id):
        return None
    if payload.get("agent_id") is not None or payload.get("agent_type") is not None:
        return None
    prompt_id = payload.get("prompt_id")
    if not _valid_identifier(prompt_id):
        _with_state(
            session_id,
            state_root,
            lambda state: state.update(
                {"active": None, "status": {"status": "SKIPPED", "reason": "native_prompt_id_missing"}}
            ),
        )
        return None
    prompt = payload.get("prompt")
    categories = classify_prompt(prompt)
    intent = classify_review_intent(prompt)
    prompt_hash = hash_prompt_id(prompt_id)
    fingerprint = blocker_fingerprint(prompt, categories)

    def update(state: dict[str, Any]) -> str | None:
        previous = state.get("active")
        if isinstance(previous, dict) and previous.get("prompt_hash") == prompt_hash:
            return "duplicate_prompt"
        attempted = bool(
            isinstance(previous, dict)
            and previous.get("blocker_fingerprint") == fingerprint
            and previous.get("required")
        ) and bool(previous.get("attempted"))
        state["active"] = {
            "prompt_hash": prompt_hash,
            "blocker_fingerprint": fingerprint,
            "categories": list(categories),
            "intent": intent,
            "required": requires_review(categories),
            "attempted": attempted,
            "children": [],
            "started_at": _now(),
        }
        state["status"] = {"status": "CONFIGURED", "reason": "prompt_observed"}
        return None

    updated = _with_state(session_id, state_root, update)
    if updated is None or updated == "duplicate_prompt":
        return None
    return _context(payload, categories, intent)


def _handle_subagent_start(payload: dict[str, Any], state_root: Path) -> None:
    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    agent_id = payload.get("agent_id")
    role = payload.get("agent_type")
    if not all(_valid_identifier(value) for value in (session_id, prompt_id, agent_id)) or role not in KNOWN_ROLES:
        return
    bindings = _bindings()
    if bindings is None or role not in bindings:
        return

    def update(state: dict[str, Any]) -> None:
        active = state.get("active")
        if not isinstance(active, dict) or active.get("prompt_hash") != hash_prompt_id(prompt_id):
            state["status"] = {"status": "SKIPPED", "reason": "prompt_identity_mismatch"}
            return
        children = active.get("children")
        if not isinstance(children, list) or len(children) >= MAX_CHILDREN:
            state["status"] = {"status": "SKIPPED", "reason": "child_budget_exceeded"}
            return
        agent_hash = _hash_text(agent_id)
        if any(child.get("agent_hash") == agent_hash for child in children if isinstance(child, dict)):
            state["status"] = {"status": "SKIPPED", "reason": "duplicate_child_start"}
            return
        children.append(
            {
                "agent_hash": agent_hash,
                "parent_hash": _hash_text(session_id),
                "role": role,
                "expected": bindings[role],
                "hook_effort": _effort(payload),
                "completed": False,
            }
        )

    _with_state(session_id, state_root, update)


def _handle_subagent_stop(payload: dict[str, Any], state_root: Path, projects_root: Path) -> None:
    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    agent_id = payload.get("agent_id")
    role = payload.get("agent_type")
    transcript_value = payload.get("agent_transcript_path", payload.get("transcript_path"))
    root_transcript_value = payload.get("transcript_path")
    if (
        not all(_valid_identifier(value) for value in (session_id, prompt_id, agent_id))
        or role not in KNOWN_ROLES
        or not isinstance(transcript_value, str)
        or not isinstance(root_transcript_value, str)
    ):
        return
    path = Path(transcript_value)
    root_path = Path(root_transcript_value)
    if (
        not path.is_absolute()
        or not root_path.is_absolute()
        or root_path.suffix != ".jsonl"
        or root_path.stem != session_id
        or path.name != f"agent-{agent_id}.jsonl"
        or path.parent != root_path.with_suffix("") / "subagents"
    ):
        return
    decoded = None
    observation = None
    for attempt in range(TRANSCRIPT_SETTLE_ATTEMPTS):
        decoded = _jsonl(path, projects_root=projects_root)
        if decoded is not None:
            observation = _child_observation(
                decoded[0],
                payload.get("last_assistant_message"),
                agent_id=agent_id,
                session_id=session_id,
            )
            if (
                not observation["missing_model"]
                and not observation["missing_effort"]
                and observation["verdict"] != "MALFORMED"
            ):
                break
        if attempt + 1 < TRANSCRIPT_SETTLE_ATTEMPTS:
            time.sleep(TRANSCRIPT_SETTLE_SECONDS)
    if decoded is None or observation is None:
        return
    _, transcript_hash = decoded

    def update(state: dict[str, Any]) -> None:
        active = state.get("active")
        if not isinstance(active, dict) or active.get("prompt_hash") != hash_prompt_id(prompt_id):
            state["status"] = {"status": "SKIPPED", "reason": "prompt_identity_mismatch"}
            return
        matches = [
            child
            for child in active.get("children", [])
            if isinstance(child, dict)
            and child.get("agent_hash") == _hash_text(agent_id)
            and child.get("role") == role
        ]
        if len(matches) != 1 or matches[0].get("completed"):
            state["status"] = {"status": "SKIPPED", "reason": "child_lifecycle_mismatch"}
            return
        matches[0].update(
            {
                "completed": True,
                "observed": observation,
                "transcript_hash": transcript_hash,
                "hook_effort_stop": _effort(payload),
            }
        )

    _with_state(session_id, state_root, update)


def _handle_root_stop(payload: dict[str, Any], state_root: Path, projects_root: Path) -> dict[str, str] | None:
    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    transcript_value = payload.get("transcript_path")
    if (
        not _valid_identifier(session_id)
        or not _valid_identifier(prompt_id)
        or not isinstance(transcript_value, str)
        or not Path(transcript_value).is_absolute()
    ):
        return None
    decoded = _jsonl(Path(transcript_value), projects_root=projects_root)
    if decoded is None:
        return None
    events, root_hash = decoded
    sources = _source_hashes()
    source_ok = sources is not None and _current_sources(sources)
    produced: list[dict[str, Any]] = []

    def update(state: dict[str, Any]) -> dict[str, str] | None:
        active = state.get("active")
        if not isinstance(active, dict) or active.get("prompt_hash") != hash_prompt_id(prompt_id):
            state["status"] = {"status": "SKIPPED", "reason": "prompt_identity_mismatch"}
            return None
        for child in active.get("children", []):
            if not isinstance(child, dict) or not child.get("completed"):
                continue
            correlated, reason = _correlate_root(
                events, child, active, session_id=session_id
            )
            child["correlated"] = correlated
            child["correlation_reason"] = reason
            contract = REQUIRED_TASK if child.get("role") == "plan-verifier" and active.get("required") else "typed_dispatch"
            receipt = _receipt(
                role=child["role"],
                contract=contract,
                active=active,
                child=child,
                source_hashes=sources or {"policy": "0" * 64, "roles": "0" * 64, "runtime": "0" * 64},
                root_hash=root_hash,
            )
            if not source_ok:
                receipt.update(status="FAILED", reason="source_drift", readiness_granted=False)
            produced.append(receipt)
        review = [receipt for receipt in produced if receipt["contract"] == REQUIRED_TASK]
        valid = len(review) == 1 and review[0]["status"] == "VERIFIED"
        state["status"] = (
            {"status": "VERIFIED", "reason": review[0]["verdict_status"]}
            if valid
            else {"status": "WAITING_FOR_REVIEW", "reason": "review_evidence_missing"}
            if active.get("required")
            else {"status": "OBSERVED", "reason": "no_required_review"}
        )
        if active.get("required") and not valid and payload.get("stop_hook_active") is False and not active.get("attempted"):
            active["attempted"] = True
            return {"decision": "block", "reason": BLOCK_REASON}
        return None

    response = _with_state(session_id, state_root, update)
    for receipt in produced:
        _write_receipt(state_root, receipt)
    return response if isinstance(response, dict) else None


def handle(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    roots = _roots()
    if roots is None:
        return None
    state_root, projects_root = roots
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        return _handle_prompt(payload, state_root)
    if event == "SubagentStart":
        _handle_subagent_start(payload, state_root)
    elif event == "SubagentStop":
        _handle_subagent_stop(payload, state_root, projects_root)
    elif event == "Stop" and payload.get("agent_id") is None:
        return _handle_root_stop(payload, state_root, projects_root)
    elif event == "SessionEnd" and _valid_identifier(payload.get("session_id")):
        _with_state(payload["session_id"], state_root, lambda state: _DELETE)
    return None


def status(state_root: Path) -> dict[str, Any]:
    latest = state_root / "latest"
    receipts: list[dict[str, Any]] = []
    if _secure_directory(latest, create=False):
        for path in sorted(latest.glob("*.json")):
            result = _read_file(path, MAX_STATE_BYTES, beneath=latest)
            if result is None:
                continue
            try:
                receipt = json.loads(result[0])
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if _valid_receipt(receipt):
                if not _current_sources(receipt["source_hashes"]):
                    receipt = dict(receipt)
                    receipt.update(
                        status="FAILED",
                        reason="source_drift",
                        readiness_granted=False,
                    )
                receipts.append(receipt)
    return {"schema": SCHEMA, "registration": "configured_unobserved" if not receipts else "observed", "receipts": receipts}


def verify_named_files(
    root_transcript: Path,
    child_transcript: Path,
    *,
    projects_root: Path,
    role: str,
    expected_model: str,
    expected_effort: str,
    prompt_id: str,
) -> dict[str, Any]:
    """Verify explicit fixtures without reading or updating live receipt state."""
    if (
        role not in KNOWN_ROLES
        or not _valid_identifier(prompt_id)
        or not _safe_runtime_value(expected_model)
        or not _safe_runtime_value(expected_effort)
    ):
        raise ValueError("invalid role or prompt_id")
    root = _jsonl(root_transcript, projects_root=projects_root)
    child = _jsonl(child_transcript, projects_root=projects_root)
    if root is None or child is None:
        return {"schema": SCHEMA, "status": "SKIPPED", "reason": "invalid_named_evidence"}
    uses, results = _tool_pairs(root[0])
    candidates: list[str] = []
    for tool_id, use in uses.items():
        inputs = use.get("input")
        result = results.get(tool_id)
        if (
            use.get("name") in {"Agent", "Task"}
            and isinstance(inputs, dict)
            and inputs.get("subagent_type", inputs.get("agent_type")) == role
            and isinstance(result, dict)
            and _valid_identifier(result.get("agentId"))
        ):
            candidates.append(result["agentId"])
    session_ids = {
        event.get("sessionId")
        for event in root[0]
        if event.get("type") in {"assistant", "user"}
        and isinstance(event.get("message"), dict)
        and _valid_identifier(event.get("sessionId"))
    }
    if len(candidates) != 1 or len(session_ids) != 1:
        return {"schema": SCHEMA, "status": "SKIPPED", "reason": "parent_child_link_missing"}
    agent_id = candidates[0]
    session_id = next(iter(session_ids))
    active = {
        "prompt_hash": hash_prompt_id(prompt_id),
        "required": role == "plan-verifier",
    }
    observation = _child_observation(
        child[0],
        None,
        agent_id=agent_id,
        session_id=session_id,
    )
    child_state = {
        "agent_hash": _hash_text(agent_id),
        "parent_hash": _hash_text(session_id),
        "role": role,
        "expected": {"model": expected_model, "effort": expected_effort},
        "hook_effort": None,
        "completed": True,
        "observed": observation,
        "transcript_hash": child[1],
    }
    correlated, reason = _correlate_root(
        root[0], child_state, active, session_id=session_id
    )
    child_state.update(correlated=correlated, correlation_reason=reason)
    source_hashes = {
        "policy": source_hash(Path(__file__).resolve().parent.parent / "agents" / "orchestration.md"),
        "roles": source_hash(Path(__file__).resolve().parent.parent / "agents" / "agents.json"),
        "runtime": source_hash(Path(__file__).resolve()),
    }
    return _receipt(
        role=role,
        contract=REQUIRED_TASK if role == "plan-verifier" else "typed_dispatch",
        active=active,
        child=child_state,
        source_hashes=source_hashes,
        root_hash=root[1],
    )


SELFTEST_OK = f"remora-orchestration-runtime schema={SCHEMA} launchable"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--selftest"]:
        print(SELFTEST_OK)
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--root-transcript")
    parser.add_argument("--child-transcript")
    parser.add_argument("--projects-root")
    parser.add_argument("--role")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--prompt-id")
    parser.add_argument("--output")
    parsed, unknown = parser.parse_known_args(args)
    if parsed.verify:
        if unknown:
            return 2
        required = (
            parsed.root_transcript,
            parsed.child_transcript,
            parsed.projects_root,
            parsed.role,
            parsed.model,
            parsed.effort,
            parsed.prompt_id,
        )
        if not all(required):
            return 2
        receipt = verify_named_files(
            Path(parsed.root_transcript),
            Path(parsed.child_transcript),
            projects_root=Path(parsed.projects_root),
            role=parsed.role,
            expected_model=parsed.model,
            expected_effort=parsed.effort,
            prompt_id=parsed.prompt_id,
        )
        rendered = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        if parsed.output:
            output = Path(parsed.output)
            if not _secure_directory(
                output.parent, create=not output.parent.exists()
            ) or not _atomic_json(output, receipt, limit=MAX_STATE_BYTES):
                return 2
        else:
            sys.stdout.write(rendered)
        return 0
    if args:
        return 0
    payload = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(payload) > MAX_HOOK_INPUT_BYTES:
        return 0
    try:
        result = handle(json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if result is not None:
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
