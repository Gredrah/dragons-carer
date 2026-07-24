from __future__ import annotations

import asyncio
import json
import time

import discord

import assignment
import db
import notifications
from state import OrderState

_refresh_lock: asyncio.Lock | None = None


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def refresh_queued_orders(bot: discord.Client) -> int:
    """Try to assign queued orders to any currently online reviver."""
    async with _get_refresh_lock():
        queued = await db.orders_in_state(OrderState.QUEUED_NO_REVIVER.value)
        if not queued:
            return 0

        from cogs.reviver import AssignmentView

        assigned_count = 0
        for order in sorted(queued, key=lambda row: row["created_at"]):
            target_closed = await notifications._close_order_if_target_left_hospital(
                bot,
                order,
                event="closed_no_action",
            )
            if target_closed:
                continue

            excluded_ids = set()
            try:
                excluded_ids = {
                    int(reviver_id)
                    for reviver_id in json.loads(order["reviver_attempt_history"])
                }
            except Exception:
                excluded_ids = set()

            next_reviver = await assignment.pick_reviver(
                order["tier_requested"],
                exclude_ids=excluded_ids,
            )
            if next_reviver is None:
                continue

            await db.update_order(
                order["order_id"],
                assigned_reviver_id=next_reviver["torn_id"],
                assigned_at=time.time(),
            )
            await db.record_reviver_assignment(next_reviver["torn_id"])
            await db.transition_order(order["order_id"], OrderState.ASSIGNED.value)

            reviver_identifier = next_reviver["discord_id"]
            await notifications.send_assignment_ping(
                bot,
                reviver_identifier,
                order["order_id"],
                view=AssignmentView(order["order_id"]),
            )
            await notifications.refresh_reviver_order_channel_log(
                bot,
                order["order_id"],
                event="available_again",
            )
            assigned_count += 1

        return assigned_count