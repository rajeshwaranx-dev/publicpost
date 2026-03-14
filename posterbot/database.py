"""
database.py — MongoDB connection and user CRUD operations.
"""
from config import MONGO_URL, MONGO_DB_NAME, log

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
    col = get_col("users")
    if col is None:
        return None
    return await col.find_one({"log_channels": channel_id, "active": True})


async def get_user_by_tg_id(tg_id: int) -> dict | None:
    """Find user account linked to this Telegram user ID."""
    db = get_db()
    if db is None:
        return None
    return await db["users"].find_one({"telegram_user_id": tg_id})


# ── Posted dict persistence ────────────────────────────────────
async def save_post(user_name: str, mkey: str, data: dict):
    """Save a single post entry to MongoDB."""
    col = get_col("posted")
    if col is None:
        return
    doc = {"_id": f"{user_name}::{mkey}", "user": user_name, "mkey": mkey, "data": data}
    await col.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)


async def delete_post(user_name: str, mkey: str):
    """Delete a post entry from MongoDB."""
    col = get_col("posted")
    if col is None:
        return
    await col.delete_one({"_id": f"{user_name}::{mkey}"})


async def load_all_posts() -> dict[str, dict]:
    """Load all posts from MongoDB into memory on startup.
    Returns: posted[user_name][mkey] = data
    """
    col = get_col("posted")
    if col is None:
        return {}
    result: dict[str, dict] = {}
    async for doc in col.find({}):
        user_name = doc["user"]
        mkey      = doc["mkey"]
        data      = doc["data"]
        if user_name not in result:
            result[user_name] = {}
        result[user_name][mkey] = data
    log.info("📦 Loaded %d post entries from MongoDB", sum(len(v) for v in result.values()))
    return result
