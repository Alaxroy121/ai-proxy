"""
AI API rotating proxy - OpenRouter / any OpenAI-compatible upstream.
Fill-then-shift: use one key until its budget is full, then shift to
the next key. Preemptive per-key token budgets: switch BEFORE the
provider errors.
"""
import asyncio
import json
import os
import re
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
KEYS_FILE = os.getenv("KEYS_FILE", "keys.txt")
USAGE_WAL = os.getenv("USAGE_WAL", "usage.wal")
FLUSH_SEC = int(os.getenv("FLUSH_SEC", "5"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = [s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()]

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
    req_limit: int = 0
    token_limit: int = 0
    site: str = "V1"
    disabled: bool = False

    def __post_init__(self):
        k = self.key
        self.masked = f"{k[:7]}...{k[-4:]}" if len(k) > 12 else "***"

    def eff_token_limit(self) -> int:
        if self.token_limit > 0:
            return self.token_limit
        sb = site_token_budget(self.site)
        return sb if sb > 0 else MAX_TOKENS_PER_KEY

    def eff_req_limit(self) -> int:
        if self.req_limit > 0:
            return self.req_limit
        sb = site_req_budget(self.site)
        return sb if sb > 0 else 0

    def over_budget(self) -> bool:
        tlim = self.eff_token_limit()
        if tlim > 0 and self.tokens_used >= tlim:
            return True
        rlim = self.eff_req_limit()
        if rlim > 0 and self.req_used >= rlim:
            return True
        return False

    @property
    def available(self) -> bool:
        return not self.disabled and not self.over_budget()


class Outbox:
    """Usage deltas wait here (memory + local WAL) until flushed to the store."""

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
            self.wal.clear()
        return ok


class KeyPool:
    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [KeyState(k) for k in keys]
        self._current = 0
        self._lock = asyncio.Lock()
        self.store = None
        from store import UsageWal
        self.wal = UsageWal(USAGE_WAL)
        self.outbox = Outbox(self.wal)
        self.store_ok = True
        self.last_store_error = ""

    async def reload(self, keys: List[str]):
        async with self._lock:
            existing = {k.key: k for k in self.keys}
            self.keys = [existing.get(k, KeyState(k)) for k in keys]
            self._current = 0
        await self._persist_keys()

    @property
    def size(self) -> int:
        return len(self.keys)

    async def _scan_locked(self, site_id: Optional[str] = None) -> Optional[KeyState]:
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
        async with self._lock:
            return await self._scan_locked(site_id)

    async def advance_past(self, bad: KeyState, site_id: Optional[str] = None) -> Optional[KeyState]:
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
        self.outbox.add(ks.key, tokens=max(0, tokens), cached=max(0, cached), success=1, reqs=1)

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
                "eff_req_limit": ks.eff_req_limit(),
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
        key = key.strip()
        async with self._lock:
            if any(k.key == key for k in self.keys):
                return False
            self.keys.append(KeyState(key, site=site))
        await self._persist_keys()
        return True

    def _find_index(self, ident: str) -> Optional[int]:
        ident = ident.strip()
        if ident.isdigit():
            i = int(ident) - 1
            if 0 <= i < len(self.keys):
                return i
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

    async def swap_key_sites(self, sa: str, sb: str) -> tuple:
        moved_ab = moved_ba = 0
        async with self._lock:
            for ks in self.keys:
                if ks.site == sa:
                    ks.site = sb
                    moved_ab += 1
                elif ks.site == sb:
                    ks.site = sa
                    moved_ba += 1
        await self._persist_keys()
        return moved_ab, moved_ba

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
        save_keys_list([k.key for k in pool.keys])
    except Exception:
        pass

tg_app = None


class SiteManager:
    def __init__(self, default_url: str):
        self.sites = [self._blank("V1", default_url)]
        self._cursor = 0
        self.store = None
        self._lock = asyncio.Lock()
        self.last_save_ok = True
        self.last_save_error = ""

    @staticmethod
    def _blank(sid: str, url: str) -> dict:
        return {"id": sid, "url": url, "reset_hours": 0, "last_reset": 0.0,
                "token_budget": 0, "req_budget": 0, "timeout": 0, "referer": "", "title": ""}

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
                          "last_reset": float(s.get("last_reset") or 0),
                          "token_budget": int(s.get("token_budget") or 0),
                          "req_budget": int(s.get("req_budget") or 0),
                          "timeout": int(s.get("timeout") or 0),
                          "referer": s.get("referer") or "",
                          "title": s.get("title") or ""})
        if not valid:
            return
        async with self._lock:
            self.sites = valid

    async def _save(self) -> bool:
        if self.store is None:
            self.last_save_ok = False
            self.last_save_error = "no store attached"
            return False
        try:
            await self.store.save_meta("sites", {"sites": self.sites})
            self.last_save_ok = True
            self.last_save_error = ""
            return True
        except Exception as e:
            self.last_save_ok = False
            self.last_save_error = str(e)[:150]
            print(f"[sites] save failed: {e}")
            return False

    def save_status_note(self) -> str:
        if self.last_save_ok:
            return ""
        return f"\n⚠️ change NOT saved ({self.last_save_error}) — will vanish on restart"

    async def list_sites(self):
        async with self._lock:
            return [dict(s) for s in self.sites]

    async def advance(self):
        async with self._lock:
            if self.sites:
                self._cursor = (self._cursor + 1) % len(self.sites)

    async def ordered_ids(self):
        async with self._lock:
            n = len(self.sites)
            if n == 0:
                return []
            return [self.sites[(self._cursor + i) % n]["id"] for i in range(n)]

    async def take_order(self):
        async with self._lock:
            n = len(self.sites)
            if n == 0:
                return []
            order = [self.sites[(self._cursor + i) % n]["id"] for i in range(n)]
            self._cursor = (self._cursor + 1) % n
            return order

    async def add_site(self, url: str) -> Optional[str]:
        url = (url or "").strip().rstrip("/")
        if not url.startswith("http"):
            return None
        async with self._lock:
            n = 1
            while any(s["id"] == f"V{n}" for s in self.sites):
                n += 1
            nid = f"V{n}"
            self.sites.append(self._blank(nid, url))
        await self._save()
        return nid

    async def set_url(self, sid: str, url: str) -> bool:
        sid = self.norm(sid)
        url = (url or "").strip().rstrip("/")
        if not url.startswith("http"):
            return False
        async with self._lock:
            hit = next((s for s in self.sites if s["id"] == sid), None)
            if hit is None:
                return False
            hit["url"] = url
        await self._save()
        return True

    async def swap_sites(self, sa: str, sb: str) -> bool:
        sa, sb = self.norm(sa), self.norm(sb)
        if sa == sb:
            return False
        async with self._lock:
            a = next((s for s in self.sites if s["id"] == sa), None)
            b = next((s for s in self.sites if s["id"] == sb), None)
            if a is None or b is None:
                return False
            for field in ("url", "reset_hours", "last_reset",
                          "token_budget", "req_budget", "timeout", "referer", "title"):
                a[field], b[field] = b[field], a[field]
        await self._save()
        return True

    async def set_reset(self, sid: str, hours: float) -> bool:
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

    def get_site(self, sid: str) -> Optional[dict]:
        sid = self.norm(sid)
        return next((s for s in self.sites if s["id"] == sid), None)

    async def configure_site(self, sid: str, token_budget=None, req_budget=None, timeout=None,
                             referer=None, title=None) -> bool:
        sid = self.norm(sid)
        async with self._lock:
            hit = next((s for s in self.sites if s["id"] == sid), None)
            if hit is None:
                return False
            if token_budget is not None:
                hit["token_budget"] = max(0, int(token_budget))
            if req_budget is not None:
                hit["req_budget"] = max(0, int(req_budget))
            if timeout is not None:
                hit["timeout"] = max(0, int(timeout))
            if referer is not None:
                hit["referer"] = "" if referer == "-" else referer.strip()
            if title is not None:
                hit["title"] = "" if title == "-" else title.strip()
        await self._save()
        return True

    def due_sites(self):
        import time
        now = time.time()
        return [s["id"] for s in self.sites
                if (s.get("reset_hours") or 0) > 0
                and now - (s.get("last_reset") or 0) >= s["reset_hours"] * 3600]

    async def remove_site(self, sid: str) -> bool:
        sid = self.norm(sid)
        async with self._lock:
            if len(self.sites) <= 1:
                return False
            before = len(self.sites)
            self.sites = [s for s in self.sites if s["id"] != sid]
            ok = len(self.sites) < before
            self._cursor = 0
        if ok:
            await self._save()
        return ok

    def resolve(self, override: Optional[str] = None) -> Optional[str]:
        if not override:
            return None
        return self.pick(override)[1]

    def pick(self, override: Optional[str] = None):
        if override:
            sid = self.norm(override)
            for s in self.sites:
                if s["id"] == sid:
                    return sid, s["url"]
            return sid, None
        return None, None


