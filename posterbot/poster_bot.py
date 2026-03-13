"""
AskMovies Public Poster Bot — Multi-User Edition
=================================================
Entry point. Registers all handlers and starts the bot.

Environment Variables:
  BOT_TOKEN       = Telegram bot token
  ADMIN_IDS       = Comma-separated admin Telegram user IDs
  TMDB_API_KEY    = Global TMDB API key
  MONGO_URL       = MongoDB connection string
  MONGO_DB_NAME   = MongoDB database name (default: askfiles_public)

File structure:
  config.py          — Env vars, constants, caption defaults
  state.py           — Shared mutable state (posted, pending, stats)
  database.py        — MongoDB CRUD operations
  tmdb.py            — TMDB poster + rating fetching
  parser.py          — Log message parsing, button extraction
  caption.py         — Caption builder, send_post
  helpers.py         — notify_admins, stats, failed queue
  commands_admin.py  — All admin commands
  commands_user.py   — User commands (linked by Telegram ID)
  handlers.py        — Channel post handlers
  poster_bot.py      — Main entry (this file)
"""

import datetime
import signal
import sys

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, MONGO_URL, ADMIN_IDS, log
from database import all_users
from helpers import notify_admins
import state

# ── Import all command handlers ────────────────────────────────
from commands_admin import (
    admin_only,
    start_command, commands_command,
    adduser_command, removeuser_command, listusers_command,
    userinfo_command, toggleuser_command,
    copyuser_command, linkuser_command,
    setlog_command, removelog_command,
    setchannel_command, removechannel_command,
    setfilestore_command, setworker_command,
    setdbchannel_command, settrinitydb_command, setbatchmode_command,
    setheader_command, removeheader_command,
    setjoin_command, removejoin_command,
    setcaption_command, resetcaption_command,
    setnote_command, removenote_command,
    pin_command, setposter_toggle_command, setrating_toggle_command,
    setqualityemoji_command,
    poster_command, rating_command,
    pause_command, resume_command,
    stats_command, failed_command, retry_command,
    notify_command, broadcast_command,
)
from commands_user import (
    myinfo_command, recentposts_command,
    preview_command, repost_command,
    handle_setposter_photo,
)
from handlers import handle_channel_post, handle_edited_post


# ═══════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════
async def on_startup(app):
    state.bot_app = app
    users = await all_users()
    now   = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    await notify_admins(
        f"✅ <b>Public Poster Bot Online</b>\n\n"
        f"🕐 Started: {now} UTC\n"
        f"👥 Active users: {len(users)}\n"
        f"🖼 Poster: {'ON' if state.poster_enabled else 'OFF'} | "
        f"⭐ Rating: {'ON' if state.rating_enabled else 'OFF'}\n\n"
        f"Bot is ready! 🚀"
    )
    log.info("✅ Bot started. Active users: %d", len(users))


async def on_shutdown(app=None):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    await notify_admins(
        f"⚠️ <b>Public Poster Bot Offline</b>\n\n"
        f"🕐 Stopped: {now} UTC\n"
        f"📊 Total posts this session: {state.stats['total']}\n\n"
        f"Restart: <code>systemctl restart public-upload</code>"
    )
    log.info("⚠️ Bot shutting down.")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not MONGO_URL:
        log.warning("⚠️ MONGO_URL not set — user configs will not persist!")
    if not ADMIN_IDS:
        log.warning("⚠️ ADMIN_IDS not set — all commands are unrestricted!")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    state.bot_app = app

    # ── General ───────────────────────────────────────────────
    app.add_handler(CommandHandler("start",           start_command))
    app.add_handler(CommandHandler("commands",        commands_command))

    # ── User Management ───────────────────────────────────────
    app.add_handler(CommandHandler("adduser",         adduser_command))
    app.add_handler(CommandHandler("removeuser",      removeuser_command))
    app.add_handler(CommandHandler("listusers",       listusers_command))
    app.add_handler(CommandHandler("userinfo",        userinfo_command))
    app.add_handler(CommandHandler("toggleuser",      toggleuser_command))
    app.add_handler(CommandHandler("copyuser",        copyuser_command))
    app.add_handler(CommandHandler("linkuser",        linkuser_command))

    # ── User Config ───────────────────────────────────────────
    app.add_handler(CommandHandler("setlog",          setlog_command))
    app.add_handler(CommandHandler("removelog",       removelog_command))
    app.add_handler(CommandHandler("setchannel",      setchannel_command))
    app.add_handler(CommandHandler("removechannel",   removechannel_command))
    app.add_handler(CommandHandler("setfilestore",    setfilestore_command))
    app.add_handler(CommandHandler("setworker",       setworker_command))
    app.add_handler(CommandHandler("setdbchannel",    setdbchannel_command))
    app.add_handler(CommandHandler("settrinitydb",    settrinitydb_command))
    app.add_handler(CommandHandler("setbatchmode",    setbatchmode_command))
    app.add_handler(CommandHandler("setheader",       setheader_command))
    app.add_handler(CommandHandler("removeheader",    removeheader_command))
    app.add_handler(CommandHandler("setjoin",         setjoin_command))
    app.add_handler(CommandHandler("removejoin",      removejoin_command))
    app.add_handler(CommandHandler("setcaption",      setcaption_command))
    app.add_handler(CommandHandler("resetcaption",    resetcaption_command))
    app.add_handler(CommandHandler("setnote",         setnote_command))
    app.add_handler(CommandHandler("removenote",      removenote_command))
    app.add_handler(CommandHandler("pin",             pin_command))
    app.add_handler(CommandHandler("setposter",       setposter_toggle_command))
    app.add_handler(CommandHandler("setrating",       setrating_toggle_command))
    app.add_handler(CommandHandler("setqualityemoji", setqualityemoji_command))

    # ── Bot Control ───────────────────────────────────────────
    app.add_handler(CommandHandler("poster",          poster_command))
    app.add_handler(CommandHandler("rating",          rating_command))
    app.add_handler(CommandHandler("pause",           pause_command))
    app.add_handler(CommandHandler("resume",          resume_command))
    app.add_handler(CommandHandler("broadcast",       broadcast_command))

    # ── Stats & Monitoring ────────────────────────────────────
    app.add_handler(CommandHandler("stats",           stats_command))
    app.add_handler(CommandHandler("failed",          failed_command))
    app.add_handler(CommandHandler("retry",           retry_command))
    app.add_handler(CommandHandler("notify",          notify_command))

    # ── User Commands (linked by Telegram ID) ─────────────────
    app.add_handler(CommandHandler("myinfo",          myinfo_command))
    app.add_handler(CommandHandler("recentposts",     recentposts_command))
    app.add_handler(CommandHandler("preview",         preview_command))
    app.add_handler(CommandHandler("repost",          repost_command))

    # ── Photo handler — /setposter via image ──────────────────
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        handle_setposter_photo,
    ))

    # ── Channel listeners ─────────────────────────────────────
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & ~filters.UpdateType.EDITED,
        handle_channel_post,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.UpdateType.EDITED,
        handle_edited_post,
    ))

    log.info("🤖 AskMovies Public Poster Bot starting (polling)")
    app.run_polling(drop_pending_updates=True)
