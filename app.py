"""
AI API rotating proxy - OpenRouter / any OpenAI-compatible upstream.
Fill-then-shift: use one key until its budget is full, then shift to
the next key. Preemptive per-key token budgets: switch BEFORE the
provider errors.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
USAGE_WAL = os.getenv("USAGE_WAL", "usage.wal")  # local write-ahead log for usage deltas
FLUSH_SEC = int(os.getenv("FLUSH_SEC", "5"))  # outbox -> store interval
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
    cached_tokens: int = 0
    req_used: int = 0
    req_limit: int = 0  # 0 = unlimited requests
    token_limit: int = 0  # 0 = use global MAX_TOKENS_PER_KEY
    site: str = "V1"  # which website (upstream) this key belongs to
    disabled: bool = False  # manual off via Telegram

    def __post_init__(self):
        k = self.key
        self.masked = f"{k[:7]}...{k[-4:]}" if len(k) > 12 else "***"

    def eff_token_limit(self) -> int:
        return self.token_limit if self.token_limit > 0 else MAX_TOKENS_PER_KEY

    def over_budget(self) -> bool:
        tlim = self.eff_token_limit()
        if tlim > 0 and self.tokens_used >= tlim:
            return True
        if self.req_limit > 0 and self.req_used >= self.req_limit:
            return True
        return False

    @property
    def available(self) -> bool:
        return not self.disabled and not self.over_budget()


class Outbox:
    """Usage deltas wait here (memory + local WAL) until flushed to the store.

    Memory counters on KeyState are always exact; the store catches up every
    FLUSH_SEC seconds. A crash loses at most one flush window (WAL replays
    the rest on boot). Nothing here ever raises into request handling.
    """

    def __init__(self, wal):
        self.wal = wal
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.ops = 0

    def add(self, key: str, tokens: int = 0, cached: int = 0, success: int = 0,
            fails: int = 0, reqs: int = 0, disabled: Optional[bool] = None,
            log: bool = True):
        if not any((tokens, cached, success, fails, reqs)) and disabled is None:
            return
        entry = self.pending.setdefault(
            key, {"tokens": 0, "cached": 0, "success": 0, "fails": 0, "reqs": 0,
                  "disabled": None, "n": 0})
        entry["tokens"] += tokens
        entry["cached"] += cached
        entry["success"] += success
        entry["fails"] += fails
        entry["reqs"] += reqs
        if disabled is not None:
            entry["disabled"] = disabled
        entry["n"] += 1
        self.ops += 1
        if log:
            self.wal.append({"key": key, "tokens": tokens, "cached": cached,
                             "success": success, "fails": fails, "reqs": reqs})

    def drop(self, key: Optional[str] = None):
        if key is None:
            self.pending = {}
            self.ops = 0
        elif key in self.pending:
            self.ops -= self.pending.pop(key)["n"]

    def depth(self) -> int:
        return self.ops

    async def flush(self, store) -> bool:
        """Push coalesced deltas (one store write per key). True if all ok."""
        if not self.pending:
            return True
        ok = True
        for key, e in list(self.pending.items()):
            try:
                await store.update_key(key, tokens=e["tokens"], cached=e["cached"],
                                       success=e["success"], fails=e["fails"],
                                       reqs=e["reqs"], disabled=e["disabled"])
                self.ops -= e["n"]
                del self.pending[key]
            except Exception as ex:
                print(f"[outbox] flush {key} failed: {ex}")
                ok = False
        if ok:
            self.wal.clear()  # everything durable -> log can go
        return ok


class KeyPool:
    """Fill-then-shift: stick to one key until its budget is full, then shift."""

    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [KeyState(k) for k in keys]
        self._current = 0
        self._lock = asyncio.Lock()
        self.store = None  # attached in lifespan (file or mongo)
        from store import UsageWal
        self.wal = UsageWal(USAGE_WAL)
        self.outbox = Outbox(self.wal)
        self.store_ok = True
        self.last_store_error = ""

    async def reload(self, keys: List[str]):
        async with self._lock:
            # keep stats for existing keys, add new ones
            existing = {k.key: k for k in self.keys}
            self.keys = [existing.get(k, KeyState(k)) for k in keys]
            self._current = 0
        await self._persist_keys()

    @property
    def size(self) -> int:
        return len(self.keys)

    async def _scan_locked(self, site_id: Optional[str] = None) -> Optional[KeyState]:
        """First available key from _current onward, optionally for one site."""
        n = len(self.keys)
        if n == 0:
            return None
        for i in range(n):
            ks = self.keys[(self._current + i) % n]
            if site_id is not None and ks.site != site_id:
                continue
            if ks.available:
                self._current = (self._current + i) % n
                return ks
        return None

    async def next_key(self, site_id: Optional[str] = None) -> Optional[KeyState]:
        """Current key of a site until its budget is full, then shift within the site."""
        async with self._lock:
            return await self._scan_locked(site_id)

    async def advance_past(self, bad: KeyState, site_id: Optional[str] = None) -> Optional[KeyState]:
        """Skip a just-failed key and shift to the next available one (same site)."""
        async with self._lock:
            n = len(self.keys)
            for i, ks in enumerate(self.keys):
                if ks is bad:
                    self._current = (i + 1) % n
                    break
            return await self._scan_locked(site_id)

    async def mark_success(self, ks: KeyState, tokens: int = 0, cached: int = 0):
        async with self._lock:
            ks.success += 1
            ks.req_used += 1
            ks.tokens_used += max(0, tokens)
            ks.cached_tokens += max(0, cached)
        self.outbox.add(ks.key, tokens=max(0, tokens), cached=max(0, cached),
                        success=1, reqs=1)

    async def add_usage(self, ks: KeyState, tokens: int, cached: int = 0):
        async with self._lock:
            ks.tokens_used += max(0, tokens)
            ks.cached_tokens += max(0, cached)
        self.outbox.add(ks.key, tokens=max(0, tokens), cached=max(0, cached))

    async def mark_failed(self, ks: KeyState):
        async with self._lock:
            ks.fails += 1
        self.outbox.add(ks.key, fails=1)

    async def set_token_limit(self, ident: str, limit: int) -> Optional[str]:
        """Per-key token budget (0 = fall back to global). Persists immediately."""
        async with self._lock:
            i = self._find_index(ident)
            if i is None:
                return None
            self.keys[i].token_limit = max(0, limit)
            full, masked = self.keys[i].key, self.keys[i].masked
        if self.store is not None:
            try:
                await self.store.update_key(full, token_limit=max(0, limit))
            except Exception as e:
                print(f"[store] limit save failed: {e}")
        return masked

    async def set_req_limit(self, ident: str, limit: int) -> Optional[str]:
        """Per-key request-count budget (0 = unlimited). Persists immediately."""
        async with self._lock:
            i = self._find_index(ident)
            if i is None:
                return None
            self.keys[i].req_limit = max(0, limit)
            full, masked = self.keys[i].key, self.keys[i].masked
        if self.store is not None:
            try:
                await self.store.update_key(full, req_limit=max(0, limit))
            except Exception as e:
                print(f"[store] limit save failed: {e}")
        return masked

    async def status(self, site_id: Optional[str] = None):
        keys = [ks for ks in self.keys if site_id is None or ks.site == site_id]
        return [
            {
                "key": ks.masked,
                "site": ks.site,
                "available": ks.available,
                "tokens_used": ks.tokens_used,
                "cached_tokens": ks.cached_tokens,
                "limit": ks.eff_token_limit(),
                "custom_limit": ks.token_limit,
                "req_used": ks.req_used,
                "req_limit": ks.req_limit,
                "over_budget": ks.over_budget(),
                "disabled": ks.disabled,
                "success": ks.success,
                "fails": ks.fails,
            }
            for ks in keys
        ]

    def pending_count(self) -> int:
        return self.outbox.depth()

    async def add_key(self, key: str, site: str = "V1") -> bool:
        """Append key to a site. Returns False if already present."""
        key = key.strip()
        async with self._lock:
            if any(k.key == key for k in self.keys):
                return False
            self.keys.append(KeyState(key, site=site))
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
        self.outbox.add(ks.key, disabled=off)
        return masked

    async def reset_usage(self, site_id: Optional[str] = None):
        """Zero counters globally, or for one website only."""
        async with self._lock:
            for ks in self.keys:
                if site_id is not None and ks.site != site_id:
                    continue
                ks.tokens_used = 0
                ks.cached_tokens = 0
                ks.req_used = 0
                ks.fails = 0
        for ks in list(self.keys):
            if site_id is None or ks.site == site_id:
                self.outbox.drop(ks.key)
        if self.store is not None:
            try:
                await self.store.reset_usage(site_id)
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
            docs = []
        if docs:
            async with self._lock:
                existing = {k.key: k for k in self.keys}
                merged = []
                for d in sorted(docs, key=lambda x: x.get("order", 0)):
                    k = d.get("key")
                    if not k:
                        continue
                    ks = existing.get(k, KeyState(k))
                    ks.tokens_used = int(d.get("tokens_used", ks.tokens_used or 0))
                    ks.cached_tokens = int(d.get("cached_tokens", ks.cached_tokens or 0))
                    ks.success = int(d.get("success", ks.success or 0))
                    ks.fails = int(d.get("fails", ks.fails or 0))
                    ks.req_used = int(d.get("req_used", ks.req_used or 0))
                    ks.req_limit = int(d.get("req_limit", ks.req_limit or 0))
                    ks.token_limit = int(d.get("token_limit", ks.token_limit or 0))
                    ks.site = str(d.get("site", ks.site or "V1") or "V1")
                    if "disabled" in d:
                        ks.disabled = bool(d["disabled"])
                    merged.append(ks)
                self.keys = merged
        # Replay any deltas that never reached the store (crash window).
        replayed = self.wal.take_all()
        if replayed:
            by_key: Dict[str, KeyState] = {k.key: k for k in self.keys}
            n = 0
            for e in replayed:
                ks = by_key.get(e.get("key", ""))
                if ks is None:
                    continue
                ks.tokens_used += max(0, int(e.get("tokens", 0)))
                ks.cached_tokens += max(0, int(e.get("cached", 0)))
                ks.success += max(0, int(e.get("success", 0)))
                ks.fails += max(0, int(e.get("fails", 0)))
                ks.req_used += max(0, int(e.get("reqs", 0)))
                self.outbox.add(ks.key, tokens=max(0, int(e.get("tokens", 0))),
                                cached=max(0, int(e.get("cached", 0))),
                                success=max(0, int(e.get("success", 0))),
                                fails=max(0, int(e.get("fails", 0))),
                                reqs=max(0, int(e.get("reqs", 0))), log=False)
                n += 1
            print(f"[store] replayed {n} WAL entries")

    async def _persist_keys(self):
        items = [{"key": k.key, "site": k.site} for k in self.keys]
        if self.store is None:
            try:
                from store import FileStore
                await FileStore(KEYS_FILE).save_keys(items)
            except Exception as e:
                print(f"[store] file save failed: {e}")
            return
        try:
            await self.store.save_keys(items)
        except Exception as e:
            print(f"[store] save to {self.store.label} failed: {e}")


pool = KeyPool(load_keys_list())
if not Path(KEYS_FILE).exists() and pool.size:
    try:
        save_keys_list([k.key for k in pool.keys])  # seed keys.txt from env on first run
    except Exception:
        pass

tg_app = None  # telegram Application, set in lifespan when enabled


class SiteManager:
    """Multiple upstream websites, each V1, V2, ... Telegram-switchable."""

    def __init__(self, default_url: str):
        self.sites = [{"id": "V1", "url": default_url, "reset_hours": 0, "last_reset": 0.0}]
        self.active_id = "V1"
        self.store = None
        self._lock = asyncio.Lock()

    @staticmethod
    def norm(sid: str) -> str:
        s = (sid or "").strip().upper()
        if s.isdigit():
            s = "V" + s
        return s

    async def attach(self, store):
        self.store = store
        try:
            doc = await store.load_meta("sites")
        except Exception as e:
            print(f"[sites] load failed: {e}")
            return
        if not isinstance(doc, dict) or not doc.get("sites"):
            return
        valid = []
        for s in doc["sites"]:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            url = s.get("url", "")
            if not url.startswith("http"):
                continue
            valid.append({"id": s["id"], "url": url.rstrip("/"),
                          "reset_hours": float(s.get("reset_hours") or 0),
                          "last_reset": float(s.get("last_reset") or 0)})
        if not valid:
            return
        async with self._lock:
            self.sites = valid
            if any(s["id"] == doc.get("active") for s in valid):
                self.active_id = doc["active"]

    async def _save(self):
        if self.store is None:
            return
        try:
            await self.store.save_meta("sites", {"sites": self.sites, "active": self.active_id})
        except Exception as e:
            print(f"[sites] save failed: {e}")

    async def list_sites(self):
        async with self._lock:
            return [dict(s, active=(s["id"] == self.active_id)) for s in self.sites]

    async def add_site(self, url: str) -> Optional[str]:
        url = (url or "").strip().rstrip("/")
        if not url.startswith("http"):
            return None
        async with self._lock:
            n = 1
            while any(s["id"] == f"V{n}" for s in self.sites):
                n += 1
            nid = f"V{n}"
            self.sites.append({"id": nid, "url": url, "reset_hours": 0, "last_reset": 0.0})
        await self._save()
        return nid

    async def use_site(self, sid: str) -> bool:
        sid = self.norm(sid)
        async with self._lock:
            if not any(s["id"] == sid for s in self.sites):
                return False
            self.active_id = sid
        await self._save()
        return True

    async def set_reset(self, sid: str, hours: float) -> bool:
        """Auto-reset schedule for one website's counters (0 = off)."""
        sid = self.norm(sid)
        async with self._lock:
            hit = next((s for s in self.sites if s["id"] == sid), None)
            if hit is None:
                return False
            hit["reset_hours"] = max(0.0, hours)
            import time
            hit["last_reset"] = time.time()
        await self._save()
        return True

    async def stamp_reset(self, sid: str):
        import time
        async with self._lock:
            for s in self.sites:
                if s["id"] == sid:
                    s["last_reset"] = time.time()
        await self._save()

    def due_sites(self):
        """Site IDs whose reset interval has elapsed."""
        import time
        now = time.time()
        return [s["id"] for s in self.sites
                if (s.get("reset_hours") or 0) > 0
                and now - (s.get("last_reset") or 0) >= s["reset_hours"] * 3600]

    async def remove_site(self, sid: str) -> bool:
        sid = self.norm(sid)
        async with self._lock:
            if sid == self.active_id or len(self.sites) <= 1:
                return False
            before = len(self.sites)
            self.sites = [s for s in self.sites if s["id"] != sid]
            ok = len(self.sites) < before
        if ok:
            await self._save()
        return ok

    def resolve(self, override: Optional[str] = None) -> Optional[str]:
        return self.pick(override)[1]

    def pick(self, override: Optional[str] = None):
        """(site_id, url) for an override or the active site; url None if unknown."""
        if override:
            sid = self.norm(override)
            for s in self.sites:
                if s["id"] == sid:
                    return sid, s["url"]
            return sid, None
        for s in self.sites:
            if s["id"] == self.active_id:
                return s["id"], s["url"]
        if self.sites:
            return self.sites[0]["id"], self.sites[0]["url"]
        return None, None


