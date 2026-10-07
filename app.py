"""
AI API rotating proxy - OpenRouter / any OpenAI-compatible upstream.
Strict 1->2->3 rotation (new key every request) + preemptive per-key
token budgets: switch BEFORE the provider errors.
"""
import asyncio
import json
import os
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
# Preemptive budget: switch to next key BEFORE the provider errors.
# 0 = unlimited. Counts real `usage.total_tokens` when upstream reports it,
# else estimates (chars // 4). Resets on restart, or via Telegram /resetusage.
MAX_TOKENS_PER_KEY = int(os.getenv("MAX_TOKENS_PER_KEY", "0"))
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
    tokens_used: int = 0
    disabled: bool = False  # manual off via Telegram

    def __post_init__(self):
        k = self.key
        self.masked = f"{k[:7]}...{k[-4:]}" if len(k) > 12 else "***"

    def over_budget(self) -> bool:
        return MAX_TOKENS_PER_KEY > 0 and self.tokens_used >= MAX_TOKENS_PER_KEY

    @property
    def available(self) -> bool:
        return not self.disabled and not self.over_budget()


class KeyPool:
    """Strict 1->2->3 rotation: new key every request, failover to next on error."""

    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [KeyState(k) for k in keys]
        self._pos = 0
        self._lock = asyncio.Lock()
        self.store = None  # attached in lifespan (file or mongo)

    async def reload(self, keys: List[str]):
        async with self._lock:
            # keep stats for existing keys, add new ones
            existing = {k.key: k for k in self.keys}
            self.keys = [existing.get(k, KeyState(k)) for k in keys]
            self._pos = 0
        await self._persist_keys()

    @property
    def size(self) -> int:
        return len(self.keys)

    async def next_key(self) -> Optional[KeyState]:
        """Next key in strict rotation, skipping disabled / over-budget keys."""
        async with self._lock:
            n = len(self.keys)
            if n == 0:
                return None
            for _ in range(n):
                ks = self.keys[self._pos % n]
                self._pos += 1
                if ks.available:
                    return ks
            return None

    async def mark_success(self, ks: KeyState, tokens: int = 0):
        async with self._lock:
            ks.success += 1
            ks.tokens_used += max(0, tokens)
        await self._persist_one(ks, tokens=max(0, tokens), success=1)

    async def add_usage(self, ks: KeyState, tokens: int):
        async with self._lock:
            ks.tokens_used += max(0, tokens)
        await self._persist_one(ks, tokens=max(0, tokens))

    async def mark_failed(self, ks: KeyState):
        async with self._lock:
            ks.fails += 1
        await self._persist_one(ks, fails=1)

    async def status(self):
        return [
            {
                "key": ks.masked,
                "available": ks.available,
                "tokens_used": ks.tokens_used,
                "limit": MAX_TOKENS_PER_KEY,
                "over_budget": ks.over_budget(),
                "disabled": ks.disabled,
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
        await self._persist_keys()
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
        await self._persist_keys()
        return masked

    async def set_enabled(self, ident: str, enabled: bool) -> Optional[str]:
        async with self._lock:
            i = self._find_index(ident)
            if i is None:
                return None
            ks = self.keys[i]
            ks.disabled = not enabled
            masked, off = ks.masked, ks.disabled
        await self._persist_one(ks, disabled=off)
        return masked

    async def reset_usage(self):
        """Zero token counters + fail counts (budgets restart)."""
        async with self._lock:
            for ks in self.keys:
                ks.tokens_used = 0
                ks.fails = 0
        if self.store is not None:
            try:
                await self.store.reset_usage()
            except Exception as e:
                print(f"[store] reset failed: {e}")

    async def reset_all(self):
        await self.reset_usage()

    async def use_store(self, store):
        """Attach persistence and adopt its keys/counters (mongo wins if non-empty)."""
        self.store = store
        try:
            docs = await store.load()
        except Exception as e:
            print(f"[store] load from {store.label} failed: {e}; keeping local keys")
            return
        if not docs:
            return
        async with self._lock:
            existing = {k.key: k for k in self.keys}
            merged = []
            for d in sorted(docs, key=lambda x: x.get("order", 0)):
                k = d.get("key")
                if not k:
                    continue
                ks = existing.get(k, KeyState(k))
                ks.tokens_used = int(d.get("tokens_used", ks.tokens_used or 0))
                ks.success = int(d.get("success", ks.success or 0))
                ks.fails = int(d.get("fails", ks.fails or 0))
                if "disabled" in d:
                    ks.disabled = bool(d["disabled"])
                merged.append(ks)
            self.keys = merged

    async def _persist_keys(self):
        if self.store is None:
            try:
                save_keys_list([k.key for k in self.keys])
            except Exception as e:
                print(f"[store] file save failed: {e}")
            return
        try:
            await self.store.save_keys([k.key for k in self.keys])
        except Exception as e:
            print(f"[store] save to {self.store.label} failed: {e}")

    async def _persist_one(self, ks: KeyState, tokens: int = 0, success: int = 0,
                           fails: int = 0, disabled: Optional[bool] = None):
        if self.store is None:
            return
        try:
            await self.store.update_key(ks.key, tokens=tokens, success=success,
                                        fails=fails, disabled=disabled)
        except Exception as e:
            print(f"[store] update {self.store.label} failed: {e}")


pool = KeyPool(load_keys_list())
if not Path(KEYS_FILE).exists() and pool.size:
    try:
        save_keys_list([k.key for k in pool.keys])  # seed keys.txt from env on first run
    except Exception:
        pass

tg_app = None  # telegram Application, set in lifespan when enabled


def count_tokens(req_body: bytes, resp_body: bytes, data=None) -> int:
    """Real usage.total_tokens when upstream reports it, else chars // 4 estimate."""
    if isinstance(data, dict):
        u = data.get("usage") or {}
        if isinstance(u, dict):
            total = u.get("total_tokens")
            if isinstance(total, int) and total > 0:
                return total
            p = u.get("prompt_tokens", 0) or 0
            c = u.get("completion_tokens", 0) or 0
            if p or c:
                return int(p) + int(c)
    return (len(req_body) + len(resp_body)) // 4 or 1


def count_stream_tokens(req_body: bytes, stream_bytes: bytes) -> int:
    """Usage chunk sometimes carries total_tokens at stream end; else estimate."""
    try:
        import re
        m = re.search(rb'"total_tokens"\s*:\s*(\d+)', stream_bytes)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return (len(req_body) + len(stream_bytes)) // 4 or 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    # Attach persistence first: mongo (survives unstable disk) or local file
    from store import FileStore, build_store
    store = build_store(KEYS_FILE)
    try:
        await store.connect()
        print(f"[store] using {store.label}")
    except Exception as e:
        print(f"[store] {e}; falling back to local file")
        store = FileStore(KEYS_FILE)
    app.state.store = store
    await pool.use_store(store)
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
    try:
        await app.state.store.close()
    except Exception:
        pass


app = FastAPI(title="AI Key Rotating Proxy", version="1.3.0", lifespan=lifespan)
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


def _retryable(status_code: int, body_text: str) -> bool:
    return status_code in (429, 500, 502, 503, 504) or is_exhausted(status_code, body_text)


def _wants_stream(request: Request, body: bytes) -> bool:
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("stream") is True:
                return True
        except Exception:
            pass
    return "text/event-stream" in request.headers.get("accept", "").lower()


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
    used = sum(s["tokens_used"] for s in st)
    cap = MAX_TOKENS_PER_KEY * len(st) if MAX_TOKENS_PER_KEY else None
    return {"ok": True, "upstream": UPSTREAM_BASE_URL, "keys_total": len(st),
            "keys_available": avail, "tokens_used_total": used,
            "tokens_capacity_total": cap}


@app.get("/keys/status")
async def keys_status(_=Depends(check_proxy_auth)):
    st = await pool.status()
    used = sum(s["tokens_used"] for s in st)
    cap = MAX_TOKENS_PER_KEY * len(st) if MAX_TOKENS_PER_KEY else None
    return {"overall": {"tokens_used": used, "capacity": cap}, "keys": st}


@app.post("/keys/reload")
async def keys_reload(_=Depends(check_proxy_auth)):
    # re-read from env (useful if you update keys via env restart script)
    load_dotenv(override=True)
    await pool.reload(parse_keys(os.getenv("API_KEYS", "")))
    return {"reloaded": pool.size}


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "AI rotating proxy running. Use /v1/chat/completions with your PROXY_API_KEY."


async def proxy_request(request: Request):
    """Strict 1->2->3 rotation + preemptive token budgets. Supports SSE streaming."""
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
    want_stream = _wants_stream(request, body)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
        while tried < max_tries:
            ks = await pool.next_key()
            if ks is None:
                raise HTTPException(
                    status_code=503,
                    detail="All API keys over token budget or disabled. /resetusage or add keys.",
                )
            tried += 1
            headers = build_upstream_headers(request, ks.key)

            if want_stream:
                # Streaming needs its OWN client owned by the generator below.
                # The shared client above closes when this function returns,
                # which used to cut streams mid-response (interruptions).
                sclient = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
                try:
                    sreq = sclient.build_request(request.method, upstream_url, content=body, headers=headers)
                    sresp = await sclient.send(sreq, stream=True)
                except httpx.RequestError as e:
                    await sclient.aclose()
                    last_error = f"network error with {ks.masked}: {e}"
                    await pool.mark_failed(ks)
                    continue  # next key in sequence
                if sresp.status_code >= 400:
                    raw = await sresp.aread()
                    await sresp.aclose()
                    await sclient.aclose()
                    try:
                        text = raw[:2000].decode("utf-8", errors="replace")
                    except Exception:
                        text = ""
                    last_error = f"{ks.masked} -> {sresp.status_code}: {text[:300]}"
                    last_status = sresp.status_code
                    await pool.mark_failed(ks)
                    if _retryable(sresp.status_code, text):
                        continue  # rate-limit / dead key / blip -> next key
                    return JSONResponse(status_code=sresp.status_code,
                                        content={"error": text, "proxy_note": params_note})
                await pool.mark_success(ks)

                async def gen(_r=sresp, _c=sclient, _k=ks):
                    buf = bytearray()
                    try:
                        async for chunk in _r.aiter_bytes():
                            buf.extend(chunk)
                            yield chunk
                    finally:
                        try:
                            await _r.aclose()
                        finally:
                            await _c.aclose()
                        await pool.add_usage(_k, count_stream_tokens(body, bytes(buf)))

                return StreamingResponse(gen(), status_code=sresp.status_code,
                                         media_type="text/event-stream")

            try:
                resp = await client.request(
                    request.method,
                    upstream_url,
                    content=body,
                    headers=headers,
                )
            except httpx.RequestError as e:
                last_error = f"network error with {ks.masked}: {e}"
                await pool.mark_failed(ks)
                continue  # next key in sequence

            ctype = resp.headers.get("content-type", "")

            # Success -> record tokens, return JSON
            if resp.status_code < 400:
                data = resp.json() if resp.content else {}
                await pool.mark_success(ks, count_tokens(body, resp.content, data))
                return JSONResponse(status_code=resp.status_code, content=data)

            # Failure -> next key in sequence (no cooldowns)
            try:
                text = resp.text[:2000]
            except Exception:
                text = ""
            last_error = f"{ks.masked} -> {resp.status_code}: {text[:300]}"
            last_status = resp.status_code
            await pool.mark_failed(ks)

            if _retryable(resp.status_code, text):
                continue  # rate-limit / dead key / blip -> next key

            # Real client error (400, 404, 422...) -> don't rotate further, return as-is
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
