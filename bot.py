"""
Telegram DM control bot for the AI key-rotating proxy.
Polling mode (no webhook/firewall needed). Owner-only via TELEGRAM_ADMIN_IDS.
Shares the same KeyPool object (same process) -> live health + config changes.

Commands (DM the bot):
  /health          - upstream, total/available keys
  /stats | /keys   - per-key table with token usage (masked, never full keys)
  /config          - token limit, upstream, timeouts
  /add <full-key>  - append a new provider key (persists to keys.txt)
  /rm <n|mask>     - remove key by number (see /stats) or masked id
  /enable <n>      - re-enable a disabled key
  /disable <n>     - manually take a key offline
  /reset | /resetusage - zero all token counters
  /help (limit is set in .env as MAX_TOKENS_PER_KEY)
Auto-alerts every 30s: key hit budget / all keys over budget.
"""
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

PENDING_TTL = 300  # seconds to reply with a budget number
GUIDED_ACTIONS = ("limit", "reqlimit", "rm")

ADMIN_IDS = {s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()}
MILESTONES = (50, 80, 100)  # live progress pushes when a key crosses these %

COMMAND_MENU = [
    ("start", "Control panel with buttons"),
    ("health", "Upstream + overall usage"),
    ("stats", "Keys by website: /stats [V1]"),
    ("config", "Budget, store, timeouts"),
    ("sites", "List upstream websites V1–V10"),
    ("siteadd", "Add website: /siteadd <url>"),
    ("sitename", "Set site title: /sitename <V1> <name>"),
    ("siteuse", "Fix website URL: /siteuse <V1> <url>"),
    ("siteswap", "Swap two sites: /siteswap <V1> <V2>"),
    ("models", "List upstream models: /models [V1]"),
    ("sitereset", "Auto-reset schedule: /sitereset <V1> <hours>"),
    ("sitelimit", "Site token budget: /sitelimit <V1> <n>"),
    ("sitereqlimit", "Site request budget: /sitereqlimit <V1> <n>"),
    ("sitetimeout", "Site timeout: /sitetimeout <V1> <sec>"),
    ("siteheaders", "Site headers: /siteheaders <V1> <ref> <title>"),
    ("siterm", "Remove website: /siterm <V1>"),
    ("add", "Add a key: /add <key> [V1]"),
    ("limit", "Token budget per key: /limit <num> <n>"),
    ("reqlimit", "Request budget per key: /reqlimit <num> <n>"),
    ("rm", "Remove a key: /rm <num>"),
    ("enable", "Re-enable a key: /enable <num>"),
    ("disable", "Take a key offline: /disable <num>"),
    ("reset", "Zero all token/request counters"),
    ("cancel", "Cancel a pending input"),
    ("menu", "Re-sync the / command menu now"),
    ("help", "All commands guide"),
]

HELP_TEXT = (
    "🤖 *AI Key-Rotating Proxy Commands (V1–V10)*\n\n"
    "📊 *Monitoring:*\n"
    "/health – totals, store health & sites status\n"
    "/stats `[V1..V10]` – keys and metrics grouped by website\n"
    "/models `[V1..V10]` – fetch accepted models from upstream\n"
    "/config – global budget, store, retries & timeouts\n\n"
    "🌐 *Sites Management (V1 through V10):*\n"
    "/sites – list all configured websites with routing paths\n"
    "/siteadd <url> – add website to next available slot\n"
    "/sitename <V1..V10> <title> – set friendly name (e.g. OpenRouter)\n"
    "/siteuse <V1..V10> <url> – set or inspect site upstream URL\n"
    "/siteswap <V1> <V2> – exchange URLs, schedules and keys\n"
    "/sitereset <V1..V10> <hours> – auto-reset usage every N hours (0 = off)\n"
    "/sitelimit <V1..V10> <tokens> – default token budget on that site\n"
    "/sitereqlimit <V1..V10> <n> – default request budget on that site\n"
    "/sitetimeout <V1..V10> <sec> – request timeout on that site\n"
    "/siteheaders <V1..V10> <ref> <title> – custom headers (`-` keeps global)\n"
    "/siterm <V1..V10> – remove website slot\n\n"
    "🔑 *Keys Management:*\n"
    "/add <key> `[V1..V10]` – add key to a website (default: first site)\n"
    "/limit <num> <tokens> – token budget for one key (`0` or `off` = unlimited)\n"
    "/reqlimit <num> <n> – request budget for one key (`0` or `off` = unlimited)\n"
    "/rm <num|mask> – remove key\n"
    "/enable <num> – re-enable key\n"
    "/disable <num> – take key offline\n"
    "/reset – zero all token and request counters\n"
    "/cancel – cancel guided input\n"
    "/menu – force re-sync of this / menu\n\n"
    "💡 *Path Routing:* `/v1/…`→V1 … `/v10/…`→V10. Each path routes strictly to that site.\n"
    "📈 *Live Push:* Automatic alerts at 50%, 80%, and 100% per key (tokens & requests)."
)

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Stats", callback_data="stats"),
     InlineKeyboardButton("❤️ Health", callback_data="health")],
    [InlineKeyboardButton("🌐 Sites (V1–V10)", callback_data="sites"),
     InlineKeyboardButton("⚙️ Config", callback_data="config")],
    [InlineKeyboardButton("♻️ Reset Counters", callback_data="reset:ask"),
     InlineKeyboardButton("📋 Menu", callback_data="menu")],
    [InlineKeyboardButton("ℹ️ Help Guide", callback_data="help")],
])

HOME_BUTTON = InlineKeyboardButton("🏠 Panel", callback_data="panel")


def _site_badge(keys) -> str:
    if not keys:
        return "⚪"
    usable = sum(1 for k in keys if k.available)
    if usable == 0:
        return "⛔"
    return "✅" if usable == len(keys) else "⚠️"


def panel_text(pool) -> str:
    import app as appmod
    total = len(pool.keys)
    avail = sum(1 for k in pool.keys if k.available)
    used = sum(k.tokens_used for k in pool.keys)
    overall_tokens = _bar(used, _cap_sum(pool.keys))
    req_used = sum(k.req_used for k in pool.keys)
    req_cap = _req_cap_sum(pool.keys)
    has_req = any(k.eff_req_limit() > 0 for k in pool.keys)
    req_line = f"🧾 Requests {_bar(req_used, req_cap) if req_cap else f'`{req_used:,}` reqs'}\n" if (has_req or req_used > 0) else ""
    site_lines = []
    for s in appmod.sites.sites:
        keys = [k for k in pool.keys if k.site == s["id"]]
        usable = sum(1 for k in keys if k.available)
        badge = _site_badge(keys)
        title_suffix = f" · `{s['title'][:12]}`" if s.get("title") else ""
        site_lines.append(f"{badge} `{s['id']}` *{usable}/{len(keys)}*{title_suffix}")
    store = "ok" if getattr(pool, "store_ok", True) else "lag"
    pend = pool.pending_count()
    tail = f"\n⚠️ `{pend}` updates unsynced" if pend else ""
    return (
        "🤖 *Proxy Control (V1–V10)*\n"
        f"🔑 *{avail}/{total}* keys ready · 💾 store {store}\n\n"
        f"🌐 *Sites Status:*\n"
        f"{chr(10).join(site_lines) or '`no sites`'}\n\n"
        f"📊 Tokens {overall_tokens}\n"
        f"{req_line}"
        "🎯 Rotation: Key 1 → 2 → 3 as limits fill\n"
        "📈 Live alerts at 50% · 80% · 100%" + tail
    )


def stats_keyboard(pool, site_id=None) -> InlineKeyboardMarkup:
    if site_id:
        items = [(i, ks) for i, ks in enumerate(pool.keys, start=1) if ks.site == site_id]
        tag = lambda n: f"tg:{n}:{site_id}"
    else:
        items = [(i, ks) for i, ks in enumerate(pool.keys, start=1)]
        tag = lambda n: f"toggle:{n}"
    rows, row = [], []
    for i, ks in items:
        icon = "⛔" if ks.disabled else ("❌" if ks.over_budget() else "✅")
        row.append(InlineKeyboardButton(f"{i} {icon}", callback_data=tag(i)))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if site_id:
        rows.append([
            InlineKeyboardButton("🧠 Models", callback_data=f"models:{site_id}"),
            InlineKeyboardButton("🌐 Site", callback_data=f"site:{site_id}"),
        ])
        rows.append([
            InlineKeyboardButton("🌐 Websites", callback_data="stats"),
            HOME_BUTTON,
        ])
    else:
        rows.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh:stats"),
            InlineKeyboardButton("♻️ Reset", callback_data="reset:ask"),
        ])
        rows.append([
            InlineKeyboardButton("🌐 Sites", callback_data="sites"),
            HOME_BUTTON,
        ])
    return InlineKeyboardMarkup(rows)


