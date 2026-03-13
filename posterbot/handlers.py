"""
handlers.py — Telegram channel post and edited post handlers.
"""
import copy
import asyncio

from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from config import log
from database import find_user_by_log_channel
from parser import parse_log_message, extract_button_entry, already_stored, movie_key, ep_num
from caption import build_caption, send_post
from tmdb import fetch_tmdb
from helpers import add_failed, update_stats
import state


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.channel_post
    if not msg:
        return

    channel_id = str(msg.chat.id)
    text       = (msg.text or msg.caption or "").strip()
    if not text:
        return

    user = await find_user_by_log_channel(channel_id)
    if not user:
        return

    parsed = parse_log_message(text)
    if not parsed:
        return

    async with state.state_lock:
        if channel_id not in state.pending:
            state.pending[channel_id] = {}
        state.pending[channel_id][msg.message_id] = parsed
        log.info("⏳ [%s] Pending msg_id=%d → %r", user["_id"], msg.message_id, parsed["title"])


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.edited_channel_post
    if not msg:
        return

    if state.bot_paused:
        return

    channel_id   = str(msg.chat.id)
    text         = (msg.text or msg.caption or "").strip()
    reply_markup = msg.reply_markup

    user = await find_user_by_log_channel(channel_id)
    if not user:
        return

    async with state.state_lock:
        meta = state.pending.get(channel_id, {}).pop(msg.message_id, None)

    if not meta:
        meta = parse_log_message(text)
        if not meta:
            return

    file_entry = extract_button_entry(text, reply_markup, meta)
    if not file_entry:
        return

    title     = meta["title"]
    year      = meta.get("year")
    languages = meta.get("languages", [])
    user_name = user["_id"]

    # Per-user poster/rating toggles
    user_poster_on = user.get("poster_enabled", state.poster_enabled)
    user_rating_on = user.get("rating_enabled", state.rating_enabled)

    if user_poster_on or user_rating_on:
        tmdb_poster, tmdb_rating = await fetch_tmdb(title, year, languages)
    else:
        tmdb_poster, tmdb_rating = None, None

    if not user_rating_on:
        tmdb_rating = None
    if not user_poster_on:
        tmdb_poster = None

    public_channels = user.get("public_channels", [])
    if not public_channels:
        log.warning("User %s has no public channels configured", user_name)
        return

    async def _post_to_channel(target_channel: str):
        mkey          = movie_key(title, year, target_channel)
        ch_file_entry = copy.deepcopy(file_entry)
        user_posted   = state.posted.setdefault(user_name, {})

        log.info("🔑 mkey=%r title=%r ep=%s q=%s files_in_store=%d",
                 mkey, title, ch_file_entry.get("ep"), ch_file_entry.get("quality"),
                 len(user_posted.get(mkey, {}).get("files", [])))

        async with state.state_lock:
            if mkey in user_posted:
                data  = user_posted[mkey]
                ep_no = ep_num(ch_file_entry)
                if already_stored(data["files"], ch_file_entry["file_id"],
                                  ep_no, ch_file_entry["quality"],
                                  ch_file_entry.get("display_name", "")):
                    log.info("⏭ Duplicate for %r user=%s ch=%s", title, user_name, target_channel)
                    return
                data["files"].append(ch_file_entry)
                log.info("➕ Added file to existing post — total files=%d title=%r", len(data["files"]), title)
                caption = await build_caption(data, user)
                try:
                    if data.get("has_photo"):
                        await context.bot.edit_message_caption(
                            chat_id=target_channel, message_id=data["message_id"],
                            caption=caption, parse_mode=ParseMode.HTML,
                        )
                    else:
                        await context.bot.edit_message_text(
                            chat_id=target_channel, message_id=data["message_id"],
                            text=caption, parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    log.info("✏️ Edited post user=%s ch=%s title=%r", user_name, target_channel, title)
                except Exception as exc:
                    log.error("Edit failed user=%s ch=%s: %s", user_name, target_channel, exc)
                    add_failed(user_name, target_channel, caption, tmdb_poster, str(exc))
            else:
                data = {
                    "title":         title,
                    "year":          year,
                    "languages":     languages,
                    "quality_label": meta.get("quality_label", "WEB-DL"),
                    "is_series":     meta.get("is_series", False),
                    "filename":      meta.get("filename", ""),
                    "files":         [ch_file_entry],
                    "tmdb_rating":   tmdb_rating,
                    "message_id":    None,
                    "has_photo":     False,
                }
                caption = await build_caption(data, user)
                try:
                    sent = await send_post(context.bot, target_channel, tmdb_poster, caption, user)
                    data["message_id"] = sent.message_id
                    data["has_photo"]  = bool(tmdb_poster and user.get("poster_enabled", state.poster_enabled))
                    user_posted[mkey]  = data
                    update_stats(user_name)
                    log.info("✅ Posted user=%s ch=%s title=%r", user_name, target_channel, title)
                except Exception as exc:
                    log.error("Post failed user=%s ch=%s: %s", user_name, target_channel, exc)
                    add_failed(user_name, target_channel, caption, tmdb_poster, str(exc))

    log.info("🚀 Posting to %d channel(s) for user=%s", len(public_channels), user_name)
    await asyncio.gather(*[_post_to_channel(ch) for ch in public_channels])
