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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

ADMIN_IDS = {s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()}
MILESTONES = (50, 80, 100)  # live progress pushes when a key crosses these %

COMMAND_MENU = [
    ("start", "Control panel with buttons"),
    ("health", "Upstream + overall usage"),
    ("stats", "Live per-key usage bars"),
    ("config", "Budget, store, timeouts"),
    ("sites", "List upstream websites V1, V2…"),
    ("siteadd", "Add website: /siteadd <url>"),
    ("siteuse", "Switch website: /siteuse <V1>"),
    ("siterm", "Remove website: /siterm <V1>"),
    ("add", "Add a provider key: /add <key>"),
    ("limit", "Token budget per key: /limit <num> <n>"),
    ("reqlimit", "Request budget per key: /reqlimit <num> <n>"),
    ("rm", "Remove a key: /rm <num>"),
    ("enable", "Re-enable a key: /enable <num>"),
    ("disable", "Take a key offline: /disable <num>"),
    ("reset", "Zero all token counters"),
    ("help", "All commands"),
]

HELP_TEXT = (
    "Commands:\n"
    "/health – totals + upstream\n"
    "/stats – live overall + per-key bars\n"
    "/config – budget, store & timeouts\n"
    "/sites – list websites (V1, V2…)\n"
    "/siteadd <url> – add website\n"
    "/siteuse <V1> – switch active website\n"
    "/siterm <V1> – remove website\n"
    "/add <full-key> – add key\n"
    "/limit <num> <tokens> – token budget for one key (0 = global)\n"
    "/reqlimit <num> <n> – request budget for one key (0 = unlimited)\n"
    "/rm <num|mask> – remove key\n"
    "/enable <num> – re-enable key\n"
    "/disable <num> – take key offline\n"
    "/reset – zero all token counters\n"
    "📈 Live: auto-push at 50/80/100% per key."
)

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Stats", callback_data="stats"),
     InlineKeyboardButton("❤️ Health", callback_data="health")],
    [InlineKeyboardButton("⚙️ Config", callback_data="config"),
     InlineKeyboardButton("ℹ️ Help", callback_data="help")],
])


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


def _bar(used: int, limit: int, width: int = 12) -> str:
    """Unique progress style: ▰ filled, ▱ empty, clamped, with %."""
    if limit <= 0:
        return f"`{used:,}` (no limit)"
    pct = min(100, int(used * 100 / limit))
    fill = min(width, int(used * width / limit))
    return f"`{'▰' * fill}{'▱' * (width - fill)}` {pct}% ({used:,}/{limit:,})"


