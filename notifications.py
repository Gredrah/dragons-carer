"""Helpers for sending Discord notifications such as DMs and channel pings.

These helpers keep the bot's cog/view logic focused on state changes while
providing a single place to handle Discord delivery issues.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import aiohttp
import discord

import db
from config import cfg
from formatting import torn_link
import torn_api
from state import OrderState

logger = logging.getLogger(__name__)
ACTIVE_ORDER_REMINDER_NAME = "active_order_reminder"
ONLINE_REVIVERS_LIST_NAME = "online_revivers_list"
HARD_TERMINAL_ORDER_STATES = {
    OrderState.CLOSED.value,
    OrderState.CLOSED_NO_ACTION.value,
    OrderState.BLACKLISTED_ORDER.value,
}


def _apply_forwarding_footer(embed: discord.Embed) -> discord.Embed:
    if cfg.forwarding_join_link.strip():
        embed.set_footer(text=f"Join link: {cfg.forwarding_join_link.strip()}")
    return embed


async def _send_ops_alert(bot: discord.Client, message: str) -> None:
    """Best-effort alert to the ops channel without recursing through the notification helpers."""
    channel_id = await db.get_setting_int("ops_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            return

    if not isinstance(channel, discord.abc.Messageable):
        return

    try:
        await channel.send(content=message)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return


async def _report_notification_failure(bot: discord.Client, kind: str, recipient: str, reason: str) -> None:
    message = f"{kind} notification failed for {recipient}: {reason}"
    logger.warning(message)
    await _send_ops_alert(bot, f"[notification failure] {message}")


async def _send_to_messageable(
    destination: discord.abc.Messageable,
    content: str,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
) -> bool:
    """Send a message to a Discord messageable object and swallow common errors."""
    try:
        await destination.send(content=content, embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return False


async def _fetch_message(channel: discord.abc.Messageable, message_id: int) -> discord.Message | None:
    if not hasattr(channel, "fetch_message"):
        return None
    try:
        return await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _fetch_last_message(channel: discord.abc.Messageable) -> discord.Message | None:
    if not hasattr(channel, "history"):
        return None
    try:
        async for message in channel.history(limit=1, oldest_first=False):
            return message
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def _find_recent_order_message(channel: discord.abc.Messageable, order_id: str) -> discord.Message | None:
    if not hasattr(channel, "history"):
        return None

    marker = f"Order `{order_id}`"
    try:
        async for message in channel.history(limit=25, oldest_first=False):
            if getattr(message.author, "bot", False) and getattr(message, "content", "").startswith(marker):
                return message
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def _delete_reviver_order_message_record(order_id: str, recipient_discord_id: str) -> None:
    await db.delete_reviver_order_message(order_id, recipient_discord_id)


async def _delete_reviver_order_channel_message_record(order_id: str) -> None:
    await db.delete_reviver_order_channel_message(order_id)


async def _delete_forwarding_order_message_record(order_id: str) -> None:
    await db.delete_forwarding_order_message(order_id)


async def _close_order_if_target_left_hospital(
    bot: discord.Client,
    order: dict,
    *,
    event: str,
) -> bool:
    lookup_key = await db.get_api_key_for_torn_id(order["buyer_torn_id"])
    if lookup_key is None:
        return False

    async with aiohttp.ClientSession() as session:
        try:
            status = await torn_api.get_hospital_status(session, order["target_torn_id"], lookup_key)
        except Exception:
            return False

    if status.in_hospital:
        return False

    await db.transition_order(order["order_id"], OrderState.CLOSED_NO_ACTION.value)
    assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
    if assigned_reviver is not None:
        await refresh_reviver_order_dm(bot, order["order_id"], assigned_reviver["discord_id"])
    await refresh_reviver_order_channel_log(bot, order["order_id"], event=event)
    await send_dm(
        bot,
        order["buyer_discord_id"],
        (
            f"Order `{order['order_id']}` closed without action: target {torn_link(order['target_torn_id'])} "
            "left hospital before the revive could be assigned or delivered."
        ),
    )
    await send_order_ops_notice(bot, order["order_id"], order["buyer_torn_id"], order["target_torn_id"])
    await refresh_active_order_reminder(bot)
    return True


async def _delete_reviver_order_dm(
    bot: discord.Client,
    order_id: str,
    recipient_discord_id: str,
) -> bool:
    user = bot.get_user(int(recipient_discord_id))
    if user is None:
        try:
            user = await bot.fetch_user(int(recipient_discord_id))
        except (discord.NotFound, discord.HTTPException):
            await _delete_reviver_order_message_record(order_id, recipient_discord_id)
            return False

    try:
        dm_channel = await user.create_dm()
    except (discord.Forbidden, discord.HTTPException):
        await _delete_reviver_order_message_record(order_id, recipient_discord_id)
        return False

    record = await db.get_reviver_order_message(order_id, recipient_discord_id)
    message = None
    if record is not None and int(record["channel_id"]) == dm_channel.id:
        message = await _fetch_message(dm_channel, int(record["message_id"]))

    if message is None:
        message = await _find_recent_order_message(dm_channel, order_id)

    if message is not None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    await _delete_reviver_order_message_record(order_id, recipient_discord_id)
    return True


async def _fetch_reviver_order_channel_target(bot: discord.Client, order_id: str) -> tuple[discord.abc.Messageable | None, discord.Message | None]:
    record = await db.get_reviver_order_channel_message(order_id)
    if record is None:
        return None, None

    channel = bot.get_channel(int(record["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(record["channel_id"]))
        except (discord.NotFound, discord.HTTPException):
            return None, None

    if not isinstance(channel, discord.abc.Messageable):
        return None, None

    message = await _fetch_message(channel, int(record["message_id"]))
    return channel, message


async def _fetch_forwarding_order_target(bot: discord.Client, order_id: str) -> tuple[discord.abc.Messageable | None, discord.Message | None]:
    record = await db.get_forwarding_order_message(order_id)
    if record is None:
        return None, None

    channel = bot.get_channel(int(record["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(record["channel_id"]))
        except (discord.NotFound, discord.HTTPException):
            return None, None

    if not isinstance(channel, discord.abc.Messageable):
        return None, None

    message = await _fetch_message(channel, int(record["message_id"]))
    return channel, message


def _build_channel_log_embed(order: dict, event: str, assigned_reviver_discord_id: str | None) -> discord.Embed:
    tier = order["tier_requested"]
    revives_requested = order["revives_requested"]
    target_torn_id = order["target_torn_id"]
    buyer_torn_id = order["buyer_torn_id"]
    order_id = order["order_id"]

    embed = discord.Embed(color=discord.Color.blurple())
    embed.add_field(name="Order", value=order_id, inline=True)
    embed.add_field(name="Tier", value=tier, inline=True)
    embed.add_field(name="Revives", value=str(revives_requested), inline=True)
    embed.add_field(name="Target", value=torn_link(target_torn_id), inline=True)
    embed.add_field(name="Buyer", value=torn_link(buyer_torn_id), inline=True)

    if event == "queued":
        embed.title = f"Order queued: {order_id}"
        embed.description = (
            f"No reviver is currently online for order `{order_id}`.\n"
            f"Tier `{tier}` is still open for target {torn_link(target_torn_id)} from buyer {torn_link(buyer_torn_id)}."
        )
    elif event == "available_again":
        embed.title = f"Order re-assigned: {order_id}"
        if assigned_reviver_discord_id is not None:
            embed.description = f"Order `{order_id}` has been re-assigned to <@{assigned_reviver_discord_id}>."
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
        else:
            embed.description = f"Order `{order_id}` has been re-assigned."
    elif event == "assigned":
        embed.title = f"New order assigned: {order_id}"
        embed.description = (
            f"Target: {torn_link(target_torn_id)}\n"
            f"Tier: {tier}\n"
            f"Revives requested: {revives_requested}"
        )
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    elif event == "forwarded":
        embed.title = f"Order available again: {order_id}"
        embed.description = (
            f"Order `{order_id}` was passed on to another reviver and is available again."
        )
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    elif event == "claimed":
        embed.title = f"Order claimed: {order_id}"
        embed.description = f"Order `{order_id}` has been claimed and is now in progress."
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    elif event == "delivered":
        embed.title = f"Order delivered: {order_id}"
        embed.description = f"Order `{order_id}` has been delivered and is awaiting payment confirmation."
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    elif event == "paid":
        embed.title = f"Payment confirmed: {order_id}"
        embed.description = f"Payment has been confirmed for order `{order_id}`."
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    elif event == "review":
        embed.title = f"Order under review: {order_id}"
        embed.description = f"Order `{order_id}` is under moderator review."
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)
    else:
        embed.title = f"Order update: {order_id}"
        embed.description = f"Order `{order_id}` changed state to `{event}`."
        if assigned_reviver_discord_id is not None:
            embed.add_field(name="Reviver", value=f"<@{assigned_reviver_discord_id}>", inline=False)

    return embed


def _build_forwarding_order_embed(order: dict, event: str, assigned_reviver_discord_id: str | None) -> discord.Embed:
    return _apply_forwarding_footer(_build_channel_log_embed(order, event, assigned_reviver_discord_id))


def _build_forwarding_order_content(order: dict) -> str:
    return (
        f"Order `{order['order_id']}` | buyer {torn_link(order['buyer_torn_id'])} -> target {torn_link(order['target_torn_id'])} "
        f"| tier `{order['tier_requested']}` | revives `{order['revives_requested']}`"
    )


def build_discord_profile_embed(user: discord.abc.User | discord.Member, *, title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.green())
    embed.add_field(name="Discord", value=f"<@{user.id}>", inline=True)
    embed.add_field(name="User ID", value=str(user.id), inline=True)
    embed.add_field(name="Username", value=str(user), inline=False)
    avatar_url = getattr(getattr(user, "display_avatar", None), "url", None)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    return embed


async def build_user_profile_embed(bot: discord.Client, user_id: int | str, *, title: str, description: str) -> discord.Embed:
    user = bot.get_user(int(user_id))
    if user is None:
        user = await bot.fetch_user(int(user_id))
    return build_discord_profile_embed(user, title=title, description=description)


async def refresh_forwarding_order_message(bot: discord.Client, order_id: str, *, event: str) -> bool:
    order = await db.get_order(order_id)
    channel_id = await db.get_setting_int("forwarding_channel_id")
    if order is None or not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            return False

    if not isinstance(channel, discord.abc.Messageable):
        return False

    if order["state"] in HARD_TERMINAL_ORDER_STATES:
        _, message = await _fetch_forwarding_order_target(bot, order_id)
        if message is not None:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        await _delete_forwarding_order_message_record(order_id)
        return True

    assigned_reviver_discord_id: str | None = None
    if order["assigned_reviver_id"] is not None:
        assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])
        if assigned_reviver is not None:
            assigned_reviver_discord_id = str(assigned_reviver["discord_id"])

    embed = _build_forwarding_order_embed(order, event, assigned_reviver_discord_id)
    content = _build_forwarding_order_content(order)
    view = _forwarding_view_for_state(order)

    record = await db.get_forwarding_order_message(order_id)
    message = None
    if record is not None and int(record["channel_id"]) == getattr(channel, "id", channel_id):
        message = await _fetch_message(channel, int(record["message_id"]))

    if message is not None:
        try:
            await message.edit(content=content, embed=embed, view=view)
            await db.upsert_forwarding_order_message(order_id, int(getattr(channel, "id", channel_id)), message.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            message = None

    try:
        new_message = await channel.send(content=content, embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        return False

    await db.upsert_forwarding_order_message(order_id, int(getattr(channel, "id", channel_id)), new_message.id)
    return True


async def refresh_reviver_order_channel_log(
    bot: discord.Client,
    order_id: str,
    *,
    event: str,
) -> bool:
    order = await db.get_order(order_id)
    if order is None:
        return False

    channel_id = await db.get_setting_int("reviver_ping_channel_id")
    if not channel_id:
        return False

    assigned_reviver_discord_id: str | None = None
    if order["assigned_reviver_id"] is not None:
        assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])
        if assigned_reviver is not None:
            assigned_reviver_discord_id = str(assigned_reviver["discord_id"])

    if order["state"] in HARD_TERMINAL_ORDER_STATES:
        channel, message = await _fetch_reviver_order_channel_target(bot, order_id)
        if message is not None:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        await _delete_reviver_order_channel_message_record(order_id)
        _, forwarding_message = await _fetch_forwarding_order_target(bot, order_id)
        if forwarding_message is not None:
            try:
                await forwarding_message.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        await _delete_forwarding_order_message_record(order_id)
        return True

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            return False

    if not isinstance(channel, discord.abc.Messageable):
        return False

    embed = _build_channel_log_embed(order, event, assigned_reviver_discord_id)
    record = await db.get_reviver_order_channel_message(order_id)
    message = None
    if record is not None and int(record["channel_id"]) == getattr(channel, "id", channel_id):
        message = await _fetch_message(channel, int(record["message_id"]))

    if message is not None:
        try:
            await message.edit(embed=embed)
            await db.upsert_reviver_order_channel_message(order_id, int(getattr(channel, "id", channel_id)), message.id)
            forwarding_success = await refresh_forwarding_order_message(bot, order_id, event=event)
            return forwarding_success or True
        except (discord.Forbidden, discord.HTTPException):
            message = None

    try:
        new_message = await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return False

    await db.upsert_reviver_order_channel_message(order_id, int(getattr(channel, "id", channel_id)), new_message.id)
    return await refresh_forwarding_order_message(bot, order_id, event=event)


async def refresh_existing_order_notifications(bot: discord.Client) -> None:
    """Rebuild missing per-order notifications for live orders after a restart."""
    state_event_map = {
        OrderState.ASSIGNED.value: "assigned",
        OrderState.FORWARDED_CLAIMED.value: "forwarded_claimed",
        OrderState.CLAIMED.value: "claimed",
        OrderState.DELIVERED.value: "delivered",
        OrderState.PAID.value: "paid",
        OrderState.QUEUED_NO_REVIVER.value: "queued",
        OrderState.FLAGGED_FOR_REVIEW.value: "review",
    }

    for state, event in state_event_map.items():
        for order in await db.orders_in_state(state):
            assigned_reviver = None
            if order["assigned_reviver_id"] is not None:
                assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])

            if assigned_reviver is not None:
                await refresh_reviver_order_dm(
                    bot,
                    order["order_id"],
                    assigned_reviver["discord_id"],
                )

            await refresh_reviver_order_channel_log(
                bot,
                order["order_id"],
                event=event,
            )


def _forwarding_view_for_state(order: dict) -> discord.ui.View | None:
    from cogs.reviver import ForwardedAssignmentView

    if order["state"] in {
        OrderState.ASSIGNED.value,
        OrderState.QUEUED_NO_REVIVER.value,
    }:
        return ForwardedAssignmentView(order["order_id"])

    if order["state"] == OrderState.FORWARDED_CLAIMED.value:
        return ForwardedAssignmentView(
            order["order_id"],
            claimant_discord_id=order["forwarded_claimed_by_discord_id"],
        )

    return None


def build_active_order_reminder_content(orders: Iterable[dict]) -> str:
    order_lines = [
        (
            f"- `{order['order_id']}` | `{order['state']}` | buyer {torn_link(order['buyer_torn_id'])} "
            f"-> target {torn_link(order['target_torn_id'])} | tier `{order['tier_requested']}` | requested `{order['revives_requested']}`"
        )
        for order in orders
    ]
    header = "Active revive orders reminder:\n"
    if not order_lines:
        return header + "No active revive orders right now."
    return _render_limited_lines(header, order_lines, "No active revive orders right now.")


def _render_limited_lines(header: str, lines: list[str], empty_message: str) -> str:
    if not lines:
        return header + empty_message

    rendered_lines: list[str] = []
    for index, line in enumerate(lines):
        candidate = header + "\n".join(rendered_lines + [line])
        if len(candidate) > cfg.discord_message_soft_limit:
            remaining = len(lines) - index
            if remaining > 0:
                truncated_line = f"... and {remaining} more line(s)."
                if len(header + "\n".join(rendered_lines + [truncated_line])) <= cfg.discord_message_soft_limit:
                    rendered_lines.append(truncated_line)
            break
        rendered_lines.append(line)

    return header + "\n".join(rendered_lines)


def _tier_display_name(tier: str) -> str:
    normalized = str(tier).strip().lower()
    if normalized == "100":
        return "100+"
    if normalized == "75":
        return "75+"
    return "Standard"


def build_online_revivers_content(revivers: Iterable[dict]) -> str:
    online_revivers = [
        reviver
        for reviver in revivers
        if str(reviver["status"]).strip().lower() == "online"
    ]
    order_lines: list[str] = []
    for tier in ("100", "75", "standard"):
        tier_revivers = sorted(
            [reviver for reviver in online_revivers if str(reviver["tier"]).strip().lower() == tier],
            key=lambda row: (int(row["torn_id"]), str(row["discord_id"])),
        )
        if not tier_revivers:
            continue

        order_lines.append(f"{_tier_display_name(tier)}:")
        order_lines.extend(f"- <@{reviver['discord_id']}> | {torn_link(reviver['torn_id'])}" for reviver in tier_revivers)

    return _render_limited_lines("Currently online revivers:\n", order_lines, "No revivers are online right now.")


def build_reviver_order_dm_content(
    order: dict,
    recipient_discord_id: str,
    current_assignee_discord_id: str | None,
    assignment_notice: str | None = None,
) -> str:
    recipient_discord_id = str(recipient_discord_id)
    current_assignee_discord_id = str(current_assignee_discord_id) if current_assignee_discord_id is not None else None
    is_current_assignee = current_assignee_discord_id is not None and recipient_discord_id == current_assignee_discord_id
    state = order["state"]

    lines = [
        f"Order `{order['order_id']}`",
        f"Target {torn_link(order['target_torn_id'])} | tier `{order['tier_requested']}` | revives `{order['revives_requested']}`",
    ]

    if assignment_notice:
        lines.append(assignment_notice)

    if state == OrderState.ASSIGNED.value:
        if is_current_assignee:
            lines.append("Status: assigned to you. Use the order buttons when you are ready.")
            lines.append("Claim to take ownership or forward if you cannot do it.")
        else:
            lines.append("Status: forwarded to another reviver.")
            lines.append("This order has moved on from you.")
    elif state == OrderState.CLAIMED.value:
        if is_current_assignee:
            lines.append("Status: claimed by you. Mark delivered when the revive is complete.")
            lines.append("If the buyer bailed and already revived another way, use Buyer bailed.")
        else:
            lines.append("Status: claimed by another reviver.")
    elif state == OrderState.DELIVERED.value:
        if is_current_assignee:
            lines.append("Status: delivered. Confirm payment once the buyer pays.")
        else:
            lines.append("Status: delivered by another reviver.")
    elif state == OrderState.PAID.value:
        if is_current_assignee:
            dispute_window_expires_at = order["dispute_window_expires_at"]
            if dispute_window_expires_at is not None:
                lines.append(
                    f"Status: payment confirmed. Dispute window closes <t:{int(dispute_window_expires_at)}:R>."
                )
            else:
                lines.append("Status: payment confirmed. Dispute window is open.")
        else:
            lines.append("Status: payment confirmed.")
    elif state == OrderState.QUEUED_NO_REVIVER.value:
        if is_current_assignee:
            lines.append("Status: no other reviver was available, so this order is queued.")
            lines.append("If you still want it, re-signal interest from the channel when you can.")
        else:
            lines.append("Status: queued while the bot waits for another available reviver.")
    elif state == OrderState.FLAGGED_FOR_REVIEW.value:
        lines.append("Status: under moderator review.")
    elif state == OrderState.CLOSED_NO_ACTION.value:
        lines.append("Status: closed because the target left hospital before the revive was completed.")
    elif state == OrderState.CLOSED.value:
        lines.append("Status: closed.")
    elif state == OrderState.BLACKLISTED_ORDER.value:
        lines.append("Status: closed after moderation action.")
    else:
        lines.append(f"Status: {state}.")

    return "\n".join(lines)


async def _build_assignment_notice(order: dict) -> str | None:
    import assignment

    chance = await assignment.get_target_revive_chance(order["target_torn_id"])
    if chance is None:
        return None
    if chance < cfg.warning_reviver_threshold:
        return (
            f"Warning: this target is estimated at {chance:.1f}% revive chance. "
            "Failures must be personally negotiated or the fee waived before you accept."
        )
    if chance < cfg.low_priority_reviver_threshold:
        return (
            f"Notice: this target is estimated at {chance:.1f}% revive chance, so this order is in the secondary non-priority pool."
        )
    return None


def _order_view_for_state(order: dict, recipient_discord_id: str, current_assignee_discord_id: str | None) -> discord.ui.View | None:
    from cogs.reviver import AssignmentView, DeliveredView, ResignalView  # local import avoids circulars

    if (
        order["state"] == OrderState.ASSIGNED.value
        and current_assignee_discord_id is not None
        and str(recipient_discord_id) == str(current_assignee_discord_id)
    ):
        return AssignmentView(order["order_id"])

    if (
        order["state"] == OrderState.QUEUED_NO_REVIVER.value
        and current_assignee_discord_id is not None
        and str(recipient_discord_id) == str(current_assignee_discord_id)
    ):
        return ResignalView(order["order_id"])

    if (
        order["state"] == OrderState.FORWARDED_CLAIMED.value
        and order["forwarded_claimed_by_discord_id"] is not None
        and str(recipient_discord_id) == str(order["forwarded_claimed_by_discord_id"])
    ):
        return DeliveredView(order["order_id"])

    return None


async def refresh_reviver_order_dm(
    bot: discord.Client,
    order_id: str,
    recipient_discord_id: int | str,
    *,
    view: discord.ui.View | None = None,
) -> bool:
    order = await db.get_order(order_id)
    if order is None:
        return False

    try:
        recipient_discord_id = str(int(recipient_discord_id))
    except (TypeError, ValueError):
        return False

    current_assignee_discord_id: str | None = None
    if order["state"] == OrderState.FORWARDED_CLAIMED.value:
        if order["forwarded_claimed_by_discord_id"] is not None:
            current_assignee_discord_id = str(order["forwarded_claimed_by_discord_id"])
    elif order["assigned_reviver_id"] is not None:
        assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])
        if assigned_reviver is not None:
            current_assignee_discord_id = str(assigned_reviver["discord_id"])

    if order["state"] in HARD_TERMINAL_ORDER_STATES:
        return await _delete_reviver_order_dm(bot, order_id, recipient_discord_id)

    assignment_notice = await _build_assignment_notice(order)
    content = build_reviver_order_dm_content(
        order,
        recipient_discord_id,
        current_assignee_discord_id,
        assignment_notice=assignment_notice,
    )
    resolved_view = view
    if resolved_view is None:
        resolved_view = _order_view_for_state(order, recipient_discord_id, current_assignee_discord_id)

    user = bot.get_user(int(recipient_discord_id))
    if user is None:
        try:
            user = await bot.fetch_user(int(recipient_discord_id))
        except (discord.NotFound, discord.HTTPException):
            return False

    try:
        dm_channel = await user.create_dm()
    except (discord.Forbidden, discord.HTTPException):
        return False

    record = await db.get_reviver_order_message(order_id, recipient_discord_id)
    message = None
    if record is not None and int(record["channel_id"]) == dm_channel.id:
        message = await _fetch_message(dm_channel, int(record["message_id"]))

    if message is None:
        message = await _find_recent_order_message(dm_channel, order_id)

    if message is not None:
        try:
            await message.edit(content=content, view=resolved_view)
            await db.upsert_reviver_order_message(order_id, recipient_discord_id, dm_channel.id, message.id, order["state"])
            return True
        except (discord.Forbidden, discord.HTTPException):
            message = None

    try:
        new_message = await dm_channel.send(content=content, view=resolved_view)
    except (discord.Forbidden, discord.HTTPException):
        return False

    await db.upsert_reviver_order_message(order_id, recipient_discord_id, dm_channel.id, new_message.id, order["state"])
    return True


async def _maintain_sticky_message(
    bot: discord.Client,
    *,
    name: str,
    channel_id: int,
    content: str,
) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.HTTPException):
            return False

    if not isinstance(channel, discord.abc.Messageable):
        return False

    state = await db.get_sticky_message(name)
    stored_message = None
    if state is not None and int(state["channel_id"]) == channel_id:
        stored_message = await _fetch_message(channel, int(state["message_id"]))

    last_message = await _fetch_last_message(channel)
    if stored_message is not None and last_message is not None and last_message.id == stored_message.id:
        try:
            await stored_message.edit(content=content)
            await db.upsert_sticky_message(name, channel_id, stored_message.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    try:
        new_message = await channel.send(content=content)
    except (discord.Forbidden, discord.HTTPException):
        return False

    if stored_message is not None and stored_message.id != new_message.id:
        try:
            await stored_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    await db.upsert_sticky_message(name, channel_id, new_message.id)
    return True


async def refresh_active_order_reminder(bot: discord.Client) -> bool:
    channel_id = await db.get_setting_int("reviver_ping_channel_id")
    if not channel_id:
        return False

    active_orders: list[dict] = []
    for state in (
        OrderState.ASSIGNED.value,
        OrderState.CLAIMED.value,
        OrderState.QUEUED_NO_REVIVER.value,
    ):
        active_orders.extend(await db.orders_in_state(state))

    content = build_active_order_reminder_content(active_orders)
    return await _maintain_sticky_message(
        bot,
        name=ACTIVE_ORDER_REMINDER_NAME,
        channel_id=channel_id,
        content=content,
    )


async def refresh_online_revivers_list(bot: discord.Client) -> bool:
    channel_id = await db.get_setting_int("buyer_channel_id")
    if not channel_id:
        return False

    revivers = await db.list_revivers()
    content = build_online_revivers_content(revivers)
    return await _maintain_sticky_message(
        bot,
        name=ONLINE_REVIVERS_LIST_NAME,
        channel_id=channel_id,
        content=content,
    )


async def send_dm(
    bot: discord.Client,
    user_id: int | str,
    content: str,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
) -> bool:
    """Send a DM to a user by Discord ID."""
    user = bot.get_user(int(user_id))
    if user is None:
        try:
            user = await bot.fetch_user(int(user_id))
        except (discord.NotFound, discord.HTTPException):
            await _report_notification_failure(bot, "DM", str(user_id), "user lookup failed")
            return False

    try:
        dm_channel = await user.create_dm()
    except (discord.Forbidden, discord.HTTPException):
        await _report_notification_failure(bot, "DM", str(user_id), "could not create DM channel")
        return False

    success = await _send_to_messageable(dm_channel, content, view=view, embed=embed)
    if not success:
        await _report_notification_failure(bot, "DM", str(user_id), "send failed")
    return success


async def send_channel_message(
    bot: discord.Client,
    channel_id: int | str,
    content: str,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
) -> bool:
    """Send a message to a guild channel or DM channel by ID."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.HTTPException):
            await _report_notification_failure(bot, "channel", str(channel_id), "channel lookup failed")
            return False

    if not isinstance(channel, discord.abc.Messageable):
        await _report_notification_failure(bot, "channel", str(channel_id), "destination is not messageable")
        return False

    success = await _send_to_messageable(channel, content, view=view, embed=embed)
    if not success:
        await _report_notification_failure(bot, "channel", str(channel_id), "send failed")
    return success


