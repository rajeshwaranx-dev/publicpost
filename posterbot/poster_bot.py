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
DEFAULT_NOTE    = "<b>Note 💢: If link not working, copy and paste in browser.</b>"
DEFAULT_CAPTION = (
    "{header}\n"
    "🎬 <b>Title: {title}</b>\n"
    "📅 <b>Year : {year}</b>{season}\n"
    "🎞 <b>Quality: {quality}</b>\n"
    "🎧 <b>Audio: {audio}</b>\n"
    "{rating}"
    "\n<b>🔺Telegram File🔻</b>\n"
    "{files}\n\n"
    "{batch}\n\n"
    "{note}\n\n"
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

    # Strip episode number AND anything after it (episode subtitle)
    # e.g. "Show Name S01E05 Blind Spot" → "Show Name"
    # e.g. "Show Name EP05 Live Target"  → "Show Name"
    title = re.sub(r"\s*S\d{1,2}E?\d*.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*EP?\s*\d+.*$", "", title, flags=re.IGNORECASE)

    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def quality_from_text(text: str) -> str:
    m = QUALITY_RE.search(text)
    return m.group(1) if m else ""


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

                    # Resolution: 480p / 720p / 1080p from Quality line in log
                    resolution = (quality_from_text(display)
                                  or quality_from_text(meta.get("filename", ""))
                                  or meta.get("quality", ""))

                    # Source: WEB-DL / HDRip etc from filename
                    source = meta.get("quality_label", "")

                    # Combine: "720p" or "WEB-DL" or "WEB-DL 720p"
                    if resolution and source and source.upper() not in ("HD",):
                        quality = f"{resolution}"   # just resolution for series ep display
                    elif resolution:
                        quality = resolution
                    elif source:
                        quality = source
                    else:
                        quality = "HD"

                    fid   = file_id_from_url(url)
                    entry = {
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
    if is_series and len(files_sorted) > 1:
        # Multi-file series: Group by episode → EP01 : 480p | 720p
        ep_groups: dict = {}
        for f in files_sorted:
            ep  = f.get("ep") if f.get("ep") is not None else 0
            if ep not in ep_groups:
                ep_groups[ep] = []
            ep_groups[ep].append(f)

        for ep, ep_files in sorted(ep_groups.items()):
            q_links = []
            for f in ep_files:
                fid = file_id_from_url(f["link"])
                lnk = f"{worker_url}/?start={fid}" if worker_url else f["link"]
                q   = f.get("quality") or quality_label or "HD"
                # Apply custom quality emoji if set
                q_emojis = user.get("quality_emojis", {})
                emoji    = q_emojis.get(q, "")
                label    = f"{emoji}{q}" if emoji else q
                q_links.append(f'<a href="{lnk}"><b>{label}</b></a>')
            ep_label = f"EP{ep:02d}" if ep else "EP"
            qualities = " | ".join(q_links)
            file_parts.append(f'🌊 <b>{ep_label} :</b> {qualities}')
    else:
        # Single file (movie or single-file series): show full filename as link
        for f in files_sorted:
            fid   = file_id_from_url(f["link"])
            lnk   = f"{worker_url}/?start={fid}" if worker_url else f["link"]
            label = f.get("display_name") or f.get("quality") or "HD"
            file_parts.append(f'<b>🔥 <a href="{lnk}">{label}</a></b>')

    # Series: single newline between episodes, movies: double newline
    sep = "\n" if is_series else "\n\n"
    file_lines = "\n" + sep.join(file_parts) if file_parts else ""

    async def make_trinity_batch(file_list: list) -> str:
        """Build batch link — supports both Trinity batchkey_ and range format."""
        msg_ids = []
        for f in file_list:
            fid = file_id_from_url(f["link"])
            if fid.startswith("fs_"):
                try:
                    b64    = fid[3:]
                    msg_id = int(base64.urlsafe_b64decode(b64 + "==").decode())
                    msg_ids.append(msg_id)
                except Exception:
                    pass

        if not msg_ids:
            return f"https://t.me/{filestore_bot}"

        batch_mode      = user.get("batch_mode", "batchkey")   # batchkey | range
        trinity_mongo   = user.get("trinity_mongo_url", "")
        trinity_db_name = user.get("trinity_db_name", "Leechx")
        db_channel_id   = abs(int(user.get("db_channel_id", 0)))

        if batch_mode == "batchkey" and trinity_mongo
