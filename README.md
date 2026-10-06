# AI Key-Rotating Proxy (OpenRouter / OpenAI-compatible)

Same model + same provider. Auto-switches API key when one key's credits are used up.

## How it works
- You give it N keys: `API_KEYS=key1,key2,key3`
- Clients call **the proxy** instead of OpenRouter directly:
  `POST http://YOUR-VPS:8000/v1/chat/completions`
- Proxy injects `Authorization: Bearer <one-of-your-keys>`, forwards to `https://openrouter.ai/api/v1/...`
- If upstream returns:
  - `402` / `insufficient credits/quota/balance` / `401 invalid key` -> key marked **exhausted** (cooldown 1h default), auto-retries with **next key**
  - `429` rate-limit -> key cooldown 60s, retry next key
  - `5xx` -> try next key
  - `2xx` -> return to client
- Round-robin across healthy keys. Check `/keys/status`.

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
- `/health` - upstream + available/total
- `/stats` - per-key table (masked keys only, never full)
- `/config` - cooldowns, timeouts, upstream
- `/add <full-key>` - add key live + saved to `keys.txt` (then delete your message)
- `/rm <num>` - remove key by number from `/stats`
- `/enable <num>` / `/disable <num>` - manual on/off
- `/reset` - clear all cooldowns
- `/setcool 60 3600` - change rate-limit / exhausted cooldowns

Auto-alerts: bot DMs you when a key dies, recovers, or ALL keys are dead.