def _stats_lines(pool) -> list[str]:
    lines = []
    for i, ks in enumerate(pool.keys, start=1):
        if ks.disabled:
            state, extra = "⛔", "disabled"
        elif ks.over_budget():
            state, extra = "❌", "over budget"
        else:
            state, extra = "✅", "in rotation"
        bits = f"{_bar(ks.tokens_used, ks.eff_token_limit())} ok={ks.success} fail={ks.fails}"
        if ks.cached_tokens:
            bits += f" ({ks.cached_tokens:,} cached)"
        if ks.token_limit:
            bits += f" [lim {ks.token_limit:,}]"
        if ks.req_limit:
            bits += f" [req {ks.req_used:,}/{ks.req_limit:,}]"
        elif ks.req_used:
            bits += f" [req {ks.req_used:,}]"
        lines.append(f"{i}. {state} `{ks.masked}` {bits} ({extra})")
    pend = pool.pending_count()
    if pend:
        lines.append(f"⚠️ `{pend}` updates not yet saved to store")
    return lines or ["(no keys)"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 *Proxy control panel*\n"
        "Fill-then-shift: one key until its budget is full.\n"
        "📈 Live progress auto-pushes at 50/80/100%.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT)


def render_stats(pool) -> str:
    total = len(pool.keys)
    used = sum(k.tokens_used for k in pool.keys)
    cap = sum(k.eff_token_limit() for k in pool.keys)
    overall = _bar(used, cap) if cap else f"`{used:,}` (no limit)"
    return f"📊 *overall* {overall}\n\n🔑 *per key*\n" + "\n".join(_stats_lines(pool))


def render_config(pool=None) -> str:
    import app as appmod
    store = pool.store.label if pool is not None and pool.store else "file"
    return (
        "⚙️ *config*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"mode: fill key 1 to budget, then shift to key 2, …\n"
        f"token budget/key: `{appmod.MAX_TOKENS_PER_KEY}` (0 = unlimited)\n"
        f"live milestones: `50/80/100%`\n"
        f"auto digest: `{os.getenv('LIVE_DIGEST_MIN', '0')} min (0 = off)`\n"
        f"store: `{store}`\n"
        f"max retries: `{appmod.MAX_RETRIES_PER_REQUEST}`\n"
        f"timeout: `{appmod.REQUEST_TIMEOUT_SEC}s`\n"
        f"keys file: `{appmod.KEYS_FILE}`"
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline buttons under /start (professional bot UX)."""
    q = update.callback_query
    if not q or not _is_admin(update):
        if q:
            await q.answer("⛔ Not authorized.", show_alert=True)
        return
    await q.answer()
    pool = ctx.bot_data["pool"]
    if q.data == "stats":
        text = render_stats(pool)
    elif q.data == "health":
        text = _make_health(pool)
    elif q.data == "config":
        text = render_config(pool)
    else:
        text = HELP_TEXT
    try:
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception:
        pass


def _make_health(pool):
    import app as appmod
    total = len(pool.keys)
    avail = sum(1 for k in pool.keys if k.available)
    used = sum(k.tokens_used for k in pool.keys)
    cap = sum(k.eff_token_limit() for k in pool.keys)
    budget = _bar(used, cap) if cap else f"`{used:,}` (no limit)"
    pend = pool.pending_count()
    extra = f"\n⚠️ `{pend}` updates unsynced" if pend else ""
    return (
        f"❤️ *health*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"keys: {avail}/{total} in rotation\n"
        f"tokens total: {budget}{extra}"
    )


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(_make_health(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(render_stats(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(render_config(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /add <full-api-key>")
    pool = ctx.bot_data["pool"]
    ok = await pool.add_key(ctx.args[0])
    if ok:
        await update.message.reply_text("✅ Key added and saved to keys.txt. Please delete your /add message for safety.")
    else:
        await update.message.reply_text("⚠️ That key is already in the pool.")


async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /rm <num|masked> (see /stats)")
    masked = await ctx.bot_data["pool"].remove_key(ctx.args[0])
    await update.message.reply_text(f"🗑 Removed `{masked}`" if masked else "❓ Not found.", parse_mode="Markdown")


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
    await update.message.reply_text("♻️ All token/request counters zeroed.")


async def cmd_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2 or not ctx.args[1].isdigit():
        return await update.message.reply_text("Usage: /limit <num> <tokens>  e.g. /limit 1 50000 (0 = global)")
    masked = await ctx.bot_data["pool"].set_token_limit(ctx.args[0], int(ctx.args[1]))
    await update.message.reply_text(f"⚙️ Token budget for `{masked}`: `{int(ctx.args[1]):,}`" if masked else "❓ Not found.", parse_mode="Markdown")


async def cmd_reqlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2 or not ctx.args[1].isdigit():
        return await update.message.reply_text("Usage: /reqlimit <num> <requests>  e.g. /reqlimit 1 500 (0 = unlimited)")
    masked = await ctx.bot_data["pool"].set_req_limit(ctx.args[0], int(ctx.args[1]))
    await update.message.reply_text(f"⚙️ Request budget for `{masked}`: `{int(ctx.args[1]):,}`" if masked else "❓ Not found.", parse_mode="Markdown")


async def cmd_sites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    lines = []
    for s in await appmod.sites.list_sites():
        mark = "🟢 active" if s.get("active") else "⚪"
        lines.append(f"`{s['id']}` {mark} `{s['url']}`")
    await update.message.reply_text("🌐 *websites*\n" + "\n".join(lines or ["(none)"]), parse_mode="Markdown")


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
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /siteuse <V1> (see /sites)")
    import app as appmod
    ok = await appmod.sites.use_site(ctx.args[0])
    await update.message.reply_text(f"🔀 Now serving `{ctx.args[0].upper()}`" if ok else "❓ Unknown site.", parse_mode="Markdown")


async def cmd_siterm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /siterm <V1> (not the active one)")
    import app as appmod
    ok = await appmod.sites.remove_site(ctx.args[0])
    await update.message.reply_text("🗑 Removed." if ok else "❓ Can't remove (unknown, active, or last).")


def _pct(used: int, limit: int) -> int:
    return min(100, int(used * 100 / limit)) if limit > 0 else 0


async def live_progress(ctx: ContextTypes.DEFAULT_TYPE):
    """Live usage pushes: milestones 50/80/100% per key + optional digest."""
    import time
    import app as appmod
    pool = ctx.bot_data["pool"]
    ms: dict = ctx.bot_data.setdefault("ms", {})
    seeded = "ms_seeded" in ctx.bot_data
    for ks in pool.keys:
        lim = ks.eff_token_limit()
        if lim <= 0:
            ms.pop(ks.masked, None)
            continue
        pct = _pct(ks.tokens_used, lim)
        prev = ms.get(ks.masked)
        if not seeded:
            ms[ks.masked] = pct  # silent baseline, no spam after restart
            continue
        hit = [m for m in MILESTONES if (prev or 0) < m <= pct]
        ms[ks.masked] = pct
        if hit:
            bar = _bar(ks.tokens_used, ks.eff_token_limit())
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(
                        int(aid),
                        f"📈 `{ks.masked}` hit {hit[-1]}%\n{bar}",
                        parse_mode="Markdown")
                except Exception:
                    pass
    ctx.bot_data["ms_seeded"] = True
    # optional periodic digest (LIVE_DIGEST_MIN=0 disables)
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
    pool = ctx.bot_data["pool"]
    prev: dict = ctx.bot_data.setdefault("prev_over", {})
    cur = {k.masked: (k.over_budget() or k.disabled) for k in pool.keys}
    if not prev:
        ctx.bot_data["prev_over"] = cur
        return
    for masked, out in cur.items():
        was = prev.get(masked, False)
        if out and not was:
            ks = next((k for k in pool.keys if k.masked == masked), None)
            why = "disabled" if ks and ks.disabled else f"budget hit ({ks.tokens_used:,} tokens)"
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(int(aid), f"⚠️ Key out: `{masked}`\n{why}", parse_mode="Markdown")
                except Exception:
                    pass
        elif was and not out:
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(int(aid), f"✅ Key back in rotation: `{masked}`", parse_mode="Markdown")
                except Exception:
                    pass
    if cur and all(cur.values()) and not all(prev.values()):
        for aid in _admin_ids():
            try:
                await ctx.bot.send_message(int(aid), "🚨 ALL keys over budget/disabled! /resetusage or /add <key>")
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


async def _post_init(app: Application):
    """Initialize bot commands in Telegram menu."""
    from telegram import BotCommand
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    
    if not token:
        print("[telegram] ERROR: TELEGRAM_BOT_TOKEN not set in environment!")
        return
    
    try:
        # Convert COMMAND_MENU to BotCommand objects
        commands = [BotCommand(cmd, desc) for cmd, desc in COMMAND_MENU]
        
        # Set commands for default scope (private chats)
        await app.bot.set_my_commands(commands)
        print(f"[telegram] ✅ Commands registered successfully with Telegram")
        print(f"[telegram] Command list: {[c.command for c in commands]}")
        
    except Exception as e:
        print(f"[telegram] ❌ FAILED to register commands: {e}")
        print(f"[telegram] Token valid: {bool(token)}")
        print(f"[telegram] Make sure bot token is correct and bot can access Telegram API")


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
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CommandHandler("reqlimit", cmd_reqlimit))
    app.add_handler(CommandHandler("sites", cmd_sites))
    app.add_handler(CommandHandler("siteadd", cmd_siteadd))
    app.add_handler(CommandHandler("siteuse", cmd_siteuse))
    app.add_handler(CommandHandler("siterm", cmd_siterm))
    if app.job_queue:
        app.job_queue.run_repeating(watch_keys, interval=30, first=10)
        app.job_queue.run_repeating(live_progress, interval=30, first=15)
    return app