async def send_reviver_ping(
    bot: discord.Client,
    reviver_user_id: int | str,
    content: str,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
    fallback_channel_id: int | str | None = None,
) -> bool:
    """Try DMing a reviver first and fall back to a configured channel if needed."""
    if await send_dm(bot, reviver_user_id, content, view=view, embed=embed):
        return True

    channel_id = fallback_channel_id if fallback_channel_id is not None else await db.get_setting_int("reviver_ping_channel_id")
    if channel_id:
        return await send_channel_message(bot, channel_id, content, view=view, embed=embed)

    await _report_notification_failure(bot, "reviver ping", str(reviver_user_id), "no fallback channel configured")
    return False


async def send_assignment_ping(
    bot: discord.Client,
    reviver_user_id: int | str,
    order_id: str,
    *,
    view: discord.ui.View | None = None,
) -> bool:
    """Send or update the per-order DM shown to a reviver."""
    return await refresh_reviver_order_dm(bot, order_id, reviver_user_id, view=view)


async def send_no_reviver_available_notice(
    bot: discord.Client,
    order_id: str,
    *_,
) -> bool:
    """Broadcast that an order is open even though no reviver was available."""
    channel_id = await db.get_setting_int("reviver_ping_channel_id")
    if not channel_id:
        await _report_notification_failure(
            bot,
            "queued-order broadcast",
            order_id,
            "no reviver ping channel configured",
        )
        return False
    success = await refresh_reviver_order_channel_log(bot, order_id, event="queued")
    if success:
        await refresh_active_order_reminder(bot)
    return success