async def stats_picker_keyboard(pool) -> InlineKeyboardMarkup:
    import app as appmod
    rows = []
    curr_row = []
    for s in await appmod.sites.list_sites():
        keys = [k for k in pool.keys if k.site == s["id"]]
        usable = sum(1 for k in keys if k.available)
        title_str = f" {s['title'][:6]}" if s.get("title") else ""
        curr_row.append(InlineKeyboardButton(
            f"{_site_badge(keys)} {s['id']}{title_str} ({usable}/{len(keys)})",
            callback_data=f"statssite:{s['id']}"))
        if len(curr_row) == 2:
            rows.append(curr_row)
            curr_row = []
    if curr_row:
        rows.append(curr_row)
    rows.append([InlineKeyboardButton("🌍 All sites", callback_data="statssite:ALL")])
    rows.append([InlineKeyboardButton("🌐 Sites", callback_data="sites"), HOME_BUTTON])
    return InlineKeyboardMarkup(rows)


def health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="stats"),
         InlineKeyboardButton("🌐 Sites", callback_data="sites")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh:health"),
         HOME_BUTTON],
    ])


def sites_keyboard(pool=None) -> InlineKeyboardMarkup:
    import app as appmod
    rows = []
    curr_row = []
    for s in appmod.sites.sites:
        keys = [k for k in pool.keys if k.site == s["id"]] if pool is not None else []
        usable = sum(1 for k in keys if k.available)
        title_str = f" {s['title'][:6]}" if s.get("title") else ""
        btn_text = f"{_site_badge(keys)} {s['id']}{title_str} ({usable}/{len(keys)})"
        curr_row.append(InlineKeyboardButton(btn_text, callback_data=f"site:{s['id']}"))
        if len(curr_row) == 2:
            rows.append(curr_row)
            curr_row = []
    if curr_row:
        rows.append(curr_row)
    rows.append([InlineKeyboardButton("📊 All Stats", callback_data="statssite:ALL"),
                 InlineKeyboardButton("❤️ Health", callback_data="health")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh:sites"),
                 HOME_BUTTON])
    return InlineKeyboardMarkup(rows)


def site_detail_keyboard(sid: str) -> InlineKeyboardMarkup:
    import app as appmod
    known = [s["id"] for s in appmod.sites.sites]
    sid = appmod.SiteManager.norm(sid)
    prev_sid = next_sid = sid
    if sid in known and len(known) > 1:
        idx = known.index(sid)
        prev_sid = known[(idx - 1) % len(known)]
        next_sid = known[(idx + 1) % len(known)]

    rows = []
    if len(known) > 1:
        rows.append([
            InlineKeyboardButton(f"⬅️ {prev_sid}", callback_data=f"site:{prev_sid}"),
            InlineKeyboardButton("🌐 Sites (V1–V10)", callback_data="sites"),
            InlineKeyboardButton(f"{next_sid} ➡️", callback_data=f"site:{next_sid}"),
        ])
    else:
        rows.append([InlineKeyboardButton("🌐 Sites (V1–V10)", callback_data="sites")])

    rows.append([
        InlineKeyboardButton(f"📊 Keys ({sid})", callback_data=f"statssite:{sid}"),
        InlineKeyboardButton("🧠 Models", callback_data=f"models:{sid}"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"site:{sid}"),
        HOME_BUTTON,
    ])
    return InlineKeyboardMarkup(rows)


def models_keyboard(sid: str) -> InlineKeyboardMarkup:
    import app as appmod
    known = [s["id"] for s in appmod.sites.sites]
    sid = appmod.SiteManager.norm(sid)
    prev_sid = next_sid = sid
    if sid in known and len(known) > 1:
        idx = known.index(sid)
        prev_sid = known[(idx - 1) % len(known)]
        next_sid = known[(idx + 1) % len(known)]

    rows = []
    if len(known) > 1:
        rows.append([
            InlineKeyboardButton(f"⬅️ {prev_sid}", callback_data=f"models:{prev_sid}"),
            InlineKeyboardButton(f"🌐 Site {sid}", callback_data=f"site:{sid}"),
            InlineKeyboardButton(f"{next_sid} ➡️", callback_data=f"models:{next_sid}"),
        ])
    else:
        rows.append([InlineKeyboardButton(f"🌐 Site {sid}", callback_data=f"site:{sid}")])

    rows.append([
        InlineKeyboardButton("🔄 Refresh models", callback_data=f"models:{sid}:r"),
        InlineKeyboardButton("🌐 All Sites", callback_data="sites"),
    ])
    rows.append([HOME_BUTTON])
    return InlineKeyboardMarkup(rows)


RESET_CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Yes, zero them", callback_data="reset:yes"),
     InlineKeyboardButton("❌ Cancel", callback_data="reset:no")],
])


