#!/usr/bin/env python3
"""Session-scoped OpenAI agent routing for Claude Code."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "0.1.15"
ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = ROOT / "agents" / "agents.json"
ORCHESTRATION_FILE = ROOT / "agents" / "orchestration.md"
DEFAULT_CONFIG = Path.home() / ".config" / "remora-cc" / "config.toml"
MODEL_ENV = {
    "default_opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "default_sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "default_haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
DEFAULT_CONTEXT_MODE = "stock"
DEFAULT_STOCK_CONTEXT_WINDOW = 200_000
DEFAULT_PROVIDER_CONTEXT_WINDOW = 372_000
DEFAULT_CODEX_CONTEXT_WINDOW = 272_000
DEFAULT_CODEX_CACHE_TTL_SECONDS = 300
DEFAULT_EFFECTIVE_CONTEXT_PERCENT = 95
DEFAULT_AUTO_COMPACT_PERCENT = 90
AUTO_COMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
AUTO_COMPACT_PERCENT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
CLAUDE_EXTRA_BODY_ENV = "CLAUDE_CODE_EXTRA_BODY"
COMPOSE_SYSTEM_PROMPT_ENV = "REMORA_COMPOSE_SYSTEM_PROMPT"
CALLER_SYSTEM_PROMPT_ENV = "REMORA_CALLER_SYSTEM_PROMPT"
FAST_SERVICE_TIER = "priority"
FAST_ACCEPTED_SERVICE_TIERS = {"fast", FAST_SERVICE_TIER}
REMORA_BUILTIN_COMMANDS = {
    "doctor",
    "agents",
    "render-agents",
    "dry-run",
    "version",
    "--version",
    "-V",
    "help",
    "--help",
    "-h",
}
CALICO_CONTEXT_MAP_ENV = "CALICO_MODEL_CONTEXT_WINDOWS"
CALICO_DISPLAY_PERCENT_ENV = "CALICO_CONTEXT_DISPLAY_PERCENT"
# coralline (a Claude Code statusline) keeps cross-session stores for its 5h/7d
# rate-limit segments (high-water dirs) and its optional burn segment (a 5h
# sample log driving the ETA/slope). All of them come from Anthropic API
# responses, so a remora child (talking to a GPT gateway on a different account)
# must not share the host's native stores or the two accounts poison each other.
# We point the child at a per-gateway subdir; the host statusline ignores these
# vars when coralline is not installed.
CORALLINE_STORE_ENV = {
    "CORALLINE_RL5H_FILE": "limit-5h",
    "CORALLINE_RL7D_FILE": "limit-7d",
    "CORALLINE_BURN_FILE": "burn-5h.tsv",
}
CORALLINE_GATEWAY_PREFIX_MAX = 80
CALICO_ACTIVE_TURN_MARKERS = (
    b"calico-active-turn-adapter:v1",
    b"x-calico-prompt-id",
    b"x-calico-active-turn-version",
)
GATEWAY_ACTIVE_TURN_HEADER = "X-CLIProxyAPI-Codex-Active-Turn"
SETTINGS_FILE_ENV = "_REMORA_SETTINGS_FILE"
SETTINGS_GUARD_FD_ENV = "_REMORA_SETTINGS_GUARD_FD"
SETTINGS_CLEANUP_SCRIPT = """\
import os
import sys

if os.fork():
    os._exit(0)
fd = int(sys.argv[1])
path = sys.argv[2]
try:
    while os.read(fd, 4096):
        pass
