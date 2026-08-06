"""
Async SQLite layer. Kept as plain SQL (no ORM) since the schema is small and
stable — easier to reason about for a solo-operator prototype.

NOTE (security TODO): `api_key_encrypted` columns below are written using
Fernet symmetric encryption (see encrypt_key/decrypt_key). Never log a
decrypted key, and never persist one in plaintext outside this layer.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

import aiosqlite
from cryptography.fernet import Fernet

from config import cfg
from constants import SECONDS_PER_DAY

SCHEMA = """
CREATE TABLE IF NOT EXISTS revivers (
    torn_id             INTEGER PRIMARY KEY,
    discord_id          TEXT UNIQUE NOT NULL,
    api_key_encrypted   TEXT NOT NULL,
    tier                TEXT NOT NULL,              -- standard | 75 | 100
    status              TEXT NOT NULL DEFAULT 'offline',  -- online | offline
    last_status_change  REAL NOT NULL,
    last_assigned_at    REAL,                       -- last time this reviver was handed an order (drought weighting)
    created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS buyers (
    torn_id             INTEGER PRIMARY KEY,
    discord_id          TEXT UNIQUE NOT NULL,
    api_key_encrypted   TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',  -- active | paused | blacklisted
    flag_reason         TEXT,
    created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id                    TEXT PRIMARY KEY,
    buyer_torn_id               INTEGER NOT NULL,
    buyer_discord_id            TEXT NOT NULL,
    target_torn_id              INTEGER NOT NULL,
    tier_requested               TEXT NOT NULL,
    is_multi                    INTEGER NOT NULL DEFAULT 0,
    revives_requested           INTEGER NOT NULL DEFAULT 1,
    state                       TEXT NOT NULL,
    assigned_reviver_id         INTEGER,
    assigned_at                 REAL,                          -- timestamp when order was assigned (for timeout logic)
    reviver_attempt_history     TEXT NOT NULL DEFAULT '[]',  -- JSON list of reviver_ids tried
    claimed_at                  REAL,
    forwarded_claimed_at        REAL,
    forwarded_claimed_by_discord_id TEXT,
    delivered_at                REAL,
    payment_window_expires_at   REAL,
    paid_confirmed_at           REAL,
    dispute_window_expires_at   REAL,
    disputed_at                 REAL,
    disputed_by_discord_id      TEXT,
    dispute_reason              TEXT,
    forwarding_claimed_by_discord_id TEXT,
    forwarding_claimed_at       REAL,
    created_at                  REAL NOT NULL,
    updated_at                  REAL NOT NULL,
    FOREIGN KEY (buyer_torn_id) REFERENCES buyers (torn_id),
    FOREIGN KEY (assigned_reviver_id) REFERENCES revivers (torn_id)
);

CREATE TABLE IF NOT EXISTS reviver_incidents (
    incident_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    reviver_id    INTEGER NOT NULL,
    order_id      TEXT NOT NULL,
    type          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_review_queue (
    order_id      TEXT PRIMARY KEY,
    reason        TEXT NOT NULL,
    opened_at     REAL NOT NULL,
    resolved_by   TEXT,
    resolution    TEXT,
    resolved_at   REAL
);

CREATE TABLE IF NOT EXISTS sticky_messages (
    name          TEXT PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    name          TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reviver_order_messages (
    order_id      TEXT NOT NULL,
    discord_id    TEXT NOT NULL,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    last_state    TEXT NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (order_id, discord_id)
);

CREATE TABLE IF NOT EXISTS reviver_order_channel_messages (
    order_id      TEXT PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS forwarding_order_messages (
    order_id      TEXT PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    updated_at    REAL NOT NULL
);
"""


def _fernet() -> Fernet:
    if not cfg.db_encryption_key:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and put it in .env."
        )
    return Fernet(cfg.db_encryption_key.encode())


def encrypt_key(raw_api_key: str) -> str:
    return _fernet().encrypt(raw_api_key.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode()).decode()


async def init_db() -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.executescript(SCHEMA)

        cur = await db.execute("PRAGMA table_info(revivers)")
        existing_reviver_columns = {row[1] for row in await cur.fetchall()}
        if "last_assigned_at" not in existing_reviver_columns:
            await db.execute("ALTER TABLE revivers ADD COLUMN last_assigned_at REAL")

        cur = await db.execute("PRAGMA table_info(orders)")
        existing_columns = {row[1] for row in await cur.fetchall()}
        extra_columns = {
            "assigned_at": "REAL",
            "paid_confirmed_at": "REAL",
            "dispute_window_expires_at": "REAL",
            "disputed_at": "REAL",
            "disputed_by_discord_id": "TEXT",
            "dispute_reason": "TEXT",
            "forwarding_claimed_by_discord_id": "TEXT",
            "forwarding_claimed_at": "REAL",
            "forwarded_claimed_at": "REAL",
            "forwarded_claimed_by_discord_id": "TEXT",
            "revives_requested": "INTEGER",
        }
        for column_name, column_type in extra_columns.items():
            if column_name not in existing_columns:
                default_clause = " DEFAULT 1" if column_name == "revives_requested" else ""
                await db.execute(
                    f"ALTER TABLE orders ADD COLUMN {column_name} {column_type}{default_clause}"
                )
        if "revives_requested" in existing_columns:
            await db.execute(
                "UPDATE orders SET revives_requested = CASE WHEN is_multi = 1 THEN 2 ELSE 1 END "
                "WHERE revives_requested IS NULL OR revives_requested < 1"
            )
        await db.commit()


def new_order_id() -> str:
    # Short, human-typeable order ID for use as a trade message, e.g. "RV-7F2A9C".
    return f"RV-{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------------
# Revivers
# ---------------------------------------------------------------------------

async def upsert_reviver(torn_id: int, discord_id: str, raw_api_key: str, tier: str) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO revivers (torn_id, discord_id, api_key_encrypted, tier, status,
                                   last_status_change, created_at)
            VALUES (?, ?, ?, ?, 'online', ?, ?)
            ON CONFLICT(torn_id) DO UPDATE SET
                discord_id=excluded.discord_id,
                api_key_encrypted=excluded.api_key_encrypted,
                tier=excluded.tier
            """,
            (torn_id, discord_id, encrypt_key(raw_api_key), tier, now, now),
        )
        await db.commit()


async def delete_reviver_by_discord(discord_id: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM revivers WHERE discord_id=?", (discord_id,))
        await db.commit()


async def set_reviver_status(torn_id: int, status: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE revivers SET status=?, last_status_change=? WHERE torn_id=?",
            (status, time.time(), torn_id),
        )
        await db.commit()


async def set_reviver_tier(torn_id: int, tier: str) -> None:
    """Updates the DB tier column directly -- used by role_sync so a
    reviver's skill-based tier stays in sync with what assignment.py
    actually routes on, not just their Discord roles."""
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE revivers SET tier=? WHERE torn_id=?",
            (tier, torn_id),
        )
        await db.commit()


async def record_reviver_assignment(torn_id: int, assigned_at: float | None = None) -> None:
    """Stamps last_assigned_at for drought-weighted assignment. Call this
    everywhere assigned_reviver_id is set on an order (initial assignment,
    forward, timeout reassignment, queue refresh)."""
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE revivers SET last_assigned_at=? WHERE torn_id=?",
            (assigned_at if assigned_at is not None else time.time(), torn_id),
        )
        await db.commit()


async def get_online_revivers(tier: str) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM revivers WHERE tier=? AND status='online'", (tier,)
        )
        return await cur.fetchall()


async def get_reviver(torn_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM revivers WHERE torn_id=?", (torn_id,))
        return await cur.fetchone()


async def completed_count_last_n_days(torn_id: int, days: int | None = None) -> int:
    window_days = cfg.assignment_fairness_window_days if days is None else days
    cutoff = time.time() - window_days * SECONDS_PER_DAY
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE assigned_reviver_id=? AND state IN ('paid','closed')
              AND created_at >= ?
            """,
            (torn_id, cutoff),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Buyers
# ---------------------------------------------------------------------------

async def upsert_buyer(torn_id: int, discord_id: str, raw_api_key: str) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO buyers (torn_id, discord_id, api_key_encrypted, status, created_at)
            VALUES (?, ?, ?, 'active', ?)
            ON CONFLICT(torn_id) DO UPDATE SET
                discord_id=excluded.discord_id,
                api_key_encrypted=excluded.api_key_encrypted
            """,
            (torn_id, discord_id, encrypt_key(raw_api_key), now),
        )
        await db.commit()


async def delete_buyer_by_discord(discord_id: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM buyers WHERE discord_id=?", (discord_id,))
        await db.commit()


async def get_buyer_by_discord(discord_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM buyers WHERE discord_id=?", (discord_id,))
        return await cur.fetchone()


async def list_buyers() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM buyers")
        return await cur.fetchall()


async def get_reviver_by_discord(discord_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM revivers WHERE discord_id=?", (discord_id,))
        return await cur.fetchone()


async def list_revivers() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM revivers")
        return await cur.fetchall()


async def get_torn_id_for_discord(discord_id: str) -> Optional[int]:
    """Unified lookup used by command handlers — checks buyers, then revivers.
    A person can in principle be linked in both roles; buyers table wins on
    tie since /request is the more common entry point."""
    buyer = await get_buyer_by_discord(discord_id)
    if buyer is not None:
        return buyer["torn_id"]
    reviver = await get_reviver_by_discord(discord_id)
    if reviver is not None:
        return reviver["torn_id"]
    return None


async def get_api_key_for_discord(discord_id: str) -> Optional[str]:
    """Returns the decrypted API key linked to this Discord user, if any."""
    buyer = await get_buyer_by_discord(discord_id)
    if buyer is not None:
        return decrypt_key(buyer["api_key_encrypted"])
    reviver = await get_reviver_by_discord(discord_id)
    if reviver is not None:
        return decrypt_key(reviver["api_key_encrypted"])
    return None


async def get_api_key_for_torn_id(torn_id: int) -> Optional[str]:
    """Same as above but keyed by Torn ID instead of Discord ID — used when
    the bot needs a key for a *target* (e.g. checking hospital status) rather
    than the command invoker."""
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT api_key_encrypted FROM buyers WHERE torn_id=?", (torn_id,))
        row = await cur.fetchone()
        if row is None:
            cur = await db.execute(
                "SELECT api_key_encrypted FROM revivers WHERE torn_id=?", (torn_id,)
            )
            row = await cur.fetchone()
        return decrypt_key(row["api_key_encrypted"]) if row else None


async def get_buyer(torn_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM buyers WHERE torn_id=?", (torn_id,))
        return await cur.fetchone()


async def set_buyer_status(torn_id: int, status: str, reason: str | None = None) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE buyers SET status=?, flag_reason=? WHERE torn_id=?",
            (status, reason, torn_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def create_order(
    buyer_torn_id: int,
    buyer_discord_id: str,
    target_torn_id: int,
    tier_requested: str,
    revives_requested: int,
    initial_state: str,
) -> str:
    order_id = new_order_id()
    now = time.time()
    normalized_requested = max(1, int(revives_requested))
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO orders (order_id, buyer_torn_id, buyer_discord_id, target_torn_id,
                                 tier_requested, is_multi, revives_requested, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, buyer_torn_id, buyer_discord_id, target_torn_id,
                tier_requested, int(normalized_requested > 1), normalized_requested, initial_state, now, now,
            ),
        )
        await db.commit()
    return order_id


async def get_order(order_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        return await cur.fetchone()


async def update_order(order_id: str, **fields: Any) -> None:
    """Generic field setter. `state` changes should go through transition_order instead."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            f"UPDATE orders SET {cols} WHERE order_id=?",
            (*fields.values(), order_id),
        )
        await db.commit()


async def transition_order(order_id: str, target_state: str) -> None:
    """Validated state transition. Raises if the transition isn't allowed."""
    from state import OrderState, is_valid_transition  # local import avoids cycle

    order = await get_order(order_id)
    if order is None:
        raise ValueError(f"No such order: {order_id}")
    current = OrderState(order["state"])
    target = OrderState(target_state)
    if not is_valid_transition(current, target):
        raise ValueError(f"Invalid transition {current} -> {target} for order {order_id}")
    await update_order(order_id, state=target.value)


async def orders_in_state(state: str) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE state=?", (state,))
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# Incidents & mod queue
# ---------------------------------------------------------------------------

async def log_incident(reviver_id: int, order_id: str, incident_type: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "INSERT INTO reviver_incidents (reviver_id, order_id, type, created_at) "
            "VALUES (?, ?, ?, ?)",
            (reviver_id, order_id, incident_type, time.time()),
        )
        await db.commit()


async def open_mod_review(order_id: str, reason: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO mod_review_queue (order_id, reason, opened_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_id) DO NOTHING
            """,
            (order_id, reason, time.time()),
        )
        await db.commit()


async def resolve_mod_review(order_id: str, resolved_by: str, resolution: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE mod_review_queue SET resolved_by=?, resolution=?, resolved_at=? "
            "WHERE order_id=?",
            (resolved_by, resolution, time.time(), order_id),
        )
        await db.commit()


async def list_open_mod_reviews() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM mod_review_queue WHERE resolved_at IS NULL ORDER BY opened_at ASC"
        )
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# Sticky channel messages
# ---------------------------------------------------------------------------

async def get_sticky_message(name: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM sticky_messages WHERE name=?", (name,))
        return await cur.fetchone()


async def upsert_sticky_message(name: str, channel_id: int, message_id: int) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO sticky_messages (name, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                updated_at=excluded.updated_at
            """,
            (name, channel_id, message_id, now),
        )
        await db.commit()


async def delete_sticky_message(name: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM sticky_messages WHERE name=?", (name,))
        await db.commit()


# ---------------------------------------------------------------------------
# Bot settings
# ---------------------------------------------------------------------------

async def get_setting(name: str) -> Optional[str]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT value FROM bot_settings WHERE name=?", (name,))
        row = await cur.fetchone()
        return row["value"] if row is not None else None


async def get_setting_int(name: str) -> Optional[int]:
    value = await get_setting(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def upsert_setting(name: str, value: int | str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO bot_settings (name, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (name, str(value), time.time()),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Reviver order DMs
# ---------------------------------------------------------------------------

async def get_reviver_order_message(order_id: str, discord_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reviver_order_messages WHERE order_id=? AND discord_id=?",
            (order_id, discord_id),
        )
        return await cur.fetchone()


async def upsert_reviver_order_message(
    order_id: str,
    discord_id: str,
    channel_id: int,
    message_id: int,
    last_state: str,
) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO reviver_order_messages (order_id, discord_id, channel_id, message_id, last_state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id, discord_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                last_state=excluded.last_state,
                updated_at=excluded.updated_at
            """,
            (order_id, discord_id, channel_id, message_id, last_state, now),
        )
        await db.commit()


async def delete_reviver_order_message(order_id: str, discord_id: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "DELETE FROM reviver_order_messages WHERE order_id=? AND discord_id=?",
            (order_id, discord_id),
        )
        await db.commit()


async def get_reviver_order_channel_message(order_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reviver_order_channel_messages WHERE order_id=?",
            (order_id,),
        )
        return await cur.fetchone()


async def upsert_reviver_order_channel_message(order_id: str, channel_id: int, message_id: int) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO reviver_order_channel_messages (order_id, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                updated_at=excluded.updated_at
            """,
            (order_id, channel_id, message_id, now),
        )
        await db.commit()


async def delete_reviver_order_channel_message(order_id: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM reviver_order_channel_messages WHERE order_id=?", (order_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Forwarding order messages
# ---------------------------------------------------------------------------

async def get_forwarding_order_message(order_id: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM forwarding_order_messages WHERE order_id=?",
            (order_id,),
        )
        return await cur.fetchone()


async def upsert_forwarding_order_message(order_id: str, channel_id: int, message_id: int) -> None:
    now = time.time()
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO forwarding_order_messages (order_id, channel_id, message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                updated_at=excluded.updated_at
            """,
            (order_id, channel_id, message_id, now),
        )
        await db.commit()


async def delete_forwarding_order_message(order_id: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM forwarding_order_messages WHERE order_id=?", (order_id,))
        await db.commit()
