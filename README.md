# AI Key-Rotating Proxy (OpenRouter / OpenAI-compatible)

Strict 1→2→3 rotation + preemptive token budgets. One key serves every
request until its `MAX_TOKENS_PER_KEY` budget is full — then traffic
shifts to the next key. A key that errors is skipped the same way.

## How it works
- You give it N keys: `API_KEYS=key1,key2,key3`
- Clients call **the proxy** instead of the provider directly:
  `POST http://YOUR-VPS:25007/v1/chat/completions`
- All requests → key1 until key1's budget is full → all requests → key2 …
- If a key errors (402/401/429/5xx), the same request retries with the next key
- Token counting: real `usage.total_tokens` when upstream reports it
  (streaming requests auto-add `stream_options.include_usage` so stream
  counts are exact too), otherwise estimated as chars ÷ 4.
  **Cached tokens count on top** (OpenAI details + Anthropic
  cache_read/cache_creation), since platforms bill them.
  Budgets reset on restart or `/resetusage`
- Usage is crash-safe: memory counters are exact, deltas queue in a local
  write-ahead log + flush to the store every few seconds (and on shutdown),
  so a restart loses at most seconds of data — never hours
- Streaming holds its own upstream connection, so long generations never
  get cut mid-response (each request still uses the current key)

## Run on VPS (no Docker)

```bash
cd ai-proxy
./start.sh          # first run creates .env - edit it, then run again
# edit .env, put your keys
./start.sh          # installs deps + starts in background
# Or one command (from this folder, reads PORT from .env):
python -m ai_proxy
./start.sh status   # check health
./start.sh logs     # tail logs
./start.sh restart  # after editing .env
./start.sh stop     # stop server
```

## Use it (same as OpenAI)

```bash
curl http://YOUR-VPS:8000/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

Python (openai lib):
```python
from openai import OpenAI
c = OpenAI(base_url="http://YOUR-VPS:8000/v1", api_key="PROXY_API_KEY")
print(c.chat.completions.create(model="openai/gpt-4o-mini",
  messages=[{"role":"user","content":"hi"}]).choices[0].message.content)
```

Python (Anthropic SDK — keys rotate via `x-api-key`, same budgets apply):
```python
from anthropic import Anthropic
c = Anthropic(base_url="http://YOUR-VPS:8000", api_key="PROXY_API_KEY")
m = c.messages.create(model="your-upstream-model", max_tokens=100,
    messages=[{"role": "user", "content": "hi"}])
print(m.content[0].text)
```

## Ops
- `GET /health` - public
- `GET /keys/status` - which key is live / cooling down
- `POST /keys/reload` - reload keys after editing env

## Storage: local file vs MongoDB

Unstable server disk (files like `keys.txt` / `proxy.log` vanish)? Set
`MONGODB_URI` in `.env` (free Atlas cluster → Connect → copy string).
Keys added via Telegram + per-key token counters then live in MongoDB
(`ai_proxy.proxy_keys`) and survive restarts/wipes. Empty `MONGODB_URI`
= local `keys.txt` + in-memory counters. If Mongo is unreachable at boot,
the proxy warns and falls back to local — it never crashes over storage.

## Telegram DM control (recommended)

No webhook / no extra port. Bot polls Telegram from inside the proxy (same process, shares memory).

Setup (2 min):
1. `@BotFather` -> `/newbot` -> copy token into `.env` as `TELEGRAM_BOT_TOKEN`
2. DM `@userinfobot` -> copy your numeric id into `TELEGRAM_ADMIN_IDS`
3. Restart: `./start.sh restart`
4. DM your bot `/health`

DM commands (also in the `/` menu + buttons under `/start`):
- `/health` - upstream + keys in rotation + tokens used
- `/stats` - overall total bar + per-key usage bars (masked keys only)
- `/config` - budget, timeouts, upstream, store backend
- `/sites` - list upstream websites (V1, V2, …)
- `/siteadd <url>` / `/siteuse <V2>` / `/siterm <V1>` - manage websites live
  (clients can also pin one request via `X-Site: V2` header)
- `/config` - budget, timeouts, upstream
- `/add <full-key>` - add key live + saved to `keys.txt` (then delete your message)
- `/limit <num> <tokens>` - token budget for one key (`0` = global default)
- `/reqlimit <num> <n>` - request-count budget for one key (`0` = unlimited)
- `/rm <num>` - remove key by number from `/stats`
- `/enable <num>` / `/disable <num>` - manual on/off
- `/reset` - zero all token counters
- Budget is `.env`-only: `MAX_TOKENS_PER_KEY` (restart proxy to change)

Auto-alerts: budget/over-budget + **live progress pushes at 50/80/100%
per key** (no need to spam `/stats`). Optional auto-digest:
`LIVE_DIGEST_MIN=10` pushes `/stats` every 10 min while usage changes.