async def render_sites(pool=None) -> str:
    import app as appmod
    import time
    lines = []
    for s in await appmod.sites.list_sites():
        keys = [k for k in pool.keys if k.site == s["id"]] if pool is not None else []
        usable = sum(1 for k in keys if k.available)
        keys_note = f"{usable}/{len(keys)}"
        title_bit = f" `{s['title']}`" if s.get("title") else ""
        rh = s.get("reset_hours") or 0
        if rh > 0:
            left = max(0, rh * 3600 - (time.time() - (s.get("last_reset") or 0)))
            hrs = int(left // 3600)
            mins = int((left % 3600) // 60)
            sched = f" · 🔁 {rh:g}h (~{hrs}h{mins:02d}m)"
        else:
            sched = ""
        bits = ""
        if (s.get("token_budget") or 0) > 0:
            bits += f" · lim {_fmt(s['token_budget'])}"
        if (s.get("req_budget") or 0) > 0:
            bits += f" · req {_fmt(s['req_budget'])}"
        if (s.get("timeout") or 0) > 0:
            bits += f" · t/o {s['timeout']}s"
        if s.get("referer") or s.get("title"):
            bits += " · H"
        lines.append(f"{_site_badge(keys)} `{s['id']}`{title_bit} ({keys_note}) · `{s['url']}`{sched}{bits}")
    return ("🌐 *Websites (V1–V10) — tap one for details*\n\n" + "\n".join(lines or ["(none)"]) +
            "\n\n💡 *Path routing:* `/v1/…`→V1 … `/v10/…`→V10 (isolated per site).")


async def render_site_detail(pool, sid: str) -> str:
    import app as appmod
    import time
    sid = appmod.SiteManager.norm(sid)
    site = appmod.sites.get_site(sid)
    if site is None:
        return "❓ Unknown site."
    keys = [k for k in pool.keys if k.site == sid]
    usable = sum(1 for k in keys if k.available)
    used = sum(k.tokens_used for k in keys)
    nxt = await pool.next_key(sid)
    nxt_txt = "none available"
    if nxt is not None:
        num = next((i for i, k in enumerate(pool.keys, 1) if k is nxt), "?")
        nxt_txt = f"`{nxt.masked}` (#{num})"
    title_hdr = f" — `{site['title']}`" if site.get("title") else ""
    lines = [
        f"🌐 *Site {sid}*{title_hdr}",
        f"🔗 Upstream: `{site['url']}`",
        f"🛣 Endpoint: `/v{sid[1:].lower()}/chat/completions` (or `/V{sid[1:]}/…`)",
        "",
        f"🔑 *{usable}/{len(keys)}* keys usable",
        f"▶️ Next key in rotation: {nxt_txt}",
        f"📊 Tokens: {_bar(used, _cap_sum(keys))}",
    ]
    req_used = sum(k.req_used for k in keys)
    req_caps = [k.eff_req_limit() for k in keys]
    if any(c > 0 for c in req_caps):
        if all(c > 0 for c in req_caps):
            lines.append(f"🧾 Requests: {_bar(req_used, sum(req_caps))}")
        else:
            lines.append(f"🧾 Requests: `{req_used:,}` (mixed limits)")
    elif req_used > 0:
        lines.append(f"🧾 Requests: `{req_used:,}` (no limit)")
    rh = site.get("reset_hours") or 0
    if rh > 0:
        left = max(0, rh * 3600 - (time.time() - (site.get("last_reset") or 0)))
        hrs, mins = int(left // 3600), int((left % 3600) // 60)
        lines.append(f"🔁 Auto-reset: every {rh:g}h · next ~{hrs}h{mins:02d}m")
    else:
        lines.append("🔁 Auto-reset: off")
    if (site.get("token_budget") or 0) > 0:
        lines.append(f"⚙️ Token budget/key: `{_fmt(site['token_budget'])}`")
    if (site.get("req_budget") or 0) > 0:
        lines.append(f"⚙️ Request budget/key: `{site['req_budget']:,}`")
    if (site.get("timeout") or 0) > 0:
        lines.append(f"⏱ Timeout: `{site['timeout']}s`")
    if site.get("referer") or site.get("title"):
        lines.append("🏷 Custom headers: on")
    lines.append("")
    lines.append("Tap 🧠 *Models* to see the exact model names this site accepts.")
    return "\n".join(lines)


async def fetch_site_models(pool, sid: str, bot_data, force: bool = False) -> str:
    """Fetch /models from one site so V4 etc. are easy to verify."""
    import app as appmod
    import httpx
    import time
    sid = appmod.SiteManager.norm(sid)
    site = appmod.sites.get_site(sid)
    if site is None:
        return "❓ Unknown site."
    cache = bot_data.setdefault("models_cache", {})
    now = time.time()
    hit = cache.get(sid)
    if hit and not force and now - hit[0] < 300:
        models, err = hit[1], hit[2]
    else:
        models, err = [], ""
        keys = [k for k in pool.keys if k.site == sid]
        key = next((k for k in keys if k.available), keys[0] if keys else None)
        if key is None:
            err = "no key assigned to this site"
        else:
            url = f"{site['url'].rstrip('/')}/models"
            headers = {"Accept": "application/json"}
            if site.get("referer") or appmod.HTTP_REFERER:
                headers["HTTP-Referer"] = site.get("referer") or appmod.HTTP_REFERER
            if site.get("title") or appmod.X_TITLE:
                headers["X-Title"] = site.get("title") or appmod.X_TITLE
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        url, headers={**headers, "Authorization": f"Bearer {key.key}"})
                    if resp.status_code in (401, 403):
                        resp = await client.get(
                            url, headers={**headers, "X-Api-Key": key.key})
                if resp.status_code >= 400:
                    err = f"HTTP {resp.status_code}: {resp.text[:160]}"
                else:
                    data = resp.json()
                    arr = data if isinstance(data, list) else (
                        data.get("data") or data.get("models") or [])
                    seen = set()
                    for m in arr:
                        mid = m if isinstance(m, str) else (
                            m.get("id") if isinstance(m, dict) else None)
                        if mid and mid not in seen:
                            seen.add(mid)
                            models.append(str(mid))
                    if not models:
                        err = "upstream returned no models"
            except Exception as e:
                err = str(e)[:160]
        cache[sid] = (now, models, err)

    if err:
        return (f"🧠 *{sid} models*\n`{site['url']}`\n\n"
                f"❌ `{err}`\n\nCheck `/siteuse {sid} <url>` or the site key.")
    show = models[:30]
    lines = [f"🧠 *{sid} models*", f"`{site['url']}`", ""]
    lines += [f"{i}. `{m}`" for i, m in enumerate(show, 1)]
    if len(models) > len(show):
        lines.append(f"… +{len(models) - len(show)} more")
    lines += ["", f"Use the exact name with `/v{sid[1:]}/chat/completions`."]
    return "\n".join(lines)


def _admin_ids() -> set:
    # read live so container env changes don't need code reload
    ids = {s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()}
    return ids or ADMIN_IDS


def _is_admin(update: Update) -> bool:
    u = update.effective_user
    return u is not None and str(u.id) in _admin_ids()


async def _deny(update: Update):
    if update.message:
        await update.message.reply_text("⛔ Not authorized. Your ID is not in TELEGRAM_ADMIN_IDS.")


def _fmt(n: int) -> str:
    """Compact token counts: 950 -> 950, 12,400 -> 12.4k, 13,808,000 -> 13.8m."""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n:,}"


def _bar(used: int, limit: int, width: int = 12) -> str:
    """Unique progress style: ▰ filled, ▱ empty, clamped, with %."""
    if limit <= 0:
        return f"`{_fmt(used)}` (no limit)"
    pct = min(100, int(used * 100 / limit))
    fill = min(width, int(used * width / limit))
    return f"`{'▰' * fill}{'▱' * (width - fill)}` {pct}% ({_fmt(used)}/{_fmt(limit)})"


def _cap_sum(keys) -> int:
    """Total token capacity, or 0 when at least one key is unlimited."""
    caps = [ks.eff_token_limit() for ks in keys]
    return sum(caps) if caps and all(c > 0 for c in caps) else 0


def _req_cap_sum(keys) -> int:
    """Total request capacity, or 0 when at least one key is unlimited."""
    caps = [ks.eff_req_limit() for ks in keys]
    return sum(caps) if caps and all(c > 0 for c in caps) else 0


def _stats_lines(items) -> list[str]:
    """items: [(1-based number, KeyState)]. Numbers stay global (toggle/rm use them)."""
    lines = []
    for i, ks in items:
        if ks.disabled:
            state = "⛔"
        elif ks.available:
            state = "✅"
        else:
            state = "❌"
        extra = ks.status_reason() if hasattr(ks, "status_reason") else ("over budget" if ks.over_budget() else "in rotation")
        bits = f"{_bar(ks.tokens_used, ks.eff_token_limit())}"
        if ks.eff_req_limit() > 0:
            bits += f" | req {_bar(ks.req_used, ks.eff_req_limit(), width=8)}"
        elif ks.req_used:
            bits += f" | req {ks.req_used:,}"
        bits += f" ok={ks.success} fail={ks.fails}"
        if ks.cached_tokens:
            bits += f" ({_fmt(ks.cached_tokens)} cached)"
        if ks.token_limit:
            bits += f" [lim {_fmt(ks.token_limit)}]"
        lines.append(f"{i}. {state} `{ks.masked}` {bits} (`{extra}`)")
    return lines or ["(no keys)"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        panel_text(ctx.bot_data["pool"]),
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT)


def _numbered(pool, site_id=None):
    return [(i, ks) for i, ks in enumerate(pool.keys, start=1)
            if site_id is None or ks.site == site_id]


def render_stats(pool, site_filter=None) -> str:
    import app as appmod
    known = [s["id"] for s in appmod.sites.sites]
    groups = []
    if site_filter:
        groups = [(site_filter, _numbered(pool, site_filter))]
    else:
        for s in appmod.sites.sites:
            items = _numbered(pool, s["id"])
            if items:
                groups.append((s["id"], items))
        orphans = [(i, ks) for i, ks in enumerate(pool.keys, start=1) if ks.site not in known]
        if orphans:
            groups.append(("?", orphans))
    sections = []
    for sid, items in groups:
        site_obj = appmod.sites.get_site(sid) if sid != "?" else None
        title_str = f" — `{site_obj['title']}`" if site_obj and site_obj.get("title") else ""
        if sid == "?":
            title = "❓ *unknown site*"
        else:
            title = f"*{sid}*{title_str} `{appmod.sites.resolve(sid) or '?'}`"
        used = sum(ks.tokens_used for _, ks in items)
        cap = _cap_sum([ks for _, ks in items])
        overall = _bar(used, cap)
        req_used = sum(ks.req_used for _, ks in items)
        req_cap = _req_cap_sum([ks for _, ks in items])
        req_line = f" · 🧾 {_bar(req_used, req_cap) if req_cap else f'`{req_used:,}` reqs'}" if (req_cap or req_used) else ""
        sections.append(f"{title}\n📊 ({sid}) {overall}{req_line}\n" + "\n".join(_stats_lines(items)))
    body = "\n\n".join(sections) or "(no keys on this site)"
    if site_filter is None:
        all_used = sum(k.tokens_used for k in pool.keys)
        all_cap = _cap_sum(pool.keys)
        grand = _bar(all_used, all_cap)
        all_reqs = sum(k.req_used for k in pool.keys)
        all_rcap = _req_cap_sum(pool.keys)
        req_grand = f"\n🧾 *overall reqs* {_bar(all_reqs, all_rcap) if all_rcap else f'`{all_reqs:,}` reqs'}" if any(k.eff_req_limit() > 0 for k in pool.keys) else ""
        body = f"🌍 *overall tokens* {grand}{req_grand}\n\n" + body
    if pool.pending_count() and site_filter is None:
        body += f"\n\n⚠️ `{pool.pending_count()}` updates not yet saved to store"
    return f"🔑 *keys by website (V1–V10)*\n\n{body}"


def render_config(pool=None) -> str:
    import app as appmod
    store = pool.store.label if pool is not None and pool.store else "file"
    sitelines = []
    for s in appmod.sites.sites:
        bits = ""
        if s.get("title"):
            bits += f" title=`{s['title']}`"
        if (s.get("token_budget") or 0) > 0:
            bits += f" lim={_fmt(s['token_budget'])}"
        if (s.get("req_budget") or 0) > 0:
            bits += f" req={s['req_budget']}"
        if (s.get("timeout") or 0) > 0:
            bits += f" t/o={s['timeout']}s"
        if s.get("referer") or s.get("title"):
            bits += " headers=custom"
        n = sum(1 for k in (pool.keys if pool else []) if k.site == s["id"])
        sitelines.append(f"  `{s['id']}` `{s['url']}` ({n} keys){bits}")
    sitelines = "\n".join(sitelines) or "  (none)"
    return (
        "⚙️ *Configuration (V1–V10)*\n"
        f"mode: isolated paths per site — /v1→V1 … /v10→V10 (X-Site overrides)\n"
        f"websites:\n{sitelines}\n"
        f"token budget/key: `{appmod.MAX_TOKENS_PER_KEY}` (0 = unlimited)\n"
        f"request budget/key: `{getattr(appmod, 'MAX_REQS_PER_KEY', 0)}` (0 = unlimited)\n"
        f"live milestones: `50/80/100%` (tokens & requests)\n"
        f"auto digest: `{os.getenv('LIVE_DIGEST_MIN', '0')} min (0 = off)`\n"
        f"store: `{store}`\n"
        f"max retries: `{appmod.MAX_RETRIES_PER_REQUEST}`\n"
        f"timeout: `{appmod.REQUEST_TIMEOUT_SEC}s`\n"
        f"keys file: `{appmod.KEYS_FILE}`"
    )


async def _safe_edit_message(q, text: str, reply_markup=None):
    from telegram.error import BadRequest
    import re
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            try:
                await q.answer("Refreshed")
            except Exception:
                pass
            return
        if "can't parse entities" in err.lower() or "entity" in err.lower():
            print(f"[bot] markdown parse error ({err}); falling back to plain text")
            try:
                clean = re.sub(r"[*`]", "", text)
                await q.edit_message_text(clean, parse_mode=None, reply_markup=reply_markup)
                return
            except BadRequest as e2:
                if "Message is not modified" in str(e2):
                    try:
                        await q.answer("Refreshed")
                    except Exception:
                        pass
                    return
                print(f"[bot] plain text fallback failed: {e2}")
        else:
            print(f"[bot] edit failed: {e}")
    except Exception as e:
        print(f"[bot] unexpected edit error: {e}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Button router: panel, views, key toggles, site switching, reset confirm."""
    q = update.callback_query
    if not q:
        return
    if not _is_admin(update):
        await q.answer("⛔ Not authorized.", show_alert=True)
        return
    try:
        await q.answer()
    except Exception:
        pass
    pool = ctx.bot_data["pool"]
    data = q.data or ""
    if data == "noop":
        return  # legacy label button: nothing to do
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        print(f"[bot] button '{data}' from {uid}")
        text, kb = await _route_button(data, pool, ctx, update)
    except Exception as e:
        print(f"[bot] button '{data}' failed: {e}")
        import traceback
        traceback.print_exc()
        text, kb = f"❌ Button error: `{e}`\nTry again or use the typed command.", MAIN_KEYBOARD
    await _safe_edit_message(q, text, kb)


async def _route_button(data: str, pool, ctx, update=None):
    """Returns (text, keyboard). Raises on failure (caller shows it)."""
    if data == "panel":
        return panel_text(pool), MAIN_KEYBOARD
    if data == "stats":
        return "📊 *Stats — pick a website*", await stats_picker_keyboard(pool)
    if data.startswith("statssite:"):
        sid = data.split(":", 1)[1]
        if sid == "ALL":
            return render_stats(pool), stats_keyboard(pool)
        import app as _appmod
        if sid not in [s["id"] for s in _appmod.sites.sites]:
            return "❓ Unknown site.", await stats_picker_keyboard(pool)
        return render_stats(pool, sid), stats_keyboard(pool, sid)
    if data == "health":
        return _make_health(pool), health_keyboard()
    if data == "config":
        return render_config(pool), MAIN_KEYBOARD
    if data == "help":
        return HELP_TEXT, MAIN_KEYBOARD
    if data == "menu":
        ok, info = await sync_menu_now(ctx.bot)
        if ok:
            return (f"✅ Menu synced — `{info}` commands.\n"
                    "Tap `/` to see them (reopen chat if cached)."), MAIN_KEYBOARD
        return f"❌ Menu sync failed: `{info}`", MAIN_KEYBOARD
    if data == "sites":
        return await render_sites(pool), sites_keyboard(pool)
    if data.startswith("site:"):
        import app as _appmod
        sid = _appmod.SiteManager.norm(data.split(":", 1)[1])
        if _appmod.sites.get_site(sid) is None:
            return "❓ Unknown site.", sites_keyboard(pool)
        return await render_site_detail(pool, sid), site_detail_keyboard(sid)
    if data.startswith("models:"):
        parts = data.split(":")
        import app as _appmod
        sid = _appmod.SiteManager.norm(parts[1] if len(parts) > 1 else "")
        if _appmod.sites.get_site(sid) is None:
            return "❓ Unknown site.", sites_keyboard(pool)
        force = len(parts) > 2 and parts[2] == "r"
        return await fetch_site_models(pool, sid, ctx.bot_data, force), models_keyboard(sid)
    if data == "refresh:stats":
        return render_stats(pool), stats_keyboard(pool)
    if data == "refresh:health":
        return _make_health(pool), health_keyboard()
    if data == "refresh:sites":
        return await render_sites(pool), sites_keyboard(pool)
    if data.startswith("tg:"):
        parts = data.split(":")
        num = parts[1] if len(parts) > 1 else ""
        site = parts[2] if len(parts) > 2 else ""
        masked = await pool.set_enabled(num, _key_disabled(pool, f"toggle:{num}"))
        if not masked:
            return "❓ Key not found.", stats_keyboard(pool, site or None)
        if site:
            return render_stats(pool, site), stats_keyboard(pool, site)
        return render_stats(pool), stats_keyboard(pool)
    if data.startswith("toggle:"):
        masked = await pool.set_enabled(data.split(":", 1)[1], _key_disabled(pool, data))
        text = render_stats(pool) if masked else "❓ Key not found."
        return text, stats_keyboard(pool)
    if data.startswith("siteuse:"):
        import app as appmod
        sid = appmod.SiteManager.norm(data.split(":", 1)[1])
        hit = next((s for s in appmod.sites.sites if s["id"] == sid), None)
        if hit is None:
            return "❓ Unknown site.", sites_keyboard(pool)
        nkeys = sum(1 for k in pool.keys if k.site == sid)
        return (f"`{sid}` → `{hit['url']}` ({nkeys} keys)\n"
                f"Fix it: `/siteuse {sid} <new-url>`"), sites_keyboard(pool)
    if data == "reset:ask":
        return "⚠️ Zero *all* token/request counters?", RESET_CONFIRM_KEYBOARD
    if data == "reset:yes":
        await pool.reset_usage()
        ctx.bot_data.pop("ms", None)
        return "♻️ Counters zeroed — sequence restarts at key 1.\n\n" + render_stats(pool), stats_keyboard(pool)
    if data == "reset:no":
        return render_stats(pool), stats_keyboard(pool)
    for _act in ("limit", "reqlimit", "rm"):
        if data == f"guided:{_act}":
            titles = {"limit": "⚙️ Token budget", "reqlimit": "⚙️ Request budget", "rm": "🗑 Remove a key"}
            return titles[_act] + " — pick a website:", await action_site_keyboard(pool, _act)
    for _act in ("limit", "reqlimit", "rm"):
        if data.startswith(f"{_act}site:"):
            sid = data.split(":", 1)[1]
            import app as _appmod
            if sid not in [s["id"] for s in _appmod.sites.sites]:
                return "❓ Unknown site.", await action_site_keyboard(pool, _act)
            nouns = {"limit": "token budget", "reqlimit": "request budget", "rm": "removal"}
            return (f"*{nouns[_act].title()}* — pick a key on `{sid}`:",
                    await action_key_keyboard(pool, _act, sid))
    for _act in ("limit", "reqlimit"):
        if data.startswith(f"{_act}key:"):
            parts = data.split(":")
            num = parts[1] if len(parts) > 2 else ""
            site = parts[2] if len(parts) > 2 else ""
            import time
            i = pool._find_index(num)
            if i is None:
                return "❓ Key not found.", await action_key_keyboard(pool, _act, site)
            ks = pool.keys[i]
            uid = (update.effective_user.id if update is not None and update.effective_user else 0)
            ctx.bot_data.setdefault("pending", {})[uid] = {
                "action": _act, "key": ks.key, "site": site, "ts": time.time()}
            noun = "token budget" if _act == "limit" else "request budget"
            return (f"✏️ Reply with the {noun} number for `{ks.masked}` "
                    f"(`0` or `off` = unlimited), or /cancel.",
                    CANCEL_KEYBOARD)
    if data.startswith("rmkey:"):
        parts = data.split(":")
        num = parts[1] if len(parts) > 2 else ""
        site = parts[2] if len(parts) > 2 else ""
        i = pool._find_index(num)
        if i is None:
            return "❓ Key not found.", await action_key_keyboard(pool, "rm", site)
        ks = pool.keys[i]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"rmyes:{num}:{site}"),
             InlineKeyboardButton("❌ Keep", callback_data=f"rmno:{site}")],
        ])
        return f"⚠️ Delete `{ks.masked}`?", kb
    if data.startswith("rmyes:"):
        parts = data.split(":")
        num = parts[1] if len(parts) > 2 else ""
        site = parts[2] if len(parts) > 2 else ""
        masked = await pool.remove_key(num)
        text = (f"🗑 Removed `{masked}`." if masked else "❓ Key not found.") + "\n\n" + render_stats(pool, site or None)
        return text, await action_key_keyboard(pool, "rm", site) if site else stats_keyboard(pool)
    if data.startswith("rmno:"):
        site = data.split(":", 1)[1]
        return render_stats(pool, site or None), stats_keyboard(pool, site or None)
    if data == "cancel:":
        uid = (update.effective_user.id if update is not None and update.effective_user else 0)
        ctx.bot_data.get("pending", {}).pop(uid, None)
        return "Cancelled.", MAIN_KEYBOARD
    return HELP_TEXT, MAIN_KEYBOARD


