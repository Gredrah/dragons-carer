from __future__ import annotations

# Discord integer perms 395405502480

import asyncio
import json
import time

import aiohttp
import discord
from discord.ext import commands, tasks

import assignment
import db
import notifications
from role_sync import sync_all_linked_roles
from queue_refresh import refresh_queued_orders
import torn_api
from config import cfg
from state import OrderState, IncidentType

ACTIVE_WORKING_ORDER_STATES = (
    OrderState.ASSIGNED.value,
    OrderState.CLAIMED.value,
    OrderState.QUEUED_NO_REVIVER.value,
)

ACTIVE_HOSPITAL_POLL_STATES = (
    OrderState.ASSIGNED.value,
    OrderState.QUEUED_NO_REVIVER.value,
)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True  # needed for role reconciliation against guild members


class ReviveBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self):
        await db.init_db()
        for ext in ("cogs.linking", "cogs.buyer", "cogs.reviver", "cogs.moderation"):
            await self.load_extension(ext)

        # Re-register persistent views so buttons on old messages still work
        # after a restart. Import here to avoid circulars at module load time.
        from cogs.buyer import ReviveRequestPanelView
        from cogs.moderation import ResolutionView
        from cogs.reviver import AssignmentView, DeliveredView, ForwardedAssignmentView, PaymentView, ResignalView, ReviverStatusPanelView
        from cogs.linking import BuyerRegistrationPanelView, ReviverRegistrationPanelView

        # NOTE: persistent views with dynamic custom_ids need a "template"
        # registration approach; the simplest correct pattern is to re-add a
        # view per in-flight order on startup rather than one static instance.
        self.add_view(ReviveRequestPanelView())
        self.add_view(BuyerRegistrationPanelView())
        self.add_view(ReviverRegistrationPanelView())
        self.add_view(ReviverStatusPanelView())
        
        for order in await db.orders_in_state(OrderState.ASSIGNED.value):
            self.add_view(AssignmentView(order["order_id"]))
            self.add_view(ForwardedAssignmentView(order["order_id"]))
        for order in await db.orders_in_state(OrderState.QUEUED_NO_REVIVER.value):
            self.add_view(ResignalView(order["order_id"]))
            self.add_view(ForwardedAssignmentView(order["order_id"]))
        for order in await db.orders_in_state(OrderState.CLAIMED.value):
            self.add_view(DeliveredView(order["order_id"]))
            self.add_view(ForwardedAssignmentView(order["order_id"]))
        for order in await db.orders_in_state(OrderState.DELIVERED.value):
            self.add_view(PaymentView(order["order_id"]))
        for review in await db.list_open_mod_reviews():
            self.add_view(ResolutionView(review["order_id"]))

        if cfg.storefront_guild_id:
            guild = discord.Object(id=cfg.storefront_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        if cfg.ops_guild_id:
            guild = discord.Object(id=cfg.ops_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

        await notifications.refresh_existing_order_notifications(self)
        await refresh_queued_orders(self)
        sync_linked_roles_loop.start(self)
        sweep_timeouts.start(self)
        poll_hospital_status.start(self)
        refresh_queued_orders_loop.start(self)
        remind_active_orders_loop.start(self)

    async def on_message(self, message: discord.Message):
        if self.user is None:
            return

        if getattr(message.author, "bot", False):
            return

        await self.process_commands(message)

        if message.channel.id != cfg.reviver_ping_channel_id:
            return

        await notifications.refresh_active_order_reminder(self)


bot = ReviveBot()


@tasks.loop(hours=cfg.linked_role_sync_interval_hours)
async def sync_linked_roles_loop(bot: ReviveBot):
    await sync_all_linked_roles(bot)


@sync_linked_roles_loop.before_loop
async def _before_sync_linked_roles_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Background pollers
# ---------------------------------------------------------------------------

@tasks.loop(seconds=cfg.timeout_sweep_interval)
async def sweep_timeouts(bot: ReviveBot):
    """Checks ASSIGNED/CLAIMED/DELIVERED orders for expired timeouts and
    flags/reassigns as appropriate. Runs frequently since it's a pure DB
    operation (no API calls), so the interval can stay short."""
    now = time.time()

    # Claim timeout: assigned but reviver never claimed/forwarded.
    for order in await db.orders_in_state(OrderState.ASSIGNED.value):
        await _handle_assigned_timeout(order, now, bot)

    # Delivery timeout: claimed but never marked delivered.
    for order in await db.orders_in_state(OrderState.CLAIMED.value):
        await _handle_claimed_timeout(order, now, bot)

    # Payment window timeout: delivered but never paid.
    for order in await db.orders_in_state(OrderState.DELIVERED.value):
        await _handle_delivered_timeout(bot, order, now)

    # Dispute window: the reviver has confirmed payment, and the buyer has a
    # short period to dispute before the order closes automatically.
    for order in await db.orders_in_state(OrderState.PAID.value):
        await _handle_paid_timeout(bot, order, now)


@tasks.loop(seconds=cfg.queue_reassign_interval)
async def refresh_queued_orders_loop(bot: ReviveBot):
    await refresh_queued_orders(bot)


@tasks.loop(seconds=cfg.active_order_reminder_interval)
async def remind_active_orders_loop(bot: ReviveBot):
    active_orders = []
    for state in ACTIVE_WORKING_ORDER_STATES:
        active_orders.extend(await db.orders_in_state(state))
    await notifications.send_active_order_reminder(bot, active_orders)


async def _handle_assigned_timeout(order: dict, now: float, bot: ReviveBot | None = None):
    # Check timeout from when the order was assigned, not from last update.
    # This is distinct from updated_at since orders can bounce through ASSIGNED
    # multiple times via forwarding, and we want to timeout from the latest
    # assignment, not from the last DB update.
    if not order["assigned_at"] or now - order["assigned_at"] <= cfg.claim_timeout_seconds:
        return

    await db.log_incident(
        order["assigned_reviver_id"], order["order_id"], IncidentType.STALL_CLAIM.value
    )
    
    tried = set(json.loads(order["reviver_attempt_history"]))
    tried.add(order["assigned_reviver_id"])
    previous_reviver = await db.get_reviver(order["assigned_reviver_id"])
    next_reviver = await assignment.pick_reviver(order["tier_requested"], exclude_ids=tried)
    if next_reviver:
        await db.update_order(
            order["order_id"],
            assigned_reviver_id=next_reviver["torn_id"],
            assigned_at=time.time(),
            reviver_attempt_history=json.dumps(list(tried)),
        )
        await db.record_reviver_assignment(next_reviver["torn_id"])
        # state stays ASSIGNED; timestamp reset via update_order's updated_at bump
        from cogs.reviver import AssignmentView

        if previous_reviver is not None:
            await notifications.refresh_reviver_order_dm(
                bot,
                order["order_id"],
                previous_reviver["discord_id"],
            )

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
        return

    await db.transition_order(order["order_id"], OrderState.QUEUED_NO_REVIVER.value)
    await db.update_order(order["order_id"], reviver_attempt_history=json.dumps(list(tried)))
    if previous_reviver is not None:
        await notifications.refresh_reviver_order_dm(
            bot,
            order["order_id"],
            previous_reviver["discord_id"],
        )
    await notifications.refresh_reviver_order_channel_log(
        bot,
        order["order_id"],
        event="queued",
    )
    
    # Notify buyer that order is queued due to assignment timeout
    await notifications.send_dm(
        bot,
        order["buyer_discord_id"],
        f"Order `{order['order_id']}` is now queued: the assigned reviver did not claim it in time. "
        "Your request will be assigned to the next available reviver when one comes online."
    )
    await notifications.send_no_reviver_available_notice(
        bot,
        order["order_id"],
        order["tier_requested"],
        order["buyer_torn_id"],
        order["target_torn_id"],
    )


async def _handle_claimed_timeout(order: dict, now: float, bot: ReviveBot):
    if not order["claimed_at"] or now - order["claimed_at"] <= cfg.delivery_timeout_seconds:
        return

    await db.log_incident(
        order["assigned_reviver_id"], order["order_id"], IncidentType.STALL_DELIVERY.value
    )
    await db.transition_order(order["order_id"], OrderState.FLAGGED_FOR_REVIEW.value)
    await db.open_mod_review(
        order["order_id"], "Delivery timeout expired — possible reviver no-show/clog."
    )
    assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
    if assigned_reviver is not None:
        await notifications.refresh_reviver_order_dm(
            bot,
            order["order_id"],
            assigned_reviver["discord_id"],
        )
    await notifications.refresh_reviver_order_channel_log(
        bot,
        order["order_id"],
        event="review",
    )
    # Post to mod queue channel for visibility
    if cfg.mod_queue_channel_id:
        channel = bot.get_channel(cfg.mod_queue_channel_id)
        if channel is not None:
            await channel.send(
                f"Order `{order['order_id']}` opened for review: reviver claimed but never marked delivered. "
                f"Possible reviver no-show/clog."
            )


async def _handle_delivered_timeout(bot: ReviveBot, order: dict, now: float):
    if not order["payment_window_expires_at"] or now <= order["payment_window_expires_at"]:
        return

    await db.transition_order(order["order_id"], OrderState.FLAGGED_FOR_REVIEW.value)
    await db.open_mod_review(
        order["order_id"], "Payment window expired with no confirmed payment."
    )
    assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
    if assigned_reviver is not None:
        await notifications.refresh_reviver_order_dm(
            bot,
            order["order_id"],
            assigned_reviver["discord_id"],
        )
    await notifications.refresh_reviver_order_channel_log(
        bot,
        order["order_id"],
        event="review",
    )
    # Buyer is paused (not auto-blacklisted) pending mod review, per design.
    await db.set_buyer_status(
        order["buyer_torn_id"], "paused", reason=f"pending review on {order['order_id']}"
    )
    if cfg.mod_queue_channel_id:
        channel = bot.get_channel(cfg.mod_queue_channel_id)
        if channel is not None:
            await channel.send(
                f"Order `{order['order_id']}` opened for review: payment window expired with no confirmation."
            )


async def _handle_paid_timeout(bot: ReviveBot, order: dict, now: float):
    if order["dispute_window_expires_at"] and now > order["dispute_window_expires_at"]:
        await db.transition_order(order["order_id"], OrderState.CLOSED.value)
        assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
        if assigned_reviver is not None:
            await notifications.refresh_reviver_order_dm(
                bot,
                order["order_id"],
                assigned_reviver["discord_id"],
            )
        await notifications.refresh_reviver_order_channel_log(
            bot,
            order["order_id"],
            event="closed",
        )


async def _handle_target_left_hospital(bot: ReviveBot, order: dict) -> None:
    await db.transition_order(order["order_id"], OrderState.CLOSED_NO_ACTION.value)
    assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
    if assigned_reviver is not None:
        await notifications.refresh_reviver_order_dm(
            bot,
            order["order_id"],
            assigned_reviver["discord_id"],
        )
    await notifications.refresh_reviver_order_channel_log(
        bot,
        order["order_id"],
        event="closed_no_action",
    )
    await notifications.send_dm(
        bot,
        order["buyer_discord_id"],
        f"Order `{order['order_id']}` closed: target (Torn ID {order['target_torn_id']}) left hospital before revive was completed.",
    )
    await notifications.send_order_ops_notice(
        bot,
        order["order_id"],
        order["buyer_torn_id"],
        order["target_torn_id"],
    )

@tasks.loop(seconds=cfg.hospital_poll_interval)
async def poll_hospital_status(bot: ReviveBot):
    """Check whether any unclaimed active order's target has left hospital and close it.

    Claimed orders are left open so the reviver can explicitly mark that the
    buyer bailed on the request instead of auto-closing the revive."""
    active_orders = []
    for state in ACTIVE_HOSPITAL_POLL_STATES:
        active_orders.extend(await db.orders_in_state(state))
    if not active_orders:
        return
    async with aiohttp.ClientSession() as session:
        for order in active_orders:
            key = await db.get_api_key_for_torn_id(order["target_torn_id"])
            if key is None:
                # Target isn't linked (not our buyer/reviver, and not visible via
                # faction key) -- see the cross-faction-visibility caveat from
                # the design discussion. Nothing to poll with; skip this cycle.
                continue
            try:
                status = await torn_api.get_hospital_status(session, order["target_torn_id"], key)
            except Exception:
                continue
            if not status.in_hospital:
                await _handle_target_left_hospital(bot, order)


if __name__ == "__main__":
    bot.run(cfg.discord_token)