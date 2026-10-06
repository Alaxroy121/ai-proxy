"""Pluggable persistence for the proxy.

- FileStore (default): keys in keys.txt, counters in memory.
- MongoStore (MONGODB_URI set): keys + token counters in MongoDB,
  so everything survives an unstable server disk / restarts.

Mongo driver (motor) is imported lazily so file-mode never needs it.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        keys = [l.strip() for l in self.path.read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        return [{"key": k, "order": i} for i, k in enumerate(keys)]

    async def save_keys(self, keys: List[str]):
        self.path.write_text("\n".join(keys) + ("\n" if keys else ""))

    async def update_key(self, key: str, tokens: int = 0, success: int = 0,
                         fails: int = 0, disabled: Optional[bool] = None):
        return False  # counters live in memory in file mode

    async def reset_usage(self):
        return None  # nothing durable in file mode


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
                "tokens_used": int(d.get("tokens_used", 0)),
                "success": int(d.get("success", 0)),
                "fails": int(d.get("fails", 0)),
                "disabled": bool(d.get("disabled", False)),
            })
        return [d for d in out if d["key"]]

    async def save_keys(self, keys: List[str]):
        for i, k in enumerate(keys):
            await self.col.update_one(
                {"_id": k},
                {"$setOnInsert": {"tokens_used": 0, "success": 0, "fails": 0,
                                  "disabled": False},
                 "$set": {"order": i}},
                upsert=True,
            )
        await self.col.delete_many({"_id": {"$nin": list(keys)}})

    async def update_key(self, key: str, tokens: int = 0, success: int = 0,
                         fails: int = 0, disabled: Optional[bool] = None):
        inc = {}
        if tokens:
            inc["tokens_used"] = tokens
        if success:
            inc["success"] = success
        if fails:
            inc["fails"] = fails
        update: Dict[str, Any] = {}
        if inc:
            update["$inc"] = inc
        if disabled is not None:
            update["$set"] = {"disabled": disabled}
        if not update:
            return False
        await self.col.update_one({"_id": key}, update, upsert=True)
        return True

    async def reset_usage(self):
        await self.col.update_many({}, {"$set": {"tokens_used": 0, "success": 0, "fails": 0}})


def build_store(keys_file: str):
    """Mongo when MONGODB_URI is set, else local file. Import-safe (no motor needed)."""
    import os
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        return MongoStore(uri, os.getenv("MONGODB_DB", "ai_proxy"))
    return FileStore(keys_file)
