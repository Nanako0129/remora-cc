# Security Policy

> remora is not a sandbox. Its security promise is narrow, observable change scope: a verified installer, a session-scoped launcher, and no mutation of native Claude state.

## Installation trust

| Control | Expected behavior |
|---|---|
| Approval gate | The agent runbook performs read-only inspection and waits before every installation or update |
| Version pinning | A runbook invoked from a release tag fetches every other source from that exact tag |
| Artifact integrity | `bootstrap.sh` rejects an archive that does not match `checksums.txt` |
| Build provenance | GitHub artifact attestation is required unless the user explicitly accepts checksum-only verification |
| Safe extraction | Bootstrap rejects absolute paths, parent traversal, symlinks, and hard links in the archive |
| Collision protection | `install.sh` refuses an unrelated executable or symlink at the launcher path |
| Atomic update | The existing payload is restored if replacement fails before completion |
| Isolation verification | The one-prompt flow compares the `~/.claude` path manifest before and after installation |

> ⚠️ **A checksum fetched beside an archive proves consistency, not publisher provenance.** Use GitHub attestation verification for the normal path. `REMORA_ALLOW_CHECKSUM_ONLY=1` is an explicit trust downgrade for environments without GitHub CLI.

## Trust model

remora is a launcher, not a sandbox. Claude Code keeps its normal filesystem and tool permissions, while the configured gateway and upstream model receive the prompts and code Claude sends. Use only gateways and accounts you trust for the repository being processed.

| Asset | Protection |
|---|---|
| Native Claude OAuth | Never read or changed by remora |
| Gateway bearer token | Read from a dedicated environment variable or direct credential command; never printed |
| Codex model cache | In Calico mode, read-only access to fresh model metadata; auth files are never opened |
| Agent prompts | Stored as auditable JSON in this repository |
| Orchestration prompt | Stored as auditable Markdown and appended only to the remora child session |
| Child environment | Exists only for the launched Claude process tree |
| Child routing settings | Contains model ids only, is passed as a process argument, and is never persisted |
| Gateway OAuth files | Outside remora's scope; protect at the gateway host |

## Runtime boundaries

| Boundary | Consequence |
|---|---|
| Claude Code process | Retains its normal filesystem, network, shell, MCP, and tool permissions |
| remora child environment | Contains the gateway URL, bearer token, model aliases, and concurrency controls |
| CLIProxyAPI | Can receive prompts, tool context, and source selected by Claude Code |
| OpenAI account | Processes content forwarded by the gateway and enforces its own quota and retention policy |
| Native Claude session | Unchanged unless the user separately exports gateway variables globally |

## Optional Calico mode

`context.mode = "calico"` is a separate trust decision. remora itself still does not replace Claude Code, but this mode only works with a patched Calico Claude binary. Calico changes the executable supply chain and must be reviewed, installed, verified, and rolled back independently.

remora fails closed by scanning the selected binary for the `custom-context-window` capability marker before launch. The child receives an exact JSON model/window map containing no credentials. Each value is bounded by both the gateway catalog and a fresh Codex runtime catalog, with a conservative configured fallback. Calico accepts only exact model ids and integer windows from 100K through 1M; malformed or missing values fall back to Claude Code's stock behavior. The public remora installer defaults to `stock` and never installs Calico implicitly.

## Secret handling

Do not place a bearer token directly in `config.toml`, commit an OAuth `auths` directory, or paste debug logs into an issue without inspection. Prefer an OS credential store and keep the TOML command array free of shell syntax.

remora deliberately executes `auth_token_command` without a shell. Variables, pipes, redirection, command substitution, and globbing are not expanded. If a credential helper needs those features, put the logic in a separately reviewed executable and reference that executable as the first array item.

## Reporting

For a suspected vulnerability, open a private GitHub security advisory rather than a public issue. Include the remora version, platform, Claude Code version, redacted configuration, and a minimal reproduction. Never include bearer tokens, OAuth files, prompts containing proprietary code, or unredacted gateway logs.
