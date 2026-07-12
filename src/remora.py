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
import urllib.request
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = ROOT / "agents" / "agents.json"
DEFAULT_CONFIG = Path.home() / ".config" / "remora-cc" / "config.toml"
MODEL_ENV = {
    "default_opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "default_sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "default_haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


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


def load_agent_definitions() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoraError(f"cannot load agent definitions from {AGENTS_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise RemoraError("agents/agents.json must contain a JSON object")
    return data


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


def has_option(args: list[str], long_name: str, short_name: str | None = None) -> bool:
    for arg in args:
        if arg == long_name or arg.startswith(f"{long_name}="):
            return True
        if short_name and arg == short_name:
            return True
    return False


def build_launch(
    config: dict[str, Any], claude_args: list[str], *, require_token: bool = True
) -> tuple[list[str], dict[str, str]]:
    runtime = config.get("runtime", {})
    models = config["models"]
    proxy = config["proxy"]
    claude_bin = str(runtime.get("claude_binary", "claude")).strip() or "claude"
    args = list(claude_args)

    prefix: list[str] = []
    if not has_option(args, "--model", "-m"):
        prefix.extend(["--model", str(models["main"])])
    if not has_option(args, "--agents"):
        compact = json.dumps(render_agents(config), ensure_ascii=False, separators=(",", ":"))
        prefix.extend(["--agents", compact])

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = str(proxy["base_url"]).rstrip("/")
    if require_token:
        env["ANTHROPIC_AUTH_TOKEN"] = resolve_auth_token(config)
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
    try:
        token = resolve_auth_token(config)
        print("PASS proxy token: available (value hidden)")
    except RemoraError as exc:
        token = ""
        failures += 1
        print(f"FAIL proxy token: {exc}")

    if os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"):
        print("PASS global subagent override: present, but Remora clears it for its child session")
    else:
        print("PASS global subagent override: absent")

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
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
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
  version            print Remora version
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
