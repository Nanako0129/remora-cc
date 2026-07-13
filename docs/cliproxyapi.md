# CLIProxyAPI Gateway Runbook

> remora does not install or manage CLIProxyAPI. This runbook documents the boundary between them and keeps OAuth enrollment as an explicit human step.

## Contents

- [Quick Docker Compose deployment](#quick-docker-compose-deployment)
- [Choose the host topology](#choose-the-host-topology)
- [Start and inspect](#start-and-inspect)
- [OAuth enrollment in the GUI](#oauth-enrollment-in-the-gui)
- [remora connection](#remora-connection)
- [Model aliases](#model-aliases)
- [Experimental active-turn bridge](#experimental-active-turn-bridge)
- [429 diagnosis](#429-diagnosis)
- [Data and backup policy](#data-and-backup-policy)

[繁體中文](./cliproxyapi.zh-TW.md)

## Quick Docker Compose deployment

This example follows the upstream [Docker Compose guide](https://help.router-for.me/docker/docker-compose) and [configuration schema](https://github.com/router-for-me/CLIProxyAPI/blob/main/config.example.yaml). It exposes the proxy and management UI to the LAN while keeping the Codex OAuth callback on loopback for an SSH tunnel.

| Requirement | Minimum |
|---|---|
| Host | The remora computer itself, or a Linux server/NAS reachable from it |
| Runtime | Docker Engine with the Compose plugin |
| Network | Trusted LAN or VPN; use TLS when crossing an untrusted network |
| Secrets | Separate random proxy API key and management key |

Create a private deployment directory and generate two independent secrets:

```bash
mkdir -p ~/containers/cliproxyapi/{auths,logs}
chmod 700 ~/containers/cliproxyapi ~/containers/cliproxyapi/auths
cd ~/containers/cliproxyapi

openssl rand -base64 32  # proxy API key: remora uses this
openssl rand -base64 32  # management key: browser GUI uses this
```

> ⚠️ **Keep the two values separate.** The proxy API key authorizes model requests. The management key can change configuration and OAuth credentials, so it is more privileged.

## Choose the host topology

CLIProxyAPI can run on the same computer as remora or on another machine. Decide before creating the files because the published port, management policy, callback handling, and remora URL differ.

| Setting | Same computer | Separate host / home lab |
|---|---|---|
| Proxy port | Bind `8317` to loopback only | Publish `8317` to a trusted LAN/VPN |
| Management API | `allow-remote: false` | Keep `allow-remote: false`; reach it through SSH |
| Management URL | `http://127.0.0.1:8317/management.html` | `http://127.0.0.1:8318/management.html` through the tunnel |
| OAuth callback | Browser reaches local `1455` directly | Keep server `1455` on loopback and use an SSH tunnel |
| remora `base_url` | `http://127.0.0.1:8317` | `http://SERVER_LAN_IP:8317` |
| Network control | No LAN exposure | Firewall `8317` to the remora client subnet/address |

The examples below use the safer same-computer defaults. For a separate host, apply every change marked **separate host**.

Create `config.yaml`, replacing both placeholder values:

```yaml
host: ""
port: 8317

remote-management:
  allow-remote: false
  secret-key: "REPLACE_WITH_MANAGEMENT_KEY"
  disable-control-panel: false

auth-dir: "/root/.cli-proxy-api"

api-keys:
  - "REPLACE_WITH_PROXY_API_KEY"

debug: false
logging-to-file: false
usage-statistics-enabled: false
request-retry: 3
max-retry-interval: 30
disable-cooling: false
save-cooldown-status: false
```

Protect the configuration after saving it:

```bash
chmod 600 config.yaml
```

Create `compose.yaml`:

```yaml
services:
  cli-proxy-api:
    image: eceasy/cli-proxy-api:latest
    pull_policy: always
    container_name: cli-proxy-api
    ports:
      - "127.0.0.1:8317:8317"
      - "127.0.0.1:1455:1455"
    volumes:
      - ./config.yaml:/CLIProxyAPI/config.yaml
      - ./auths:/root/.cli-proxy-api
      - ./logs:/CLIProxyAPI/logs
    restart: unless-stopped
```

For a **separate host**, publish only the proxy port to the LAN; the configuration can keep `allow-remote: false`:

```yaml
# compose.yaml, under services.cli-proxy-api.ports
ports:
  - "8317:8317"
  - "127.0.0.1:1455:1455"
```

Keep port `1455` on server loopback. Restrict `8317` with the host firewall to the remora machine or trusted VPN subnet. Although the management page and model endpoint share this port, `allow-remote: false` rejects management requests that do not arrive from server localhost.

If a trusted LAN must access the management UI directly, `allow-remote: true` is an explicit alternative. It makes the management key the remaining protection for those routes; prefer the SSH method below.

> ⚠️ **`latest` follows the upstream quick start but is not reproducible.** After validating a version, pin its immutable image digest for a shared or production-like deployment.

Do not commit the deployment directory. If it sits inside another repository, add these entries to that repository's `.gitignore`:

```gitignore
config.yaml
auths/
logs/
```

## Start and inspect

Start the service and confirm that it loaded one configuration without exposing either key:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail 100 cli-proxy-api
```

The management panel is served by CLIProxyAPI itself. On a separate host, create the management and OAuth callback tunnels together:

```bash
ssh \
  -L 8318:127.0.0.1:8317 \
  -L 1455:127.0.0.1:1455 \
  USER@SERVER_LAN_IP
```

Choose the management URL for the topology:

```text
Same computer: http://127.0.0.1:8317/management.html
Separate host through SSH: http://127.0.0.1:8318/management.html
```

Enter the management key when prompted. If the browser is not on the same trusted network, use a VPN or TLS reverse proxy instead of exposing port `8317` directly to the internet.

## OAuth enrollment in the GUI

OAuth is intentionally a human handoff. Open the management panel, select the OAuth section, start a Codex login, and follow the displayed authorization flow. The official [Management Center](https://github.com/router-for-me/Cli-Proxy-API-Management-Center) supports Codex OAuth and callback submission.

On the **same computer**, no tunnel is needed because the browser can reach the loopback callback directly. On a **separate Docker host**, keep the two-forward SSH session from the previous section open until the browser flow completes. If the panel asks for the final callback URL or authorization result, paste it into the panel rather than storing it in a shell script. Successful enrollment creates a Codex JSON credential under the mounted `auths/` directory.

> ⚠️ **Never automate browser login or copy OAuth JSON into remora.** The `auths/` directory contains refresh material and belongs only on the gateway host.

The deployment is ready for remora when the gateway returns a model catalog with the configured bearer token:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${REMORA_AUTH_TOKEN}" \
  http://127.0.0.1:8317/v1/models
```

For a separate-host deployment, replace loopback with the gateway's LAN or VPN address. An HTTP `200` response and a non-empty `.data` model list confirm the proxy API key and OAuth credential are both usable.

## remora connection

```toml
[proxy]
base_url = "http://127.0.0.1:8317"
auth_token_env = "REMORA_AUTH_TOKEN"
auth_token_command = []
```

If the gateway runs on another trusted host, replace loopback with its LAN or VPN address. Prefer HTTPS when traffic crosses an untrusted network.

## Model aliases

remora forwards model names exactly as configured. Confirm every name appears in the gateway model catalog or is a documented alias:

| remora field | Default |
|---|---|
| `models.main` | `gpt-5.6-sol` |
| `models.default_opus` | `gpt-5.6-sol` |
| `models.default_sonnet` | `gpt-5.6-terra` |
| `models.default_haiku` | `gpt-5.6-luna` |

Run the local checks after changing aliases:

```bash
remora agents
remora doctor --online
remora dry-run
```

## Context-window alignment

Do not copy the public OpenAI API context number into CLIProxyAPI metadata. The public GPT-5.6 API documents 1.05M, while CLIProxyAPI currently reports 372K for the Codex OAuth route. That gateway value is only one ceiling: an authoritative Codex runtime-catalog hot update changed Sol, Terra, and Luna to 272K on 2026-07-13.

Stock CLIProxyAPI exposes that value without any server modification:

```bash
curl -fsS \
  -H "Authorization: Bearer $REMORA_AUTH_TOKEN" \
  'http://127.0.0.1:8317/v1/models?client_version=remora' \
  | jq '.models[] | select(.slug | startswith("gpt-5.6-")) | {slug, context_window}'
```

remora performs the lookup read-only, but its safe default follows stock Claude Code's 200K limit for unknown custom model ids. In `stock` mode it does not inject context or compact overrides; Claude's native output reserve and precompute policy remain authoritative. CLIProxyAPI needs no change or restart.

The optional `calico` mode requires a verified Calico Claude binary. It takes the smaller value from the gateway catalog and a fresh local Codex runtime cache for every configured model, then passes that exact map into Calico's dormant adapter. The current 272K client window gives status-line consumers 258.4K usable context and begins compaction at 244.8K. A missing, older-than-five-minutes, or incomplete Codex cache falls back to 272K. A later fresh 372K runtime catalog restores 372K automatically. remora refuses to launch this mode if the binary does not contain the adapter marker.

| Source | Meaning |
|---|---|
| Gateway `context_window` | Gateway-advertised ceiling; retained for diagnostics |
| Fresh `~/.codex/models_cache.json` | Authoritative Codex runtime ceiling; read-only, metadata only |
| `[context].mode = "stock"` | Stock-safe 200K client behavior; default |
| `[context].mode = "calico"` | Explicit opt-in to the verified custom-context adapter |
| `[context].stock_window` | Stock Claude Code custom-model window, normally 200K |
| `[context].fallback_window` | Conservative value used when catalog lookup is unavailable or incomplete |
| `[context].codex_fallback_window` | Safe Codex ceiling when its runtime cache cannot be trusted; currently 272K |
| `[context].codex_cache_ttl_seconds` | Freshness limit for the Codex runtime cache; matches Codex's 300-second TTL |
| `[context].codex_models_cache` | Optional path override; otherwise uses `$CODEX_HOME/models_cache.json` or `~/.codex/models_cache.json` |
| `[context].effective_window_percent` | Diagnostic effective-input ratio; Codex defaults to 95% |
| `[context].auto_compact_percent` | Child auto-compaction ratio; Codex defaults to 90% |
| Existing Claude auto-compact environment variables | Explicit user overrides, capped to the Codex client ceiling in Calico mode |

## Experimental active-turn bridge

The stock `eceasy/cli-proxy-api:latest` image does not currently preserve Codex `x-codex-turn-state` across separate Claude tool-result requests. A compatible build must contain the v1 bridge and must opt in explicitly:

```yaml
codex:
  active-turn-bridge: true
```

Version 1 fails closed unless the runtime topology is safe:

| Requirement | Why it is mandatory |
|---|---|
| Exactly one enabled Codex credential | A server-issued turn token belongs to the credential that received it |
| Credential-level `disable_cooling: true` or global cooling disabled | The selector must not hide that credential before a recognized continuation reaches the executor |
| One local CLIProxyAPI process | Turn state is intentionally memory-only and is not shared through Home KV |
| Calico binary with `calico-active-turn-adapter:v1` | Stock Claude does not send a stable user-prompt boundary |
| `remora doctor --online` reports protocol v1 ready | The binary and gateway capability must agree before parity is assumed |

The gateway advertises readiness only as:

```text
X-CLIProxyAPI-Codex-Active-Turn: 1
```

when every runtime requirement above is satisfied. Multiple credentials or Home mode remove the header rather than silently using unsafe failover. The raw backend turn-state value is retained only in bounded process memory, masked from request logs, and represented in debug diagnostics only by a truncated SHA-256 fingerprint.

> **Availability:** this bridge is not part of upstream CLIProxyAPI `v7.2.71`. Until a remora-maintained build or upstream release is published, the normal deployment instructions above intentionally install the stock gateway and `doctor --online` will report active-turn mode as degraded.

## 429 diagnosis

OpenAI documents an important Codex behavior: when a usage limit is reached during an active turn, Codex may continue that turn under fair-use limits and enforce the limit afterward. This is a turn-level product behavior, not a promise that every intermediary request will avoid HTTP 429. See [What happens if I reach a usage limit while Codex is working?](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan#h_d4e7d9c216).

CLIProxyAPI v7.2.67 operates at request and credential level. Its Codex executor recognizes `usage_limit_reached` and reset metadata, but the auth scheduler marks every resulting HTTP 429 as quota exhaustion and cools the credential/model unless cooling is disabled. A Claude Code turn can require multiple translated upstream requests, so a local cooldown can interrupt the remaining work even when native Codex would have been allowed to finish its active turn.

| Layer | Observed responsibility |
|---|---|
| OpenAI Codex product | May grant active-turn continuation after a plan limit is crossed |
| OpenAI upstream endpoint | Can still return `usage_limit_reached`, capacity, connection, or transient rate-limit errors |
| CLIProxyAPI executor | Converts Codex limit signals and reset metadata into an HTTP 429 result |
| CLIProxyAPI auth scheduler | Treats that 429 as quota and makes the credential/model locally unavailable |
| Claude Code | Retries according to its own client policy, but cannot select a credential hidden by the gateway scheduler |

Use latency and response body to separate upstream limiting from local gateway cooldown:

| Observation | Interpretation |
|---|---|
| First 429 takes hundreds of milliseconds or longer | Request likely reached the upstream provider |
| Later 429 responses return in a few milliseconds | Gateway selector is rejecting a cooled credential locally |
| Response contains `model_cooldown` | No credential is currently selectable for that model |
| Restart immediately clears the condition | Cooldown state was memory-only, not an expired OAuth token |

For CLIProxyAPI v7.2.67, generic 429 handling can promote the credential/model into a quota-style cooldown. With one credential, that becomes a full model blackout. Keep cooldown persistence disabled unless you explicitly need it, lower remora concurrency when the provider is sensitive, and retain the original upstream 429 body when debugging.

To keep a single file-backed Codex OAuth credential selectable after an upstream 429, add the following top-level field to that credential's JSON file inside the mounted `auths/` directory:

```json
{
  "disable_cooling": true
}
```

The fragment above is illustrative: preserve every existing token and account field. Back up the file, restrict it to the service account, and restart or reload CLIProxyAPI after editing it. This credential-scoped override is preferable to the global `disable-cooling: true` setting when the gateway also serves other providers.

| What the override changes | What it does not change |
|---|---|
| Prevents CLIProxyAPI from placing that credential/model into its local cooldown scheduler | Cannot suppress or bypass a 429 returned by OpenAI |
| Allows the client retry policy to keep reaching the upstream provider | Cannot make an exhausted quota usable |
| Avoids a memory-only `model_cooldown` blackout with a single credential | Does not provide failover; add another eligible credential for that |

> **Current conclusion:** the evidence supports a gateway-induced second-stage block, not proactive blocking based on the usage percentage. CLIProxyAPI first observes an upstream failure, then its local scheduler prevents later requests. Credential-scoped `disable_cooling` removes that second-stage block, but it cannot guarantee native Codex active-turn continuation because Claude Code and Codex may divide a turn into different upstream request boundaries.

To capture a future reproduction without exposing source or tokens, record the first failing response's timestamp, latency, HTTP status, `error.type`, `error.code`, reset fields, and whether the next response is an immediate `model_cooldown`. This distinguishes `usage_limit_reached` from transient 429 and provides enough evidence for an upstream CLIProxyAPI issue.

> ⚠️ **A container restart is a recovery lever, not a root-cause fix.** It erases memory-only selector state and can send traffic straight back into a real upstream rate limit.

## Data and backup policy

| Path | Contains | Backup guidance |
|---|---|---|
| `config.yaml` | API keys, management policy, aliases | Encrypted secret backup |
| `auths/` | OAuth access and refresh material | Encrypted, access-restricted backup |
| `logs/` | Requests/errors; may contain source or prompts when debugging | Short retention; inspect before sharing |
| remora TOML | Gateway address and token retrieval command | Safe to version only after removing host-specific secrets |
