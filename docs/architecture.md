# remora Architecture

## Design goal

remora provides a second launch surface for Claude Code. It changes routing for one process tree while preserving native Claude as a fully independent control path.

```mermaid
flowchart TD
    SHELL[Shell state] --> NATIVE[claude]
    SHELL --> REMORA[remora]
    NATIVE --> ANTHROPIC[Original authentication and model]
    REMORA --> CHILD[Child environment]
    CHILD --> FLAGS[Session flags]
    CHILD --> GATEWAY[Anthropic-compatible gateway]
    FLAGS --> AGENTS[Dynamic role agents]
```

## Isolation contract

| Boundary | remora behavior | Why it matters |
|---|---|---|
| Process | Uses `execvpe` with a copied environment | Overrides disappear with the child |
| Integration marker | Sets `REMORA_ACTIVE=1` in the copied environment | Status lines and hooks can identify remora without inspecting credentials or gateway URLs |
| Settings | Writes no Claude settings; passes configured model ids through child-only `--settings` | Native configuration remains authoritative outside remora while subagent validation can see gateway ids |
| Agents | Sends one JSON object through `--agents` | Claude Code scopes it to the current session |
| Orchestration | Appends a dependency-based scheduling policy | Independent work runs in background; only immediate dependencies block in foreground |
| Authentication | Resolves a remora-specific token, then sets `ANTHROPIC_AUTH_TOKEN` only in the child | The user's Anthropic login is neither read nor replaced on disk |
| Model defaults | Sets the three documented `ANTHROPIC_DEFAULT_*_MODEL` variables in the child | Internal Claude tiers resolve to gateway model names |
| Routing allowlist | Adds every configured gateway id to the session's `availableModels` | Claude does not silently inherit the main model for an excluded subagent id |
| Global override | Removes `CLAUDE_CODE_SUBAGENT_MODEL` from the copied child environment by default | One global variable cannot collapse every role back to one model |

Claude Code's precedence places dynamic `--agents` below managed agents but above project, user, and plugin agents. A managed organization policy can therefore still prevent or replace a remora role; remora deliberately does not bypass managed policy.

Claude Code also applies `availableModels` to subagent definitions. In 2.1.207, an excluded custom gateway id silently falls back to the parent model. remora supplies its configured ids as an additional session-only allowlist and rejects a competing explicit `--settings` argument rather than risk losing that routing control. This is a compatibility allowance, not a bypass of higher-precedence managed policy.

## Launch sequence

```mermaid
sequenceDiagram
    participant U as User
    participant R as remora
    participant S as Secret store
    participant C as Claude Code
    participant G as Gateway

    U->>R: remora --continue
    R->>R: Load and validate TOML
    R->>R: Combine role prompts with model map
    R->>S: Read gateway token
    S-->>R: Token on stdout or environment
    R->>C: exec claude --settings ... --model ... --agents ... --append-system-prompt ...
    C->>G: POST /v1/messages
    G-->>C: Anthropic-compatible stream translated from OpenAI
```

## Role policy

The split follows capability and token volume rather than file ownership. Read-only fan-out and fully specified mechanical work go to Luna. Work requiring design judgment, independent verification, or security reasoning goes to Sol. Subagents are leaf workers and are denied recursive delegation, preventing an unbounded agent tree.

For every existing named role, its `--agents` definition is the sole model source. The orchestrator omits the Agent tool's invocation-level `model` field because Claude Code gives that field higher precedence than the role definition. An explicit invocation model is reserved for a truly ad-hoc agent with no named definition.

Foreground versus background is a parent-orchestrator decision, so it cannot be enforced inside a leaf agent prompt. remora therefore appends a child-session-only policy: independent work and parallel fan-out use `run_in_background: true`; foreground execution is reserved for a result required by the main session's very next action when no other useful work can proceed. Explicit user `--append-system-prompt` or `--append-system-prompt-file` arguments replace this default for that session.

| Decision | Chosen behavior | Rejected behavior |
|---|---|---|
| Main model | Sol by default | Changing Claude's persistent default |
| Recon | Luna, low effort | Letting built-in Explore inherit Sol |
| Mechanical execution | Luna, medium effort | Paying Sol for deterministic bulk work |
| Verification | Fresh Sol context | Self-review by the implementer |
| Security | Sol, max effort | Cheap routing at a trust boundary |
| Configuration | Model names in TOML | Hard-coded provider catalog in prompts |
| Context safety | Read the gateway ceiling, reserve output space, and scope auto-compaction to the child | Pretending every provider route has the public API's maximum window |

## Gateway semantics

Claude Code speaks the Anthropic Messages protocol, while the selected models may be OpenAI models. The gateway owns protocol translation, OAuth, model aliases, cooldown, retries, and account selection. remora owns none of those concerns; it only chooses the gateway-visible model string for each role.

Codex active-turn continuity also crosses this boundary. Native Codex preserves server-issued turn state across tool continuations, but the stock CLIProxyAPI Claude bridge does not currently retain that state. This can make a remora turn stop at a subscription allowance boundary before native Codex would stop under backend fair-use rules. The confirmed source evidence, responsibility split, non-solutions, and acceptance test are documented in [Preserving Codex Active-Turn Fair Use Through the Claude Bridge](./codex-active-turn-fair-use.md).

Context capacity crosses that boundary: the gateway catalog describes its route, while ChatGPT-authenticated Codex can replace the bundled model catalog with remotely supplied runtime metadata. Those two sources temporarily diverged at 372K and 272K. Stock Claude Code also assigns unknown custom model ids a separate 200K client window. remora's default `stock` policy therefore reports the truthful 200K window and leaves Claude's native compact pipeline untouched. The optional `calico` policy queries `/v1/models?client_version=remora`, reads only fresh model metadata from the local Codex cache, and takes the smaller per-model value before supplying an exact child-only map to a separately verified Calico binary. It then reports 95% usable context and applies the 90% compact ratio exactly once. Discovery is read-only, uses a 272K TOML fallback for the Codex ceiling, and never rewrites either catalog.

This separation makes failures diagnosable:

| Error location | Typical evidence | Owner |
|---|---|---|
| Launcher | Invalid TOML, missing token, missing `claude` binary | remora |
| Claude runtime | Invalid `--agents` field or unavailable tool | Claude Code version/configuration |
| Gateway selector | Millisecond 429 with `model_cooldown` | Gateway state |
| Upstream | Slower 429/5xx after a real network request | Provider/model/account |

## Native-Claude proof

The launcher contains no code path that opens `~/.claude` for writing. Installation targets only XDG-style remora paths. Tests also assert that clearing a global subagent override mutates only the environment copy passed to the child, not the parent process.

## Compatibility boundary

The design depends on current Claude Code support for `--agents`, agent fields such as `model` and `effort`, and custom gateway environment variables. remora validates its own shape but cannot guarantee that an arbitrary OpenAI model fully reproduces Claude-specific tool-use, caching, extended-thinking, or context-window behavior. Treat each gateway/model combination as an integration that needs an end-to-end smoke test.