sites = SiteManager(UPSTREAM_BASE_URL)


def site_token_budget(site_id: str) -> int:
    s = sites.get_site(site_id)
    return int((s or {}).get("token_budget") or 0)


def site_req_budget(site_id: str) -> int:
    s = sites.get_site(site_id)
    return int((s or {}).get("req_budget") or 0)


def site_timeout(site_id: str) -> int:
    s = sites.get_site(site_id)
    t = int((s or {}).get("timeout") or 0)
    return t if t > 0 else REQUEST_TIMEOUT_SEC


def extract_usage(data) -> tuple:
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
    total, cached = extract_usage(data)
    if total > 0:
        return total + cached, cached
    est = (len(req_body) + len(resp_body)) // 4 or 1
    return est, 0


def count_stream_tokens(req_body: bytes, stream_bytes: bytes) -> tuple:
    total, cached = extract_usage(_usage_from_text(stream_bytes))
    if total > 0:
        return total + cached, cached
    return (len(req_body) + len(stream_bytes)) // 4 or 1, 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
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
    print(f"[sites] loaded: {[(s['id'], s['url']) for s in sites.sites]} (/v1→V1, /v2→V2…)")

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
        await pool.outbox.flush(store)
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


app = FastAPI(title="AI Key Rotating Proxy", version="1.9.0", lifespan=lifespan)
bearer_scheme = HTTPBearer(auto_error=False)