finally:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
"""
PROTECTED_SETTINGS_ENV = frozenset(
    {
        "REMORA_ACTIVE",
        "REMORA_AUTH_TOKEN",
        COMPOSE_SYSTEM_PROMPT_ENV,
        CALLER_SYSTEM_PROMPT_ENV,
        SETTINGS_FILE_ENV,
        SETTINGS_GUARD_FD_ENV,
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        *MODEL_ENV.values(),
        "CLAUDE_CODE_SUBAGENT_MODEL",
        CLAUDE_EXTRA_BODY_ENV,
        AUTO_COMPACT_ENV,
        AUTO_COMPACT_PERCENT_ENV,
        "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
        "ENABLE_TOOL_SEARCH",
        CALICO_CONTEXT_MAP_ENV,
        CALICO_DISPLAY_PERCENT_ENV,
        "CORALLINE_CONFIG",
        *CORALLINE_STORE_ENV,
    }
)


class RemoraError(RuntimeError):
    """An actionable configuration or launch error."""


def config_path() -> Path:
    override = os.environ.get("REMORA_CONFIG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected = path or config_path()
    try:
        with selected.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RemoraError(
            f"configuration not found: {selected}\n"
            "copy config.example.toml there or run ./install.sh"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise RemoraError(f"invalid TOML in {selected}: {exc}") from exc
    validate_config(data)
    return data


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "proxy.base_url": config.get("proxy", {}).get("base_url"),
        "models.main": config.get("models", {}).get("main"),
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RemoraError(f"missing required configuration: {', '.join(missing)}")

    definitions = load_agent_definitions()
    model_map = config.get("agent_models", {})
    effort_map = config.get("agent_effort", {})
    for name, definition in definitions.items():
        if not agent_config_value(model_map, name, definition):
            raise RemoraError(f"agent_models.{name} is missing")
        if not agent_config_value(effort_map, name, definition):
            raise RemoraError(f"agent_effort.{name} is missing")

    context = config.get("context", {})
    mode = str(context.get("mode", DEFAULT_CONTEXT_MODE)).strip().lower()
    if mode not in {"stock", "calico"}:
        raise RemoraError("context.mode must be 'stock' or 'calico'")
    discovery = context.get("discovery", True)
    if not isinstance(discovery, bool):
        raise RemoraError("context.discovery must be true or false")
    codex_cache = context.get("codex_models_cache", "")
    if not isinstance(codex_cache, str):
        raise RemoraError("context.codex_models_cache must be a path string")
    fallback_window = context_integer(
        context, "fallback_window", DEFAULT_PROVIDER_CONTEXT_WINDOW, minimum=1
    )
    context_integer(
        context, "codex_fallback_window", DEFAULT_CODEX_CONTEXT_WINDOW, minimum=1
    )
    context_integer(
        context,
        "codex_cache_ttl_seconds",
        DEFAULT_CODEX_CACHE_TTL_SECONDS,
        minimum=0,
    )
    context_integer(
        context, "stock_window", DEFAULT_STOCK_CONTEXT_WINDOW, minimum=1
    )
    effective_percent = context_percentage(
        context, "effective_window_percent", DEFAULT_EFFECTIVE_CONTEXT_PERCENT
    )
    compact_percent = context_percentage(
        context, "auto_compact_percent", DEFAULT_AUTO_COMPACT_PERCENT
    )
    if compact_percent > effective_percent:
        raise RemoraError(
            "context.auto_compact_percent must not exceed effective_window_percent"
        )


def context_integer(
    context: dict[str, Any], key: str, default: int, *, minimum: int
) -> int:
    value = context.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if minimum == 0 else "positive"
        raise RemoraError(f"context.{key} must be a {qualifier} integer")
    return value


def context_percentage(context: dict[str, Any], key: str, default: int) -> int:
    value = context.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise RemoraError(f"context.{key} must be an integer from 1 to 100")
    return value


def load_agent_definitions() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoraError(f"cannot load agent definitions from {AGENTS_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise RemoraError("agents/agents.json must contain a JSON object")
    return data


def agent_config_value(
    values: dict[str, Any], name: str, definition: dict[str, Any]
) -> str:
    """Resolve a role setting while keeping pre-eight-role configs usable."""
    value = str(values.get(name, "")).strip()
    if value:
        return value
    fallback = str(definition.get("_routing_fallback", "")).strip()
    return str(values.get(fallback, "")).strip() if fallback else ""


def load_orchestration_policy() -> str:
    try:
        policy = ORCHESTRATION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RemoraError(
            f"cannot load orchestration policy from {ORCHESTRATION_FILE}: {exc}"
        ) from exc
    if not policy:
        raise RemoraError(f"orchestration policy is empty: {ORCHESTRATION_FILE}")
    return policy


def render_agents(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions = load_agent_definitions()
    models = config["agent_models"]
    efforts = config["agent_effort"]
    rendered: dict[str, dict[str, Any]] = {}
    for name, source in definitions.items():
        agent = dict(source)
        agent.pop("_routing_fallback", None)
        agent["model"] = agent_config_value(models, name, source)
        agent["effort"] = agent_config_value(efforts, name, source)
        rendered[name] = agent
    return rendered


def resolve_auth_token(config: dict[str, Any]) -> str:
    proxy = config["proxy"]
    env_name = str(proxy.get("auth_token_env", "REMORA_AUTH_TOKEN")).strip()
    if env_name:
        token = os.environ.get(env_name, "").strip()
        if token:
            return token

    command = proxy.get("auth_token_command", [])
    if command:
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise RemoraError("proxy.auth_token_command must be a TOML string array")
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RemoraError("proxy.auth_token_command failed") from exc
        token = result.stdout.strip()
        if token:
            return token

    source = env_name or "the configured token command"
    raise RemoraError(f"proxy token unavailable from {source}")


def configured_model_names(config: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for section_name in ("models", "agent_models"):
        for value in config.get(section_name, {}).values():
            name = str(value).strip()
            if name:
                names.add(name)
    return names


def routing_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "availableModels": sorted(configured_model_names(config)),
        "fallbackModel": [],
    }


def fetch_gateway_context_windows(config: dict[str, Any], token: str) -> dict[str, int]:
    base_url = str(config["proxy"]["base_url"]).rstrip("/")
    query = urllib.parse.urlencode({"client_version": f"remora-{VERSION}"})
    request = urllib.request.Request(
        f"{base_url}/v1/models?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.load(response)

    rows = payload.get("models", payload.get("data", []))
    if not isinstance(rows, list):
        raise RemoraError("gateway model catalog has no model list")

    windows: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("slug") or row.get("id") or "").strip()
        value = row.get("context_window", row.get("context_length"))
        if name and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            windows[name] = value
    return windows


def codex_models_cache_path(context: dict[str, Any]) -> Path:
    configured = str(context.get("codex_models_cache", "")).strip()
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return home / "models_cache.json"


def fetch_codex_context_windows(config: dict[str, Any]) -> dict[str, int]:
    context = config.get("context", {})
    path = codex_models_cache_path(context)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise RemoraError(f"Codex model cache not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoraError(f"cannot read Codex model cache {path}: {exc}") from exc

    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise RemoraError(f"Codex model cache has no fetched_at timestamp: {path}")
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemoraError(f"Codex model cache has an invalid fetched_at timestamp: {path}") from exc
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    ttl = context_integer(
        context,
        "codex_cache_ttl_seconds",
        DEFAULT_CODEX_CACHE_TTL_SECONDS,
        minimum=0,
    )
    age = max(
        0.0,
        (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds(),
    )
    if age > ttl:
        raise RemoraError(
            f"Codex model cache is stale ({int(age)}s old, limit {ttl}s): {path}"
        )

    rows = payload.get("models", [])
    if not isinstance(rows, list):
        raise RemoraError(f"Codex model cache has no model list: {path}")
    windows: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("slug") or row.get("id") or "").strip()
        value = row.get("context_window")
        if name and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            windows[name] = value
    return windows


def resolve_context_policy(
    config: dict[str, Any], *, token: str = "", online: bool = False
) -> dict[str, Any]:
    inherited_window = os.environ.get(AUTO_COMPACT_ENV, "").strip()
    inherited_percent = os.environ.get(AUTO_COMPACT_PERCENT_ENV, "").strip()
    explicit_window: int | None = None
    explicit_percent: int | None = None
    if inherited_window:
        try:
            explicit_window = int(inherited_window)
        except ValueError as exc:
            raise RemoraError(f"{AUTO_COMPACT_ENV} must be a positive integer") from exc
        if explicit_window < 1:
            raise RemoraError(f"{AUTO_COMPACT_ENV} must be a positive integer")
    if inherited_percent:
        try:
            explicit_percent = int(inherited_percent)
        except ValueError as exc:
            raise RemoraError(
                f"{AUTO_COMPACT_PERCENT_ENV} must be an integer from 1 to 100"
            ) from exc
        if not 1 <= explicit_percent <= 100:
            raise RemoraError(
                f"{AUTO_COMPACT_PERCENT_ENV} must be an integer from 1 to 100"
            )

    context = config.get("context", {})
    mode = str(context.get("mode", DEFAULT_CONTEXT_MODE)).strip().lower()
    stock_window = context_integer(
        context, "stock_window", DEFAULT_STOCK_CONTEXT_WINDOW, minimum=1
    )
    fallback_window = context_integer(
        context, "fallback_window", DEFAULT_PROVIDER_CONTEXT_WINDOW, minimum=1
    )
    codex_fallback_window = context_integer(
        context, "codex_fallback_window", DEFAULT_CODEX_CONTEXT_WINDOW, minimum=1
    )
    effective_percent = context_percentage(
        context, "effective_window_percent", DEFAULT_EFFECTIVE_CONTEXT_PERCENT
    )
    configured_compact_percent = context_percentage(
        context, "auto_compact_percent", DEFAULT_AUTO_COMPACT_PERCENT
    )
    discovery = context.get("discovery", True)
    provider_window = fallback_window
    codex_window = codex_fallback_window
    source_parts = ["fallback"]
    warnings: list[str] = []
    selected_provider: dict[str, int] = {}
    selected_codex: dict[str, int] = {}

    if online and discovery and token:
        try:
            available = fetch_gateway_context_windows(config, token)
            required = configured_model_names(config)
            selected_provider = {name: available[name] for name in required if name in available}
            missing = sorted(required - selected_provider.keys())
            candidates = [fallback_window] if missing else []
            candidates.extend(selected_provider.values())
            if candidates:
                provider_window = min(candidates)
                source_parts[0] = "gateway" if not missing else "gateway+fallback"
            if missing:
                warnings.append("gateway catalog missing configured models: " + ", ".join(missing))
            elif not selected_provider:
                warnings.append("gateway catalog contains no configured model context metadata")
        except (OSError, ValueError, json.JSONDecodeError, RemoraError) as exc:
            warnings.append(f"gateway context discovery failed: {exc}")

    if mode == "calico" and discovery:
        try:
            available_codex = fetch_codex_context_windows(config)
            required = configured_model_names(config)
            selected_codex = {
                name: available_codex[name] for name in required if name in available_codex
            }
            missing_codex = sorted(required - selected_codex.keys())
            codex_candidates = [codex_fallback_window] if missing_codex else []
            codex_candidates.extend(selected_codex.values())
            if codex_candidates:
                codex_window = min(codex_candidates)
                source_parts.append(
                    "codex-cache" if not missing_codex else "codex-cache+fallback"
                )
            if missing_codex:
                warnings.append(
                    "Codex runtime catalog missing configured models: "
                    + ", ".join(missing_codex)
                )
            elif not selected_codex:
                warnings.append("Codex runtime catalog contains no configured model metadata")
        except (OSError, ValueError, json.JSONDecodeError, RemoraError) as exc:
            source_parts.append("codex-fallback")
            warnings.append(f"Codex runtime discovery unavailable: {exc}")

    client_window = min(provider_window, codex_window) if mode == "calico" else stock_window
    if mode == "calico" and provider_window > codex_window:
        warnings.append(
            f"gateway window {provider_window} exceeds Codex runtime ceiling {codex_window}; "
            "Calico is capped to the Codex value"
        )
    auto_compact_window = (
        min(explicit_window, client_window)
        if mode == "calico" and explicit_window is not None
        else client_window
        if mode == "calico"
        else explicit_window
    )
    auto_compact_percent = (
        explicit_percent or configured_compact_percent
        if mode == "calico"
        else explicit_percent
    )
    effective_window = (
        client_window * effective_percent // 100
        if mode == "calico"
        else stock_window
    )
    compact_trigger = (
        auto_compact_window * auto_compact_percent // 100
        if mode == "calico"
        else None
    )
    if explicit_window is not None or explicit_percent is not None:
        source_parts.append("environment")
    if mode == "calico" and explicit_window is not None and explicit_window > client_window:
        warnings.append(
            f"explicit {AUTO_COMPACT_ENV}={explicit_window} exceeds Codex client "
            f"ceiling {client_window} and was capped"
        )
    if mode == "stock" and auto_compact_window is not None and auto_compact_window > stock_window:
        warnings.append(
            f"explicit {AUTO_COMPACT_ENV}={auto_compact_window} exceeds stock Claude "
            f"Code custom-model window {stock_window} and will be capped"
        )
    if compact_trigger is not None and compact_trigger > effective_window:
        warnings.append(
            f"compact trigger {compact_trigger} exceeds effective context {effective_window}"
        )
    required_models = configured_model_names(config)
    client_model_windows = {
        name: min(
            selected_provider.get(name, fallback_window),
            selected_codex.get(name, codex_fallback_window),
        )
        for name in required_models
    }
    return {
        "auto_compact_window": auto_compact_window,
        "auto_compact_percent": auto_compact_percent,
        "compact_trigger": compact_trigger,
        "mode": mode,
        "client_window": client_window,
        "codex_window": codex_window,
        "codex_fallback_window": codex_fallback_window,
        "stock_window": stock_window,
        "provider_window": provider_window,
        "effective_window": effective_window,
        "effective_window_percent": effective_percent,
        "source": "+".join(source_parts),
        "warning": "; ".join(warnings),
        "model_windows": client_model_windows,
        "provider_model_windows": selected_provider,
        "codex_model_windows": selected_codex,
        "codex_models_cache": str(codex_models_cache_path(context)),
    }


def has_option(args: list[str], long_name: str, short_name: str | None = None) -> bool:
    for arg in args:
        if arg == long_name or arg.startswith(f"{long_name}="):
            return True
        if short_name and arg == short_name:
            return True
    return False


def omit_option_values(args: list[str], option_names: set[str]) -> list[str]:
    """Return option-scan arguments without operands owned by known options."""
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            remaining.extend(args[index:])
            break
        remaining.append(arg)
        if (
            arg in option_names
            and index + 1 < len(args)
            and args[index + 1] != "--"
        ):
            index += 2
            continue
        index += 1
    return remaining


def extract_option(
    args: list[str], name: str, *, allow_leading_hyphen: bool = False
) -> tuple[str | None, list[str]]:
    value: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            remaining.extend(args[index:])
            break
        if arg == name:
            if value is not None:
                raise RemoraError(f"{name} may only be specified once")
            if index + 1 >= len(args):
                raise RemoraError(f"{name} requires a value")
            next_arg = args[index + 1]
            if next_arg == "--" or (
                not allow_leading_hyphen and next_arg.startswith("-")
            ):
                raise RemoraError(f"{name} requires a value")
            value = next_arg
            index += 2
            continue
        if arg.startswith(f"{name}="):
            if value is not None:
                raise RemoraError(f"{name} may only be specified once")
            value = arg.partition("=")[2]
            if not value:
                raise RemoraError(f"{name} requires a value")
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return value, remaining


def load_settings(value: str) -> dict[str, Any]:
    path = Path(value).expanduser()
    try:
        is_file = path.is_file()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise RemoraError(f"cannot read --settings file {path}: {exc}") from exc
        is_file = False
    except ValueError:
        is_file = False
    if is_file:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise RemoraError(f"cannot read --settings file {path}: {exc}") from exc
    else:
        raw = value
    try:
        parsed = json.loads(
            raw,
            parse_constant=reject_non_finite_json_constant,
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RemoraError(
            "--settings must reference a readable JSON file or contain a valid JSON object"
        ) from exc
    if not isinstance(parsed, dict):
        raise RemoraError("--settings must contain a JSON object")
    return parsed


def merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        merged[key] = (
            merge_settings(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def sanitize_caller_settings(settings: dict[str, Any]) -> dict[str, Any]:
    sanitized = settings
    if "fallbackModel" in settings:
        fallback_models = settings["fallbackModel"]
        if type(fallback_models) is not list or any(
            type(item) is not str or not item.strip() for item in fallback_models
        ):
            raise RemoraError(
                "--settings fallbackModel must be a JSON array of non-empty strings"
            )
        sanitized = dict(settings)
        sanitized.pop("fallbackModel")

    if "env" in settings:
        caller_env = settings["env"]
        if not isinstance(caller_env, dict):
            raise RemoraError("--settings env must contain a JSON object")
        if sanitized is settings:
            sanitized = dict(settings)
        sanitized["env"] = {
            key: value
            for key, value in caller_env.items()
            if key not in PROTECTED_SETTINGS_ENV
        }
    return sanitized


def start_settings_cleanup_watcher(path: str) -> int:
    read_fd, guard_fd = os.pipe()
    started = False
    try:
        subprocess.run(
            [sys.executable, "-c", SETTINGS_CLEANUP_SCRIPT, str(read_fd), path],
            check=True,
            close_fds=True,
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.set_inheritable(guard_fd, True)
        started = True
        return guard_fd
    finally:
        os.close(read_fd)
        if not started:
            os.close(guard_fd)


def temporary_settings_file(serialized: str) -> tuple[str, int]:
    fd, path = tempfile.mkstemp(prefix="remora-settings-", suffix=".json")
    guard_fd: int | None = None
    try:
        os.fchmod(fd, 0o600)
        guard_fd = start_settings_cleanup_watcher(path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(serialized)
        return path, guard_fd
    except BaseException as exc:
        if fd >= 0:
            os.close(fd)
        try:
            Path(path).unlink(missing_ok=True)
        finally:
            if guard_fd is not None:
                os.close(guard_fd)
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise RemoraError("cannot prepare secure --settings file") from exc
        raise


def take_settings_resources(env: dict[str, str]) -> tuple[str | None, int | None]:
    path = env.pop(SETTINGS_FILE_ENV, None)
    guard = env.pop(SETTINGS_GUARD_FD_ENV, None)
    return path, int(guard) if guard is not None else None


def close_settings_resources(path: str | None, guard_fd: int | None) -> None:
    try:
        if path is not None:
            Path(path).unlink(missing_ok=True)
    finally:
        if guard_fd is not None:
            os.close(guard_fd)


def close_launch_resources(env: dict[str, str]) -> None:
    close_settings_resources(*take_settings_resources(env))


def exec_launch(command: list[str], env: dict[str, str]) -> None:
    resources = take_settings_resources(env)
    try:
        os.execvpe(command[0], command, env)
    finally:
        close_settings_resources(*resources)


def load_prompt_file(value: str) -> str:
    path = Path(value).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise RemoraError(f"cannot read --append-system-prompt-file {path}: {exc}") from exc


def split_fast_flag(args: list[str]) -> tuple[bool, list[str]]:
    """Consume the wrapper-only Fast flag when it is in the leading position."""
    remaining = list(args)
    if not remaining or remaining[0] != "--fast":
        return False, remaining
    if len(remaining) > 1 and remaining[1] in REMORA_BUILTIN_COMMANDS:
        raise RemoraError(
            f"--fast cannot be combined with remora command {remaining[1]!r}"
        )
    return True, remaining[1:]


def reject_non_finite_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON key: {key}")
        parsed[key] = value
    return parsed


def apply_fast_mode(env: dict[str, str]) -> None:
    """Merge the session-wide Fast service tier into a copied child environment."""
    raw = env.get(CLAUDE_EXTRA_BODY_ENV, "")
    if not isinstance(raw, str):
        raise RemoraError(f"{CLAUDE_EXTRA_BODY_ENV} must contain a JSON object")
    if not raw.strip():
        body: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(
                raw,
                parse_constant=reject_non_finite_json_constant,
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RemoraError(
                f"{CLAUDE_EXTRA_BODY_ENV} must contain valid JSON without duplicate "
                "keys when --fast is used"
            ) from exc
        if not isinstance(parsed, dict):
            raise RemoraError(
                f"{CLAUDE_EXTRA_BODY_ENV} must contain a JSON object when --fast is used"
            )
        body = parsed

    if "service_tier" in body:
        service_tier = body["service_tier"]
        if not isinstance(service_tier, str) or service_tier not in FAST_ACCEPTED_SERVICE_TIERS:
            raise RemoraError(
                f"{CLAUDE_EXTRA_BODY_ENV}.service_tier conflicts with --fast; "
                "only 'fast' or 'priority' is accepted"
            )
    body["service_tier"] = FAST_SERVICE_TIER
    try:
        serialized = json.dumps(
            body, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise RemoraError(
            f"{CLAUDE_EXTRA_BODY_ENV} must contain valid JSON without duplicate "
            "keys when --fast is used"
        ) from exc
    env[CLAUDE_EXTRA_BODY_ENV] = serialized


def binary_contains_marker(claude_binary: str, marker: bytes) -> bool:
    resolved = shutil.which(claude_binary)
    if not resolved:
        return False
    overlap = b""
    try:
        with Path(resolved).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                block = overlap + chunk
                if marker in block:
                    return True
                overlap = block[-len(marker) :]
    except OSError:
        return False
    return False


def calico_context_supported(claude_binary: str) -> bool:
    return binary_contains_marker(claude_binary, CALICO_CONTEXT_MAP_ENV.encode("ascii"))


def calico_active_turn_supported(claude_binary: str) -> bool:
    return all(
        binary_contains_marker(claude_binary, marker)
        for marker in CALICO_ACTIVE_TURN_MARKERS
    )


def gateway_active_turn_supported(config: dict[str, Any], token: str) -> bool:
    base_url = str(config["proxy"]["base_url"]).rstrip("/")
    query = urllib.parse.urlencode({"client_version": f"remora-{VERSION}"})
    request = urllib.request.Request(
        f"{base_url}/v1/models?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.headers.get(GATEWAY_ACTIVE_TURN_HEADER, "").strip() == "1"


def remora_state_dir(env: dict[str, str]) -> Path:
    """Return remora's XDG runtime-state directory."""
    configured = env.get("XDG_STATE_HOME", "").strip()
    if configured:
        state_home = Path(configured).expanduser()
    else:
        home = env.get("HOME", "").strip() or str(Path.home())
        state_home = Path(home) / ".local" / "state"
    return state_home / "remora-cc"


