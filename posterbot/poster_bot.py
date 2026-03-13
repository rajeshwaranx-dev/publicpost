"""
AskMovies Public Poster Bot — Multi-User Edition
=================================================
Admin-managed multi-user bot. Each user has their own log channels,
public channels, filestore bot, worker URL and caption template.

Environment Variables:
  BOT_TOKEN       = Telegram bot token
  ADMIN_IDS       = Comma-separated admin Telegram user IDs
  TMDB_API_KEY    = Global TMDB API key (shared across all users)
  MONGO_URL       = MongoDB connection string
  MONGO_DB_NAME   = MongoDB database name (default: askfiles_public)

User Config (stored in MongoDB per user):
  name            = Identifier for this user (e.g. "john")
  log_channels    = Up to 2 log channel IDs
  public_channels = Up to 2 public channel IDs
  filestore_bot   = Filestore bot username (without @)
  worker_url      = Cloudflare worker URL (optional)
  caption         = Custom caption template (optional)
  active          = True/False
"""

import os
import re
import copy
import base64
import logging
import asyncio
import hashlib
import datetime
import requests

from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "8647213611:AAH1Q6hmWVwnzzwPVJkfZMUOux2773_x1gs")
TMDB_API_KEY  = os.environ.get("TMDB_API_KEY", "992a90609f7400986165a20903ab9006")
MONGO_URL     = os.environ.get("MONGO_URL", "mongodb+srv://bharath:bharathx@cluster0.7zcyu4q.mongodb.net/?appName=Cluster0")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "bharath")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

# ── Global app reference ──────────────────────────────────────
bot_app = None

# ── Feature toggles (global) ──────────────────────────────────
poster_enabled: bool = True
rating_enabled: bool = True
bot_paused:     bool = False

# ── Retry config ──────────────────────────────────────────────
RETRY_DELAYS = [30, 120, 300]   # 30s → 2min → 5min

# ── In-memory state ───────────────────────────────────────────
# pending[log_channel_id][msg_id] = parsed meta
pending:    dict[str, dict] = {}
# posted[user_name][movie_key]    = post data
posted:     dict[str, dict] = {}
state_lock = asyncio.Lock()

# ── Failed queue ──────────────────────────────────────────────
failed_queue: list[dict] = []

# ── Stats ─────────────────────────────────────────────────────
stats: dict = {
    "total":      0,
    "by_user":    {},
    "started_at": datetime.datetime.utcnow().isoformat(),
}
post_log: list[dict] = []

# ── Constants ─────────────────────────────────────────────────
QUALITY_ORDER = {
    "240p": 1, "360p": 2, "480p": 3,
    "720p": 4, "1080p": 5, "4K": 6, "2160p": 7,
}
LANG_MAP = {
    "tam": "Tamil",   "tel": "Telugu",   "hin": "Hindi",
    "eng": "English", "mal": "Malayalam","kan": "Kannada",
    "mar": "Marathi", "ben": "Bengali",
}
QUALITY_RE = re.compile(r"\b(240p|360p|480p|720p|1080p|2160p|4K)\b", re.IGNORECASE)
SOURCE_RE  = re.compile(
    r"\b(TRUE WEB-DL|WEB-DL|HQ HDRip|HDRip|BluRay|WEBRip|HDCAM|HQ|CAMRip)\b",
    re.IGNORECASE,
)
EP_RE      = re.compile(r"\bS\d{1,2}E(\d{1,3})\b|\bEP?\s*(\d{1,3})\b", re.IGNORECASE)

# ── Default caption ───────────────────────────────────────────
DEFAULT_CAPTION = (
    "{header}\n"
    "🎬 <b>Title: {title}</b>\n"
    "📅 <b>Year : {year}</b>{season}\n"
    "🎞 <b>Quality: {quality}</b>\n"
    "🎧 <b>Audio: {audio}</b>\n"
    "{rating}"
    "\n<b>🔺Telegram File🔻</b>\n"
    "{files}\n\n"
    "<b>{batch}</b>\n\n"
    "<b>Note 💢: If link not working, copy and paste in browser.</b>\n\n"
    "{join}"
)

DEFAULT_JOIN   = "<b>❤️Join » @{filestore_bot}</b>"
DEFAULT_HEADER = '<a href="https://t.me/{filestore_bot}"><b>AskMovies</b></a>'

# ═══════════════════════════════════════════════════════════
# MONGODB
# ═══════════════════════════════════════════════════════════
_mongo_client = None
_db           = None


def get_db():
    global _mongo_client, _db
    if _db is None and MONGO_URL:
        import motor.motor_asyncio as _motor
        _mongo_client = _motor.AsyncIOMotorClient(MONGO_URL)
        _db = _mongo_client[MONGO_DB_NAME]
    return _db


def get_col(name: str):
    db = get_db()
    return db[name] if db is not None else None


async def load_user(name: str) -> dict | None:
    col = get_col("users")
    if col is None:
        return None
    return await col.find_one({"_id": name.lower()})


async def save_user(user: dict):
    col = get_col("users")
    if col is None:
        return
    await col.update_one(
        {"_id": user["_id"]},
        {"$set": user},
        upsert=True,
    )


async def delete_user(name: str):
    col = get_col("users")
    if col is None:
        return
    await col.delete_one({"_id": name.lower()})


async def all_users() -> list[dict]:
    col = get_col("users")
    if col is None:
        return []
    return await col.find({"active": True}).to_list(length=100)


async def find_user_by_log_channel(channel_id: str) -> dict | None:
    """Find the user whose log_channels contains channel_id."""
    col = get_col("users")
    if col is None:
        return None
    return await col.find_one({"log_channels": channel_id, "active": True})


