"""Central config, loaded once from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    storefront_guild_id: int = _int("STOREFRONT_GUILD_ID", 0)
    ops_guild_id: int = _int("OPS_GUILD_ID", 0)
    forwarding_guild_id: int = _int("FORWARDING_GUILD_ID", 0)
    mod_queue_channel_id: int = _int("MOD_QUEUE_CHANNEL_ID", 0)
    reviver_ping_channel_id: int = _int("REVIVER_PING_CHANNEL_ID", 0)
    forwarding_channel_id: int = _int("FORWARDING_CHANNEL_ID", 0)
    ops_channel_id: int = _int("OPS_CHANNEL_ID", 0)
    forwarding_join_link: str = os.getenv("FORWARDING_JOIN_LINK", "")

    db_path: str = os.getenv("DB_PATH", "revive_bot.db")
    db_encryption_key: str = os.getenv("DB_ENCRYPTION_KEY", "")

    standard_revive_price: str = os.getenv("STANDARD_REVIVE_PRICE", "")
    t75_revive_price: str = os.getenv("T75_REVIVE_PRICE", "")
    t100_revive_price: str = os.getenv("T100_REVIVE_PRICE", "")
    reviver_registration_faction_match_enabled: bool = _bool(
        "REVIVER_REGISTRATION_FACTION_MATCH_ENABLED",
        False,
    )
    reviver_registration_faction_id: int = _int("REVIVER_REGISTRATION_FACTION_ID", 0)
    reviver_registration_faction_name: str = os.getenv("REVIVER_REGISTRATION_FACTION_NAME", "")

    claim_timeout_seconds: int = _int("CLAIM_TIMEOUT_SECONDS", 300)
    delivery_timeout_seconds: int = _int("DELIVERY_TIMEOUT_SECONDS", 900)
    payment_window_single_seconds: int = _int("PAYMENT_WINDOW_SINGLE_SECONDS", 900)
    payment_window_multi_seconds: int = _int("PAYMENT_WINDOW_MULTI_SECONDS", 3600)
    payment_dispute_window_seconds: int = _int("PAYMENT_DISPUTE_WINDOW_SECONDS", 600)

    linked_role_sync_interval_hours: int = _int("LINKED_ROLE_SYNC_INTERVAL_HOURS", 24)
    hospital_poll_interval: int = _int("HOSPITAL_POLL_INTERVAL", 150)
    timeout_sweep_interval: int = _int("TIMEOUT_SWEEP_INTERVAL", 30)
    queue_reassign_interval: int = _int("QUEUE_REASSIGN_INTERVAL", 15)
    active_order_reminder_interval: int = _int("ACTIVE_ORDER_REMINDER_INTERVAL", 30)

    discord_message_soft_limit: int = _int("DISCORD_MESSAGE_SOFT_LIMIT", 1900)
    moderation_view_timeout_seconds: int = _int("MODERATION_VIEW_TIMEOUT_SECONDS", 300)

    torn_api_timeout_seconds: int = _int("TORN_API_TIMEOUT_SECONDS", 10)

    assignment_drought_bonus_per_day: float = _float("ASSIGNMENT_DROUGHT_BONUS_PER_DAY", 1.0)
    assignment_drought_bonus_cap: float = _float("ASSIGNMENT_DROUGHT_BONUS_CAP", 3.0)
    assignment_fairness_window_days: int = _int("ASSIGNMENT_FAIRNESS_WINDOW_DAYS", 7)
    low_priority_reviver_threshold: float = _float("LOW_PRIORITY_REVIVER_THRESHOLD", 65.0)
    warning_reviver_threshold: float = _float("WARNING_REVIVER_THRESHOLD", 50.0)

    debug_outgoing_revive_bypass_torn_id: int = _int("DEBUG_OUTGOING_REVIVE_BYPASS_TORN_ID", 0)


cfg = Config()
