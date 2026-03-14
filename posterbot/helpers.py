"""
helpers.py — Admin notifications, stats tracking, and failed/retry queue.
"""
import asyncio
import datetime

from telegram.constants import ParseMode

from config import ADMIN_IDS, RETRY_DELAYS, log
import state


async def notify_admins(text: str):
    if not state.bot_app or not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await state.bot_app.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            log.warning("DM to admin %s failed: %s", admin_id, exc)


def update_stats(user_name: str):
    state.stats["total"] += 1
    state.stats["by_user"][user_name] = state.stats["by_user"].get(user_name, 0) + 1
    state.post_log.append({
        "user": user_name,
        "ts":   datetime.datetime.utcnow().isoformat(),
    })


def add_failed(user_name: str, channel: str, caption: str,
               poster: str | None, error: str, attempt: int = 0):
    from caption import send_post  # local import to avoid circular
    entry = {
        "user":    user_name,
        "channel": channel,
        "caption": caption,
        "poster":  poster,
        "error":   error,
        "attempt": attempt,
        "ts":      datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    }
    if attempt < len(RETRY_DELAYS):
        delay = RETRY_DELAYS[attempt]
        log.info("🔄 Auto-retry #%d for user=%s in %ds", attempt + 1, user_name, delay)
        asyncio.get_event_loop().create_task(_auto_retry(entry, delay))
    else:
        state.failed_queue.append(entry)
        asyncio.get_event_loop().create_task(
            notify_admins(
                f"❌ <b>Post Failed (all retries exhausted)</b>\n\n"
                f"👤 User: <b>{user_name}</b>\n"
                f"📺 Channel: <code>{channel}</code>\n"
                f"⚠️ Error: <i>{error[:200]}</i>\n"
                f"🕐 Time: {entry['ts']} UTC\n\n"
                f"Use /retry to retry manually."
            )
        )


async def _auto_retry(entry: dict, delay: int):
    from caption import send_post  # local import to avoid circular
    await asyncio.sleep(delay)
    attempt = entry.get("attempt", 0) + 1
    log.info("🔄 Auto-retry attempt %d for user=%s", attempt, entry["user"])
    try:
        if state.bot_app:
            sent = await send_post(
                state.bot_app.bot, entry["channel"],
                entry.get("poster"), entry["caption"]
            )
            if sent:
                log.info("✅ Auto-retry success for user=%s", entry["user"])
                await notify_admins(
                    f"✅ <b>Auto-retry succeeded</b>\n"
                    f"👤 User: <b>{entry['user']}</b>\n"
                    f"📺 Channel: <code>{entry['channel']}</code>\n"
                    f"🔄 Attempt: {attempt}"
                )
                return
    except Exception as exc:
        log.warning("Auto-retry %d failed: %s", attempt, exc)
        entry["error"] = str(exc)
    add_failed(entry["user"], entry["channel"], entry["caption"],
               entry.get("poster"), entry["error"], attempt)
