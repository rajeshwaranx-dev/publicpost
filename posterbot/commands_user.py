"""
commands_user.py — User commands accessible via linked Telegram ID.
"""
import re

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import get_user_by_tg_id
from caption import build_caption, send_post
import state


async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user  = await get_user_by_tg_id(tg_id)
    if not user:
        await update.message.reply_text(
            "❌ Your Telegram ID is not linked to any account.\n\n"
            "Ask admin to run: /linkuser yourname YOUR_ID"
        )
        return
    name     = user["_id"]
    channels = ", ".join(user.get("public_channels", [])) or "None"
    bot      = user.get("filestore_bot", "Not set")
    active   = "🟢 Active" if user.get("active", True) else "🔴 Inactive"
    await update.message.reply_text(
        f"<b>Your Account Info</b>\n\n"
        f"👤 Name: <b>{name}</b>\n"
        f"📡 Status: {active}\n"
        f"🤖 Filestore bot: @{bot}\n"
        f"📢 Channels: {channels}\n"
        f"📌 Auto-pin: {'ON' if user.get('pin_posts') else 'OFF'}\n"
        f"🖼 Poster: {'ON' if user.get('poster_enabled', True) else 'OFF'}\n"
        f"⭐ Rating: {'ON' if user.get('rating_enabled', True) else 'OFF'}",
        parse_mode=ParseMode.HTML,
    )


async def recentposts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user  = await get_user_by_tg_id(tg_id)
    if not user:
        await update.message.reply_text("❌ Your ID is not linked. Ask admin to run /linkuser.")
        return
    name        = user["_id"]
    user_posted = state.posted.get(name, {})
    if not user_posted:
        await update.message.reply_text("No recent posts found.")
        return
    items = list(user_posted.items())[-5:]
    items.reverse()
    lines = []
    for mkey, data in items:
        title   = data.get("title", "?")
        files_n = len(data.get("files", []))
        ch      = mkey.split("__")[-1] if "__" in mkey else "?"
        lines.append(f"🎬 <b>{title}</b> — {files_n} file(s)\n   Channel: <code>{ch}</code>")
    await update.message.reply_text(
        f"<b>Recent Posts ({name})</b>\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user  = await get_user_by_tg_id(tg_id)
    if not user:
        await update.message.reply_text("❌ Your ID is not linked. Ask admin to run /linkuser.")
        return
    sample_data = {
        "title":         "Sample Movie",
        "year":          "2025",
        "quality_label": "WEB-DL",
        "languages":     ["Tamil", "Telugu"],
        "is_series":     False,
        "filename":      "Sample.Movie.2025.WEB-DL.1080p.mkv",
        "files":         [{"link": "https://t.me/bot?start=fs_sample", "quality": "1080p",
                           "display_name": "Sample.Movie.2025.WEB-DL.1080p.mkv", "ep": None, "file_id": "fs_sample"}],
        "tmdb_rating":   "8.5/10",
    }
    caption = await build_caption(sample_data, user)
    await update.message.reply_text(
        f"<b>Caption Preview for {user['_id']}</b>\n\n{caption}",
        parse_mode=ParseMode.HTML,
    )


async def repost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user  = await get_user_by_tg_id(tg_id)
    if not user:
        await update.message.reply_text("❌ Your ID is not linked. Ask admin to run /linkuser.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /repost Movie Title")
        return

    title_query = " ".join(context.args).strip().lower()
    name        = user["_id"]
    user_posted = state.posted.get(name, {})

    matched_key  = None
    matched_data = None
    for mkey, data in user_posted.items():
        if title_query in data.get("title", "").lower():
            matched_key  = mkey
            matched_data = data
            break

    if not matched_data:
        await update.message.reply_text(
            f"❌ No post found for '<b>{title_query}</b>'\n\nUse /recentposts to see available posts.",
            parse_mode=ParseMode.HTML,
        )
        return

    public_channels = user.get("public_channels", [])
    caption         = await build_caption(matched_data, user)
    sent_count      = 0

    for ch in public_channels:
        try:
            sent = await send_post(context.bot, ch, None, caption, user)
            for mkey2, data2 in state.posted.get(name, {}).items():
                if mkey2 == matched_key:
                    data2["message_id"] = sent.message_id
            sent_count += 1
        except Exception as e:
            from config import log
            log.error("Repost failed ch=%s: %s", ch, e)

    await update.message.reply_text(
        f"✅ Reposted <b>{matched_data['title']}</b> to {sent_count} channel(s).",
        parse_mode=ParseMode.HTML,
    )


async def handle_setposter_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User sends photo with caption /setposter Movie Title"""
    msg = update.message
    if not msg or not msg.photo:
        return

    caption_text = (msg.caption or "").strip()
    if not caption_text.lower().startswith("/setposter"):
        return

    tg_id = update.effective_user.id
    user  = await get_user_by_tg_id(tg_id)
    if not user:
        await msg.reply_text("❌ Your ID is not linked. Ask admin to run /linkuser.")
        return

    title_query = re.sub(r"^/setposter\s*", "", caption_text, flags=re.IGNORECASE).strip().lower()
    if not title_query:
        await msg.reply_text("Usage: Send photo with caption /setposter Movie Title")
        return

    name        = user["_id"]
    user_posted = state.posted.get(name, {})

    matched_key  = None
    matched_data = None
    for mkey, data in user_posted.items():
        if title_query in data.get("title", "").lower():
            matched_key  = mkey
            matched_data = data
            break

    if not matched_data:
        await msg.reply_text(
            f"❌ No post found for '<b>{title_query}</b>'\n\nUse /recentposts to see available posts.",
            parse_mode=ParseMode.HTML,
        )
        return

    photo        = msg.photo[-1]
    file_id      = photo.file_id
    public_channels = user.get("public_channels", [])
    post_caption = await build_caption(matched_data, user)
    updated      = 0

    for ch in public_channels:
        post_msg_id = matched_data.get("message_id")
        if not post_msg_id:
            continue
        try:
            if matched_data.get("has_photo"):
                from telegram import InputMediaPhoto
                await context.bot.edit_message_media(
                    chat_id=ch, message_id=post_msg_id,
                    media=InputMediaPhoto(media=file_id, caption=post_caption, parse_mode=ParseMode.HTML),
                )
            else:
                try:
                    await context.bot.delete_message(chat_id=ch, message_id=post_msg_id)
                except Exception:
                    pass
                sent = await context.bot.send_photo(
                    chat_id=ch, photo=file_id,
                    caption=post_caption, parse_mode=ParseMode.HTML,
                )
                matched_data["message_id"] = sent.message_id
                matched_data["has_photo"]  = True
            updated += 1
        except Exception as e:
            from config import log
            log.error("Poster update failed ch=%s: %s", ch, e)

    await msg.reply_text(
        f"✅ Poster updated for <b>{matched_data['title']}</b> in {updated} channel(s).",
        parse_mode=ParseMode.HTML,
    )
