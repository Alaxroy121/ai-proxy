"""
AI API rotating proxy - OpenRouter / any OpenAI-compatible upstream.
Same model + same provider, auto-switch API key when credits/rate-limit hit.
"""
import asyncio
import os
import time
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEYS_RAW = os.getenv("API_KEYS", "")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "").strip()
HTTP_REFERER = os.getenv("HTTP_REFERER", "")
X_TITLE = os.getenv("X_TITLE", "Key Rotating Proxy")
COOLDOWN_RATE_LIMIT_SEC = int(os.getenv("COOLDOWN_RATE_LIMIT_SEC", "60"))
COOLDOWN_EXHAUSTED_SEC = int(os.getenv("COOLDOWN_EXHAUSTED_SEC", "3600"))
MAX_RETRIES_PER_REQUEST = int(os.getenv("MAX_RETRIES_PER_REQUEST", "10"))
REQUEST_TIMEOUT_SEC = int(os.getenv("REQUEST_TIMEOUT_SEC", "120"))
KEYS_FILE = os.getenv("KEYS_FILE", "keys.txt")  # one full key per line, persists Telegram-added keys
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = [s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()]

# Status codes / messages that mean "this key is dead, try next key"
EXHAUSTED_MARKERS = [
    "insufficient",
    "credit",
    "quota",
    "balance",
    "payment",
    "402",
    "free tier",
    "limit: 0",
]


