"""
state.py — Shared mutable state. Import from here only — never redefine elsewhere.
"""
import asyncio
import datetime

# ── Global bot app reference (set in poster_bot.py at startup) ─
bot_app = None

# ── Feature toggles ───────────────────────────────────────────
poster_enabled: bool = True
rating_enabled: bool = True
bot_paused:     bool = False

# ── In-memory post tracking ───────────────────────────────────
# pending[log_channel_id][msg_id] = parsed meta
pending: dict[str, dict] = {}
# posted[user_name][movie_key]    = post data
posted:  dict[str, dict] = {}
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
