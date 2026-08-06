"""
Assignment logic.

Priority order (per design): tier match first, drought/fairness weighting
second. Target revive chance is used separately to classify low-priority
orders and to warn revivers before they accept risky assignments.
- A request for tier T is only ever given to a reviver at tier T, or -- if
  none are online -- the next tier(s) up (a 100-skill reviver can cover a 75
  or standard request; a standard reviver should never be handed a 75/100
  request). Assignment never falls to a *lower* tier than requested.
- Within whichever single tier level actually has an available candidate,
    revivers are ranked by completed orders in the trailing window (fair split)
    and then by a bonus that grows the longer a reviver has gone without an
    assignment (drought).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import db
from config import cfg
from constants import SECONDS_PER_DAY
from state import TIER_ORDER


def _drought_bonus(last_assigned_at: float | None, now: float) -> float:
    if last_assigned_at is None:
        # Never assigned -- treat as fully drought-bonused so brand-new
        # revivers aren't perpetually skipped in favor of established ones.
        return cfg.assignment_drought_bonus_cap
    days_since = max(0.0, now - last_assigned_at) / SECONDS_PER_DAY
    return min(
        cfg.assignment_drought_bonus_cap,
        days_since * cfg.assignment_drought_bonus_per_day,
    )


async def _weighted_score(row, now: float) -> float:
    completed = await db.completed_count_last_n_days(
        row["torn_id"],
        days=cfg.assignment_fairness_window_days,
    )
    bonus = _drought_bonus(row["last_assigned_at"], now)
    return completed - bonus


def _new_client_session() -> Any:
    import aiohttp

    return aiohttp.ClientSession()


async def get_target_revive_chance(target_torn_id: int | None) -> float | None:
    if target_torn_id is None:
        return None

    api_key = await db.get_api_key_for_torn_id(target_torn_id)
    if not api_key:
        return None

    try:
        import torn_api

        async with _new_client_session() as session:
            return await torn_api.get_target_revive_score(session, api_key)
    except Exception:
        # Chance is informational only; if the API lookup fails, leave the
        # target unclassified rather than blocking assignment.
        return None


async def pick_reviver(
    tier: str,
    exclude_ids: set[int] | None = None,
) -> Optional[dict]:
    """
    Returns the row (as a dict) of the best reviver to assign next, or None
    if nobody eligible is online. `exclude_ids` lets the caller skip
    revivers who already declined/forwarded this specific order.
    """
    exclude_ids = exclude_ids or set()

    if tier not in TIER_ORDER:
        # Unknown tier string -- don't guess at a fallback ordering for it.
        candidates = await db.get_online_revivers(tier)
        candidates = [c for c in candidates if c["torn_id"] not in exclude_ids]
        if not candidates:
            return None
        return await _best_of(candidates)

    start_index = TIER_ORDER.index(tier)
    now = time.time()
    for level in TIER_ORDER[start_index:]:
        candidates = await db.get_online_revivers(level)
        candidates = [c for c in candidates if c["torn_id"] not in exclude_ids]
        if candidates:
            return await _best_of(candidates, now=now)

    return None


async def pick_reviver_with_retry(
    tier: str,
    exclude_ids: set[int] | None = None,
) -> tuple[Optional[dict], bool]:
    """Pick the next reviver, then retry once with a fresh pool if the current
    exclusion list exhausts the active queue.

    Returns the selected reviver and a flag indicating whether the caller
    should reset the order's attempt history for a new cycle.
    """
    excluded = exclude_ids or set()
    chosen = await pick_reviver(tier, exclude_ids=excluded)
    if chosen is not None or not excluded:
        return chosen, False

    retry_choice = await pick_reviver(tier, exclude_ids=set())
    return retry_choice, retry_choice is not None


async def _best_of(
    candidates: list,
    now: float | None = None,
) -> dict:
    now = now if now is not None else time.time()
    ranked = []
    for row in candidates:
        score = await _weighted_score(row, now)
        ranked.append(((score, row["torn_id"]), row))
    ranked.sort(key=lambda pair: pair[0])
    return dict(ranked[0][1])