def coralline_store_dir(
    base_url: str, env: dict[str, str] | None = None
) -> Path:
    """Gateway-scoped root for coralline's rate-limit and burn stores.

    Keyed on the full gateway URL so remora sessions on the same gateway share
    their own stores while staying isolated from the host's native Claude limit
    and burn segments. The directory name is a bounded readable host prefix plus
    a hash of the whole URL, so path-routed gateways behind one reverse-proxy host
    (`https://gw/team-a` vs `/team-b`) do not collapse into one store. Files live
    under remora-owned XDG state, never under native Claude's ~/.claude tree.
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = "".join(c if c.isalnum() else "-" for c in (parsed.netloc or "")).strip("-")
    # Keep the human-readable part bounded below common 255-byte component limits.
    # The full-URL hash remains the uniqueness source after truncation.
    host = host[:CORALLINE_GATEWAY_PREFIX_MAX].rstrip("-")
    digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:10]
    token = f"{host}-{digest}" if host else f"gateway-{digest}"
    environment = env if env is not None else os.environ.copy()
    return remora_state_dir(environment) / "coralline" / "gateways" / token


def coralline_source_config(env: dict[str, str]) -> str:
    """Return the config path coralline would source without remora."""
    configured = env.get("CORALLINE_CONFIG", "").strip()
    if configured:
        return configured
    home = env.get("HOME", "").strip() or str(Path.home())
    return str(Path(home) / ".claude" / "coralline.conf")


def coralline_wrapper_path(store_root: Path, source_config: str) -> Path:
    """Return a stable wrapper path without collapsing distinct source configs."""
    digest = hashlib.sha256(source_config.encode("utf-8")).hexdigest()[:10]
    return store_root / f"config-{digest}.conf"


def prepare_coralline_config(env: dict[str, str], source_config: str) -> None:
    """Write a wrapper that reapplies scoped paths after user configuration.

    coralline derives RL5H_FILE, RL7D_FILE, and BURN_FILE from environment before
    sourcing its config. A user config may assign those shell variables directly,
    overriding remora's environment values. The wrapper restores the original config
    identity while sourcing that file, then assigns the scoped paths so isolation
    wins at the final layer without breaking sibling-path lookups.
    """
    wrapper = Path(env["CORALLINE_CONFIG"])
    source = shlex.quote(source_config)
    lines = [
        "# Generated by remora. Do not edit.",
        # coralline set both variables to this wrapper before sourcing it. Restore
        # the original identity so sibling-path lookups and deferred config helpers
        # observe the same values they would see in a native Claude session.
        f"CORALLINE_CONFIG={source}",
        f"VL_CONF={source}",
        f"[ ! -f {source} ] || . {source}",
    ]
    for env_name, shell_name in (
        ("CORALLINE_RL5H_FILE", "RL5H_FILE"),
        ("CORALLINE_RL7D_FILE", "RL7D_FILE"),
        ("CORALLINE_BURN_FILE", "BURN_FILE"),
    ):
        lines.append(f"{shell_name}={shlex.quote(env[env_name])}")
    content = "\n".join(lines) + "\n"

    wrapper.parent.mkdir(parents=True, exist_ok=True)
    temporary = wrapper.with_name(f".{wrapper.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, wrapper)
    finally:
        temporary.unlink(missing_ok=True)


def build_launch(
    config: dict[str, Any],
    claude_args: list[str],
    *,
    require_token: bool = True,
    fast: bool = False,
) -> tuple[list[str], dict[str, str]]:
    fallback_scan_args = omit_option_values(
        claude_args,
        {"--append-system-prompt", "--append-system-prompt-file"},
    )
    fallback_model, _ = extract_option(fallback_scan_args, "--fallback-model")
    if fallback_model is not None:
        raise RemoraError(
            "--fallback-model is not supported because remora disables automatic fallback"
        )

    runtime = config.get("runtime", {})
    models = config["models"]
    proxy = config["proxy"]
    claude_bin = str(runtime.get("claude_binary", "claude")).strip() or "claude"
    settings_value, args = extract_option(claude_args, "--settings")
    settings = (
        sanitize_caller_settings(load_settings(settings_value))
        if settings_value is not None
        else {}
    )
    settings = merge_settings(settings, routing_settings(config))
    try:
        serialized_settings = json.dumps(
            settings, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise RemoraError("--settings must contain valid finite JSON values") from exc

    prefix: list[str] = ["--settings", serialized_settings]
    if not has_option(args, "--model", "-m"):
        prefix.extend(["--model", str(models["main"])])
    if not has_option(args, "--agents"):
        compact = json.dumps(render_agents(config), ensure_ascii=False, separators=(",", ":"))
        prefix.extend(["--agents", compact])

    if os.environ.get(COMPOSE_SYSTEM_PROMPT_ENV, "").strip() == "1":
        inline_prompt, args = extract_option(
            args, "--append-system-prompt", allow_leading_hyphen=True
        )
        prompt_file, args = extract_option(
            args, "--append-system-prompt-file", allow_leading_hyphen=True
        )
        bridged_prompt = os.environ.get(CALLER_SYSTEM_PROMPT_ENV)
        caller_sources = sum(
            source is not None for source in (inline_prompt, prompt_file, bridged_prompt)
        )
        if caller_sources > 1:
            raise RemoraError(
                "caller system prompt sources cannot be combined when "
                f"{COMPOSE_SYSTEM_PROMPT_ENV}=1"
            )
        caller_prompt = (
            load_prompt_file(prompt_file)
            if prompt_file is not None
            else inline_prompt
            if inline_prompt is not None
            else bridged_prompt or ""
        )
        policy = load_orchestration_policy()
        prefix.extend(
            [
                "--append-system-prompt",
                f"{caller_prompt}\n\n{policy}" if caller_prompt else policy,
            ]
        )
    elif not has_option(args, "--append-system-prompt") and not has_option(
        args, "--append-system-prompt-file"
    ):
        prefix.extend(["--append-system-prompt", load_orchestration_policy()])

    env = os.environ.copy()
    env.pop(CALLER_SYSTEM_PROMPT_ENV, None)
    env.pop(SETTINGS_FILE_ENV, None)
    env.pop(SETTINGS_GUARD_FD_ENV, None)
    if fast:
        apply_fast_mode(env)
    source_config = coralline_source_config(env)
    env["REMORA_ACTIVE"] = "1"
    env["ANTHROPIC_BASE_URL"] = str(proxy["base_url"]).rstrip("/")
    # Isolate coralline's rate-limit and burn stores per gateway. Override any
    # inherited value: the parent shell's path points at the host's native store,
    # which is exactly what the child must not write into. CORALLINE_CONFIG points
    # at a wrapper that sources the user's config before reapplying these paths.
    store_root = coralline_store_dir(env["ANTHROPIC_BASE_URL"], env)
    for env_name, stem in CORALLINE_STORE_ENV.items():
        env[env_name] = str(store_root / stem)
    env["CORALLINE_CONFIG"] = str(
        coralline_wrapper_path(store_root, source_config)
    )
    token = resolve_auth_token(config) if require_token else ""
    if require_token:
        env["ANTHROPIC_AUTH_TOKEN"] = token
    for key, env_name in MODEL_ENV.items():
        value = str(models.get(key, "")).strip()
        if value:
            env[env_name] = value

    concurrency = int(runtime.get("max_tool_use_concurrency", 3))
    if concurrency < 1:
        raise RemoraError("runtime.max_tool_use_concurrency must be at least 1")
    env["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"] = str(concurrency)
    env["CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"] = "1"
    env["ENABLE_TOOL_SEARCH"] = "true" if runtime.get("enable_tool_search", False) else "false"
    if runtime.get("clear_subagent_model_override", True):
        env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)

    context_policy = resolve_context_policy(config, token=token, online=require_token)
    if context_policy["mode"] == "calico" and not calico_context_supported(claude_bin):
        raise RemoraError(
            "context.mode='calico' requires a Calico Claude binary containing the "
            "custom-context-window patch; install Calico or switch to mode='stock'"
        )
    if context_policy["auto_compact_window"] is not None:
        env[AUTO_COMPACT_ENV] = str(context_policy["auto_compact_window"])
    if context_policy["auto_compact_percent"] is not None:
        env[AUTO_COMPACT_PERCENT_ENV] = str(context_policy["auto_compact_percent"])
    if context_policy["mode"] == "calico":
        fallback = context_policy["client_window"]
        windows = {
            name: context_policy["model_windows"].get(name, fallback)
            for name in configured_model_names(config)
        }
        env[CALICO_CONTEXT_MAP_ENV] = json.dumps(
            windows, ensure_ascii=True, separators=(",", ":")
        )
        env[CALICO_DISPLAY_PERCENT_ENV] = str(
            context_policy["effective_window_percent"]
        )
    else:
        env.pop(CALICO_CONTEXT_MAP_ENV, None)
        env.pop(CALICO_DISPLAY_PERCENT_ENV, None)

    command = [claude_bin, *prefix, *args]
    if settings_value is None:
        return command, env

    settings_path, guard_fd = temporary_settings_file(serialized_settings)
    handed_off = False
    try:
        command[command.index("--settings") + 1] = settings_path
        env[SETTINGS_FILE_ENV] = settings_path
        env[SETTINGS_GUARD_FD_ENV] = str(guard_fd)
        handed_off = True
        return command, env
    finally:
        if not handed_off:
            close_settings_resources(settings_path, guard_fd)


def print_agents(config: dict[str, Any]) -> None:
    print("ROLE\tMODEL\tEFFORT")
    for name, agent in render_agents(config).items():
        print(f"{name}\t{agent['model']}\t{agent['effort']}")


def doctor(config: dict[str, Any], online: bool) -> int:
    failures = 0
    claude_bin = str(config.get("runtime", {}).get("claude_binary", "claude"))
    claude_path = shutil.which(claude_bin)
    if claude_path:
        print(f"PASS claude binary: {claude_path}")
    else:
        failures += 1
        print(f"FAIL claude binary not found: {claude_bin}")

    print(f"PASS configuration: {config_path()}")
    print(f"PASS agents: {len(render_agents(config))} definitions render correctly")
    print(
        "PASS routing allowlist: "
        + ", ".join(routing_settings(config)["availableModels"])
    )
    try:
        token = resolve_auth_token(config)
        print("PASS proxy token: available (value hidden)")
    except RemoraError as exc:
        token = ""
        failures += 1
        print(f"FAIL proxy token: {exc}")

    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        print("PASS global subagent override: present, but remora clears it for its child session")
    else:
        print("PASS global subagent override: absent")

    context_policy = resolve_context_policy(config, token=token, online=online)
    if context_policy["mode"] == "calico":
        if claude_path and calico_context_supported(claude_bin):
            print("PASS Calico context adapter: custom-context-window marker present")
        else:
            failures += 1
            print("FAIL Calico context adapter: custom-context-window marker not found")
    calico_active_turn = bool(
        claude_path and calico_active_turn_supported(claude_bin)
    )
    if calico_active_turn:
        print("PASS Calico active-turn identity: versioned prompt marker present")
    else:
        print("WARN Calico active-turn identity: unavailable; quota-boundary parity is not guaranteed")
    gateway_active_turn = False
    if online and token:
        try:
            gateway_active_turn = gateway_active_turn_supported(config, token)
            if gateway_active_turn:
                print("PASS gateway active-turn bridge: capability v1 present")
            else:
                print("WARN gateway active-turn bridge: capability v1 not advertised")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"WARN gateway active-turn bridge: capability check failed ({exc})")
    elif online:
        print("INFO gateway active-turn bridge: skipped because proxy token is unavailable")
    else:
        print("INFO gateway active-turn bridge: run doctor --online to verify")
    if online and calico_active_turn and gateway_active_turn:
        print("PASS Codex active-turn bridge: protocol v1 ready")
    elif online:
        print("WARN Codex active-turn bridge: DEGRADED; native quota-boundary parity is unavailable")
    provider = context_policy["provider_window"]
    compact_policy = (
        f"exact:{context_policy['compact_trigger']}"
        if context_policy["compact_trigger"] is not None
        else "native-claude"
    )
    print(
        "PASS context policy: "
        f"mode={context_policy['mode']} "
        f"source={context_policy['source']} "
        f"provider_window={provider} "
        f"codex_window={context_policy['codex_window']} "
        f"client_window={context_policy['client_window']} "
        f"effective_window={context_policy['effective_window']} "
        f"auto_compact_window={context_policy['auto_compact_window']} "
        f"auto_compact_percent={context_policy['auto_compact_percent']} "
        f"compact_policy={compact_policy}"
    )
    for name, window in sorted(context_policy["provider_model_windows"].items()):
        print(f"PASS gateway model context: {name}={window}")
    for name, window in sorted(context_policy["codex_model_windows"].items()):
        print(f"PASS Codex runtime model context: {name}={window}")
    if context_policy["mode"] == "calico":
        for name, window in sorted(context_policy["model_windows"].items()):
            print(f"PASS Calico client model context: {name}={window}")
    if context_policy["warning"]:
        print(f"WARN context policy: {context_policy['warning']}")

    if online and token:
        url = f"{str(config['proxy']['base_url']).rstrip('/')}/v1/models"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if 200 <= response.status < 300:
                    print(f"PASS proxy endpoint: HTTP {response.status}")
                else:
                    failures += 1
                    print(f"FAIL proxy endpoint: HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"FAIL proxy endpoint: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            failures += 1
            print(f"FAIL proxy endpoint: {exc.reason if hasattr(exc, 'reason') else exc}")

    return 1 if failures else 0


def dry_run(config: dict[str, Any], args: list[str], *, fast: bool = False) -> None:
    requested_fast, claude_args = split_fast_flag(args)
    fast = fast or requested_fast
    command, env = build_launch(
        config, claude_args, require_token=False, fast=fast
    )
    try:
        shown_env = {
            key: env[key]
            for key in [
                "REMORA_ACTIVE",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                "CALICO_MODEL_CONTEXT_WINDOWS",
                "CALICO_CONTEXT_DISPLAY_PERCENT",
                "ENABLE_TOOL_SEARCH",
                "CORALLINE_CONFIG",
                *CORALLINE_STORE_ENV,
            ]
            if key in env
        }
        if fast:
            # The live child receives the merged body, but previews must not disclose
            # unrelated inherited fields or sentinel values.
            shown_env[CLAUDE_EXTRA_BODY_ENV] = json.dumps(
                {"service_tier": FAST_SERVICE_TIER},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        print(
            json.dumps(
                {"environment": shown_env, "command": command},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        close_launch_resources(env)


def usage() -> str:
    return """usage: remora [command] [claude arguments...]

