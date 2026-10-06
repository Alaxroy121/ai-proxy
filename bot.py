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
    ("add", "Add a provider key: /add <key>"),
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
    "/add <full-key> – add key\n"
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
    import app as appmod
    lim = appmod.MAX_TOKENS_PER_KEY
    lines = []
    for i, ks in enumerate(pool.keys, start=1):
        if ks.disabled:
            state, extra = "⛔", "disabled"
        elif ks.over_budget():
            state, extra = "❌", "over budget"
        else:
            state, extra = "✅", "in rotation"
        lines.append(f"{i}. {state} `{ks.masked}` {_bar(ks.tokens_used, lim)} ok={ks.success} fail={ks.fails} ({extra})")
    return lines or ["(no keys)"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 *Proxy control panel*\n"
        "Strict 1→2→3 rotation + per-key budgets.\n"
        "📈 Live progress auto-pushes at 50/80/100%.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT)


def render_stats(pool) -> str:
    import app as appmod
    total = len(pool.keys)
    used = sum(k.tokens_used for k in pool.keys)
    lim = appmod.MAX_TOKENS_PER_KEY
    overall = _bar(used, lim * total) if lim else f"`{used:,}` (no limit)"
    return f"📊 *overall* {overall}\n\n🔑 *per key*\n" + "\n".join(_stats_lines(pool))


def render_config(pool=None) -> str:
    import app as appmod
    store = pool.store.label if pool is not None and pool.store else "file"
    return (
        "⚙️ *config*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"mode: strict 1→2→3 rotation (new key every request)\n"
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
    lim = appmod.MAX_TOKENS_PER_KEY
    budget = _bar(used, lim * total) if lim else f"{used:,} (no limit)"
    return (
        f"❤️ *health*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"keys: {avail}/{total} in rotation\n"
        f"tokens total: {budget}"
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
    await update.message.reply_text("♻️ All token counters zeroed.")


def _pct(used: int, limit: int) -> int:
    return min(100, int(used * 100 / limit)) if limit > 0 else 0


async def live_progress(ctx: ContextTypes.DEFAULT_TYPE):
    """Live usage pushes: milestones 50/80/100% per key + optional digest."""
    import time
    import app as appmod
    pool = ctx.bot_data["pool"]
    lim = appmod.MAX_TOKENS_PER_KEY
    if lim <= 0:
        return
    ms: dict = ctx.bot_data.setdefault("ms", {})
    seeded = "ms_seeded" in ctx.bot_data
    for ks in pool.keys:
        pct = _pct(ks.tokens_used, lim)
        prev = ms.get(ks.masked)
        if not seeded:
            ms[ks.masked] = pct  # silent baseline, no spam after restart
            continue
        hit = [m for m in MILESTONES if (prev or 0) < m <= pct]
        ms[ks.masked] = pct
        if hit:
            bar = _bar(ks.tokens_used, lim)
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


async def _post_init(app: Application):
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMAND_MENU])
    except Exception as e:
        print(f"[telegram] menu set failed: {e}")


def build_bot_app(pool) -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
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
    if app.job_queue:
        app.job_queue.run_repeating(watch_keys, interval=30, first=10)
        app.job_queue.run_repeating(live_progress, interval=30, first=15)
    return app