async def send_mod_issue_report(bot: discord.Client, content: str) -> bool:
    """Send a reviver issue report to the mod queue, with ops fallback."""
    channel_id = await db.get_setting_int("mod_queue_channel_id") or await db.get_setting_int("ops_channel_id")
    if not channel_id:
        return False
    return await send_channel_message(bot, channel_id, content)


async def send_active_order_reminder(
    bot: discord.Client,
    orders: Iterable[dict],
) -> bool:
    """Maintain the reminder about currently active orders at the bottom of the revive channel."""
    channel_id = await db.get_setting_int("reviver_ping_channel_id")
    if not channel_id:
        return False
    content = build_active_order_reminder_content(orders)
    return await _maintain_sticky_message(
        bot,
        name=ACTIVE_ORDER_REMINDER_NAME,
        channel_id=channel_id,
        content=content,
    )


async def send_order_ops_notice(
    bot: discord.Client,
    order_id: str,
    buyer_torn_id: int,
    target_torn_id: int,
) -> bool:
    """Tell the seller-facing revive channel that an active order changed operational state."""
    channel_id = await db.get_setting_int("ops_channel_id")
    if not channel_id:
        return False
    return await send_channel_message(
        bot,
        channel_id,
        f"Order `{order_id}` ops update: target {torn_link(target_torn_id)} left hospital before the revive was completed. "
        f"Buyer {torn_link(buyer_torn_id)} was notified.",
    )


