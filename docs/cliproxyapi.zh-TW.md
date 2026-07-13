# CLIProxyAPI Gateway 快速部署

> remora 不負責安裝或管理 CLIProxyAPI。這份指引涵蓋最小 Docker Compose 部署、人工 Codex OAuth GUI 操作，以及與 remora 連線的安全邊界。

## 目錄

- [Docker Compose 快速部署](#docker-compose-快速部署)
- [選擇同機或遠端拓撲](#選擇同機或遠端拓撲)
- [啟動與檢查](#啟動與檢查)
- [透過 GUI 完成 Codex OAuth](#透過-gui-完成-codex-oauth)
- [連接 remora](#連接-remora)
- [模型設定](#模型設定)
- [實驗性 active-turn bridge](#實驗性-active-turn-bridge)
- [429 與 cooldown](#429-與-cooldown)
- [資料與備份](#資料與備份)

[English](./cliproxyapi.md)

## Docker Compose 快速部署

以下範例依照上游 [Docker Compose 指引](https://help.router-for.me/docker/docker-compose) 與 [設定範例](https://github.com/router-for-me/CLIProxyAPI/blob/main/config.example.yaml) 縮減而成。Proxy 與管理介面只應開放給可信任 LAN／VPN；Codex OAuth callback 則綁在 loopback，透過 SSH tunnel 使用。

| 需求 | 最低條件 |
|---|---|
| 主機 | remora 所在電腦本身，或它能連線的 Linux server／NAS |
| Runtime | Docker Engine 與 Compose plugin |
| 網路 | 可信任 LAN／VPN；跨不可信網路時必須使用 TLS |
| Secret | Proxy API key 與 management key 各自獨立隨機產生 |

建立私有目錄並產生兩組不同密碼：

```bash
mkdir -p ~/containers/cliproxyapi/{auths,logs}
chmod 700 ~/containers/cliproxyapi ~/containers/cliproxyapi/auths
cd ~/containers/cliproxyapi

openssl rand -base64 32  # Proxy API key：remora 使用
openssl rand -base64 32  # Management key：瀏覽器 GUI 使用
```

> ⚠️ **不要共用兩組 key。** Proxy API key 只能呼叫模型；management key 能修改設定與 OAuth credential，權限更高。

## 選擇同機或遠端拓撲

CLIProxyAPI 可以和 remora 安裝在同一台電腦，也可以放在另一台 home lab。兩者的 port exposure、management policy、OAuth callback 與 remora URL 不同，建立設定前先選定拓撲。

| 設定 | 同一台電腦 | 另一台 host／home lab |
|---|---|---|
| Proxy port | `8317` 只綁 loopback | 將 `8317` 發布到可信任 LAN／VPN |
| Management API | `allow-remote: false` | 保持 `allow-remote: false`，透過 SSH 存取 |
| Management URL | `http://127.0.0.1:8317/management.html` | Tunnel 後使用 `http://127.0.0.1:8318/management.html` |
| OAuth callback | Browser 直接連本機 `1455` | Server `1455` 保持 loopback，再使用 SSH tunnel |
| remora `base_url` | `http://127.0.0.1:8317` | `http://SERVER_LAN_IP:8317` |
| 網路限制 | 不對 LAN 開放 | Firewall 只允許 remora client IP／VPN subnet |

以下範例使用較安全的「同一台電腦」預設值。若 CLIProxyAPI 位於另一台 host，請套用每一個標示為「遠端 host」的修改。

建立 `config.yaml`，並替換兩個 placeholder：

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

儲存後限制檔案權限：

```bash
chmod 600 config.yaml
```

建立 `compose.yaml`：

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

使用**遠端 host**時，只把 proxy port 發布到 LAN；設定仍可維持 `allow-remote: false`：

```yaml
# compose.yaml 的 services.cli-proxy-api.ports
ports:
  - "8317:8317"
  - "127.0.0.1:1455:1455"
```

Server 的 `1455` 仍只綁 loopback。Host firewall 必須把 `8317` 限制到 remora 電腦或可信任 VPN subnet。Management page 與 model endpoint 雖然共用 port，但 `allow-remote: false` 會拒絕不是從 server localhost 進來的 management request。

如果可信任 LAN 必須直接開啟 management UI，可以明確改成 `allow-remote: true`；此時 management key 會成為這些 route 的主要防線，安全性不如以下 SSH 方法。

> ⚠️ **`latest` 適合第一次安裝，但不具可重現性。** 驗證版本後，分享或長期部署應固定 immutable image digest。

部署目錄不可提交到 Git。若它位於其他 repository 內，至少加入：

```gitignore
config.yaml
auths/
logs/
```

## 啟動與檢查

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail 100 cli-proxy-api
```

管理介面由 CLIProxyAPI 本身提供。使用遠端 host 時，同一條 SSH session 一次轉發 management 與 OAuth callback：

```bash
ssh \
  -L 8318:127.0.0.1:8317 \
  -L 1455:127.0.0.1:1455 \
  USER@SERVER_LAN_IP
```

依拓撲選擇管理 URL：

```text
同一台電腦：http://127.0.0.1:8317/management.html
遠端 host 經 SSH：http://127.0.0.1:8318/management.html
```

開啟後輸入 management key。若瀏覽器不在同一個可信任網路，請使用 VPN 或 TLS reverse proxy，不要把 `8317` 直接暴露到 Internet。

## 透過 GUI 完成 Codex OAuth

進入 Management Center 的 OAuth 頁面，啟動 Codex login，依畫面開啟 OpenAI 授權頁。官方 [Management Center](https://github.com/router-for-me/Cli-Proxy-API-Management-Center) 支援 Codex OAuth 與 callback 提交。

同一台電腦不需要 tunnel，browser 可以直接連上 loopback callback。Docker 位於**遠端 host**時，授權完成前保持上一節的雙 port SSH session 開啟。若 GUI 要求貼回 callback URL 或 authorization result，直接貼入 GUI，不要存進 shell script。成功後，掛載的 `auths/` 目錄會出現 Codex JSON credential。

> ⚠️ **不要自動化瀏覽器登入，也不要把 OAuth JSON 複製到 remora。** `auths/` 含 refresh material，只能留在 gateway host。

使用 proxy API key 驗證 model catalog：

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${REMORA_AUTH_TOKEN}" \
  http://SERVER_LAN_IP:8317/v1/models
```

HTTP `200` 且 `.data` 內有模型，代表 proxy API key 與 OAuth credential 都能使用。

## 連接 remora

修改 remora 設定：

```toml
[proxy]
base_url = "http://SERVER_LAN_IP:8317"
auth_token_env = "REMORA_AUTH_TOKEN"
auth_token_command = []
```

暫時測試時只在目前 terminal 提供 key：

```bash
export REMORA_AUTH_TOKEN='REPLACE_WITH_PROXY_API_KEY'
remora doctor --online
```

長期使用應改從 macOS Keychain 或其他 OS credential store 讀取，避免把 key 寫入 TOML、shell profile 或 repository。

## 模型設定

remora 會原樣傳送設定的 model name。CLIProxyAPI 的 `/v1/models` 必須包含：

| remora 欄位 | 預設模型 |
|---|---|
| `models.main` | `gpt-5.6-sol` |
| `models.default_opus` | `gpt-5.6-sol` |
| `models.default_sonnet` | `gpt-5.6-terra` |
| `models.default_haiku` | `gpt-5.6-luna` |

```bash
remora agents
remora doctor --online
remora dry-run
```

## Context window 對齊

不要把 OpenAI 公開 API 的 context 數字直接覆寫進 CLIProxyAPI metadata。公開 GPT-5.6 API 標示 1.05M，CLIProxyAPI 對 Codex OAuth route 目前則回報 372K。但 gateway 只是其中一層 ceiling；權威的 Codex runtime-catalog hot update 已在 2026-07-13 把 Sol、Terra、Luna 改成 272K。

Stock CLIProxyAPI 不需修改即可唯讀取得該值：

```bash
curl -fsS \
  -H "Authorization: Bearer $REMORA_AUTH_TOKEN" \
  'http://127.0.0.1:8317/v1/models?client_version=remora' \
  | jq '.models[] | select(.slug | startswith("gpt-5.6-")) | {slug, context_window}'
```

remora 會做同一個唯讀查詢，但安全預設遵循原生 Claude Code 對未知 custom model id 的 200K 上限。`stock` 模式不注入 context 或 compact override；Claude 原生的 output reserve 與 precompute policy 維持權威。CLIProxyAPI 不需要修改或重啟。

可選的 `calico` 模式必須搭配通過驗證的 Calico Claude binary。remora 會針對每個已設定模型比較 gateway catalog 與新鮮的本機 Codex runtime cache，採用較小值後再把精確 map 交給 Calico 預設休眠的 adapter。目前 272K client window 會讓 statusline consumer 看到 258.4K usable context，並在 244.8K compact。Codex cache 不存在、超過五分鐘或不完整時，安全 fallback 也是 272K；若之後新鮮的 runtime catalog 回到 372K，remora 會自動恢復 372K。Binary 沒有 adapter marker 時，remora 會拒絕啟動該模式。

| 來源 | 意義 |
|---|---|
| Gateway `context_window` | Gateway 宣告的 ceiling；保留供診斷 |
| 新鮮的 `~/.codex/models_cache.json` | 權威 Codex runtime ceiling；唯讀且只有 metadata |
| `[context].mode = "stock"` | 原生安全的 200K client 行為；預設值 |
| `[context].mode = "calico"` | 明確選用已驗證的 custom-context adapter |
| `[context].stock_window` | 原生 Claude Code 的 custom-model window，通常是 200K |
| `[context].fallback_window` | Catalog 查不到或不完整時的保守值 |
| `[context].codex_fallback_window` | Runtime cache 不可信時的安全 Codex ceiling；目前為 272K |
| `[context].codex_cache_ttl_seconds` | Codex runtime cache 的 freshness 上限；對齊 Codex 的 300 秒 TTL |
| `[context].codex_models_cache` | 可選的路徑 override；否則使用 `$CODEX_HOME/models_cache.json` 或 `~/.codex/models_cache.json` |
| `[context].effective_window_percent` | 診斷用 effective-input 比例；Codex 預設 95% |
| `[context].auto_compact_percent` | Child auto-compaction 比例；Codex 預設 90% |
| 既有 Claude auto-compact 環境變數 | 使用者明確 override；Calico 模式仍會裁到 Codex client ceiling |

## 實驗性 active-turn bridge

目前 stock `eceasy/cli-proxy-api:latest` 不會在 Claude 分開送出的 tool-result request 之間保存 Codex `x-codex-turn-state`。相容 build 必須包含 v1 bridge，並明確啟用：

```yaml
codex:
  active-turn-bridge: true
```

Version 1 只在安全拓撲下啟用，否則 fail closed：

| 必要條件 | 原因 |
|---|---|
| 只有一個啟用中的 Codex credential | Backend turn token 屬於取得它的 credential |
| 該 credential 設定 `disable_cooling: true`，或全域關閉 cooling | Selector 不得在 continuation 到達 executor 前隱藏 credential |
| 只有一個本地 CLIProxyAPI process | Turn state 只存在 bounded process memory，不會寫入 Home KV |
| Calico binary 含 `calico-active-turn-adapter:v1` | Stock Claude 不會傳送穩定的 user-prompt boundary |
| `remora doctor --online` 顯示 protocol v1 ready | Binary 與 gateway capability 必須同時吻合 |

所有條件都成立時，gateway 才會回傳：

```text
X-CLIProxyAPI-Codex-Active-Turn: 1
```

多 credential 或 Home mode 會直接移除 capability header，不會靜默使用不安全 failover。Raw backend turn state 只保留在有上限的 process memory；request log 必須遮蔽原值，debug 診斷也只能記錄截短的 SHA-256 fingerprint。

> **可用性：** 這項 bridge 尚未進入 upstream CLIProxyAPI `v7.2.71`。在 remora 維護 build 或 upstream release 發布前，上方的一般部署仍會安裝 stock gateway，`doctor --online` 也會如實顯示 active-turn mode 為 degraded。

## 429 與 cooldown

OpenAI 官方說明了一個重要的 Codex 行為：若 active turn 執行途中達到 usage limit，Codex 可以在 fair-use 範圍內完成該 turn，之後才執行限制。這是產品層的 turn semantics，不代表中間每一個 HTTP request 都不會收到 429。參考 [What happens if I reach a usage limit while Codex is working?](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan#h_d4e7d9c216)。

CLIProxyAPI v7.2.67 工作在 request 與 credential 層。Codex executor 能辨識 `usage_limit_reached` 與 reset metadata，但 auth scheduler 仍會把最後產生的 HTTP 429 標成 quota exhaustion，並將 credential/model 放入 cooldown。Claude Code 的一個 turn 可能轉譯成多次 upstream request，因此 gateway 的本地 cooldown 可能在 native Codex 原本還能完成 active turn 時先中斷後續工作。

| 層級 | 觀察到的責任 |
|---|---|
| OpenAI Codex 產品 | 跨過方案 limit 後，可能允許 active turn 繼續完成 |
| OpenAI upstream endpoint | 仍可能回傳 `usage_limit_reached`、capacity、connection 或 transient rate-limit error |
| CLIProxyAPI executor | 將 Codex limit signal 與 reset metadata 轉成 HTTP 429 result |
| CLIProxyAPI auth scheduler | 把 429 當成 quota，讓 credential/model 在本地 unavailable |
| Claude Code | 按自身 policy retry，但無法選到被 gateway scheduler 隱藏的 credential |

| 現象 | 解讀 |
|---|---|
| 第一個 429 經過正常網路延遲才回來 | 請求可能到達 OpenAI upstream |
| 後續 429 在數毫秒內返回 | CLIProxyAPI selector 可能在本地拒絕 cooled credential |
| Response 含 `model_cooldown` | 目前沒有可選 credential |
| 重啟立即恢復 | Cooldown 很可能只存在記憶體，不代表 OAuth 過期 |

不要先全域設定 `disable-cooling: true`。若單一 Codex OAuth credential 因 transient 429 被錯誤封鎖，可以在該 credential JSON 頂層加入：

```json
{
  "disable_cooling": true
}
```

這只會取消 CLIProxyAPI 的本地 cooldown，無法繞過 OpenAI 真正回傳的 429，也無法讓耗盡的 quota 恢復。修改 OAuth JSON 前必須備份並限制檔案權限。

> **目前結論：** 證據支持「gateway 造成第二階段封鎖」，不支持 CLIProxyAPI 在 request 前根據 usage 百分比主動阻擋。它先收到一次 upstream failure，接著 local scheduler 才擋住後續 request。Credential-scoped `disable_cooling` 能移除第二階段封鎖，但 Claude Code 與 native Codex 的 upstream request boundary 不一定相同，因此無法保證完全重現 Codex 的 active-turn continuation。

下次重現時，不需要保存 prompt 或 token；只要記錄第一個 failure 的 timestamp、latency、HTTP status、`error.type`、`error.code`、reset fields，以及下一次是否立即變成 `model_cooldown`。這些資訊足以區分 `usage_limit_reached` 與 transient 429，也適合整理成 CLIProxyAPI upstream issue。

## 資料與備份

| 路徑 | 內容 | 建議 |
|---|---|---|
| `config.yaml` | API key、management policy | 加密備份，權限 `0600` |
| `auths/` | OAuth access／refresh material | 加密且限制存取，不進 Git |
| `logs/` | 錯誤與 request log | 短期保留，分享前先檢查敏感內容 |
| remora TOML | Gateway address 與 token command | 移除主機特定 secret 後才可 version control |