# ═══════════════════════════════════════════════════════════
# ADMIN GUARD
# ═══════════════════════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin(user.id):
            await update.message.reply_text("⛔ You are not authorized.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ═══════════════════════════════════════════════════════════
# ADMIN NOTIFICATIONS
# ═══════════════════════════════════════════════════════════
async def notify_admins(text: str):
    if not bot_app or not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot_app.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            log.warning("DM to admin %s failed: %s", admin_id, exc)


# ═══════════════════════════════════════════════════════════
# TMDB
# ═══════════════════════════════════════════════════════════
LANG_TMDB_CODE = {
    "tamil": "ta", "telugu": "te", "hindi": "hi",
    "malayalam": "ml", "kannada": "kn", "english": "en",
    "bengali": "bn", "marathi": "mr",
}


def _title_similarity(a: str, b: str) -> float:
    a_w = set(re.sub(r"[^\w\s]", "", a.lower()).split())
    b_w = set(re.sub(r"[^\w\s]", "", b.lower()).split())
    if not a_w:
        return 0.0
    return len(a_w & b_w) / len(a_w)


def _fetch_tmdb_sync(title: str, year: int | None, languages: list) -> tuple[str | None, str | None]:
    """Returns (poster_url, rating_str). Runs in executor."""
    if not TMDB_API_KEY:
        return None, None

    lang_codes = []
    for lang in languages:
        code = LANG_TMDB_CODE.get(lang.lower())
        if code and code not in lang_codes:
            lang_codes.append(code)
    if not lang_codes:
        lang_codes = [None]

    session = requests.Session()
    try:
        for endpoint in ("movie", "tv"):
            best_poster = None
            best_rating = None
            best_score  = 0.0

            for lang_code in (lang_codes + [None]):
                params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
                if year:
                    params["year"] = year
                if lang_code:
                    params["with_original_language"] = lang_code

                r = session.get(
                    f"https://api.themoviedb.org/3/search/{endpoint}",
                    params=params, timeout=8,
                )
                r.raise_for_status()
                results = r.json().get("results", [])

                for result in results[:5]:
                    result_title = result.get("title") or result.get("name") or ""
                    date_str     = result.get("release_date") or result.get("first_air_date") or ""
                    result_year  = int(date_str[:4]) if date_str else None

                    if year and result_year and abs(result_year - year) > 1:
                        continue

                    sim        = _title_similarity(title, result_title)
                    lang_boost = 0.3 if (lang_code and result.get("original_language") == lang_code) else 0.0
                    score      = sim + lang_boost

                    if score > best_score:
                        best_score  = score
                        if result.get("poster_path"):
                            best_poster = result["poster_path"]
                        vote = result.get("vote_average")
                        if vote and float(vote) > 0:
                            best_rating = f"⭐ {float(vote):.1f}/10"

                if best_score >= 0.4:
                    break

            if best_score >= 0.4:
                poster_url = f"https://image.tmdb.org/t/p/w500{best_poster}" if best_poster else None
                log.info("✅ TMDB: %r score=%.2f poster=%s rating=%s",
                         title, best_score, bool(poster_url), best_rating)
                return poster_url, best_rating

    except Exception as exc:
        log.warning("TMDB error for %r: %s", title, exc)
    finally:
        session.close()
    return None, None


async def fetch_tmdb(title: str, year: int | None, languages: list) -> tuple[str | None, str | None]:
    """Non-blocking TMDB fetch in executor."""
    if not TMDB_API_KEY:
        return None, None
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_tmdb_sync, title, year, languages)


# ═══════════════════════════════════════════════════════════
# TEXT PARSING
# ═══════════════════════════════════════════════════════════
ASK_TAG_RE   = re.compile(r"^(\s*\[[A-Z|\s]{1,10}\]\s*)+", re.IGNORECASE)
AT_PREFIX_RE = re.compile(r"^@\S+\s*[-_]?\s*", re.IGNORECASE)
YEAR_RE    = re.compile(r"\((\d{4})\)|\b(20\d{2})\b")
SPLIT_PAT  = re.compile(
    r"\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|480p|720p|1080p|4K|HQ|CAMRip|TRUE)\b",
    re.IGNORECASE,
)


def clean_line(raw: str) -> str:
    raw = ASK_TAG_RE.sub("", raw)
    raw = AT_PREFIX_RE.sub("", raw)   # strip @username prefix e.g. @indian_tha -
    raw = re.sub(r"[^\x00-\u024F\s()\[\]\-_+.]+", "", raw)
    return raw.strip()


