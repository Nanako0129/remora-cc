#!/usr/bin/env python3
"""Session-scoped OpenAI agent routing for Claude Code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "0.1.7"
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
CALICO_CONTEXT_MAP_ENV = "CALICO_MODEL_CONTEXT_WINDOWS"
CALICO_DISPLAY_PERCENT_ENV = "CALICO_CONTEXT_DISPLAY_PERCENT"
CALICO_ACTIVE_TURN_MARKERS = (
    b"calico-active-turn-adapter:v1",
    b"x-calico-prompt-id",
    b"x-calico-active-turn-version",
)
GATEWAY_ACTIVE_TURN_HEADER = "X-CLIProxyAPI-Codex-Active-Turn"


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
    for name in definitions:
        if not str(model_map.get(name, "")).strip():
            raise RemoraError(f"agent_models.{name} is missing")
        if not str(effort_map.get(name, "")).strip():
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
        agent["model"] = models[name]
        agent["effort"] = efforts[name]
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
    return {"availableModels": sorted(configured_model_names(config))}


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


def build_launch(
    config: dict[str, Any], claude_args: list[str], *, require_token: bool = True
) -> tuple[list[str], dict[str, str]]:
    runtime = config.get("runtime", {})
    models = config["models"]
    proxy = config["proxy"]
    claude_bin = str(runtime.get("claude_binary", "claude")).strip() or "claude"
    args = list(claude_args)

    if has_option(args, "--settings"):
        raise RemoraError(
            "--settings cannot be combined with remora routing because Claude Code "
            "accepts a single additional-settings source; put persistent settings in "
            "Claude's normal settings files instead"
        )

    prefix: list[str] = [
        "--settings",
        json.dumps(routing_settings(config), ensure_ascii=True, separators=(",", ":")),
    ]
    if not has_option(args, "--model", "-m"):
        prefix.extend(["--model", str(models["main"])])
    if not has_option(args, "--agents"):
        compact = json.dumps(render_agents(config), ensure_ascii=False, separators=(",", ":"))
        prefix.extend(["--agents", compact])
    if not has_option(args, "--append-system-prompt") and not has_option(
        args, "--append-system-prompt-file"
    ):
        prefix.extend(["--append-system-prompt", load_orchestration_policy()])

    env = os.environ.copy()
    env["REMORA_ACTIVE"] = "1"
    env["ANTHROPIC_BASE_URL"] = str(proxy["base_url"]).rstrip("/")
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

    return [claude_bin, *prefix, *args], env


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


def dry_run(config: dict[str, Any], args: list[str]) -> None:
    command, env = build_launch(config, args, require_token=False)
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
        ]
        if key in env
    }
    print(json.dumps({"environment": shown_env, "command": command}, ensure_ascii=False, indent=2))


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
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
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
            dry_run(config, args[1:])
            return 0

        launch, env = build_launch(config, args)
        os.execvpe(launch[0], launch, env)
    except RemoraError as exc:
        print(f"remora: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