async def check_proxy_auth(request: Request,
                           cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not PROXY_API_KEY:
        return
    if cred is not None and cred.credentials == PROXY_API_KEY:
        return
    if request.headers.get("x-api-key") == PROXY_API_KEY:
        return
    raise HTTPException(status_code=401, detail="Invalid proxy API key")


def is_exhausted(status_code: int, body_text: str) -> bool:
    if status_code == 402:
        return True
    if status_code in (401, 403):
        return True
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


def build_upstream_headers(incoming: Request, api_key: str, site_id: Optional[str] = None) -> dict:
    h = {}
    for k, v in incoming.headers.items():
        kl = k.lower()
        if kl in ("host", "authorization", "x-api-key", "content-length", "connection"):
            continue
        h[k] = v
    if any(k.lower() == "x-api-key" for k in incoming.headers.keys()):
        h["X-Api-Key"] = api_key
    else:
        h["Authorization"] = f"Bearer {api_key}"
    referer, title = HTTP_REFERER, X_TITLE
    if site_id is not None:
        s = sites.get_site(site_id)
        if s:
            referer = s.get("referer") or HTTP_REFERER
            title = s.get("title") or X_TITLE
    if referer:
        h["HTTP-Referer"] = referer
    if title:
        h["X-Title"] = title
    return h


@app.get("/health")
async def health():
    st = await pool.status()
    avail = sum(1 for s in st if s["available"])
    used = sum(s["tokens_used"] for s in st)
    cap = MAX_TOKENS_PER_KEY * len(st) if MAX_TOKENS_PER_KEY else None
    return {"ok": True, "mode": "path-pinned",
            "sites": [s["id"] for s in sites.sites],
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
    load_dotenv(override=True)
    await pool.reload(parse_keys(os.getenv("API_KEYS", "")))
    return {"reloaded": pool.size}


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "AI rotating proxy running. Use /v1/chat/completions with your PROXY_API_KEY."


async def proxy_request(request: Request):
    if pool.size == 0:
        raise HTTPException(status_code=500, detail="No API_KEYS configured on proxy")

    body = await request.body()
    # Site selection, precedence: X-Site header > /vN path prefix.
    # Strict mapping: /v1/... serves ONLY V1, /v2/... ONLY V2, etc.
    raw_path = request.url.path
    site_id = None
    m = re.match(r"^/[vV](\d+)(/.*)$", raw_path)
    if m:
        sid = "V" + m.group(1)
        if sites.get_site(sid) is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown site '{sid}'. Use /sites in Telegram to list V1, V2, ..."})
        site_id = sid
        raw_path = m.group(2)
    else:
        m_bare = re.match(r"^/[vV](\d+)$", raw_path)
        if m_bare:
            sid = "V" + m_bare.group(1)
            if sites.get_site(sid) is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"unknown site '{sid}'. Use /sites in Telegram to list V1, V2, ..."})
            return JSONResponse({"site": sid, "upstream": sites.resolve(sid),
                                 "hint": f"append an endpoint path, e.g. /v{sid[1:]}/chat/completions"})
    site_ov = (request.headers.get("x-site") or "").strip()
    if site_ov:
        site_id, base_url = sites.pick(site_ov)
        if base_url is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"unknown site '{site_ov}'. Use /sites in Telegram to list V1, V2, ..."})
    # strip /v1 prefix handling: client calls /v1/chat/completions -> upstream same path
    upstream_path = raw_path  # e.g. /v1/chat/completions
    if request.url.query:
        upstream_path += f"?{request.url.query}"
    suffix = upstream_path[len("/v1"):] if upstream_path.startswith("/v1") else upstream_path

    params_note = f"{request.method} {upstream_path}"
    last_error: Optional[str] = None
    last_status: int = 502
    tried = 0
    max_tries = min(MAX_RETRIES_PER_REQUEST, pool.size * 2)
    want_stream = _wants_stream(request, body)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
        while tried < max_tries:
            # Strict per-path mapping: this request serves ONLY site_id.
            sid = site_id
            ks = await pool.next_key(sid)
            if ks is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"No usable keys for site {sid}. Budgets full, disabled, or none assigned.",
                )
            tried += 1
            upstream_url = f"{sites.resolve(sid)}{suffix}"
            headers = build_upstream_headers(request, ks.key, sid)
            timeout = site_timeout(sid)

            if want_stream:
                body_out = body
                if upstream_path == "/chat/completions":
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
                sclient = httpx.AsyncClient(timeout=timeout)
                try:
                    sreq = sclient.build_request(request.method, upstream_url, content=body_out, headers=headers)
                    sresp = await sclient.send(sreq, stream=True)
                except httpx.RequestError as e:
                    await sclient.aclose()
                    last_error = f"network error with {ks.masked}: {e}"
                    await pool.mark_failed(ks)
                    await pool.advance_past(ks, sid)
                    continue
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
                        await pool.advance_past(ks, sid)
                        continue
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
                    timeout=timeout,
                )
            except httpx.RequestError as e:
                last_error = f"network error with {ks.masked}: {e}"
                await pool.mark_failed(ks)
                await pool.advance_past(ks, sid)
                continue

            if resp.status_code < 400:
                data = resp.json() if resp.content else {}
                await pool.mark_success(ks, *count_tokens(body, resp.content, data))
                return JSONResponse(status_code=resp.status_code, content=data)

            try:
                text = resp.text[:2000]
            except Exception:
                text = ""
            last_error = f"{ks.masked} -> {resp.status_code}: {text[:300]}"
            last_status = resp.status_code
            await pool.mark_failed(ks)

            if _retryable(resp.status_code, text):
                await pool.advance_past(ks, sid)
                continue

            return JSONResponse(status_code=resp.status_code, content={"error": text, "proxy_note": params_note})

    raise HTTPException(status_code=last_status if last_status != 502 else 503,
                        detail=f"All keys failed after {tried} tries. Last: {last_error}")


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
# Site-pinned paths: /v2/chat/completions -> site V2 (/v1/* stays all-sites).
app.add_api_route("/v{site}/{full_path:path}", proxy_request, methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                  dependencies=[Depends(check_proxy_auth)])
app.add_api_route("/v{site}", proxy_request, methods=["GET"],
                  dependencies=[Depends(check_proxy_auth)])
app.add_api_route("/V{site}/{full_path:path}", proxy_request, methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                  dependencies=[Depends(check_proxy_auth)])
app.add_api_route("/V{site}", proxy_request, methods=["GET"],
                  dependencies=[Depends(check_proxy_auth)])