sites = SiteManager(UPSTREAM_BASE_URL)


def extract_usage(data) -> tuple:
    """(counted_tokens, cached_tokens) from an upstream response body.

    Cached tokens are counted ON TOP (the platform bills them), taken from
    OpenAI-style `prompt_tokens_details.cached_tokens`, Anthropic-style
    `cache_read_input_tokens` / `cache_creation_input_tokens`, or the
    generic `cached_tokens` / `prompt_cache_hit_tokens` fields.
    """
    if not isinstance(data, dict):
        return 0, 0
    u = data.get("usage") or {}
    if not isinstance(u, dict):
        return 0, 0
    total = u.get("total_tokens")
    if not isinstance(total, int) or total <= 0:
        total = (u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0)
    if total <= 0:
        total = (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0)
    det = u.get("prompt_tokens_details") or {}
    cached = (u.get("cached_tokens", 0) or 0) + (u.get("prompt_cache_hit_tokens", 0) or 0)
    cached += (u.get("cache_read_input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
    if isinstance(det, dict):
        cached += det.get("cached_tokens", 0) or 0
    return int(total), int(cached)


_USAGE_KEYS = ("total_tokens", "input_tokens", "prompt_tokens",
                "output_tokens", "completion_tokens")


def _find_usage(obj):
    """First usage-like dict in a parsed JSON body (OpenAI + Anthropic)."""
    if isinstance(obj, dict):
        u = obj.get("usage")
        if isinstance(u, dict) and any(k in u for k in _USAGE_KEYS):
            return u
        for v in obj.values():
            r = _find_usage(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_usage(v)
            if r is not None:
                return r
    return None


def _usage_from_text(text: bytes):
    """Last envelope object containing usage in a body/SSE stream."""
    try:
        raw = text.decode("utf-8", errors="replace")
    except Exception:
        return None
    best = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if _find_usage(obj) is not None:
            best = obj
    return best


def count_tokens(req_body: bytes, resp_body: bytes, data=None) -> tuple:
    """(tokens, cached): real usage when reported, else chars // 4 estimate."""
    total, cached = extract_usage(data)
    if total > 0:
        return total + cached, cached
    est = (len(req_body) + len(resp_body)) // 4 or 1
    return est, 0


def count_stream_tokens(req_body: bytes, stream_bytes: bytes) -> tuple:
    """Usage chunk at stream end (exact, incl. cached); else estimate."""
    total, cached = extract_usage(_usage_from_text(stream_bytes))
    if total > 0:
        return total + cached, cached
    return (len(req_body) + len(stream_bytes)) // 4 or 1, 0


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
    await sites.attach(store)

    async def _flusher():
        while True:
            await asyncio.sleep(max(1, FLUSH_SEC))
            try:
                ok = await pool.outbox.flush(store)
            except Exception as e:
                ok = False
                pool.last_store_error = str(e)[:200]
                print(f"[outbox] flush crashed: {e}")
            pool.store_ok = ok
            if not ok:
                pool.last_store_error = pool.last_store_error or "flush failed"

    flush_task = asyncio.create_task(_flusher())
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
    flush_task.cancel()
    try:
        await pool.outbox.flush(store)  # final durable flush on clean shutdown
        pool.store_ok = True
    except Exception as e:
        pool.store_ok = False
        print(f"[outbox] shutdown flush failed: {e}")
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


app = FastAPI(title="AI Key Rotating Proxy", version="1.6.0", lifespan=lifespan)
bearer_scheme = HTTPBearer(auto_error=False)


async def check_proxy_auth(request: Request,
                           cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """Proxy credential via OpenAI-style Bearer OR Anthropic-style x-api-key."""
    if not PROXY_API_KEY:
        return  # open proxy
    if cred is not None and cred.credentials == PROXY_API_KEY:
        return
    if request.headers.get("x-api-key") == PROXY_API_KEY:
        return
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
        if kl in ("host", "authorization", "x-api-key", "content-length", "connection"):
            continue
        h[k] = v
    if any(k.lower() == "x-api-key" for k in incoming.headers.keys()):
        # Anthropic SDK style: rotate the key into x-api-key
        h["X-Api-Key"] = api_key
    else:
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
    return {"ok": True, "upstream": sites.resolve(), "site": sites.active_id,
            "keys_total": len(st), "keys_available": avail,
            "tokens_used_total": used, "tokens_capacity_total": cap,
            "store_ok": pool.store_ok, "pending_updates": pool.pending_count()}


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
    # Site selection: X-Site: V2 header, else the Telegram-active site.
    site_ov = (request.headers.get("x-site") or "").strip()
    site_id, base_url = sites.pick(site_ov or None)
    if base_url is None:
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown site '{site_ov}'. Use /sites in Telegram to list V1, V2, ..."})
    # strip /v1 prefix handling: client calls /v1/chat/completions -> upstream same path
    upstream_path = request.url.path  # e.g. /v1/chat/completions
    if request.url.query:
        upstream_path += f"?{request.url.query}"
    upstream_url = f"{base_url}{upstream_path[len('/v1'):]}" if upstream_path.startswith("/v1") else f"{base_url}{upstream_path}"

    params_note = f"{request.method} {upstream_path}"
    last_error: Optional[str] = None
    last_status: int = 502
    tried = 0
    max_tries = min(MAX_RETRIES_PER_REQUEST, pool.size * 2)
    want_stream = _wants_stream(request, body)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
        while tried < max_tries:
            ks = await pool.next_key(site_id)
            if ks is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"No usable keys for site {site_id}. Budgets full, disabled, or none assigned.",
                )
            tried += 1
            headers = build_upstream_headers(request, ks.key)

            if want_stream:
                # Ask upstream for exact token counts in the stream itself
                # (OpenAI-style `include_usage`), so budgets stay accurate.
                body_out = body
                if upstream_path == "/v1/chat/completions":
                    try:
                        payload = json.loads(body) if body else {}
                        if isinstance(payload, dict):
                            so = payload.get("stream_options") or {}
                            if isinstance(so, dict) and "include_usage" not in so:
                                so["include_usage"] = True
                                payload["stream_options"] = so
                                body_out = json.dumps(payload).encode()
                    except Exception:
                        body_out = body
                # Streaming needs its OWN client owned by the generator below.
                # The shared client above closes when this function returns,
                # which used to cut streams mid-response (interruptions).
                sclient = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
                try:
                    sreq = sclient.build_request(request.method, upstream_url, content=body_out, headers=headers)
                    sresp = await sclient.send(sreq, stream=True)
                except httpx.RequestError as e:
                    await sclient.aclose()
                    last_error = f"network error with {ks.masked}: {e}"
                    await pool.mark_failed(ks)
                    await pool.advance_past(ks, site_id)
                    continue  # shift to next key
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
                        await pool.advance_past(ks, site_id)
                        continue  # rate-limit / dead key / blip -> shift to next key
                    return JSONResponse(status_code=sresp.status_code,
                                        content={"error": text, "proxy_note": params_note})
                await pool.mark_success(ks)

                async def gen(_r=sresp, _c=sclient, _k=ks, _b=body_out):
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
                        await pool.add_usage(_k, *count_stream_tokens(_b, bytes(buf)))

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
                await pool.advance_past(ks, site_id)
                continue  # shift to next key

            ctype = resp.headers.get("content-type", "")

            # Success -> record tokens, return JSON
            if resp.status_code < 400:
                data = resp.json() if resp.content else {}
                await pool.mark_success(ks, *count_tokens(body, resp.content, data))
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
                await pool.advance_past(ks, site_id)
                continue  # rate-limit / dead key / blip -> shift to next key

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