def parse_keys(raw: str) -> List[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


def load_keys_list() -> List[str]:
    """keys.txt (Telegram-managed) takes precedence, else API_KEYS env."""
    p = Path(KEYS_FILE)
    if p.exists():
        keys = [l.strip() for l in p.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
        if keys:
            return keys
    return parse_keys(os.getenv("API_KEYS", ""))


def save_keys_list(keys: List[str]):
    Path(KEYS_FILE).write_text("\n".join(keys) + ("\n" if keys else ""))


@dataclass
class KeyState:
    key: str
    masked: str = field(init=False)
    success: int = 0
    fails: int = 0
    disabled_until: float = 0.0
    disabled_reason: str = ""

    def __post_init__(self):
        k = self.key
        self.masked = f"{k[:7]}...{k[-4:]}" if len(k) > 12 else "***"

    @property
    def available(self) -> bool:
        return time.time() >= self.disabled_until

    def cooldown_left(self) -> int:
        return max(0, int(self.disabled_until - time.time()))


class KeyPool:
    """Thread-safe round-robin pool with cooldown / exhaustion tracking."""

    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [KeyState(k) for k in keys]
        self._counter = itertools.count()
        self._lock = asyncio.Lock()

    def reload(self, keys: List[str]):
        # keep stats for existing keys, add new ones
        existing = {k.key: k for k in self.keys}
        self.keys = [existing.get(k, KeyState(k)) for k in keys]

    @property
    def size(self) -> int:
        return len(self.keys)

    async def acquire(self) -> Optional[KeyState]:
        """Return next available key (round-robin). None if all cooling down."""
        async with self._lock:
            n = len(self.keys)
            if n == 0:
                return None
            start = next(self._counter) % n
            for i in range(n):
                ks = self.keys[(start + i) % n]
                if ks.available:
                    return ks
            return None

    async def mark_success(self, ks: KeyState):
        async with self._lock:
            ks.success += 1
            ks.fails = 0

    async def mark_rate_limited(self, ks: KeyState):
        async with self._lock:
            ks.fails += 1
            ks.disabled_until = time.time() + COOLDOWN_RATE_LIMIT_SEC
            ks.disabled_reason = f"429 rate-limit (cooldown {COOLDOWN_RATE_LIMIT_SEC}s)"

    async def mark_exhausted(self, ks: KeyState, reason: str):
        async with self._lock:
            ks.fails += 1
            ks.disabled_until = time.time() + COOLDOWN_EXHAUSTED_SEC
            ks.disabled_reason = f"exhausted: {reason[:120]}"

    async def status(self):
        return [
            {
                "key": ks.masked,
                "available": ks.available,
                "cooldown_left_sec": ks.cooldown_left(),
                "reason": ks.disabled_reason,
                "success": ks.success,
                "fails": ks.fails,
            }
            for ks in self.keys
        ]

    async def add_key(self, key: str) -> bool:
        """Append key. Returns False if already present."""
        key = key.strip()
        async with self._lock:
            if any(k.key == key for k in self.keys):
                return False
            self.keys.append(KeyState(key))
        save_keys_list([k.key for k in self.keys])
        return True

    def _find_index(self, ident: str) -> Optional[int]:
        ident = ident.strip()
        # 1-based index?
        if ident.isdigit():
            i = int(ident) - 1
            if 0 <= i < len(self.keys):
                return i
        # masked/full/suffix match
        for i, ks in enumerate(self.keys):
            if ident == ks.key or ident == ks.masked or ident in ks.key or ident in ks.masked:
                return i
        return None

    async def remove_key(self, ident: str) -> Optional[str]:
        async with self._lock:
            i = self._find_index(ident)
            if i is None:
                return None
            masked = self.keys[i].masked
            del self.keys[i]
        save_keys_list([k.key for k in self.keys])
        return masked

    async def set_enabled(self, ident: str, enabled: bool) -> Optional[str]:
        async with self._lock:
            i = self._find_index(ident)
            if i is None:
                return None
            ks = self.keys[i]
            if enabled:
                ks.disabled_until = 0
                ks.disabled_reason = ""
            else:
                ks.disabled_until = time.time() + 24 * 3600
                ks.disabled_reason = "manually disabled via Telegram"
            return ks.masked

    async def reset_all(self):
        async with self._lock:
            for ks in self.keys:
                ks.disabled_until = 0
                ks.disabled_reason = ""


pool = KeyPool(load_keys_list())
if not Path(KEYS_FILE).exists() and pool.size:
    try:
        save_keys_list([k.key for k in pool.keys])  # seed keys.txt from env on first run
    except Exception:
        pass

tg_app = None  # telegram Application, set in lifespan when enabled


def set_cooldowns(rate_sec: Optional[int] = None, exhausted_sec: Optional[int] = None):
    global COOLDOWN_RATE_LIMIT_SEC, COOLDOWN_EXHAUSTED_SEC
    if rate_sec is not None:
        COOLDOWN_RATE_LIMIT_SEC = max(1, rate_sec)
    if exhausted_sec is not None:
        COOLDOWN_EXHAUSTED_SEC = max(60, exhausted_sec)
    return COOLDOWN_RATE_LIMIT_SEC, COOLDOWN_EXHAUSTED_SEC


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    # Start Telegram control bot (polling, same process so it shares KeyPool memory)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS:
        try:
            from bot import build_bot_app
            tg_app = build_bot_app(pool)
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"[telegram] failed to start: {e}")
            tg_app = None
    else:
        print("[telegram] disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_IDS to enable)")
    yield
    if tg_app is not None:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass


app = FastAPI(title="AI Key Rotating Proxy", version="1.1.0", lifespan=lifespan)
bearer_scheme = HTTPBearer(auto_error=False)


def check_proxy_auth(cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not PROXY_API_KEY:
        return  # open proxy
    if cred is None or cred.credentials != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid proxy API key")


def is_exhausted(status_code: int, body_text: str) -> bool:
    if status_code == 402:
        return True
    if status_code in (401, 403):
        return True  # invalid key / no credits -> rotate anyway
    low = body_text.lower()
    return any(m in low for m in EXHAUSTED_MARKERS)


def build_upstream_headers(incoming: Request, api_key: str) -> dict:
    h = {}
    # forward safe headers
    for k, v in incoming.headers.items():
        kl = k.lower()
        if kl in ("host", "authorization", "content-length", "connection"):
            continue
        h[k] = v
    h["Authorization"] = f"Bearer {api_key}"
    if HTTP_REFERER:
        h["HTTP-Referer"] = HTTP_REFERER
    if X_TITLE:
        h["X-Title"] = X_TITLE
    return h


@app.get("/health")
async def health():
    st = await pool.status()
    avail = sum(1 for s in st if s["available"])
    return {"ok": True, "upstream": UPSTREAM_BASE_URL, "keys_total": len(st), "keys_available": avail}


@app.get("/keys/status")
async def keys_status(_=Depends(check_proxy_auth)):
    return {"keys": await pool.status()}


@app.post("/keys/reload")
async def keys_reload(_=Depends(check_proxy_auth)):
    # re-read from env (useful if you update keys via env restart script)
    load_dotenv(override=True)
    pool.reload(parse_keys(os.getenv("API_KEYS", "")))
    return {"reloaded": pool.size}


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "AI rotating proxy running. Use /v1/chat/completions with your PROXY_API_KEY."


async def proxy_request(request: Request):
    """Forward to upstream, rotating keys on 402/429/401/5xx. Supports SSE streaming."""
    if pool.size == 0:
        raise HTTPException(status_code=500, detail="No API_KEYS configured on proxy")

    body = await request.body()
    # strip /v1 prefix handling: client calls /v1/chat/completions -> upstream same path
    upstream_path = request.url.path  # e.g. /v1/chat/completions
    if request.url.query:
        upstream_path += f"?{request.url.query}"
    upstream_url = f"{UPSTREAM_BASE_URL}{upstream_path[len('/v1'):]}" if upstream_path.startswith("/v1") else f"{UPSTREAM_BASE_URL}{upstream_path}"

    params_note = f"{request.method} {upstream_path}"
    last_error: Optional[str] = None
    last_status: int = 502
    tried = 0
    max_tries = min(MAX_RETRIES_PER_REQUEST, pool.size * 2)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
        while tried < max_tries:
            ks = await pool.acquire()
            if ks is None:
                raise HTTPException(
                    status_code=503,
                    detail="All API keys are cooling down / exhausted. Try later or add keys.",
                )
            tried += 1
            try:
                resp = await client.request(
                    request.method,
                    upstream_url,
                    content=body,
                    headers=build_upstream_headers(request, ks.key),
                )
            except httpx.RequestError as e:
                last_error = f"network error with {ks.masked}: {e}"
                await pool.mark_rate_limited(ks)  # short cooldown, try next
                continue

            ctype = resp.headers.get("content-type", "")

            # Success -> return (stream if SSE)
            if resp.status_code < 400:
                await pool.mark_success(ks)
                if "text/event-stream" in ctype:
                    async def gen():
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    return StreamingResponse(gen(), status_code=resp.status_code, media_type=ctype)
                return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.content else {})

            # Failure -> decide rotate or return
            try:
                text = resp.text[:2000]
            except Exception:
                text = ""
            last_error = f"{ks.masked} -> {resp.status_code}: {text[:300]}"
            last_status = resp.status_code

            if resp.status_code == 429:
                await pool.mark_rate_limited(ks)
                continue  # always rotate on rate limit
            if is_exhausted(resp.status_code, text):
                await pool.mark_exhausted(ks, f"{resp.status_code} {text[:150]}")
                continue  # rotate to next key
            if resp.status_code >= 500:
                await pool.mark_rate_limited(ks)
                continue  # upstream blip, try next key

            # Real client error (400, 404, 422...) -> don't rotate, return as-is
            return JSONResponse(status_code=resp.status_code, content={"error": text, "proxy_note": params_note})

    raise HTTPException(status_code=last_status if last_status != 502 else 503,
                        detail=f"All keys failed after {tried} tries. Last: {last_error}")


# OpenAI-compatible routes (all go through same rotation logic)
for _path in [
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/images/generations",
    "/v1/audio/transcriptions",
    "/v1/audio/speech",
]:
    app.add_api_route(_path, proxy_request, methods=["POST"], dependencies=[Depends(check_proxy_auth)])

app.add_api_route("/v1/models", proxy_request, methods=["GET"], dependencies=[Depends(check_proxy_auth)])
app.add_api_route("/v1/{full_path:path}", proxy_request, methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                  dependencies=[Depends(check_proxy_auth)])
