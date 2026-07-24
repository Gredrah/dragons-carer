"""
Thin async wrapper around the Torn API.

All calls here are read-only GETs against api.torn.com — nothing in this
module ever performs a game action. Rate limiting is the caller's
responsibility (100 req/min per user across all keys); the pollers in
bot.py are built to stay well under that at solo-storefront scale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from config import cfg
from constants import (
    REVIVE_SCORE_DECAY_WINDOW_SECONDS,
    TORN_BASE_URL,
    TORN_V2_PATH,
    TORN_V2_USER_PATH,
)


class TornAPIError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Torn API error {code}: {message}")


async def _get(session: aiohttp.ClientSession, path: str, params: dict[str, Any]) -> dict:
    async with session.get(
        f"{TORN_BASE_URL}{path}", params=params, timeout=cfg.torn_api_timeout_seconds
    ) as resp:
        data = await resp.json()
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise TornAPIError(err.get("code", 0), err.get("error", "unknown error"))
    return data


async def verify_key_and_get_id(session: aiohttp.ClientSession, api_key: str) -> int:
    """
    Confirms an API key is valid and returns the Torn ID of its owner.
    Raises TornAPIError if the key is rejected.

    Torn v2 commonly returns the owner ID in `profile.id` for
    `selections=basic`; keep legacy fallbacks for compatibility.
    """
    data = await _get(session, TORN_V2_USER_PATH, {"selections": "basic", "key": api_key})
    candidate = (
        data.get("profile", {}).get("id")
        or data.get("profile", {}).get("player_id")
    )
    if candidate is None:
        raise TornAPIError(0, "Could not determine player ID from key response — check response shape.")
    return int(candidate)


@dataclass
class HospitalStatus:
    in_hospital: bool
    until_ts: Optional[int]  # unix timestamp they're released, if known
    raw_state: str


async def get_hospital_status(session: aiohttp.ClientSession, torn_id: int, api_key: str) -> HospitalStatus:
    """
    Cheap check of whether a player is currently hospitalized.
    Uses the `basic` selection's `status` object.
    """
    data = await _get(session, f"{TORN_V2_USER_PATH}/{torn_id}", {"selections": "basic", "key": api_key})
    status = data.get("profile", {}).get("status")
    state = status.get("state", "")
    return HospitalStatus(
        in_hospital=(state == "Hospital"),
        until_ts=status.get("until"),
        raw_state=state,
    )

async def get_torn_time(session, personal_api_key):
    return await _get(
        session,
        TORN_V2_PATH,
        {"selections": "timestamp", "key": personal_api_key},
    )

# ---------------------------------------------------------------------------
# Revive success-chance estimate
#
# Reconstructed from community reverse-engineering (see the "Revive Chance
# (with calculator)" forum guide) — NOT an official Torn formula. Treat the
# output as an estimate for triage/ranking purposes, not a guarantee, and
# revisit this if Torn ever changes the medical system.
# ---------------------------------------------------------------------------

async def _decayed_weight(session: aiohttp.ClientSession, personal_api_key: str, revive_timestamp: int, now: Optional[int] = None) -> float:
    """The decayed weight of a revive is equivalent to 1 - (seconds_since_revive / decay_window), clamped to [0, 1]."""
    if now is None:
        now = await get_torn_time(session, personal_api_key)
    return max(0.0, 1.0 - (now - revive_timestamp) / REVIVE_SCORE_DECAY_WINDOW_SECONDS)

async def compute_revive_score(session: aiohttp.ClientSession, personal_api_key: str, revive_timestamps: list[int], now: Optional[int] = None) -> float:
    """Revive success chance, per the confirmed formula:
    chance = 90 + weighted_revives * (8 - revive_skill/25)
    (weighted_revives is the sum of each revive's decayed weight in the trailing 24h window)"""
    if now is None:
        now = await get_torn_time(session, personal_api_key)
    revive_skill = await get_revive_skill(session, personal_api_key)
    weighted_revives = sum(
        [await _decayed_weight(session, personal_api_key, ts, now) for ts in revive_timestamps]
    )
    return 90 + weighted_revives * (8 - revive_skill / 25)

async def get_revive_skill(session: aiohttp.ClientSession, personal_api_key: str) -> float:
    """Pulls the player's current revive skill from the Torn API.

    Confirmed shape via live key (selections=skills):
        {"skills": [{"slug": "reviving", "name": "Reviving", "level": 25.13}, ...]}
    Top-level list, not nested under "profile" (unlike the `basic` selection).
    """
    data = await _get(
        session,
        TORN_V2_USER_PATH,
        {"selections": "skills", "key": personal_api_key},
    )
    skills = data.get("skills", [])
    if isinstance(skills, dict):
        skills = skills.values()
    reviving_level = next(
        (
            skill.get("level")
            for skill in skills
            if isinstance(skill, dict) and skill.get("slug") == "reviving"
        ),
        None,
    )
    return float(reviving_level or 0)

async def get_target_revive_score(
    session: aiohttp.ClientSession,
    personal_api_key: str,
) -> float:
    """
    Pulls `revivesfull` for the authenticated player and computes their
    current decayed revive score. Torn v2 exposes this through `/v2/user`
    for the key owner; if you need another player's revive history, you need
    access to their key or a visibility path that exposes their history.
    """
    data = await _get(
        session,
        TORN_V2_USER_PATH,
        {"selections": "revivesfull","filters":"incoming", "key": personal_api_key},
    )
    # Accept both list and dict containers because Torn may return either
    # shape depending on endpoint/version.
    raw_events = (
        data.get("revives")
    )
    events = raw_events.values() if isinstance(raw_events, dict) else raw_events
    timestamps = [
        e.get("timestamp") for e in events
        if isinstance(e, dict) and "timestamp" in e
        
    ]
    torn_time = await get_torn_time(session, personal_api_key)
    return await compute_revive_score(session, personal_api_key, timestamps, torn_time)


# ---------------------------------------------------------------------------
# Deprecated payment detection (reviver-side log polling)
# ---------------------------------------------------------------------------

@dataclass
class PaymentMatch:
    found: bool
    raw_event: Optional[dict] = None


@dataclass
class ReviveMatch:
    found: bool
    raw_event: Optional[dict] = None


async def find_payment_event(
    session: aiohttp.ClientSession,
    reviver_api_key: str,
    order_id: str,
    since_ts: int,
) -> PaymentMatch:
    """
    Polls the reviver's own event/log selection for an incoming item or cash
    transfer whose message/description contains `order_id`.

    TODO (flagged in README): the exact selection name and field layout for
    "item/money received" events needs to be confirmed against a real
    response — candidates are the `log` selection (filterable by category,
    e.g. `log=4100` for item send/receive — verify current category IDs) or
    `events`/`newevents` depending on API version. This function currently
    does a best-effort generic scan across whatever the selection returns
    and should be tightened once you've inspected real payloads with a live
    key. DO NOT ship this to real revivers until that's verified — a silent
    parsing mismatch here means legitimate payments could go undetected.
    """
    data = await _get(
        session,
        TORN_V2_USER_PATH,
        {
            "selections": "log",
            "key": reviver_api_key,
            "from": since_ts,
        },
    )
    log_entries = (
        data.get("log")
        or data.get("logs")
        or data.get("profile", {}).get("log")
        or {}
    )
    # `log` is typically a dict keyed by log-entry ID in v1; iterate defensively.
    entries = log_entries.values() if isinstance(log_entries, dict) else log_entries
    for entry in entries:
        text = str(entry.get("data", entry)).lower()
        if order_id.lower() in text:
            return PaymentMatch(found=True, raw_event=entry)
    return PaymentMatch(found=False)


async def find_outgoing_revive_match(
    session: aiohttp.ClientSession,
    reviver_api_key: str,
    buyer_torn_id: int,
    from_ts: int,
) -> ReviveMatch:
    """Checks whether the reviver key shows an outgoing revive for the buyer since a timestamp."""
    data = await _get(
        session,
        TORN_V2_USER_PATH,
        {
            "selections": "revivesfull",
            "filters": "outgoing",
            "from": from_ts,
            "key": reviver_api_key,
        },
    )
    raw_events = data.get("revives")
    events = raw_events.values() if isinstance(raw_events, dict) else (raw_events or [])
    for entry in events:
        if not isinstance(entry, dict):
            continue
        target = entry.get("target") or {}
        try:
            target_id = int(target.get("id", 0))
        except (TypeError, ValueError):
            continue
        if target_id == int(buyer_torn_id):
            return ReviveMatch(found=True, raw_event=entry)
    return ReviveMatch(found=False)


def _revive_events_from_data(data: dict) -> list[dict]:
    raw_events = data.get("revives")
    if isinstance(raw_events, dict):
        return [event for event in raw_events.values() if isinstance(event, dict)]
    if isinstance(raw_events, list):
        return [event for event in raw_events if isinstance(event, dict)]
    return []


def _revive_target_id(entry: dict) -> Optional[int]:
    target = entry.get("target") or {}
    try:
        return int(target.get("id", 0))
    except (TypeError, ValueError):
        return None


def _is_successful_revive_result(entry: dict) -> bool:
    result_value = str(
        entry.get("result")
        or entry.get("status")
        or entry.get("outcome")
        or entry.get("revive_result")
        or ""
    ).strip().lower()
    if result_value in {"success", "successful", "succeeded", "revived", "done"}:
        return True
    if result_value in {"fail", "failed", "failure", "blocked"}:
        return False
    return result_value == ""


async def count_outgoing_revives_since(
    session: aiohttp.ClientSession,
    reviver_api_key: str,
    buyer_torn_id: int,
    from_ts: int,
) -> int:
    """Counts successful outgoing revives for a buyer since a given timestamp."""
    data = await _get(
        session,
        TORN_V2_USER_PATH,
        {
            "selections": "revivesfull",
            "filters": "outgoing",
            "from": from_ts,
            "key": reviver_api_key,
        },
    )
    count = 0
    for entry in _revive_events_from_data(data):
        if _revive_target_id(entry) != int(buyer_torn_id):
            continue
        if _is_successful_revive_result(entry):
            count += 1
    return count