def extract_title_year(text: str) -> tuple[str, int | None]:
    year: int | None = None
    m = YEAR_RE.search(text)
    if m:
        year      = int(m.group(1) or m.group(2))
        title_raw = text[:m.start()]
    else:
        title_raw = SPLIT_PAT.split(text)[0]

    title = re.sub(r"[_\-]+", " ", title_raw)
    title = re.sub(r"\.(mkv|mp4|avi)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*S\d{1,2}E?\d*\s*", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def quality_from_text(text: str) -> str:
    m = QUALITY_RE.search(text)
    return m.group(1) if m else "HD"


def parse_log_message(text: str) -> dict | None:
    if not text or not text.strip():
        return None

    lines      = text.strip().splitlines()
    first_line = clean_line(lines[0])
    title, year = extract_title_year(first_line)

    if not title or len(title) < 2:
        return None

    m             = SOURCE_RE.search(first_line)
    quality_label = m.group(1).upper() if m else "WEB-DL"

    quality = ""
    for line in lines:
        qm = re.search(r"Quality\s*:\s*#?(\S+)", line, re.IGNORECASE)
        if qm:
            quality = qm.group(1).lstrip("#")
            break
    if not quality:
        quality = quality_from_text(first_line)

    languages: list[str] = []
    for line in lines:
        if re.search(r"\blang", line, re.IGNORECASE):
            lang_part = re.sub(r"[Ll]ang[a-z]*\s*:\s*", "", line).strip()
            languages = [lx.strip().lstrip("#") for lx in re.split(r"[,+&/]", lang_part) if lx.strip()]
            break

    if not languages:
        fn_lower = first_line.lower()
        languages = [name for abbr, name in LANG_MAP.items() if abbr in fn_lower]

    is_series = bool(
        re.search(r"\bS\d{1,2}\s*E?P?\d+\b", first_line, re.IGNORECASE) or
        re.search(r"\bEP?\s*\(?\d", first_line, re.IGNORECASE)
    )

    return {
        "title":         title,
        "year":          year,
        "filename":      first_line,
        "quality":       quality,
        "quality_label": quality_label,
        "languages":     languages,
        "is_series":     is_series,
    }


def file_id_from_url(url: str) -> str:
    m = re.search(r"[?&]start=([^&\s]+)", url)
    return m.group(1) if m else url


def ep_num(f: dict) -> int | None:
    m = EP_RE.search(f.get("display_name") or "")
    return int(m.group(1) or m.group(2)) if m else None


GENERIC_RE = re.compile(
    r"^[\W\s]*(get\s+shar|download|click\s+here|open|get\s+file|watch|stream)\b",
    re.IGNORECASE,
)


def extract_button_entry(text: str, reply_markup, meta: dict) -> dict | None:
    if reply_markup and hasattr(reply_markup, "inline_keyboard"):
        for row in reply_markup.inline_keyboard:
            for btn in row:
                url = getattr(btn, "url", None)
                btn_text = (btn.text or "").strip()
                if url and url.startswith("http") and btn_text:
                    label_ascii = "".join(c for c in btn_text if ord(c) < 128).strip()
                    display     = meta.get("filename") or btn_text if GENERIC_RE.match(label_ascii) else btn_text
                    quality     = quality_from_text(display) or meta.get("quality") or "HD"
                    fid         = file_id_from_url(url)
                    entry       = {
                        "display_name": display,
                        "quality":      quality,
                        "link":         url,
                        "file_id":      fid,
                    }
                    ep_m        = EP_RE.search(display)
                    entry["ep"] = int(ep_m.group(1) or ep_m.group(2)) if ep_m else None
                    return entry
    return None


def already_stored(files: list, file_id: str, ep, quality: str, display_name: str = "") -> bool:
    for f in files:
        if f.get("file_id") == file_id:
            return True
        if ep is not None and f.get("ep") == ep and f.get("quality") == quality:
            return True
        if ep is None and display_name and f.get("display_name") == display_name:
            return True
    return False


def movie_key(title: str, year, channel_id: str = "") -> str:
    suffix = re.sub(r"[^a-z0-9]", "", channel_id.lower()) if channel_id else ""
    return re.sub(r"\s+", "_", f"{title}_{year or ''}_{suffix}".lower())


# ═══════════════════════════════════════════════════════════
# CAPTION BUILDER
# ═══════════════════════════════════════════════════════════
def render_caption(template: str, vars: dict) -> str:
    try:
        return template.format(**vars)
    except KeyError as e:
        log.warning("Caption key missing: %s — using default", e)
        return DEFAULT_CAPTION.format(**vars)


async def build_caption(data: dict, user: dict) -> str:
    title         = data["title"]
    year          = data.get("year", "")
    languages     = data.get("languages") or []
    files         = data.get("files") or []
    is_series     = data.get("is_series", False)
    quality_label = data.get("quality_label", "WEB-DL")
    filename      = data.get("filename", "")
    audio_str     = " + ".join(languages) if languages else "Multi"
    filestore_bot = user.get("filestore_bot", "")
    worker_url    = (user.get("worker_url") or "").rstrip("/")

    files_sorted = sorted(files, key=lambda f: (
        f.get("ep") or 999,
        QUALITY_ORDER.get(f.get("quality", ""), 99)
    ))

    # Build file links
    file_parts = []
    for f in files_sorted:
        fid = file_id_from_url(f["link"])
        lnk = f"{worker_url}/?start={fid}" if worker_url else f["link"]
        ep  = f.get("ep")
        q   = f.get("quality") or quality_label or "HD"
        if is_series and ep is not None:
            # Series format: 🌊 EP01 : 480p
            label = f"EP{ep:02d} : {q}"
            file_parts.append(f'🌊 <b><a href="{lnk}">{label}</a></b>')
        else:
            # Movie format: full filename
            label = f.get("display_name") or q
            file_parts.append(f'<b>🔥 <a href="{lnk}">{label}</a></b>')
    file_lines = "\n" + "\n\n".join(file_parts) if file_parts else ""

    # Batch link — group by quality for series, single link for movies
    if is_series and files_sorted:
        # Get unique qualities in order
        seen_q = []
        for f in files_sorted:
            q = f.get("quality") or quality_label or "HD"
            if q not in seen_q:
                seen_q.append(q)
        batch_parts = []
        for q in seen_q:
            # Use worker URL with quality param or first file of this quality
            q_files = [f for f in files_sorted if (f.get("quality") or quality_label) == q]
            if q_files:
                fid  = file_id_from_url(q_files[0]["link"])
                blnk = f"{worker_url}/?start=batch_{fid}" if worker_url else q_files[0]["link"]
                batch_parts.append(f'📦 Get all files for: <a href="{blnk}"><b>{q}</b></a>')
        batch_section = "\n".join(batch_parts) if batch_parts else ""
    else:
        if files_sorted:
            fid        = file_id_from_url(files_sorted[0]["link"])
            batch_link = f"{worker_url}/?start=batch_{fid}" if worker_url else files_sorted[0]["link"]
        else:
            batch_link = worker_url or f"https://t.me/{filestore_bot}"
        batch_section = f'📦 Get all files: <a href="{batch_link}">Click Here</a>'

    # Season line
    season_line = ""
    if is_series:
        sm = re.search(r"S(\d{1,2})", filename, re.IGNORECASE)
        if sm:
            season_line = f"\n💫 <b>Season: {int(sm.group(1))}</b>"

    # Rating
    tmdb_rating = data.get("tmdb_rating")
    rating_line = f"⭐ <b>Rating: {tmdb_rating}</b>\n" if tmdb_rating else ""

    # Build join line — custom or default
    join_raw  = user.get("join_text") or DEFAULT_JOIN
    join_line = join_raw.replace("\n", "\n").format(filestore_bot=filestore_bot)

    # Build header line — custom or default
    header_raw  = user.get("header_text") or DEFAULT_HEADER
    header_line = header_raw.format(filestore_bot=filestore_bot)

    template = user.get("caption") or DEFAULT_CAPTION
    return render_caption(template, {
        "title":         title,
        "year":          year or "N/A",
        "quality":       quality_label,
        "audio":         audio_str,
        "season":        season_line,
        "rating":        rating_line,
        "files":         file_lines,
        "batch":         batch_section,
        "filestore_bot": filestore_bot,
        "join":          join_line,
        "header":        header_line,
    })


# ═══════════════════════════════════════════════════════════
# SEND HELPERS
# ═══════════════════════════════════════════════════════════
async def send_post(bot, channel: str, poster: str | None, caption: str) -> Message | None:
    if poster and poster_enabled:
        try:
            return await bot.send_photo(
                chat_id=channel, photo=poster,
                caption=caption, parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Photo send failed in %s: %s", channel, exc)
    return await bot.send_message(
        chat_id=channel, text=caption,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


# ═══════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════
def update_stats(user_name: str):
    stats["total"] += 1
    stats["by_user"][user_name] = stats["by_user"].get(user_name, 0) + 1
    post_log.append({
        "user": user_name,
        "ts":   datetime.datetime.utcnow().isoformat(),
    })


# ═══════════════════════════════════════════════════════════
# FAILED / RETRY
# ═══════════════════════════════════════════════════════════
def add_failed(user_name: str, channel: str, caption: str,
               poster: str | None, error: str, attempt: int = 0):
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
        failed_queue.append(entry)
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
    await asyncio.sleep(delay)
    attempt = entry.get("attempt", 0) + 1
    log.info("🔄 Auto-retry attempt %d for user=%s", attempt, entry["user"])
    try:
        if bot_app:
            sent = await send_post(
                bot_app.bot, entry["channel"],
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


# ═══════════════════════════════════════════════════════════
# CHANNEL POST HANDLER
# ═══════════════════════════════════════════════════════════
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.channel_post
    if not msg:
        return

    channel_id = str(msg.chat.id)
    text       = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Find user who owns this log channel
    user = await find_user_by_log_channel(channel_id)
    if not user:
        return

    parsed = parse_log_message(text)
    if not parsed:
        return

    async with state_lock:
        if channel_id not in pending:
            pending[channel_id] = {}
        pending[channel_id][msg.message_id] = parsed
        log.info("⏳ [%s] Pending msg_id=%d → %r", user["_id"], msg.message_id, parsed["title"])


# ═══════════════════════════════════════════════════════════
# EDITED POST HANDLER
# ═══════════════════════════════════════════════════════════
async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.edited_channel_post
    if not msg:
        return

    if bot_paused:
        return

    channel_id   = str(msg.chat.id)
    text         = (msg.text or msg.caption or "").strip()
    reply_markup = msg.reply_markup

    # Find user who owns this log channel
    user = await find_user_by_log_channel(channel_id)
    if not user:
        return

    async with state_lock:
        meta = pending.get(channel_id, {}).pop(msg.message_id, None)

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

    # Single non-blocking TMDB call
    if poster_enabled or rating_enabled:
        tmdb_poster, tmdb_rating = await fetch_tmdb(title, year, languages)
    else:
        tmdb_poster, tmdb_rating = None, None

    if not rating_enabled:
        tmdb_rating = None
    if not poster_enabled:
        tmdb_poster = None

    public_channels = user.get("public_channels", [])
    if not public_channels:
        log.warning("User %s has no public channels configured", user_name)
        return

    async def _post_to_channel(target_channel: str):
        mkey          = movie_key(title, year, target_channel)
        ch_file_entry = copy.deepcopy(file_entry)
        user_posted   = posted.setdefault(user_name, {})

        async with state_lock:
            if mkey in user_posted:
                data  = user_posted[mkey]
                ep_no = ep_num(ch_file_entry)
                if already_stored(data["files"], ch_file_entry["file_id"],
                                  ep_no, ch_file_entry["quality"],
                                  ch_file_entry.get("display_name", "")):
                    log.info("⏭ Duplicate for %r user=%s ch=%s", title, user_name, target_channel)
                    return
                data["files"].append(ch_file_entry)
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
                    sent = await send_post(context.bot, target_channel, tmdb_poster, caption)
                    data["message_id"] = sent.message_id
                    data["has_photo"]  = bool(tmdb_poster and poster_enabled)
                    user_posted[mkey]  = data
                    update_stats(user_name)
                    log.info("✅ Posted user=%s ch=%s title=%r", user_name, target_channel, title)
                except Exception as exc:
                    log.error("Post failed user=%s ch=%s: %s", user_name, target_channel, exc)
                    add_failed(user_name, target_channel, caption, tmdb_poster, str(exc))

    # Post to all public channels in parallel
    log.info("🚀 Posting to %d channel(s) for user=%s", len(public_channels), user_name)
    await asyncio.gather(*[_post_to_channel(ch) for ch in public_channels])


# ═══════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════
async def on_startup(app):
    users = await all_users()
    now   = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    await notify_admins(
        f"✅ <b>Public Poster Bot Online</b>\n\n"
        f"🕐 Started: {now} UTC\n"
        f"👥 Active users: {len(users)}\n"
        f"🖼 Poster: {'ON' if poster_enabled else 'OFF'} | "
        f"⭐ Rating: {'ON' if rating_enabled else 'OFF'}\n\n"
        f"Bot is ready!"
    )
    log.info("✅ Bot started. Active users: %d", len(users))


async def on_shutdown(app=None):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    await notify_admins(
        f"⚠️ <b>Public Poster Bot Offline</b>\n\n"
        f"🕐 Stopped: {now} UTC\n"
        f"📊 Total posts this session: {stats['total']}\n\n"
        f"Restart: <code>systemctl restart publicposterbot</code>"
    )


# ═══════════════════════════════════════════════════════════
# COMMANDS — GENERAL
# ═══════════════════════════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm AskMovies Public Poster Bot.\n"
        "Contact admin to get access.\n\n"
        "Powered By ❤️ @Master_xkid",
        disable_web_page_preview=True,
    )


@admin_only
async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📋 <b>All Commands</b>\n\n"

        "<b>👥 User Management</b>\n"
        "/adduser name filestore_bot — Add new user\n"
        "/removeuser name — Delete user completely\n"
        "/listusers — Show all users and config\n"
        "/userinfo name — Show single user details\n"
        "/toggleuser name — Activate / deactivate user\n\n"

        "<b>⚙️ User Config</b>\n"
        "/setlog name -100xxx — Add log channel (max 2)\n"
        "/removelog name -100xxx — Remove log channel\n"
        "/setchannel name -100xxx — Add public channel (max 2)\n"
        "/removechannel name -100xxx — Remove public channel\n"
        "/setfilestore name BotUsername — Set filestore bot\n"
        "/setworker name https://... — Set worker URL\n"
        "/settmdb name apikey — Set per-user TMDB key override\n"
        "/setcaption name template — Set custom caption\n"
        "/resetcaption name — Reset caption to default\n\n"

        "<b>🎛 Bot Control</b>\n"
        "/poster on|off — Toggle TMDB poster globally\n"
        "/rating on|off — Toggle TMDB rating globally\n"
        "/pause — Pause all posting\n"
        "/resume — Resume posting\n\n"

        "<b>📊 Stats & Monitoring</b>\n"
        "/stats — Global posting statistics\n"
        "/failed — List failed posts\n"
        "/retry — Retry all failed posts\n"
        "/notify — Test admin DM\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# COMMANDS — USER MANAGEMENT
# ═══════════════════════════════════════════════════════════
@admin_only
async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adduser name filestore_bot
    /adduser john JohnFilestoreBot
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /adduser name filestore_bot\n"
            "Example: /adduser john JohnFilestoreBot"
        )
        return

    name          = args[0].lower().strip()
    filestore_bot = args[1].strip().lstrip("@")

    existing = await load_user(name)
    if existing:
        await update.message.reply_text(
            f"⚠️ User <b>{name}</b> already exists.\n"
            f"Use /userinfo {name} to see their config.",
            parse_mode=ParseMode.HTML,
        )
        return

    user = {
        "_id":             name,
        "filestore_bot":   filestore_bot,
        "log_channels":    [],
        "public_channels": [],
        "worker_url":      "",
        "caption":         None,
        "active":          True,
        "added_at":        datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    }
    await save_user(user)
    await update.message.reply_text(
        f"✅ User <b>{name}</b> added!\n\n"
        f"📌 Filestore bot: @{filestore_bot}\n\n"
        f"Next steps:\n"
        f"/setlog {name} -100xxx\n"
        f"/setchannel {name} -100xxx",
        parse_mode=ParseMode.HTML,
    )
    log.info("👤 User added: %s", name)


@admin_only
async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeuser name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removeuser name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    await delete_user(name)
    # Clear in-memory state
    posted.pop(name, None)
    await update.message.reply_text(
        f"🗑 User <b>{name}</b> deleted.", parse_mode=ParseMode.HTML
    )
    log.info("🗑 User removed: %s", name)


@admin_only
async def listusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await all_users()
    if not users:
        await update.message.reply_text("No active users yet. Use /adduser to add one.")
        return

    lines = [f"👥 <b>Active Users ({len(users)})</b>\n"]
    for u in users:
        log_chs = ", ".join(f"<code>{c}</code>" for c in u.get("log_channels", []))
        pub_chs = ", ".join(f"<code>{c}</code>" for c in u.get("public_channels", []))
        lines.append(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{u['_id']}</b> {'🟢' if u.get('active') else '🔴'}\n"
            f"📥 Log: {log_chs or 'None'}\n"
            f"📺 Pub: {pub_chs or 'None'}\n"
            f"🤖 Bot: @{u.get('filestore_bot','?')}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /userinfo name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /userinfo name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    log_chs  = "\n".join(f"  • <code>{c}</code>" for c in user.get("log_channels", [])) or "  None"
    pub_chs  = "\n".join(f"  • <code>{c}</code>" for c in user.get("public_channels", [])) or "  None"
    caption   = "Custom ✅" if user.get("caption") else "Default"
    header_text = user.get("header_text")
    header_disp = "Custom ✅" if header_text else "Default (AskMovies)"
    join_text = user.get("join_text")
    join_disp = f"Custom ✅ ({join_text[:40]}...)" if join_text and len(join_text) > 40 else (join_text or "Default")
    worker   = user.get("worker_url") or "None (direct links)"
    posts    = stats["by_user"].get(name, 0)
    status   = "🟢 Active" if user.get("active") else "🔴 Inactive"

    await update.message.reply_text(
        f"👤 <b>User: {name}</b> — {status}\n\n"
        f"🤖 Filestore bot: @{user.get('filestore_bot','?')}\n"
        f"🌐 Worker URL: {worker}\n"
        f"📝 Caption: {caption}\n"
        f"🔝 Header: {header_disp}\n"
        f"🔗 Join text: {join_disp}\n"
        f"📊 Posts this session: {posts}\n"
        f"📅 Added: {user.get('added_at','?')}\n\n"
        f"<b>📥 Log channels:</b>\n{log_chs}\n\n"
        f"<b>📺 Public channels:</b>\n{pub_chs}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def toggleuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /toggleuser name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /toggleuser name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["active"] = not user.get("active", True)
    await save_user(user)
    status = "🟢 Activated" if user["active"] else "🔴 Deactivated"
    await update.message.reply_text(
        f"{status} user <b>{name}</b>.", parse_mode=ParseMode.HTML
    )


# ═══════════════════════════════════════════════════════════
# COMMANDS — USER CONFIG
# ═══════════════════════════════════════════════════════════
@admin_only
async def setlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setlog name -100xxx
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setlog name -100xxx")
        return

    name       = args[0].lower().strip()
    channel_id = args[1].strip()
    user       = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    log_channels = user.get("log_channels", [])
    if channel_id in log_channels:
        await update.message.reply_text(
            f"⚠️ <code>{channel_id}</code> already in {name}'s log channels.",
            parse_mode=ParseMode.HTML,
        )
        return
    if len(log_channels) >= 2:
        await update.message.reply_text(
            f"❌ <b>{name}</b> already has 2 log channels (maximum).\n"
            f"Use /removelog {name} -100xxx to remove one first.",
            parse_mode=ParseMode.HTML,
        )
        return

    log_channels.append(channel_id)
    user["log_channels"] = log_channels
    await save_user(user)
    await update.message.reply_text(
        f"✅ Log channel <code>{channel_id}</code> added to <b>{name}</b>.\n"
        f"Total log channels: {len(log_channels)}/2",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def removelog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removelog name -100xxx
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /removelog name -100xxx")
        return

    name       = args[0].lower().strip()
    channel_id = args[1].strip()
    user       = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    log_channels = user.get("log_channels", [])
    if channel_id not in log_channels:
        await update.message.reply_text(
            f"<code>{channel_id}</code> not in {name}'s log channels.",
            parse_mode=ParseMode.HTML,
        )
        return

    log_channels.remove(channel_id)
    user["log_channels"] = log_channels
    await save_user(user)
    # Clear pending for this channel
    pending.pop(channel_id, None)
    await update.message.reply_text(
        f"🗑 Log channel <code>{channel_id}</code> removed from <b>{name}</b>.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def setchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setchannel name -100xxx
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setchannel name -100xxx")
        return

    name       = args[0].lower().strip()
    channel_id = args[1].strip()
    user       = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    pub_channels = user.get("public_channels", [])
    if channel_id in pub_channels:
        await update.message.reply_text(
            f"⚠️ <code>{channel_id}</code> already in {name}'s public channels.",
            parse_mode=ParseMode.HTML,
        )
        return
    if len(pub_channels) >= 3:
        await update.message.reply_text(
            f"❌ <b>{name}</b> already has 3 public channels (maximum).\n"
            f"Use /removechannel {name} -100xxx to remove one first.",
            parse_mode=ParseMode.HTML,
        )
        return

    pub_channels.append(channel_id)
    user["public_channels"] = pub_channels
    await save_user(user)
    await update.message.reply_text(
        f"✅ Public channel <code>{channel_id}</code> added to <b>{name}</b>.\n"
        f"Total public channels: {len(pub_channels)}/3",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def removechannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removechannel name -100xxx
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /removechannel name -100xxx")
        return

    name       = args[0].lower().strip()
    channel_id = args[1].strip()
    user       = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    pub_channels = user.get("public_channels", [])
    if channel_id not in pub_channels:
        await update.message.reply_text(
            f"<code>{channel_id}</code> not in {name}'s public channels.",
            parse_mode=ParseMode.HTML,
        )
        return

    pub_channels.remove(channel_id)
    user["public_channels"] = pub_channels
    await save_user(user)
    await update.message.reply_text(
        f"🗑 Public channel <code>{channel_id}</code> removed from <b>{name}</b>.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def setfilestore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setfilestore name BotUsername
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setfilestore name BotUsername")
        return

    name          = args[0].lower().strip()
    filestore_bot = args[1].strip().lstrip("@")
    user          = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["filestore_bot"] = filestore_bot
    await save_user(user)
    await update.message.reply_text(
        f"✅ Filestore bot set to @{filestore_bot} for <b>{name}</b>.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def setworker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setworker name https://worker.example.com
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setworker name https://...")
        return

    name       = args[0].lower().strip()
    worker_url = args[1].strip()
    user       = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["worker_url"] = worker_url
    await save_user(user)
    await update.message.reply_text(
        f"✅ Worker URL set for <b>{name}</b>:\n<code>{worker_url}</code>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def setcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setcaption name caption template here
    """
    msg  = update.message
    text = msg.text or ""
    body = re.sub(r"^/setcaption\s*", "", text, flags=re.IGNORECASE).strip()

    parts = body.split(None, 1)
    if len(parts) < 2:
        await msg.reply_text(
            "Usage: /setcaption name Your caption template here\n\n"
            "Placeholders: {title} {year} {quality} {audio} {season} {rating} {files} {batch} {filestore_bot}"
        )
        return

    name     = parts[0].lower().strip()
    template = parts[1].strip().replace("\\n", "\n")
    user     = await load_user(name)
    if not user:
        await msg.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["caption"] = template
    await save_user(user)
    await msg.reply_text(
        f"✅ Custom caption set for <b>{name}</b>.", parse_mode=ParseMode.HTML
    )




@admin_only
async def setheader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setheader name Text | https://link
    The | separates display text from URL.
    If no URL given, links to their filestore bot by default.

    Examples:
      /setheader john MyChannel         → links to t.me/{filestore_bot}
      /setheader john MyChannel | https://t.me/MyChannel
      /setheader john <b>AskMovies</b> | https://t.me/Askmovies4
    """
    msg  = update.message
    text = msg.text or ""
    body = re.sub(r"^/setheader\s*", "", text, flags=re.IGNORECASE).strip()

    parts = body.split(None, 1)
    if len(parts) < 2:
        await msg.reply_text(
            "Usage: /setheader name DisplayText | https://link\n\n"
            "Examples:\n"
            "/setheader john AskMovies\n"
            "/setheader john AskMovies | https://t.me/Askmovies4\n"
            "/setheader john <b>My Channel</b> | https://t.me/MyChannel\n\n"
            "Placeholder: {filestore_bot} = their bot username"
        )
        return

    name       = parts[0].lower().strip()
    header_raw = parts[1].strip()
    user       = await load_user(name)
    if not user:
        await msg.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    # Parse text | url format
    if "|" in header_raw:
        display_text, url = [x.strip() for x in header_raw.split("|", 1)]
        header_text = f'''<a href="{url}"><b>{display_text}</b></a>'''
    else:
        # No URL given — link to filestore bot
        header_text = f'''<a href="https://t.me/{{filestore_bot}}"><b>{header_raw}</b></a>'''

    user["header_text"] = header_text
    await save_user(user)
    await msg.reply_text(
        f"✅ Header set for <b>{name}</b>\n\n"
        f"<b>Preview:</b>\n{header_text.format(filestore_bot=user.get('filestore_bot','?'))}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def getheader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /getheader name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /getheader name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    header_text = user.get("header_text")
    if not header_text:
        await update.message.reply_text(
            f"<b>{name}</b> is using default header:\n\n"
            f"<code>{DEFAULT_HEADER}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"<b>{name}</b> custom header:\n\n"
        f"<b>Raw:</b> <code>{header_text}</code>\n\n"
        f"<b>Preview:</b>\n{header_text.format(filestore_bot=user.get('filestore_bot','?'))}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def removeheader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeheader name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removeheader name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["header_text"] = None
    await save_user(user)
    await update.message.reply_text(
        f"✅ Header removed for <b>{name}</b>. Now using default (AskMovies).",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def setjoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setjoin name Join line(s) here
    Supports multiple lines — use \n in message or actual newlines
    Examples:
      /setjoin john ❤️Join » @JohnChannel
      /setjoin john ❤️Join » @JohnChannel\n📢 Updates » @JohnUpdates\n💬 Chat » @JohnChat
    """
    msg  = update.message
    text = msg.text or ""
    body = re.sub(r"^/setjoin\s*", "", text, flags=re.IGNORECASE).strip()

    parts = body.split(None, 1)
    if len(parts) < 2:
        await msg.reply_text(
            "Usage: /setjoin name Your join line(s)\n\n"
            "Single line:\n"
            "/setjoin john ❤️Join » @JohnChannel\n\n"
            "Multiple lines (use \\n):\n"
            "/setjoin john ❤️Join » @JohnChannel\\n📢 Updates » @JohnUpdates\\n💬 Chat » @JohnChat\n\n"
            "Placeholder: {filestore_bot} = their bot username"
        )
        return

    name      = parts[0].lower().strip()
    join_text = parts[1].strip().replace("\\n", "\n")
    user      = await load_user(name)
    if not user:
        await msg.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["join_text"] = join_text
    await save_user(user)

    # Show preview
    preview = join_text.replace("\n", "\n")
    await msg.reply_text(
        f"✅ Join text set for <b>{name}</b>\n\n"
        f"<b>Preview:</b>\n{preview}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def getjoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /getjoin name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /getjoin name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    header_text = user.get("header_text")
    header_disp = "Custom ✅" if header_text else "Default (AskMovies)"
    join_text = user.get("join_text")
    if not join_text:
        await update.message.reply_text(
            f"<b>{name}</b> is using default join text:\n\n"
            f"<code>{DEFAULT_JOIN}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"<b>{name}</b> custom join text:\n\n"
        f"<b>Raw (copy this):</b>\n<code>{join_text.replace(chr(10), chr(92)+'n')}</code>\n\n"
        f"<b>Preview:</b>\n{join_text}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def removejoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removejoin name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removejoin name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["join_text"] = None
    await save_user(user)
    await update.message.reply_text(
        f"✅ Join text removed for <b>{name}</b>. Now using default.",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def resetcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resetcaption name
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /resetcaption name")
        return

    name = args[0].lower().strip()
    user = await load_user(name)
    if not user:
        await update.message.reply_text(f"❌ User <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    user["caption"] = None
    await save_user(user)
    await update.message.reply_text(
        f"✅ Caption reset to default for <b>{name}</b>.", parse_mode=ParseMode.HTML
    )


# ═══════════════════════════════════════════════════════════
# COMMANDS — BOT CONTROL
# ═══════════════════════════════════════════════════════════
@admin_only
async def poster_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global poster_enabled
    args = context.args
    if not args:
        s = "ON 🟢" if poster_enabled else "OFF 🔴"
        await update.message.reply_text(f"Poster is {s}\n\nUsage: /poster on OR /poster off")
        return
    if args[0].lower() == "on":
        poster_enabled = True
        await update.message.reply_text("✅ Poster enabled globally.")
    elif args[0].lower() == "off":
        poster_enabled = False
        await update.message.reply_text("🚫 Poster disabled — text-only posts.")
    else:
        await update.message.reply_text("Usage: /poster on OR /poster off")


@admin_only
async def rating_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global rating_enabled
    args = context.args
    if not args:
        s = "ON 🟢" if rating_enabled else "OFF 🔴"
        await update.message.reply_text(f"Rating is {s}\n\nUsage: /rating on OR /rating off")
        return
    if args[0].lower() == "on":
        rating_enabled = True
        await update.message.reply_text("✅ Rating enabled globally.")
    elif args[0].lower() == "off":
        rating_enabled = False
        await update.message.reply_text("🚫 Rating disabled.")
    else:
        await update.message.reply_text("Usage: /rating on OR /rating off")


@admin_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Bot paused — no posts will be made.")


@admin_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Bot resumed.")


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lines = "\n".join(
        f"  • {u}: {c}" for u, c in sorted(stats["by_user"].items())
    ) or "  None yet"

    await update.message.reply_text(
        f"📊 <b>Global Stats</b>\n\n"
        f"Status: {'⏸ Paused' if bot_paused else '▶️ Running'}\n"
        f"Poster: {'ON 🟢' if poster_enabled else 'OFF 🔴'} | "
        f"Rating: {'ON 🟢' if rating_enabled else 'OFF 🔴'}\n"
        f"Started: {stats['started_at'][:16]} UTC\n\n"
        f"<b>Total posts:</b> {stats['total']}\n\n"
        f"<b>By user:</b>\n{user_lines}\n\n"
        f"<b>Failed queue:</b> {len(failed_queue)} pending",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def failed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not failed_queue:
        await update.message.reply_text("✅ No failed posts.")
        return
    lines = [f"❌ <b>Failed Posts ({len(failed_queue)})</b>\n"]
    for i, f in enumerate(failed_queue[:20], 1):
        lines.append(
            f"{i}. 👤 <b>{f['user']}</b>\n"
            f"   📺 <code>{f['channel']}</code>\n"
            f"   ⚠️ <i>{str(f['error'])[:80]}</i>\n"
            f"   🕐 {f['ts']}"
        )
    lines.append("\n\nUse /retry to retry all.")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not failed_queue:
        await update.message.reply_text("✅ No failed posts to retry.")
        return
    count        = len(failed_queue)
    retried      = 0
    still_failed = []
    await update.message.reply_text(f"🔄 Retrying {count} failed post(s)...")
    for item in failed_queue:
        try:
            sent = await send_post(
                update.get_bot(), item["channel"],
                item.get("poster"), item["caption"]
            )
            if sent:
                retried += 1
        except Exception as exc:
            item["error"] = str(exc)
            still_failed.append(item)
    failed_queue.clear()
    failed_queue.extend(still_failed)
    await update.message.reply_text(
        f"✅ Retried: {retried} succeeded\n❌ Still failed: {len(still_failed)}"
    )


@admin_only
async def notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await notify_admins("🔔 <b>Test notification</b>\n\nAdmin DM is working ✅")
    await update.message.reply_text("✅ Test notification sent.")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not MONGO_URL:
        log.warning("⚠️ MONGO_URL not set — user configs will not persist!")
    if not ADMIN_IDS:
        log.warning("⚠️ ADMIN_IDS not set — all commands are unrestricted!")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    bot_app = app

    # General
    app.add_handler(CommandHandler("start",          start_command))
    app.add_handler(CommandHandler("commands",       commands_command))

    # User management
    app.add_handler(CommandHandler("adduser",        adduser_command))
    app.add_handler(CommandHandler("removeuser",     removeuser_command))
    app.add_handler(CommandHandler("listusers",      listusers_command))
    app.add_handler(CommandHandler("userinfo",       userinfo_command))
    app.add_handler(CommandHandler("toggleuser",     toggleuser_command))

    # User config
    app.add_handler(CommandHandler("setlog",         setlog_command))
    app.add_handler(CommandHandler("removelog",      removelog_command))
    app.add_handler(CommandHandler("setchannel",     setchannel_command))
    app.add_handler(CommandHandler("removechannel",  removechannel_command))
    app.add_handler(CommandHandler("setfilestore",   setfilestore_command))
    app.add_handler(CommandHandler("setworker",      setworker_command))
    app.add_handler(CommandHandler("setheader",       setheader_command))
    app.add_handler(CommandHandler("getheader",       getheader_command))
    app.add_handler(CommandHandler("removeheader",    removeheader_command))
    app.add_handler(CommandHandler("setjoin",         setjoin_command))
    app.add_handler(CommandHandler("getjoin",         getjoin_command))
    app.add_handler(CommandHandler("removejoin",      removejoin_command))
    app.add_handler(CommandHandler("setcaption",     setcaption_command))
    app.add_handler(CommandHandler("resetcaption",   resetcaption_command))

    # Bot control
    app.add_handler(CommandHandler("poster",         poster_command))
    app.add_handler(CommandHandler("rating",         rating_command))
    app.add_handler(CommandHandler("pause",          pause_command))
    app.add_handler(CommandHandler("resume",         resume_command))
    app.add_handler(CommandHandler("stats",          stats_command))
    app.add_handler(CommandHandler("failed",         failed_command))
    app.add_handler(CommandHandler("retry",          retry_command))
    app.add_handler(CommandHandler("notify",         notify_command))

    # Channel listeners
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
