# 🤖 AI Key-Rotating Proxy (V1–V10)

High-performance, fault-tolerant key-rotating proxy for **OpenRouter, OpenAI, Anthropic, Grok, Gemini, DeepSeek, and local Ollama**.

Features **strict 1→2→3 key rotation**, **preemptive token & request budgets**, **10 isolated upstream site slots (`V1` through `V10`)**, and an integrated **Telegram DM Control Bot** with 2-column grid controls.

---

## ⚡ Key Highlights

* **10 Isolated Upstream Sites (`V1` to `V10`)**:
  * Strict path routing: `/v1/…` routes strictly to site V1, `/v2/…` to site V2, up to `/v10/…` to site V10.
  * Uppercase `/V1/…` to `/V10/…` also fully supported.
  * Dynamic slot management: configure URLs, friendly titles (`/sitename`), custom headers, and per-site timeout/budgets on the fly.
* **Dual Budget Key Rotation (Tokens & Requests)**:
  * Strict sequential rotation: **Key 1 → 2 → 3** as either token limit (`MAX_TOKENS_PER_KEY`) or request limit (`MAX_REQS_PER_KEY`) is reached.
  * Zero-delay failover on provider errors (`401`, `402` quota exhausted, `429` rate-limited, `5xx`).
  * Automatic `Retry-After` backoff handling for provider rate limits.
* **Accurate Multi-Provider Token Accounting**:
  * Extracts usage from streaming SSE (`stream_options.include_usage = true`) and non-streaming payloads across OpenAI, Anthropic, Gemini (`usageMetadata`), Ollama, and DeepSeek.
  * Robust prompt/completion token merging across split SSE chunks.
  * Correct cached token accounting (OpenAI cached prompt tokens are never double-billed).
* **Telegram DM Control Bot**:
  * Clean **2-column grid layout** optimized for mobile and desktop screens.
  * **Single-Tap Carousel Navigation**: browse seamlessly across sites `[⬅️ V10] [🌐 Sites (V1–V10)] [V2 ➡️]`.
  * Live push notifications at **50%**, **80%**, and **100%** usage milestones for both tokens and requests.
  * Model inspector (`/models [V1..V10]`) directly queries upstream model availability.
* **Storage Options**:
  * Local file storage (`keys.txt` + write-ahead log `usage.wal`).
  * Optional MongoDB Atlas persistence (`MONGODB_URI`) surviving server restarts and container rebuilds.

---

## 🛣 Path-Based Multi-Site Routing

Each site slot (`V1` through `V10`) maps to its own upstream base URL, friendly title, and key pool:

| Route Path | Upstream Slot | Typical Provider Example |
| :--- | :--- | :--- |
| `/v1/chat/completions` | **V1** | OpenRouter (`https://openrouter.ai/api/v1`) |
| `/v2/chat/completions` | **V2** | OpenAI (`https://api.openai.com/v1`) |
| `/v3/chat/completions` | **V3** | Anthropic (`https://api.anthropic.com/v1`) |
| `/v4/chat/completions` | **V4** | Groq / DeepSeek / Ollama |
| `…` | `…` | Any OpenAI-compatible endpoint |
| `/v10/chat/completions`| **V10** | Custom / Local Upstream |

> [!TIP]
> You can also override the target site on any path by passing the header `X-Site: V2`.

---

## 🚀 Quickstart

### 1. Clone & Configure

```bash
git clone https://github.com/Alaxroy121/ai-proxy.git
cd ai-proxy

# Copy example environment configuration
cp .env.example .env
nano .env
```

### 2. Environment Variables (`.env`)

```ini
# Primary Upstream (Default V1)
UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
API_KEYS=sk-or-v1-key1,sk-or-v1-key2,sk-or-v1-key3

# Optional: Multi-Site Upstream Configurations (V1 through V10)
V1_URL=https://openrouter.ai/api/v1
V1_TITLE=OpenRouter
V2_URL=https://api.openai.com/v1
V2_TITLE=OpenAI
V3_URL=https://api.anthropic.com/v1
V3_TITLE=Claude

# Proxy Security
PROXY_API_KEY=your-secure-proxy-password
PORT=8000

# Budget Limits (0 = unlimited, default)
MAX_TOKENS_PER_KEY=0
MAX_REQS_PER_KEY=0

# Optional Telegram DM Bot
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_ADMIN_IDS=123456789
```