commands:
  doctor [--online]  validate configuration, token retrieval, and optionally the proxy
  agents             show the effective role/model/effort map
  render-agents      print the exact JSON passed to Claude Code
  dry-run [args...]  show the sanitized child environment and command
  version            print remora version
  help               show this help

Any other arguments are passed to the native claude executable.

Fast mode (opt-in, session-only, all requests in the child session):
  remora --fast [claude arguments...]
  remora dry-run --fast [claude arguments...]

Fast mode asks the configured gateway for service_tier=priority. It is off by
default, may increase provider credit or usage, requires gateway support, and
does not persist. Use dry-run to verify the synthesized child setting; unrelated
inherited fields are redacted.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        fast, args = split_fast_flag(args)
    except RemoraError as exc:
        print(f"remora: {exc}", file=sys.stderr)
        return 2
    command = args[0] if args else ""
    if command in {"help", "--help", "-h"}:
        print(usage())
        return 0
    if command in {"version", "--version", "-V"}:
        print(f"remora {VERSION}")
        return 0

    try:
        config = load_config()
        if command == "doctor":
            unknown = [arg for arg in args[1:] if arg != "--online"]
            if unknown:
                raise RemoraError(f"unknown doctor option: {unknown[0]}")
            return doctor(config, "--online" in args[1:])
        if command == "agents":
            print_agents(config)
            return 0
        if command == "render-agents":
            print(json.dumps(render_agents(config), ensure_ascii=False, indent=2))
            return 0
        if command == "dry-run":
            dry_run(config, args[1:], fast=fast)
            return 0

        source_config = coralline_source_config(os.environ.copy())
        launch, env = build_launch(config, args, fast=fast)
        try:
            prepare_coralline_config(env, source_config)
            exec_launch(launch, env)
        finally:
            close_launch_resources(env)
    except RemoraError as exc:
        print(f"remora: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
