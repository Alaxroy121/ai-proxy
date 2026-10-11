# AI Key-Rotating Proxy (OpenRouter / OpenAI-compatible)

Strict 1→2→3 rotation + preemptive token budgets. All websites serve
at once: requests rotate V1, V2, … — each site spending only its own
keys (one key to budget, then shift within that site).

## How it works
- You give it N keys: `API_KEYS=key1,key2,key3`
- Clients call **the proxy** instead of the provider directly:
  `POST http://YOUR-VPS:25007/v1/chat/completions`
- All requests rotate across sites → key1 of V1, key1 of V2, …; a site
  whose keys are all over budget is skipped until `/reset`
- Pin one site per request: `X-Site: V2` header, or path prefix
  (`/v2/chat/completions` → V2; `/v1/…` stays all-sites rotation)
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
- Token displays are compact: `13.8m`, `500k`, `950` (API keeps raw ints)
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

## Troubleshooting
- Buttons spin with no response / two processes answer: only ONE proxy may
  run per bot token. Kill duplicates, then start fresh:
  ```bash
  ./start.sh stop; pkill -f "uvicorn app:app"; sleep 2
  ps aux | grep uvicorn | grep -v grep   # must show nothing
  ./start.sh start
  ```
- Every button tap is logged (`[bot] button ...`) — check `proxy.log`.
- `/` menu not showing: the bot registers it itself on every boot
  (watch `proxy.log` for `menu registered: 23 commands` + an `online`
  DM). Shortcut: send `/menu` (or tap 📋 Menu) — it re-syncs live and
  tells you the result. If synced but invisible, fully close + reopen
  the Telegram chat (client caches the menu).

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
- `/start` - control panel: live status + buttons for everything below
- `/health` - sites + usable keys + tokens used
- `/stats` - overall total bar + per-key usage bars (masked keys only)
- `/config` - budget, timeouts, upstream, store backend
- `/sites` - list upstream websites (V1, V2, …)
- `/siteadd <url>` - register a website (all sites serve at once;
  clients can still pin one request via `X-Site: V2` header)
- `/siteuse <V1> <url>` - fix a website's URL (one arg just shows it)
- `/siterm <V1>` - remove a website (not the last one; move its keys first)
- `/siteswap <V1> <V2>` - exchange the two sites' URLs, reset schedules
  and keys
- `/sitereset <V1> <hours>` - auto-zero that website's counters every N
  hours (`0` = off). Each provider resets billing on its own cycle —
  match it (e.g. `/sitereset V1 24`). Shown in `/sites` with countdown.
- `/sitelimit <V1> <tokens>` - default token budget for that site's keys
  (`0` = global `MAX_TOKENS_PER_KEY`). Key-level `/limit` still wins.
- `/sitetimeout <V1> <sec>` - request timeout for that site (`0` = global).
- `/siteheaders <V1> <referer> <title…>` - upstream headers for that site
  (`-` keeps the global). Shown as `[H]` in `/sites`, full in `/config`.
- Keys belong to one website: `/add <key> [V2]` (default: first site).
  V1 traffic only spends V1 keys. `/stats [V2]` filters by site.
  A site can't be removed while keys point at it — `/rm` them first.
- `/config` - budget, timeouts, upstream
- `/add <full-key>` - add key live + saved to `keys.txt` (then delete your message)
- `/limit <num> <tokens>` / `/reqlimit <num> <n>` / `/rm <num>` work fully
  typed, or with no args open a guided flow: pick V1/V2 → pick key →
  type the number (or confirm delete). `/cancel` aborts.
- `/enable <num>` / `/disable <num>` - manual on/off
- `/reset` - zero all token counters
- Buttons: 📊 Stats first asks V1/V2/All, then per-site numbers with
  ✅/⛔ toggles; one-tap `Use V2` switching in `/sites`, two-tap
  confirm on Reset
- Budget is `.env`-only: `MAX_TOKENS_PER_KEY` (restart proxy to change)

Auto-alerts: budget/over-budget + **live progress pushes at 50/80/100%
per key** (no need to spam `/stats`). Optional auto-digest:
`LIVE_DIGEST_MIN=10` pushes `/stats` every 10 min while usage changes.
