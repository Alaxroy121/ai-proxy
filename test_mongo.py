#!/usr/bin/env python3
"""Diagnostic script to test MongoDB connection and inspect proxy_keys."""
import os
import sys

# Ensure root is in sys.path
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from dotenv import load_dotenv

load_dotenv()

uri = os.getenv("MONGODB_URI", "").strip()
db_name = os.getenv("MONGODB_DB", "").strip()

print("=" * 60)
print("🔍 AI-PROXY MONGODB DIAGNOSTIC TOOL")
print("=" * 60)

if not uri:
    print("❌ MONGODB_URI is NOT set in your .env file!")
    print("   The proxy is currently running in local file mode (keys.txt).")
    print("   To fix:")
    print("   1. Open .env on your server: nano .env")
    print("   2. Set MONGODB_URI=\"mongodb+srv://...\"")
    print("   3. Restart the proxy.")
    sys.exit(0)

# Mask URI for display
masked_uri = uri
if "@" in uri:
    prefix = uri.split("@")[0]
    suffix = uri.split("@")[1]
    if "://" in prefix:
        scheme, auth = prefix.split("://", 1)
        user = auth.split(":")[0] if ":" in auth else "user"
        masked_uri = f"{scheme}://{user}:****@{suffix}"

print(f"📡 MONGODB_URI: {masked_uri}")

# Extract db name if not set
if not db_name:
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(uri)
        path = parsed.path.strip("/")
        if path:
            db_name = path.split("/")[0]
    except Exception:
        pass
if not db_name:
    db_name = "ai_proxy"

print(f"📦 Target Database: {db_name}")

try:
    from pymongo import MongoClient
except ImportError:
    print("❌ pymongo is not installed! Run: pip install pymongo motor")
    sys.exit(1)

print("\n⏳ Testing connection to MongoDB Atlas (6s timeout)...")
try:
    client = MongoClient(uri, serverSelectionTimeoutMS=6000)
    client.admin.command("ping")
    print("✅ Ping successful! Connected to MongoDB.")
except Exception as e:
    err_str = str(e)
    print(f"\n❌ Connection FAILED: {err_str}")
    if "timed out" in err_str.lower() or "selection" in err_str.lower():
        print("\n💡 REASON: MongoDB Atlas is blocking your server's IP address!")
        print("   How to fix in 1 minute:")
        print("   1. Log in to https://cloud.mongodb.com")
        print("   2. Under 'Security' on the left menu, click 'Network Access'")
        print("   3. Click 'Add IP Address'")
        print("   4. Choose 'Allow Access from Anywhere' (0.0.0.0/0) and Save.")
        print("   5. Wait 30 seconds and run this script again.")
    elif "auth" in err_str.lower() or "credential" in err_str.lower():
        print("\n💡 REASON: MongoDB username or password in MONGODB_URI is incorrect.")
        print("   Check your Database Access user credentials in Atlas.")
    sys.exit(1)

try:
    dbs = client.list_database_names()
    print(f"\n📚 Databases found on cluster: {dbs}")
    
    # Check all databases for proxy_keys or keys
    found_any_keys = False
    for d in dbs:
        if d in ("admin", "local", "config"):
            continue
        colls = client[d].list_collection_names()
        print(f"\n--- Database: '{d}' (collections: {colls}) ---")
        for col_name in colls:
            count = client[d][col_name].count_documents({})
            print(f"  • {col_name}: {count} documents")
            if "key" in col_name or col_name == "proxy_keys":
                found_any_keys = True
                print(f"    Sample docs in {d}.{col_name}:")
                for doc in client[d][col_name].find().limit(5):
                    # Show doc with masked key
                    k = doc.get("key") or doc.get("_id")
                    k_str = str(k)
                    masked = f"{k_str[:6]}...{k_str[-4:]}" if len(k_str) > 10 else k_str
                    site = doc.get("site", "V1")
                    print(f"      - key: {masked} | site: {site} | tokens: {doc.get('tokens_used', 0)}")

    if not found_any_keys:
        print("\n⚠️ No 'proxy_keys' collection found in any database.")
        print("   If you have your keys, you can add them via Telegram DM (/add <key> [V1..V10]).")
    else:
        if db_name not in dbs or "proxy_keys" not in client[db_name].list_collection_names():
            print(f"\n⚠️ WARNING: Keys were found, but not in target database '{db_name}'!")
            print(f"   Set MONGODB_DB in your .env to the database where your keys are stored.")
except Exception as e:
    print(f"Error inspecting databases: {e}")

print("\n" + "=" * 60)