def _key_disabled(pool, data: str) -> bool:
    try:
        i = int(data.split(":", 1)[1]) - 1
        return pool.keys[i].disabled
    except Exception:
        return False


def _make_health(pool):
    import app as appmod
    total = len(pool.keys)
    avail = sum(1 for k in pool.keys if k.available)
    used = sum(k.tokens_used for k in pool.keys)
    pend = pool.pending_count()
    store = "ok" if getattr(pool, "store_ok", True) else "⚠️ failing"
    req_used = sum(k.req_used for k in pool.keys)
    req_cap = _req_cap_sum(pool.keys)
    has_req = any(k.eff_req_limit() > 0 for k in pool.keys)

    lines = [
        "❤️ *Proxy Health (V1–V10)*",
        f"🔑 Keys: *{avail}/{total}* usable · 💾 {store}",
        f"📊 Tokens total: {_bar(used, _cap_sum(pool.keys))}",
    ]
    if has_req or req_used > 0:
        lines.append(f"🧾 Requests total: {_bar(req_used, req_cap) if req_cap else f'`{req_used:,}` reqs'}")
    lines.append("")

    for s in appmod.sites.sites:
        keys = [k for k in pool.keys if k.site == s["id"]]
        su = sum(1 for k in keys if k.available)
        s_rcap = _req_cap_sum(keys)
        req_bit = f" · req {_bar(sum(k.req_used for k in keys), s_rcap)}" if s_rcap else ""
        title_bit = f" (`{s['title']}`)" if s.get("title") else ""
        lines.append(
            f"{_site_badge(keys)} `{s['id']}`{title_bit} *{su}/{len(keys)}* · "
            f"{_bar(sum(k.tokens_used for k in keys), _cap_sum(keys))}{req_bit}")
    if pend:
        lines.append(f"\n⚠️ `{pend}` updates unsynced")
    lines.append("\n🛣 `/v1/…`→V1 … `/v10/…`→V10 · isolated routing")
    return "\n".join(lines)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(_make_health(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    site = appmod.SiteManager.norm(ctx.args[0]) if ctx.args else None
    if site and site not in [s["id"] for s in appmod.sites.sites]:
        return await update.message.reply_text("❓ Unknown site (V1–V10). See /sites.")
    await update.message.reply_text(render_stats(ctx.bot_data["pool"], site), parse_mode="Markdown",
                                    reply_markup=stats_keyboard(ctx.bot_data["pool"], site))


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(render_config(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /add <full-api-key> [V1..V10] (site defaults to first)")
    import app as appmod
    default_site = appmod.sites.sites[0]["id"] if appmod.sites.sites else "V1"
    site = appmod.SiteManager.norm(ctx.args[1]) if len(ctx.args) > 1 else default_site
    digits = "".join(filter(str.isdigit, site))
    if digits and 1 <= int(digits) <= appmod.SiteManager.MAX_SITES:
        if site not in [s["id"] for s in appmod.sites.sites]:
            await appmod.sites.add_site(appmod.UPSTREAM_BASE_URL, sid=site)
    else:
        return await update.message.reply_text("❓ Unknown site. Must be V1 through V10. See /sites.")
    pool = ctx.bot_data["pool"]
    ok = await pool.add_key(ctx.args[0], site)
    if ok:
        await update.message.reply_text(f"✅ Key added to `{site}` and saved. Please delete your /add message for safety.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ That key is already in the pool.")


async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("🗑 Remove a key — pick a website first:",
                                               parse_mode="Markdown",
                                               reply_markup=await action_site_keyboard(ctx.bot_data["pool"], "rm"))
    masked = await ctx.bot_data["pool"].remove_key(ctx.args[0])
    await update.message.reply_text(f"🗑 Removed `{masked}`" if masked else "❓ Not found.", parse_mode="Markdown")


async def action_site_keyboard(pool, action: str) -> InlineKeyboardMarkup:
    """Site picker starting a guided flow (limit / reqlimit / rm)."""
    import app as appmod
    rows, row = [], []
    for s in await appmod.sites.list_sites():
        n = sum(1 for k in pool.keys if k.site == s["id"])
        label_title = f" {s['title'][:6]}" if s.get("title") else ""
        row.append(InlineKeyboardButton(f"🌐 {s['id']}{label_title} ({n})",
                                        callback_data=f"{action}site:{s['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([HOME_BUTTON])
    return InlineKeyboardMarkup(rows)


async def action_key_keyboard(pool, action: str, site: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, ks in enumerate(pool.keys, start=1):
        if ks.site != site:
            continue
        icon = "⛔" if ks.disabled else ("❌" if ks.over_budget() else "✅")
        row.append(InlineKeyboardButton(f"{i} {icon} {ks.masked}", callback_data=f"{action}key:{i}:{site}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🌐 Websites", callback_data=f"guided:{action}"),
                 HOME_BUTTON])
    return InlineKeyboardMarkup(rows)


CANCEL_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Cancel", callback_data="cancel:")],
])


def pending_for(ctx, uid):
    return ctx.bot_data.get("pending", {}).get(uid)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    uid = update.effective_user.id if update.effective_user else None
    if uid is not None and ctx.bot_data.get("pending", {}).pop(uid, None) is not None:
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing pending.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Completes a guided limit/reqlimit flow with the typed number."""
    import time
    if not update.message or not update.message.text:
        return
    if not _is_admin(update):
        return
    uid = update.effective_user.id
    pend = pending_for(ctx, uid)
    if not pend:
        return
    if time.time() - pend.get("ts", 0) > PENDING_TTL:
        ctx.bot_data["pending"].pop(uid, None)
        await update.message.reply_text("⌛ Expired — start again with /limit or /reqlimit.")
        return
    raw = update.message.text.strip().replace(",", "").replace(" ", "").lower()
    if raw in ("0", "off", "unlim", "unlimited", "none", "-1"):
        val = -1
    elif raw.isdigit():
        val = int(raw)
    else:
        await update.message.reply_text("❓ Send a number, `0`/`off` for unlimited, or /cancel.", parse_mode="Markdown")
        return
    pool = ctx.bot_data["pool"]
    if pend["action"] == "limit":
        masked = await pool.set_token_limit(pend["key"], val)
        what = "Token budget"
        desc = "`unlimited` (token limit disabled)" if val < 0 else f"`{val:,}` tokens"
    else:
        masked = await pool.set_req_limit(pend["key"], val)
        what = "Request budget"
        desc = "`unlimited` (request limit disabled)" if val < 0 else f"`{val:,}` requests"
    ctx.bot_data["pending"].pop(uid, None)
    if not masked:
        await update.message.reply_text("❓ That key is gone. Start again.")
        return
    await update.message.reply_text(f"⚙️ {what} for `{masked}`: {desc}", parse_mode="Markdown")


async def cmd_enable(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /enable <num>")
    masked = await ctx.bot_data["pool"].set_enabled(ctx.args[0], True)
    await update.message.reply_text(f"✅ Enabled `{masked}`" if masked else "❓ Not found.", parse_mode="Markdown")


async def cmd_disable(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /disable <num>")
    masked = await ctx.bot_data["pool"].set_enabled(ctx.args[0], False)
    await update.message.reply_text(f"🚫 Disabled `{masked}`" if masked else "❓ Not found.", parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await ctx.bot_data["pool"].reset_usage()
    ctx.bot_data.pop("ms", None)  # restart live milestones from zero
    await update.message.reply_text(
        "♻️ All token/request counters zeroed.\n"
        "Key sequence restarts at key 1 on every website.")


async def cmd_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) == 2:
        val_str = ctx.args[1].lower().strip()
        if val_str in ("0", "off", "unlim", "unlimited", "none", "-1"):
            limit_val = -1
            desc = "`unlimited` (token limit disabled)"
        elif val_str.isdigit():
            limit_val = int(val_str)
            desc = f"`{limit_val:,}` tokens"
        else:
            return await update.message.reply_text("Usage: /limit <num> <tokens|0|off>")
        masked = await ctx.bot_data["pool"].set_token_limit(ctx.args[0], limit_val)
        if not masked:
            return await update.message.reply_text("❓ Key not found.")
        await update.message.reply_text(f"⚙️ Token budget for `{masked}`: {desc}", parse_mode="Markdown")
        return
    if len(ctx.args) == 1:
        return await _begin_guided(update, ctx, "limit", ctx.args[0])
    await update.message.reply_text("⚙️ Token budget — pick a website first (or type `/limit <num> <tokens>`):",
                                    parse_mode="Markdown",
                                    reply_markup=await action_site_keyboard(ctx.bot_data["pool"], "limit"))


async def cmd_reqlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) == 2:
        val_str = ctx.args[1].lower().strip()
        if val_str in ("0", "off", "unlim", "unlimited", "none", "-1"):
            limit_val = -1
            desc = "`unlimited` (request limit disabled)"
        elif val_str.isdigit():
            limit_val = int(val_str)
            desc = f"`{limit_val:,}` requests"
        else:
            return await update.message.reply_text("Usage: /reqlimit <num> <n|0|off>")
        pool = ctx.bot_data["pool"]
        masked = await pool.set_req_limit(ctx.args[0], limit_val)
        if not masked:
            return await update.message.reply_text("❓ Key not found.")
        i = pool._find_index(ctx.args[0])
        ks = pool.keys[i] if i is not None else None
        extra_note = ""
        if ks and limit_val > 0 and ks.eff_token_limit() > 0:
            extra_note = (
                f"\n\nℹ️ *Note:* Key also has a token limit of `{_fmt(ks.eff_token_limit())}`. "
                f"Key shifts when *either* limit is reached.\n"
                f"💡 To rotate *purely* after {limit_val} requests, run `/limit {ctx.args[0]} 0` to disable the token cap."
            )
        await update.message.reply_text(f"⚙️ Request budget for `{masked}`: {desc}{extra_note}", parse_mode="Markdown")
        return
    if len(ctx.args) == 1:
        return await _begin_guided(update, ctx, "reqlimit", ctx.args[0])
    await update.message.reply_text("⚙️ Request budget — pick a website first (or type `/reqlimit <num> <n>`):",
                                    parse_mode="Markdown",
                                    reply_markup=await action_site_keyboard(ctx.bot_data["pool"], "reqlimit"))


async def _begin_guided(update, ctx, action: str, ident: str):
    """Jump straight to the value prompt when the key is already known."""
    import time
    pool = ctx.bot_data["pool"]
    i = pool._find_index(ident)
    if i is None:
        await update.message.reply_text("❓ Key not found. See /stats.")
        return
    ks = pool.keys[i]
    uid = update.effective_user.id
    ctx.bot_data.setdefault("pending", {})[uid] = {
        "action": action, "key": ks.key, "site": ks.site, "ts": time.time()}
    noun = "token budget" if action == "limit" else "request budget"
    await update.message.reply_text(
        f"✏️ Reply with the {noun} number for `{ks.masked}` (`0` or `off` = unlimited), or /cancel.",
        parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)


async def cmd_sites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(await render_sites(ctx.bot_data["pool"]),
                                    parse_mode="Markdown",
                                    reply_markup=sites_keyboard(ctx.bot_data["pool"]))


async def cmd_siteadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /siteadd <https-url>  e.g. /siteadd https://api.other.com/v1")
    import app as appmod
    nid = await appmod.sites.add_site(ctx.args[0])
    await update.message.reply_text(f"✅ Website added as `{nid}`" if nid else "❓ Need an http(s) URL.", parse_mode="Markdown")


async def cmd_siteuse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    if len(ctx.args) == 2:
        ok = await appmod.sites.set_url(ctx.args[0], ctx.args[1])
        if not ok:
            return await update.message.reply_text("❓ Invalid site (must be V1–V10) or bad URL (must start with http).", parse_mode="Markdown")
        await update.message.reply_text(
            f"🔗 `{appmod.SiteManager.norm(ctx.args[0])}` URL updated." + appmod.sites.save_status_note(),
            parse_mode="Markdown")
        return
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /siteuse <V1..V10> <new-url> — or one arg to show it")
    sid = appmod.SiteManager.norm(ctx.args[0])
    hit = next((s for s in appmod.sites.sites if s["id"] == sid), None)
    if hit is None:
        return await update.message.reply_text("❓ Unknown site (V1–V10). See /sites.", parse_mode="Markdown")
    nkeys = sum(1 for k in ctx.bot_data["pool"].keys if k.site == sid)
    await update.message.reply_text(
        f"`{sid}` → `{hit['url']}` ({nkeys} keys)\nFix it: `/siteuse {sid} <new-url>`",
        parse_mode="Markdown")


async def cmd_sitename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /sitename <V1..V10> <Title>  e.g. /sitename V1 OpenRouter")
    import app as appmod
    sid = appmod.SiteManager.norm(ctx.args[0])
    digits = "".join(filter(str.isdigit, sid))
    if not digits or not (1 <= int(digits) <= appmod.SiteManager.MAX_SITES):
        return await update.message.reply_text("❓ Invalid site. Must be V1 through V10.")
    title = " ".join(ctx.args[1:]).strip()
    ok = await appmod.sites.set_title(sid, title)
    if not ok:
        return await update.message.reply_text("❓ Failed to set site title.")
    await update.message.reply_text(
        f"🏷 Title for `{sid}` set to *{title}*." + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_models(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    default_site = appmod.sites.sites[0]["id"] if appmod.sites.sites else "V1"
    sid = appmod.SiteManager.norm(ctx.args[0]) if ctx.args else default_site
    if appmod.sites.get_site(sid) is None:
        return await update.message.reply_text("❓ Unknown site (V1–V10). See /sites.")
    text = await fetch_site_models(ctx.bot_data["pool"], sid, ctx.bot_data, force=True)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=models_keyboard(sid))


async def cmd_siteswap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /siteswap <V1> <V2> — exchange URLs, schedules and keys")
    import app as appmod
    sa = appmod.SiteManager.norm(ctx.args[0])
    sb = appmod.SiteManager.norm(ctx.args[1])
    if sa == sb:
        return await update.message.reply_text("❓ Pick two different sites.")
    known = [s["id"] for s in appmod.sites.sites]
    if sa not in known or sb not in known:
        return await update.message.reply_text("❓ Unknown site. See /sites.")
    ok = await appmod.sites.swap_sites(sa, sb)
    if not ok:
        return await update.message.reply_text("❓ Swap failed.")
    pool = ctx.bot_data["pool"]
    moved_ab, moved_ba = await pool.swap_key_sites(sa, sb)
    await update.message.reply_text(
        f"🔀 Swapped `{sa}` ⇄ `{sb}`: URLs, schedules and keys exchanged "
        f"(`{moved_ab}` keys → `{sb}`, `{moved_ba}` keys → `{sa}`)." + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_siterm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /siterm <V1..V10> (can't remove the last one)")
    import app as appmod
    sid = appmod.SiteManager.norm(ctx.args[0])
    if any(ks.site == sid for ks in ctx.bot_data["pool"].keys):
        return await update.message.reply_text("❓ Site still has keys — /rm them first (see /stats).")
    ok = await appmod.sites.remove_site(ctx.args[0])
    await update.message.reply_text("🗑 Removed." if ok else "❓ Can't remove (unknown site or last one).")


async def cmd_sitelimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /sitelimit <V1..V10> <tokens|0|off>  e.g. /sitelimit V1 0 (0 = unlimited)")
    import app as appmod
    val_str = ctx.args[1].lower().strip()
    if val_str in ("0", "off", "unlim", "unlimited", "none", "-1"):
        budget = -1
        desc = "`unlimited` (token limit disabled for this site)"
    elif val_str.isdigit():
        budget = int(val_str)
        desc = f"`{budget:,}` tokens"
    else:
        return await update.message.reply_text("Usage: /sitelimit <V1..V10> <tokens|0|off>")
    ok = await appmod.sites.configure_site(ctx.args[0], token_budget=budget)
    if not ok:
        return await update.message.reply_text("❓ Unknown site (V1–V10).")
    await update.message.reply_text(
        f"⚙️ Token budget on `{appmod.SiteManager.norm(ctx.args[0])}`: {desc}" + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_sitereqlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /sitereqlimit <V1..V10> <n|0|off>  e.g. /sitereqlimit V1 50 (0 = unlimited)")
    import app as appmod
    val_str = ctx.args[1].lower().strip()
    if val_str in ("0", "off", "unlim", "unlimited", "none", "-1"):
        budget = -1
        desc = "`unlimited` (request limit disabled for this site)"
    elif val_str.isdigit():
        budget = int(val_str)
        desc = f"`{budget:,}` requests"
    else:
        return await update.message.reply_text("Usage: /sitereqlimit <V1..V10> <n|0|off>")
    ok = await appmod.sites.configure_site(ctx.args[0], req_budget=budget)
    if not ok:
        return await update.message.reply_text("❓ Unknown site (V1–V10).")
    await update.message.reply_text(
        f"⚙️ Request budget on `{appmod.SiteManager.norm(ctx.args[0])}`: {desc}" + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_sitetimeout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2 or not ctx.args[1].isdigit():
        return await update.message.reply_text("Usage: /sitetimeout <V1..V10> <seconds>  e.g. /sitetimeout V1 90 (0 = global default)")
    import app as appmod
    ok = await appmod.sites.configure_site(ctx.args[0], timeout=int(ctx.args[1]))
    if not ok:
        return await update.message.reply_text("❓ Unknown site (V1–V10).")
    await update.message.reply_text(
        f"⚙️ Timeout on `{appmod.SiteManager.norm(ctx.args[0])}`: `{int(ctx.args[1])}s`" + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_siteheaders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) < 3:
        return await update.message.reply_text("Usage: /siteheaders <V1..V10> <referer> <title…>  (`-` keeps global)")
    import app as appmod
    ok = await appmod.sites.configure_site(ctx.args[0], referer=ctx.args[1], title=" ".join(ctx.args[2:]))
    if not ok:
        return await update.message.reply_text("❓ Unknown site (V1–V10).")
    await update.message.reply_text(
        f"⚙️ Headers on `{appmod.SiteManager.norm(ctx.args[0])}` updated." + appmod.sites.save_status_note(),
        parse_mode="Markdown")


async def cmd_sitereset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /sitereset <V1..V10> <hours>  e.g. /sitereset V1 24 (0 = off)")
    try:
        hours = float(ctx.args[1])
    except ValueError:
        return await update.message.reply_text("❓ Hours must be a number.")
    import app as appmod
    ok = await appmod.sites.set_reset(ctx.args[0], hours)
    if not ok:
        return await update.message.reply_text("❓ Unknown site (V1–V10).")
    msg = f"🔁 `{ctx.args[0].upper()}` auto-resets every `{hours:g}h`." if hours > 0 else \
        f"🔁 Auto-reset off for `{ctx.args[0].upper()}`."
    await update.message.reply_text(msg, parse_mode="Markdown")


def _pct(used: int, limit: int) -> int:
    return min(100, int(used * 100 / limit)) if limit > 0 else 0


async def live_progress(ctx: ContextTypes.DEFAULT_TYPE):
    """Live usage pushes: milestones 50/80/100% per key for BOTH tokens and requests."""
    import time
    import app as appmod
    pool = ctx.bot_data["pool"]
    ms: dict = ctx.bot_data.setdefault("ms", {})
    seeded = "ms_seeded" in ctx.bot_data

    for ks in pool.keys:
        # 1. Token limit milestone tracking
        tlim = ks.eff_token_limit()
        if tlim > 0:
            tpct = _pct(ks.tokens_used, tlim)
            tprev = ms.get(f"token:{ks.masked}")
            if not seeded:
                ms[f"token:{ks.masked}"] = tpct
            elif tpct < (tprev or 0):
                ms[f"token:{ks.masked}"] = tpct
            else:
                thit = [m for m in MILESTONES if (tprev or 0) < m <= tpct]
                ms[f"token:{ks.masked}"] = tpct
                if thit:
                    bar = _bar(ks.tokens_used, tlim)
                    for aid in _admin_ids():
                        try:
                            await ctx.bot.send_message(
                                int(aid),
                                f"📈 🪙 Token limit: `{ks.masked}` hit {thit[-1]}%\n{bar}\n🎯 Shift to next key at 100%",
                                parse_mode="Markdown")
                        except Exception:
                            pass
        else:
            ms.pop(f"token:{ks.masked}", None)

        # 2. Request limit milestone tracking (treated identically to token limit)
        rlim = ks.eff_req_limit()
        if rlim > 0:
            rpct = _pct(ks.req_used, rlim)
            rprev = ms.get(f"req:{ks.masked}")
            if not seeded:
                ms[f"req:{ks.masked}"] = rpct
            elif rpct < (rprev or 0):
                ms[f"req:{ks.masked}"] = rpct
            else:
                rhit = [m for m in MILESTONES if (rprev or 0) < m <= rpct]
                ms[f"req:{ks.masked}"] = rpct
                if rhit:
                    rbar = _bar(ks.req_used, rlim)
                    for aid in _admin_ids():
                        try:
                            await ctx.bot.send_message(
                                int(aid),
                                f"📈 🧾 Request limit: `{ks.masked}` hit {rhit[-1]}%\n{rbar}\n🎯 Shift to next key at 100%",
                                parse_mode="Markdown")
                        except Exception:
                            pass
        else:
            ms.pop(f"req:{ks.masked}", None)

    ctx.bot_data["ms_seeded"] = True
    try:
        every = int(os.getenv("LIVE_DIGEST_MIN", "0"))
    except ValueError:
        every = 0
    if every > 0:
        used = sum(k.tokens_used for k in pool.keys)
        last_t = ctx.bot_data.get("digest_t", 0)
        if time.time() - last_t >= every * 60 and used != ctx.bot_data.get("digest_used"):
            ctx.bot_data["digest_t"] = time.time()
            ctx.bot_data["digest_used"] = used
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(int(aid), f"⏱ *auto update*\n{render_stats(pool)}",
                                               parse_mode="Markdown")
                except Exception:
                    pass


async def watch_keys(ctx: ContextTypes.DEFAULT_TYPE):
    """Background job: alert when a key hits budget / all keys over budget."""
    import time
    pool = ctx.bot_data["pool"]
    prev: dict = ctx.bot_data.setdefault("prev_over", {})
    cur = {k.key: (not k.available) for k in pool.keys}
    if not prev:
        ctx.bot_data["prev_over"] = cur
        return
    for k_key, out in cur.items():
        was = prev.get(k_key, False)
        if out and not was:
            ks = next((k for k in pool.keys if k.key == k_key), None)
            if not ks:
                continue
            num = next((i for i, k in enumerate(pool.keys, 1) if k.key == k_key), "?")
            why = ks.status_reason() if hasattr(ks, "status_reason") else (
                "disabled" if ks.disabled else "budget hit"
            )
            # Check next available key specifically on this site
            next_ks = await pool.next_key(ks.site)
            if next_ks is not None:
                next_num = next((i for i, k in enumerate(pool.keys, 1) if k.key == next_ks.key), "?")
                shift_line = f"🎯 Shifted to next key on `{ks.site}`: `#{next_num}` `{next_ks.masked}`."
            else:
                shift_line = f"🚨 No more usable keys on `{ks.site}`! Use `/add <key> {ks.site}` or `/resetusage`."
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(
                        int(aid),
                        f"⚠️ Key out of rotation: `#{num}` `{ks.masked}` (`{ks.site}`)\nReason: `{why}`\n{shift_line}",
                        parse_mode="Markdown")
                except Exception:
                    try:
                        clean_msg = f"⚠️ Key out of rotation: #{num} {ks.masked} ({ks.site})\nReason: {why}\n{shift_line.replace('`', '')}"
                        await ctx.bot.send_message(int(aid), clean_msg, parse_mode=None)
                    except Exception:
                        pass
        elif was and not out:
            ks = next((k for k in pool.keys if k.key == k_key), None)
            if ks:
                num = next((i for i, k in enumerate(pool.keys, 1) if k.key == k_key), "?")
                for aid in _admin_ids():
                    try:
                        await ctx.bot.send_message(
                            int(aid),
                            f"✅ Key back in rotation: `#{num}` `{ks.masked}` (`{ks.site}`)",
                            parse_mode="Markdown")
                    except Exception:
                        try:
                            await ctx.bot.send_message(
                                int(aid),
                                f"✅ Key back in rotation: #{num} {ks.masked} ({ks.site})",
                                parse_mode=None)
                        except Exception:
                            pass
    if cur and all(cur.values()) and not all(prev.values()):
        for aid in _admin_ids():
            try:
                await ctx.bot.send_message(int(aid), "🚨 ALL keys across all websites are over budget/disabled! /resetusage or /add <key>")
            except Exception:
                pass
    ctx.bot_data["prev_over"] = cur
    ok = getattr(pool, "store_ok", True)
    was_ok = ctx.bot_data.get("store_was_ok", True)
    if was_ok and not ok:
        for aid in _admin_ids():
            try:
                await ctx.bot.send_message(
                    int(aid),
                    f"⚠️ Storage failing — usage may lag (`{pool.pending_count()}` unsynced). Mongo/disk issue?",
                    parse_mode="Markdown")
            except Exception:
                pass
    elif ok and not was_ok:
        for aid in _admin_ids():
            try:
                await ctx.bot.send_message(int(aid), "✅ Storage recovered, counters synced.")
            except Exception:
                pass
    ctx.bot_data["store_was_ok"] = ok


async def sync_menu_now(bot) -> tuple:
    """Register the / menu via setMyCommands. Returns (ok, count_or_error)."""
    import asyncio
    from telegram import BotCommand, BotCommandScopeAllPrivateChats
    commands = [BotCommand(cmd, desc) for cmd, desc in COMMAND_MENU]
    err = "unknown"
    for attempt in range(1, 4):
        try:
            await bot.set_my_commands(commands)
            try:
                await bot.set_my_commands(
                    commands, scope=BotCommandScopeAllPrivateChats())
            except Exception as e:
                print(f"[telegram] private-scope menu failed: {e}")
            check = await bot.get_my_commands()
            print(f"[telegram] menu registered: {len(check)} commands (attempt {attempt})")
            print(f"[telegram] Command list: {[c.command for c in commands]}")
            return True, len(check)
        except Exception as e:
            err = str(e)[:200]
            print(f"[telegram] menu attempt {attempt} failed: {e}")
            await asyncio.sleep(2 * attempt)
    print("[telegram] menu registration failed after retries")
    return False, err


async def _post_init(app: Application):
    """Register the / command menu via setMyCommands (no @BotFather needed)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[telegram] ERROR: TELEGRAM_BOT_TOKEN not set in environment!")
        return
    ok, _ = await sync_menu_now(app.bot)
    if not ok:
        return
    for aid in _admin_ids():
        try:
            await app.bot.send_message(int(aid), "🤖 Proxy bot online — command menu synced.")
        except Exception:
            pass


async def _resync_menu(ctx: ContextTypes.DEFAULT_TYPE):
    """Second-chance menu sync a minute after boot (unstable networks)."""
    try:
        ok, info = await sync_menu_now(ctx.bot)
        if ok:
            print(f"[telegram] menu re-sync ok: {info} commands")
    except Exception as e:
        print(f"[telegram] menu re-sync failed: {e}")


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    ok, info = await sync_menu_now(ctx.bot)
    if ok:
        await update.message.reply_text(
            f"✅ Menu synced — `{info}` commands registered.\n"
            "Tap `/` in this chat to see them. If they don't appear, "
            "fully close + reopen the chat (Telegram caches menus).",
            parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Menu sync failed: `{info}`\nPaste this to support.", parse_mode="Markdown")


def build_bot_app(pool) -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["pool"] = pool
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler(["stats", "keys"], cmd_stats))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler(["rm", "remove", "del"], cmd_rm))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler(["reset", "resetusage"], cmd_reset))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CommandHandler("reqlimit", cmd_reqlimit))
    app.add_handler(CommandHandler("sites", cmd_sites))
    app.add_handler(CommandHandler("siteadd", cmd_siteadd))
    app.add_handler(CommandHandler("sitename", cmd_sitename))
    app.add_handler(CommandHandler("siteuse", cmd_siteuse))
    app.add_handler(CommandHandler("siteswap", cmd_siteswap))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("sitereset", cmd_sitereset))
    app.add_handler(CommandHandler("sitelimit", cmd_sitelimit))
    app.add_handler(CommandHandler("sitereqlimit", cmd_sitereqlimit))
    app.add_handler(CommandHandler("sitetimeout", cmd_sitetimeout))
    app.add_handler(CommandHandler("siteheaders", cmd_siteheaders))
    app.add_handler(CommandHandler("siterm", cmd_siterm))
    if app.job_queue:
        app.job_queue.run_repeating(watch_keys, interval=30, first=10)
        app.job_queue.run_repeating(live_progress, interval=30, first=15)
        app.job_queue.run_once(_resync_menu, when=60)
    return app
