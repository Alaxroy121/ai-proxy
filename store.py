"""Pluggable persistence for the proxy.

- FileStore (default): keys in keys.txt, counters in memory.
- MongoStore (MONGODB_URI set): keys + token counters in MongoDB,
  so everything survives an unstable server disk / restarts.

Usage deltas additionally go through a local append-only WAL + in-memory
outbox (see app.Outbox): memory counters are always exact, Mongo catches
up every few seconds, and a crash can only lose seconds of data.
Mongo driver (motor) is imported lazily so file-mode never needs it.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class UsageWal:
    """Append-only local log of usage deltas. Best-effort, never raises."""

    def __init__(self, path: str):
        self.path = Path(path)

    def append(self, entry: Dict[str, Any]):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[wal] append failed: {e}")

    def take_all(self) -> List[Dict[str, Any]]:
        """Read + delete all entries (they move into the outbox queue)."""
        try:
            if not self.path.exists():
                return []
            lines = self.path.read_text().splitlines()
            self.path.unlink()
        except Exception as e:
            print(f"[wal] read failed: {e}")
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out

    def clear(self):
        """Drop the log (called after a fully successful flush)."""
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            print(f"[wal] clear failed: {e}")


def _norm_key_items(keys) -> List[Dict[str, Any]]:
    """Accept [{'key','site'}] or plain key strings (site defaults V1)."""
    out = []
    for i, k in enumerate(keys):
        if isinstance(k, dict):
            out.append({"key": k.get("key", ""), "site": k.get("site") or "V1", "order": i})
        else:
            out.append({"key": k, "site": "V1", "order": i})
    return [d for d in out if d["key"]]


class FileStore:
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path)
        self.label = f"file:{path}"

    async def connect(self):
        return None  # nothing to connect

    async def close(self):
        return None

    async def load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text().splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                site, key = line.split("|", 1)
                out.append({"key": key.strip(), "site": site.strip() or "V1", "order": i})
            else:
                out.append({"key": line, "site": "V1", "order": i})
        return [d for d in out if d["key"]]

    async def save_keys(self, keys):
        items = _norm_key_items(keys)
        lines = [f"{d['site']}|{d['key']}" if d["site"] != "V1" else d["key"] for d in items]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""))

    async def update_key(self, key: str, tokens: int = 0, cached: int = 0, success: int = 0,
                         fails: int = 0, reqs: int = 0, disabled: Optional[bool] = None,
                         token_limit: Optional[int] = None, req_limit: Optional[int] = None):
        return False  # counters live in memory in file mode

    async def reset_usage(self, site_id: Optional[str] = None):
        return None  # nothing durable in file mode

    async def load_meta(self, name: str) -> Optional[Dict[str, Any]]:
        p = self.path.parent / f"{name}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    async def save_meta(self, name: str, doc: Dict[str, Any]):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            (self.path.parent / f"{name}.json").write_text(json.dumps(doc))
        except Exception as e:
            print(f"[store] meta save failed: {e}")


class MongoStore:
    name = "mongo"

    def __init__(self, uri: str, db_name: str):
        self.uri = uri
        self.db_name = db_name
        self.label = f"mongo:{db_name}.proxy_keys"
        self.client = None
        self.col = None

    async def connect(self):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as e:
            raise RuntimeError("motor not installed: pip install motor") from e
        self.client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=8000)
        await self.client.admin.command("ping")  # fail fast if unreachable
        self.col = self.client[self.db_name]["proxy_keys"]

    async def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    async def load(self) -> List[Dict[str, Any]]:
        docs = await self.col.find().sort("order", 1).to_list(length=1000)
        out = []
        for d in docs:
            out.append({
                "key": d.get("_id"),
                "order": d.get("order", 0),
                "site": d.get("site") or "V1",
                "tokens_used": int(d.get("tokens_used", 0)),
                "cached_tokens": int(d.get("cached_tokens", 0)),
                "success": int(d.get("success", 0)),
                "fails": int(d.get("fails", 0)),
                "req_used": int(d.get("req_used", 0)),
                "req_limit": int(d.get("req_limit", 0)),
                "token_limit": int(d.get("token_limit", 0)),
                "disabled": bool(d.get("disabled", False)),
            })
        return [d for d in out if d["key"]]

    async def save_keys(self, keys):
        for item in _norm_key_items(keys):
            k = item["key"]
            await self.col.update_one(
                {"_id": k},
                {"$setOnInsert": {"tokens_used": 0, "cached_tokens": 0, "success": 0,
                                  "fails": 0, "req_used": 0, "req_limit": 0,
                                  "token_limit": 0, "disabled": False},
                 "$set": {"order": item["order"], "site": item["site"]}},
                upsert=True,
            )
        await self.col.delete_many({"_id": {"$nin": [d["key"] for d in _norm_key_items(keys)]}})

    async def update_key(self, key: str, tokens: int = 0, cached: int = 0, success: int = 0,
                         fails: int = 0, reqs: int = 0, disabled: Optional[bool] = None,
                         token_limit: Optional[int] = None, req_limit: Optional[int] = None):
        inc = {}
        if tokens:
            inc["tokens_used"] = tokens
        if cached:
            inc["cached_tokens"] = cached
        if success:
            inc["success"] = success
        if fails:
            inc["fails"] = fails
        if reqs:
            inc["req_used"] = reqs
        update: Dict[str, Any] = {}
        if inc:
            update["$inc"] = inc
        setters = {}
        if disabled is not None:
            setters["disabled"] = disabled
        if token_limit is not None:
            setters["token_limit"] = token_limit
        if req_limit is not None:
            setters["req_limit"] = req_limit
        if setters:
            update["$set"] = setters
        if not update:
            return False
        await self.col.update_one({"_id": key}, update, upsert=True)
        return True

    async def reset_usage(self, site_id: Optional[str] = None):
        filt = {"site": site_id} if site_id else {}
        await self.col.update_many(filt, {"$set": {"tokens_used": 0, "cached_tokens": 0,
                                                  "success": 0, "fails": 0, "req_used": 0}})

    async def load_meta(self, name: str) -> Optional[Dict[str, Any]]:
        return await self.col.database["proxy_meta"].find_one({"_id": name})

    async def save_meta(self, name: str, doc: Dict[str, Any]):
        await self.col.database["proxy_meta"].update_one(
            {"_id": name}, {"$set": doc}, upsert=True)


def build_store(keys_file: str):
    """Mongo when MONGODB_URI is set, else local file. Import-safe (no motor needed)."""
    import os
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        return MongoStore(uri, os.getenv("MONGODB_DB", "ai_proxy"))
    return FileStore(keys_file)
