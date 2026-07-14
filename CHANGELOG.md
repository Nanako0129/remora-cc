# Changelog

All notable changes to remora are documented here.

## 0.1.9 - 2026-07-14

Isolate the coralline status-line stores per gateway. coralline keeps a cross-session high-water store for its 5-hour and 7-day rate-limit segments, plus a 5-hour sample log for its optional burn segment, all fed by Anthropic rate-limit responses. Because a remora child talks to a GPT gateway whose usage accounting is a different account than the host's native Claude login, a shared store let a gateway percentage leak into the native segments (and vice versa), and the burn ETA and slope drifted the same way. The child environment now points `CORALLINE_RL5H_FILE`, `CORALLINE_RL7D_FILE`, and `CORALLINE_BURN_FILE` at a per-gateway subdirectory under `${XDG_STATE_HOME:-$HOME/.local/state}/remora-cc/coralline/gateways/`, overriding any inherited value so the child never writes into the host's native stores or `~/.claude`. The subdirectory is keyed on the full gateway URL (a bounded readable host prefix plus a hash of the whole URL), so path-routed gateways behind one reverse-proxy host stay separate without exceeding filesystem component limits. Because coralline sources `~/.claude/coralline.conf` after deriving its internal file variables from the environment, remora also points `CORALLINE_CONFIG` at a generated wrapper in the same remora-owned state directory; the wrapper restores `CORALLINE_CONFIG` and `VL_CONF` to the original config path while sourcing that file, then reapplies the scoped paths. This preserves sibling-file lookups and every user option while preventing direct `RL5H_FILE`, `RL7D_FILE`, or `BURN_FILE` assignments from escaping isolation. Uninstall removes the known coralline runtime subtree while preserving the user config by default, including when XDG state and config homes alias, and `dry-run` surfaces every path for isolation checks.

## 0.1.8 - 2026-07-14

Add a phase-aware dispatch lifecycle before role routing. Discovery stabilizes the question, scope, evidence format, and stop condition without requiring a pre-decided implementation outcome. The main session then synthesizes one Plan; large, architectural, risky, or explicitly plan-first work waits for explicit approval; writing agents receive only stable execution contracts; and non-trivial results pass fresh completed-work verification. All eligible work is still chosen by net benefit across model cost, scarce context, elapsed time, isolation, and verification rather than requiring delegation to win every axis.

Plan verification and completed-work verification now have separate enforced contracts. Plan readiness requests only `READY` or `REVISE`; outcome verification requests only `CONFIRMED` or `REFUTED`. The verifier remains read-only and cannot write the Plan or fix findings.

Single unknown bugs now keep root-cause discovery, trace-driven debugging, the first minimal fix, and live verification in one main-session reasoning chain instead of becoming a sequential scout-to-executor pipeline. Read-only repository fan-out is opt-in: small bounded scans stay inline, while substantial independent scans, overlapable external latency, or deliberately independent perspectives can still fan out. Stable multi-file repetition retains an explicit path to `mech-executor`.

The change was tested against a disposable state-clone fixture modeled after a real status-line investigation. The v0.1.6 baseline used a foreground scout, foreground executor, and background verifier, completing in 322.90 seconds with a $1.316911 client-reported cost field. The balanced single-bug guard kept diagnosis and implementation inline, retained a fresh verifier, completed in 200.86 seconds, and reported $0.817504: 37.79% less wall time and 37.92% less reported cost in this single-run workload. Correctness remained 2/2 tests passing.

The complete bilingual dispatch experiment remains public in pilotfish, including the negative and positive-control fixtures, neutral prompts, rejected policy iterations, exact Agent tool inputs, normalized traces, model usage, raw-stream hashes, commands, limitations, and machine-readable results. The positive control confirms that the brake does not disable delegation: a 12-file stable mechanical edit still routed to the cheaper worker, reducing the client-reported cost field by 36.01% while taking 7.92% more wall time in one run. Regression tests lock both the negative boundary and positive delegation paths without changing remora's model routing, session isolation, or native-Claude guarantees.

