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

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

ADMIN_IDS = {s.strip() for s in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()}


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
        "🤖 Proxy control online. Strict 1→2→3 rotation + token budgets.\n"
        "/health /stats /config\n"
        "/add /rm /enable /disable /reset\n"
        "Send /help for details."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        "Commands:\n"
        "/health – totals + upstream\n"
        "/stats – per-key usage (numbers used by rm/enable/disable)\n"
        "/config – token budget & timeouts\n"
        "/add <full-key> – add key\n"
        "/rm <num|mask> – remove key\n"
        "/enable <num> – re-enable key\n"
        "/disable <num> – take key offline\n"
        "/reset – zero all token counters",
    )


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
    import app as appmod
    pool = ctx.bot_data["pool"]
    total = len(pool.keys)
    used = sum(k.tokens_used for k in pool.keys)
    lim = appmod.MAX_TOKENS_PER_KEY
    overall = _bar(used, lim * total) if lim else f"`{used:,}` (no limit)"
    await update.message.reply_text(
        f"📊 *overall* {overall}\n\n🔑 *per key*\n" + "\n".join(_stats_lines(pool)),
        parse_mode="Markdown")


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    await update.message.reply_text(
        "⚙️ *config*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"mode: strict 1→2→3 rotation (new key every request)\n"
        f"token budget/key: `{appmod.MAX_TOKENS_PER_KEY}` (0 = unlimited)\n"
        f"max retries: `{appmod.MAX_RETRIES_PER_REQUEST}`\n"
        f"timeout: `{appmod.REQUEST_TIMEOUT_SEC}s`\n"
        f"keys file: `{appmod.KEYS_FILE}`",
        parse_mode="Markdown",
    )


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
    await update.message.reply_text("♻️ All token counters zeroed.")


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


def build_bot_app(pool) -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    app = Application.builder().token(token).build()
    app.bot_data["pool"] = pool
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
    return app
