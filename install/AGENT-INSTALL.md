# remora — Approval-Gated Agent Install

> This runbook is for a coding agent installing remora on a user's machine. Perform the read-only preflight first, present the complete plan, and make no filesystem or configuration changes until the user explicitly approves.

## Trust contract

remora attaches a GPT-5.6 agent fleet to one Claude Code session. It must not modify Claude Code, the user's Anthropic login, or anything under `~/.claude`. Installation is limited to the following paths unless the user overrides standard XDG locations:

| Path | Purpose | Write rule |
|---|---|---|
| `~/.local/share/remora-cc/` | Versioned launcher payload | Create or atomically replace after approval |
| `~/.local/bin/remora` | Launcher symlink | Create only if absent or already owned by remora |
| `~/.config/remora-cc/config.toml` | User-owned gateway and model configuration | Create if absent; never overwrite |

All downloaded files must come from the exact release tag referenced by the install prompt. Never substitute `main`, `latest`, another tag, or an unpinned dependency. Never request that the user paste a bearer token, OAuth file, or credential into the conversation.

## Step 1 — Read-only preflight

Collect and report the following without changing anything:

| Check | Required observation |
|---|---|
| Invocation URL | Confirm it contains an immutable release tag such as `v0.1.0`; stop if it uses `main` |
| Platform | macOS or Linux; report architecture |
| Python | `python3` is 3.11 or newer |
| Claude Code | `claude` exists on `PATH`; do not replace or patch it |
| GitHub CLI | `gh` exists and can verify attestations; otherwise explain the checksum-only downgrade |
| Existing install | Inspect the three remora paths above and classify each as create, upgrade, preserve, or conflict |
| Native Claude boundary | Record a sorted path manifest of `~/.claude` for post-install comparison; do not read credential contents |
| Gateway | Ask only whether an Anthropic Messages-compatible gateway exists and what base URL should be configured; if absent, point to `docs/cliproxyapi.md` and keep OAuth as a human handoff |
| Secret source | Ask whether the user will use an environment variable or an OS credential command; never ask for the secret value |
| Context mode | Offer `stock` (official Claude binary, 200K, Claude-managed compact, recommended) and `calico` (separately installed patched binary, bounded by the smaller gateway/Codex runtime window). Never select or install Calico implicitly |
| Runtime policy | Read `agents/agents.json` and `agents/orchestration.md` from the same immutable tag; report that the latter is appended only to the remora child session, controls foreground/background delegation, and leaves every named role's model to its agent definition |

If the executable path already exists and is not a symlink owned by remora, treat it as a conflict and stop. If a configuration exists, preserve it and report that installation will not edit it.

## Step 2 — Approval gate

Present one table covering every proposed write, download source, release version, verification method, preserved file, conflict, context-mode choice, session-only orchestration policy, and rollback action. Explicitly state that this remora runbook will not write `~/.claude` or replace the native `claude` executable. If the user wants `calico`, disclose that its separate installation replaces the native binary and requires its own source review and approval. Wait for an unambiguous approval before continuing.

> ⚠️ **No approval means no installation.** Reading this runbook is not permission to mutate the machine.

## Step 3 — Verify and install

Fetch `bootstrap.sh` from the same immutable release tag as this runbook and inspect it before execution. Run it with a pinned version:

```bash
REMORA_VERSION='<VERSION_WITHOUT_v>' sh /path/to/reviewed/bootstrap.sh
```

The bootstrap must verify both the release SHA-256 and GitHub artifact attestation before it invokes the packaged installer. If `gh` is unavailable, stop and offer these choices: install GitHub CLI, perform the documented manual verification, or explicitly accept checksum-only verification with:

```bash
REMORA_VERSION='<VERSION_WITHOUT_v>' \
REMORA_ALLOW_CHECKSUM_ONLY=1 \
sh /path/to/reviewed/bootstrap.sh
```

Do not choose the weaker mode on the user's behalf. Do not pipe the remote script directly into a shell because that prevents meaningful review and provenance verification of the bootstrap itself.

## Step 4 — Configure without handling secrets

If the configuration was newly created, edit only the non-secret gateway address, model aliases, and context mode approved by the user. For an environment-variable setup, leave `auth_token_command` empty. For an OS credential store, configure a direct argument array that does not invoke a shell.

Keep `context.mode = "stock"` unless a Calico binary containing the literal `CALICO_MODEL_CONTEXT_WINDOWS` marker is already installed and the user explicitly selected `calico`. If Calico is requested but absent, finish the safe remora installation in `stock` mode and hand off to the [Calico Claude trust and install documentation](https://github.com/Nanako0129/calico-claude); do not pipe its remote installer into a shell or replace `claude` under this approval.

Never place a bearer token in TOML. OAuth enrollment and CLIProxyAPI GUI actions remain a human handoff outside remora.

## Step 5 — Verify and hand off

Run the following checks and report their exit status without printing secrets:

```bash
remora version
remora agents
remora dry-run
remora doctor
remora doctor --online
```

Confirm that `remora doctor` prints every configured GPT model under `PASS routing allowlist`, and that `remora dry-run` contains the reviewed `--append-system-prompt` policy without exposing the gateway token. remora owns the child-only `--settings` argument used for this allowlist; do not add another `--settings` flag to the launch command.

Compare the post-install `~/.claude` path manifest with the preflight manifest. A difference is a failed isolation check: stop, show the changed paths, and do not claim success. Finish with the installed version, verification method, created and preserved paths, gateway reachability, and uninstall command.

## Update and uninstall

An update repeats the same pinned-release and approval process. Preserve `config.toml`, verify the new archive, and atomically replace only the remora payload. To remove remora while retaining configuration, run the installed `uninstall.sh`; use `--purge` only after separate confirmation because it deletes user configuration.
