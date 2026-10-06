# AI Key-Rotating Proxy (OpenRouter / OpenAI-compatible)

Strict 1→2→3 rotation + preemptive token budgets. A new key is used for
every request; a key that reaches `MAX_TOKENS_PER_KEY` is skipped BEFORE
the provider can rate-limit or error.

## How it works
- You give it N keys: `API_KEYS=key1,key2,key3`
- Clients call **the proxy** instead of the provider directly:
  `POST http://YOUR-VPS:25007/v1/chat/completions`
- Request 1 → key1, request 2 → key2, request 3 → key3, request 4 → key1…
- If a key errors (402/401/429/5xx), the same request retries with the next key
- Token counting: real `usage.total_tokens` when upstream reports it,
  otherwise estimated as chars ÷ 4. Budgets reset on restart or `/resetusage`

## Run on VPS (no Docker)

```bash
cd ai-proxy
./start.sh          # first run creates .env - edit it, then run again
# edit .env, put your keys
./start.sh          # installs deps + starts in background
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

## Ops
- `GET /health` - public
- `GET /keys/status` - which key is live / cooling down
- `POST /keys/reload` - reload keys after editing env

## Telegram DM control (recommended)

No webhook / no extra port. Bot polls Telegram from inside the proxy (same process, shares memory).

Setup (2 min):
1. `@BotFather` -> `/newbot` -> copy token into `.env` as `TELEGRAM_BOT_TOKEN`
2. DM `@userinfobot` -> copy your numeric id into `TELEGRAM_ADMIN_IDS`
3. Restart: `./start.sh restart`
4. DM your bot `/health`

DM commands:
- `/health` - upstream + keys in rotation + tokens used
- `/stats` - per-key usage `used/limit` (masked keys only, never full)
- `/config` - budget, timeouts, upstream
- `/add <full-key>` - add key live + saved to `keys.txt` (then delete your message)
- `/rm <num>` - remove key by number from `/stats`
- `/enable <num>` / `/disable <num>` - manual on/off
- `/reset` - zero all token counters
- `/setlimit 50000` - change tokens-per-key budget (`0` = unlimited)

Auto-alerts: bot DMs you when a key hits its budget or ALL keys are over budget.
