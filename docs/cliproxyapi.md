# CLIProxyAPI Gateway Runbook

> Remora does not install or manage CLIProxyAPI. This runbook documents the boundary between them and keeps OAuth enrollment as an explicit human step.

## Deployment shape

CLIProxyAPI's upstream Compose file publishes port `8317`, mounts a configuration, OAuth material, and logs, and runs the `eceasy/cli-proxy-api` image. A minimal deployment derived from that layout is:

```yaml
services:
  cli-proxy-api:
    image: eceasy/cli-proxy-api:latest
    container_name: cli-proxy-api
    ports:
      - "8317:8317"
    volumes:
      - ./config.yaml:/CLIProxyAPI/config.yaml
      - ./auths:/root/.cli-proxy-api
      - ./logs:/CLIProxyAPI/logs
    restart: unless-stopped
```

> ⚠️ **Pin a tested image tag for a shared deployment.** `latest` is convenient for first setup but can change gateway behavior without changing Remora.

Generate independent API and management secrets rather than reusing an account password:

```bash
openssl rand -base64 32
openssl rand -base64 32
```

Follow the [CLIProxyAPI documentation](https://help.router-for.me/) for the current `config.yaml` schema. Keep the service behind a trusted LAN, VPN, or TLS reverse proxy; port `8317` accepts prompts, source code, and bearer credentials.

## OAuth enrollment

OAuth is intentionally a human handoff. Start the container, open its documented management UI or login flow, and connect the OpenAI/Codex account interactively. Do not automate browser login, copy refresh tokens into this repository, or commit the mounted `auths` directory.

The deployment is ready for Remora when the gateway returns a model catalog with the configured bearer token:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${REMORA_AUTH_TOKEN}" \
  http://127.0.0.1:8317/v1/models
```

## Remora connection

```toml
[proxy]
base_url = "http://127.0.0.1:8317"
auth_token_env = "REMORA_AUTH_TOKEN"
auth_token_command = []
```

If the gateway runs on another trusted host, replace loopback with its LAN or VPN address. Prefer HTTPS when traffic crosses an untrusted network.

## Model aliases

Remora forwards model names exactly as configured. Confirm every name appears in the gateway model catalog or is a documented alias:

| Remora field | Default |
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

## 429 diagnosis

Use latency and response body to separate upstream limiting from local gateway cooldown:

| Observation | Interpretation |
|---|---|
| First 429 takes hundreds of milliseconds or longer | Request likely reached the upstream provider |
| Later 429 responses return in a few milliseconds | Gateway selector is rejecting a cooled credential locally |
| Response contains `model_cooldown` | No credential is currently selectable for that model |
| Restart immediately clears the condition | Cooldown state was memory-only, not an expired OAuth token |

For CLIProxyAPI v7.2.67, generic 429 handling can promote the credential/model into a quota-style cooldown. With one credential, that becomes a full model blackout. Keep cooldown persistence disabled unless you explicitly need it, lower Remora concurrency when the provider is sensitive, and retain the original upstream 429 body when debugging.

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

> ⚠️ **A container restart is a recovery lever, not a root-cause fix.** It erases memory-only selector state and can send traffic straight back into a real upstream rate limit.

## Data and backup policy

| Path | Contains | Backup guidance |
|---|---|---|
| `config.yaml` | API keys, management policy, aliases | Encrypted secret backup |
| `auths/` | OAuth access and refresh material | Encrypted, access-restricted backup |
| `logs/` | Requests/errors; may contain source or prompts when debugging | Short retention; inspect before sharing |
| Remora TOML | Gateway address and token retrieval command | Safe to version only after removing host-specific secrets |