### 3. Start the Server

```bash
./start.sh          # Installs dependencies and starts in background
./start.sh status   # Check server health
./start.sh logs     # View live logs
./start.sh restart  # Restart server
./start.sh stop     # Stop server
```

Or run directly with Python:
```bash
python3 -m ai_proxy
```

---

## 💻 Client Usage Examples

### cURL

```bash
curl http://YOUR-VPS:8000/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Python (`openai` SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://YOUR-VPS:8000/v1",  # Use /v2, /v3... for other upstream sites
    api_key="your-proxy-api-key"
)

response = client.chat.completions.create(
    model="openai/gpt-4o-mini",
    messages=[{"role": "user", "content": "Write a python function to check primes."}]
)
print(response.choices[0].message.content)
```

### Python (`anthropic` SDK)

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://YOUR-VPS:8000/v3",  # Pointing to Anthropic site slot
    api_key="your-proxy-api-key"
)

message = client.messages.create(
    model="claude-3-7-sonnet-20250219",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain quantum computing briefly."}]
)
print(message.content[0].text)
```

---

## 🛠 Coding Agent Integration Guide

Looking to connect your proxy with **Claude Code, Cursor, Cline, Codex, Grok Build, OpenCode, Hermes, OpenClaw, or Pi**?

👉 **See the complete step-by-step setup guide:** [**`apisetup.md`**](file:///home/codespace/ai-proxy/apisetup.md)

---

## 📱 Telegram Control Bot Commands

The proxy features a built-in Telegram DM bot with auto-synced slash menus and inline buttons:

| Command | Description |
| :--- | :--- |
| `/start` | Open main interactive control dashboard |
| `/health` | Check keys ready, upstream status, storage health |
| `/stats [V1..V10]` | View per-key tokens and request metrics by site |
| `/models [V1..V10]` | Fetch live accepted models from upstream provider |
| `/config` | View global budgets, timeouts, retries, and sites |
| `/sites` | List sites V1–V10 with routing URLs and schedules |
| `/siteadd <url>` | Add a new upstream site to next free slot |
| `/sitename <V1..V10> <title>` | Set friendly site label (e.g. `OpenRouter`, `Groq`) |
| `/siteuse <V1..V10> [url]` | Update or inspect upstream URL for slot |
| `/siteswap <V1> <V2>` | Swap URLs, reset schedules, and keys between sites |
| `/sitereset <V1..V10> <hours>` | Schedule auto-reset counter every N hours (0 = off) |
| `/sitelimit <V1..V10> <tokens>`| Set default token budget on specific site |
| `/sitereqlimit <V1..V10> <n>` | Set default request budget on specific site |
| `/sitetimeout <V1..V10> <sec>` | Set HTTP timeout for specific site |
| `/siteheaders <V1..V10> <ref> <title>` | Set custom upstream headers (`-` keeps global) |
| `/siterm <V1..V10>` | Remove a site slot (must have 0 keys) |
| `/add <key> [V1..V10]` | Add a key to a site (auto-creates slot if needed) |
| `/limit <num> <tokens>` | Set individual key token budget (0 = global) |
| `/reqlimit <num> <n>` | Set individual key request budget (0 = unlimited) |
| `/rm <num\|mask>` | Remove a key from pool |
| `/enable <num>` | Re-enable a disabled key |
| `/disable <num>` | Take key offline manually |
| `/reset` | Zero all token and request counters across all sites |
| `/cancel` | Cancel any pending guided budget/removal flow |
| `/menu` | Re-sync Telegram command menu |
| `/help` | Detailed command help guide |

---

## 🛡 Storage & Persistence

* **Local Mode (Default)**:
  * Key list stored in `keys.txt`.
  * Usage metrics journaled to write-ahead log (`usage.wal`).
* **MongoDB Atlas (Recommended for cloud/VPS)**:
  * Set `MONGODB_URI` in `.env`.
  * Keys, site configurations, token metrics, and request counts persist across disk reboots, container redeployments, and host migrations.

---

## 📄 License

MIT License. Open-source and free to deploy anywhere.