Add an explicit composition contract for [Baton](https://github.com/cablate/baton): Baton may plan discovery and execution topology, while remora remains the source for named roles, model routing, leaf boundaries, approval, and verifier modes. A public fresh-session compatibility gate completed two background Luna discovery agents, main-session Plan synthesis, a Sol readiness review that returned `REVISE` then `READY`, explicit approval, direct main-session writing after Baton rejected a needless writer, and a separate Sol completed-work verifier returning `CONFIRMED`. All four named Agent calls omitted the invocation-level `model` field. The two-turn pass took 542.406 seconds and reported $2.149292 in the client cost field; these are single-run observations, not population estimates or invoices.

The report also discloses the rejected first candidate: its Plan verifier invocation requested the wrong completed-work vocabulary, and later outcome verifiers found citation gaps. That candidate was not counted as compatible even though its report eventually passed. The policy, role prompt, and regression tests were strengthened before the full gate was rerun in a new session. Exact prompts, normalized calls, model usage, transcript hashes, startup-stall criteria, and limitations are published under `benchmarks/baton-compatibility`.

## 0.1.7 — 2026-07-13

Harden Calico context handling against Codex runtime-catalog hot updates. CLIProxyAPI and the bundled Codex catalog can temporarily retain an older 372K value after the ChatGPT-authenticated runtime catalog changes GPT-5.6 Sol, Terra, and Luna to 272K. remora now keeps the gateway value as a diagnostic ceiling, reads a fresh local Codex `models_cache.json` as the runtime ceiling, and maps the smaller per-model value into Calico. Missing, stale, or incomplete Codex metadata falls back safely to 272K, while a later fresh 372K runtime catalog automatically restores 372K instead of being permanently pinned.

The current runtime defaults match Codex exactly: 272K raw, 258.4K effective at 95%, and 244.8K auto-compact at 90%. Explicit Calico compact-window overrides cannot exceed that client ceiling. `doctor` reports the gateway, Codex runtime, and final Calico client values separately.

## 0.1.6 — 2026-07-13

Fix named-role model routing at the Agent invocation boundary. The child-session orchestration policy now requires every existing named role to be invoked without a `model` argument, leaving its session-scoped `--agents` definition as the sole model source. This prevents a per-call alias such as `sonnet` from overriding a configured Luna executor with Terra, or otherwise bypassing the role map.

Only truly ad-hoc agents with no named role definition may specify `model` explicitly. Regression coverage verifies both halves of the contract while preserving remora's existing session isolation, model allowlist, and background-delegation behavior.

## 0.1.5 — 2026-07-13

Align delegation scheduling with pilotfish's dependency semantics. remora now appends a child-session-only orchestration policy that sends independent work and parallel fan-out to background agents, while retaining foreground execution only for an immediate blocking dependency. Background work must still be collected before dependent actions or the final response.

The policy is shipped as an auditable file and does not modify native Claude state. An explicit user `--append-system-prompt` or `--append-system-prompt-file` continues to win and suppresses remora's default policy for that session.

This release also adds `doctor --online` capability checks for the experimental Codex active-turn bridge, publishes the source-backed responsibility and acceptance-test report, standardizes the lowercase remora brand across runtime and documentation, and adds bilingual answers for common installation, routing, context, Calico, and security questions.

## 0.1.4 — 2026-07-12

Fix per-agent GPT-5.6 routing when the user's Claude Code `availableModels` setting excludes gateway model ids. Claude Code 2.1.207 silently inherited the main Sol model in that case even though the `--agents` definitions loaded correctly. remora now supplies a child-only additional settings document containing the configured Sol, Terra, and Luna ids, so subagent validation succeeds without modifying user settings, CLIProxyAPI, or Calico.

The launcher fails closed when callers also pass `--settings`, because Claude Code accepts only one additional-settings source and silently losing remora's routing allowlist would recreate the bug. `doctor` now reports the effective routing allowlist. Fresh-session end-to-end verification confirms a Sol main session delegated to a Luna subagent and reported both models in `modelUsage`.

## 0.1.3 — 2026-07-12

Correct the context policy introduced in 0.1.2. Stock Claude Code treats unknown custom model ids as 200K and caps a larger auto-compact setting, so remora now defaults to a truthful 200K client window and leaves Claude's native output-reserve and precompute behavior untouched.

Add an explicit `calico` mode for users who install a verified Calico Claude binary containing the new `custom-context-window` adapter. In this mode remora supplies an exact gateway-derived model/window map, reports 95% usable context to status-line consumers, and compacts at 90% of the raw provider window. Invalid maps and missing patches fail closed. Documentation also warns that `/resume` restores the old session's agent definitions after routing changes, and the release test no longer assumes Claude Code exists on the CI runner.

## 0.1.2 — 2026-07-12

Add gateway-aware context safety for long Claude Code sessions. remora reads CLIProxyAPI's Codex-compatible model catalog and uses the smallest context window among every configured main and agent model. It mirrors Codex CLI 0.144.1's defaults: 95% is reported as effective context and auto-compaction starts at 90% of the raw provider window. For the current 372K GPT-5.6 catalog, that means 353.4K effective context and a 334.8K compact trigger.

Discovery failures and incomplete catalogs fall back conservatively without blocking startup. Explicit user environment overrides still win. `doctor --online` now reports the detected per-model ceilings, effective context, and compact trigger, while `dry-run` exposes the token-free child settings.

## 0.1.1 — 2026-07-12

Fix the offline bootstrap test on Linux runners by making the explicit checksum-only override take precedence when requested. Bootstrap remains attestation-first by default; only callers that set `REMORA_ALLOW_CHECKSUM_ONLY=1` opt into the documented trust downgrade. The bootstrap test now derives its artifact version from `VERSION` so future releases cannot silently test a stale package name.

## 0.1.0 — 2026-07-12

Initial release. It includes the isolated launcher, a six-role GPT-5.6 Sol/Luna map with Terra as the default Sonnet alias, TOML configuration, environment or credential-command authentication, offline/online doctor checks, and isolation-focused tests.

The public installation path is approval-gated and release-pinned. Release archives carry SHA-256 checksums and GitHub build-provenance attestations; the bootstrap requires both unless the user explicitly accepts checksum-only verification. The installer performs collision checks, preserves existing configuration, updates the payload atomically, and never writes native Claude state.

The gateway documentation now includes a minimal Docker Compose deployment, independent OpenSSL-generated keys, separate same-computer and home-lab paths, LAN management hardening, SSH-tunneled Codex OAuth callback, GUI enrollment handoff, remora connectivity checks, and a Traditional Chinese quick-start. Its 429 analysis distinguishes OpenAI's active-turn continuation from CLIProxyAPI's request-level credential cooldown.
