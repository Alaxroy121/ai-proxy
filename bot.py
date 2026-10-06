"""
Telegram DM control bot for the AI key-rotating proxy.
Polling mode (no webhook/firewall needed). Owner-only via TELEGRAM_ADMIN_IDS.
Shares the same KeyPool object (same process) -> live health + config changes.

Commands (DM the bot):
  /health          - upstream, total/available keys
  /stats | /keys   - per-key table (masked, never full keys)
  /config          - cooldowns, upstream, timeouts
  /add <full-key>  - append a new provider key (persists to keys.txt)
  /rm <n|mask>     - remove key by number (see /stats) or masked id
  /enable <n>      - clear cooldown / re-enable key
  /disable <n>     - manually disable key (24h)
  /reset           - clear all cooldowns
  /setcool <rate_s> <exhausted_s> - e.g. /setcool 60 3600
  /help
Auto-alerts every 30s: key died / all keys dead / recovered.
"""
import os
import time

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


def _stats_lines(pool) -> list[str]:
    lines = []
    for i, ks in enumerate(pool.keys, start=1):
        ok = "✅" if time.time() >= ks.disabled_until else "❌"
        extra = ""
        if ks.disabled_reason:
            left = max(0, int(ks.disabled_until - time.time()))
            extra = f" | {ks.disabled_reason} ({left}s)"
        lines.append(f"{i}. {ok} `{ks.masked}` ok={ks.success} fail={ks.fails}{extra}")
    return lines or ["(no keys)"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 Proxy control online.\n"
        "/health /stats /config\n"
        "/add /rm /enable /disable /reset /setcool\n"
        "Send /help for details."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(
        "Commands:\n"
        "/health – totals + upstream\n"
        "/stats – per-key status (numbers used by rm/enable/disable)\n"
        "/config – cooldowns & timeouts\n"
        "/add <full-key> – add key\n"
        "/rm <num|mask> – remove key\n"
        "/enable <num> – re-enable key\n"
        "/disable <num> – take key offline\n"
        "/reset – clear all cooldowns\n"
        "/setcool <rate_s> <exhausted_s>\n"
        "e.g. /setcool 60 3600",
    )


def _make_health(pool):
    import app as appmod
    total = len(pool.keys)
    avail = sum(1 for k in pool.keys if time.time() >= k.disabled_until)
    return (
        f"❤️ *health*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"keys: {avail}/{total} available"
    )


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    await update.message.reply_text(_make_health(ctx.bot_data["pool"]), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    pool = ctx.bot_data["pool"]
    await update.message.reply_text("🔑 *keys*\n" + "\n".join(_stats_lines(pool)), parse_mode="Markdown")


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    import app as appmod
    await update.message.reply_text(
        "⚙️ *config*\n"
        f"upstream: `{appmod.UPSTREAM_BASE_URL}`\n"
        f"rate-limit cooldown: `{appmod.COOLDOWN_RATE_LIMIT_SEC}s`\n"
        f"exhausted cooldown: `{appmod.COOLDOWN_EXHAUSTED_SEC}s`\n"
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
    await ctx.bot_data["pool"].reset_all()
    await update.message.reply_text("♻️ All cooldowns cleared.")


async def cmd_setcool(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _deny(update)
    if len(ctx.args) != 2 or not all(a.isdigit() for a in ctx.args):
        return await update.message.reply_text("Usage: /setcool <rate_s> <exhausted_s>  e.g. /setcool 60 3600")
    import app as appmod
    r, e = appmod.set_cooldowns(int(ctx.args[0]), int(ctx.args[1]))
    await update.message.reply_text(f"⚙️ Cooldowns updated: rate={r}s exhausted={e}s.\n(Restart resets to .env values.)")


async def watch_keys(ctx: ContextTypes.DEFAULT_TYPE):
    """Background job: alert admins on key down / all-dead / recovery."""
    pool = ctx.bot_data["pool"]
    prev: dict = ctx.bot_data.setdefault("prev_avail", {})
    cur = {k.masked: (time.time() >= k.disabled_until) for k in pool.keys}
    if not prev:
        ctx.bot_data["prev_avail"] = cur
        return
    for masked, ok in cur.items():
        was = prev.get(masked, True)
        if was and not ok:
            reason = next((k.disabled_reason for k in pool.keys if k.masked == masked), "")
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(int(aid), f"⚠️ Key down: `{masked}`\n{reason}", parse_mode="Markdown")
                except Exception:
                    pass
        elif not was and ok:
            for aid in _admin_ids():
                try:
                    await ctx.bot.send_message(int(aid), f"✅ Key recovered: `{masked}`", parse_mode="Markdown")
                except Exception:
                    pass
    if cur and not any(cur.values()) and any(prev.values()):
        for aid in _admin_ids():
            try:
                await ctx.bot.send_message(int(aid), "🚨 ALL keys exhausted! Add keys with /add <key>")
            except Exception:
                pass
    ctx.bot_data["prev_avail"] = cur


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
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setcool", cmd_setcool))
    if app.job_queue:
        app.job_queue.run_repeating(watch_keys, interval=30, first=10)
    return app