async def send_order_canceled_notice(
    bot: discord.Client,
    order_id: str,
    buyer_torn_id: int,
    target_torn_id: int,
) -> bool:
    """Tell the revive channel that a buyer canceled an active order."""
    message = (
        f"Order `{order_id}` was canceled by buyer {torn_link(buyer_torn_id)} for target {torn_link(target_torn_id)}."
    )
    channel_id = await db.get_setting_int("reviver_ping_channel_id")
    if not channel_id:
        return False
    return await send_channel_message(bot, channel_id, message)


async def send_payment_instructions(
    bot: discord.Client,
    buyer_user_id: int | str,
    order_id: str,
    reviver_torn_id: int,
    expires_at: float,
) -> bool:
    """Send payment instructions to a buyer after order delivery.
    
    Args:
        bot: Discord client
        buyer_user_id: Discord ID of the buyer
        order_id: The order ID
        reviver_torn_id: Torn ID of the reviver (for reference)
        expires_at: Unix timestamp when payment window expires
    """
    message = (
        f"**Payment Instructions for Order `{order_id}`**\n\n"
        f"Your revive has been delivered! Please send payment to reviver {torn_link(reviver_torn_id)}.\n"
        f"**Payment window closes <t:{int(expires_at)}:R>.**\n\n"
        f"Once you've sent payment, confirm it using the button below or wait for the reviver to confirm."
    )
    success = await send_dm(bot, buyer_user_id, message)
    return success


async def send_payment_confirmed_notification(
    bot: discord.Client,
    buyer_user_id: int | str,
    order_id: str,
    dispute_window_expires_at: float,
) -> bool:
    """Notify buyer that payment has been confirmed and dispute window is open.
    
    Args:
        bot: Discord client
        buyer_user_id: Discord ID of the buyer
        order_id: The order ID
        dispute_window_expires_at: Unix timestamp when dispute window expires
    """
    message = (
        f"**Payment Confirmed for Order `{order_id}`**\n\n"
        f"The reviver has confirmed receipt of your payment. "
        f"You have until <t:{int(dispute_window_expires_at)}:R> to open a dispute if needed.\n"
        f"If you received your revive and payment is complete, no further action is required."
    )
    success = await send_dm(bot, buyer_user_id, message)
    return success
