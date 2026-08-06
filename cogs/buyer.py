from __future__ import annotations

import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import assignment
import notifications
import db
import torn_api
from config import cfg
from formatting import torn_link
from permissions import BUYER_ACCESS_ROLE_NAMES, deny_if_missing_buyer_access, deny_if_missing_role
from state import OrderState, Tier

NO_ORDER_MESSAGE = "No order with that ID."
NOT_YOUR_ORDER_MESSAGE = "That's not your order."
ACTIVE_CANCELLABLE_STATES = {
    OrderState.ASSIGNED.value,
    OrderState.CLAIMED.value,
    OrderState.QUEUED_NO_REVIVER.value,
}
REQUEST_PANEL_TITLE = "Revive Request Panel"


def _format_price(value: str) -> str:
    if value is None:
        return "$0"

    text = str(value).strip()
    if not text:
        return "$0"

    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if match is None:
        return "$0"

    number_text = match.group(0).replace(",", "")
    try:
        amount = int(float(number_text))
    except ValueError:
        return "$0"

    formatted_number = f"${amount:,}"
    return f"{text[:match.start()]}{formatted_number}{text[match.end():]}"


def _parse_tier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"standard", "std"}:
        return Tier.STANDARD.value
    if normalized in {"75", "75+", "t75"}:
        return Tier.T75.value
    if normalized in {"100", "100+", "t100"}:
        return Tier.T100.value
    raise ValueError("tier must be one of: standard, 75, 100")


def _parse_revives_requested(value: str) -> int:
    try:
        requested = int(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("Revives requested must be a whole number.") from exc
    if requested < 1:
        raise ValueError("Revives requested must be at least 1.")
    return requested


def build_request_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=REQUEST_PANEL_TITLE,
        description=(
            "Use the button below to request a revive. Fill in the details, and the bot will route the order.\n\n"
            "Requirements:\n"
            "- Your Torn account must be linked\n"
            "- The target must be in hospital\n"
            "- Prices are shown by tier below"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Standard", value=_format_price(cfg.standard_revive_price), inline=True)
    embed.add_field(name="75+", value=_format_price(cfg.t75_revive_price), inline=True)
    embed.add_field(name="100+", value=_format_price(cfg.t100_revive_price), inline=True)
    embed.add_field(name="Requested revives", value="Enter 1 for a single revive or a higher number for a bundle.", inline=False)
    embed.add_field(
        name="What happens next",
        value=(
            "1. Submit the request\n"
            "2. A reviver is assigned if one is online\n"
            "3. You will get payment instructions after delivery"
        ),
        inline=False,
    )
    embed.set_footer(text="revive_request_panel")
    return embed


async def _delete_previous_panel_messages(channel: discord.abc.Messageable, bot_id: int, *, title: str) -> None:
    if not hasattr(channel, "history"):
        return

    async for message in channel.history(limit=None, oldest_first=False):
        if getattr(message.author, "id", None) != bot_id:
            continue
        if not message.embeds:
            continue
        if message.embeds[0].title != title:
            continue
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return


class ReviveRequestModal(discord.ui.Modal, title="Request a Revive"):
    def __init__(self, buyer_cog: "BuyerCog"):
        super().__init__(timeout=None)
        self.buyer_cog = buyer_cog
        self.target_id = discord.ui.TextInput(
            label="Target Torn ID",
            placeholder="Leave blank to revive yourself",
            required=False,
            max_length=20,
        )
        self.tier = discord.ui.TextInput(
            label="Tier",
            placeholder="standard, 75, or 100",
            default="standard",
            required=True,
            max_length=20,
        )
        self.revives_requested = discord.ui.TextInput(
            label="Revives requested",
            placeholder="1",
            default="1",
            required=True,
            max_length=4,
        )
        self.add_item(self.target_id)
        self.add_item(self.tier)
        self.add_item(self.revives_requested)

    async def on_submit(self, interaction: discord.Interaction):
        await self.buyer_cog._submit_request(
            interaction,
            tier_value=self.tier.value,
            target_value=self.target_id.value,
            revives_requested_value=self.revives_requested.value,
        )


class ReviveRequestPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Request a Revive", style=discord.ButtonStyle.success, custom_id="revive_request_panel:request_button")
    async def request_revive(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_buyer_access(interaction, "This button is only available to buyers."):
            return
        buyer_cog = interaction.client.get_cog("BuyerCog")
        if buyer_cog is None:
            await interaction.response.send_message(
                "The revive request system is not ready yet. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ReviveRequestModal(buyer_cog))


class BuyerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ensure_linked_and_active(self, interaction: discord.Interaction):
        buyer = await db.get_buyer_by_discord(str(interaction.user.id))
        if buyer is None:
            await interaction.followup.send(
                "You haven't linked your Torn account yet — run `/link` with your API key first.",
                ephemeral=True,
            )
            return None
        if buyer["status"] == "blacklisted":
            await interaction.followup.send(
                "You're currently blacklisted from requesting revives. "
                "Contact a moderator if you believe this is a mistake.",
                ephemeral=True,
            )
            return None
        if buyer["status"] == "paused":
            await interaction.followup.send(
                "Your account is paused pending a moderation review on a prior order. "
                "Please wait for that to resolve before placing a new request.",
                ephemeral=True,
            )
            return None
        return buyer

    async def _check_target_hospital(self, buyer_torn_id: int, target_id: int):
        lookup_key = await db.get_api_key_for_torn_id(buyer_torn_id)
        if lookup_key is None:
            return None
        async with aiohttp.ClientSession() as session:
            try:
                return await torn_api.get_hospital_status(session, target_id, lookup_key)
            except Exception:
                return None

    async def _resolve_target_id(
        self,
        interaction: discord.Interaction,
        buyer_torn_id: int,
        target_value: str | None,
    ) -> int | None:
        target_id = buyer_torn_id
        if target_value and target_value.strip():
            try:
                target_id = int(target_value.strip())
            except ValueError:
                await interaction.followup.send(
                    "Target Torn ID must be a number.",
                    ephemeral=True,
                )
                return None
        return target_id

    async def _resolve_revives_requested(
        self,
        interaction: discord.Interaction,
        requested_value: str,
    ) -> int | None:
        try:
            return _parse_revives_requested(requested_value)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return None

    async def _send_assigned_request_response(
        self,
        interaction: discord.Interaction,
        order_id: str,
        tier_key: str,
        target_id: int,
        buyer_torn_id: int,
        revives_requested: int,
        reviver: dict,
    ) -> None:
        from cogs.reviver import AssignmentView

        reviver_identifier = reviver["discord_id"]
        await notifications.send_assignment_ping(
            interaction.client,
            reviver_identifier,
            order_id,
            view=AssignmentView(order_id),
        )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            order_id,
            event="assigned",
        )
        await interaction.followup.send(
            f"Order `{order_id}` created and assigned to a reviver. "
            "You'll get a DM once it's delivered with payment instructions.",
            ephemeral=True,
        )

    async def _send_queued_request_response(
        self,
        interaction: discord.Interaction,
        order_id: str,
        tier_key: str,
        buyer_torn_id: int,
        target_id: int,
        revives_requested: int,
    ) -> None:
        await interaction.followup.send(
            f"Order `{order_id}` created. No revivers are currently online for "
            f"the **{tier_key}** tier, so there's an unspecified delay — "
            f"your request for {revives_requested} revive(s) stays open until the target leaves hospital.",
            ephemeral=True,
        )
        await notifications.send_no_reviver_available_notice(
            self.bot,
            order_id,
            tier_key,
            buyer_torn_id,
            target_id,
        )
        await notifications.refresh_reviver_order_channel_log(
            self.bot,
            order_id,
            event="queued",
        )

    async def submit_forwarding_report(self, interaction: discord.Interaction, order_id: str, reason: str) -> None:
        order = await db.get_order(order_id)
        if order is None:
            await interaction.response.send_message(NO_ORDER_MESSAGE, ephemeral=True)
            return
        if order["buyer_discord_id"] != str(interaction.user.id):
            await interaction.response.send_message(NOT_YOUR_ORDER_MESSAGE, ephemeral=True)
            return

        await db.update_order(
            order_id,
            disputed_at=time.time(),
            disputed_by_discord_id=str(interaction.user.id),
            dispute_reason=reason,
        )
        await db.open_mod_review(order_id, f"Forwarding report: {reason}")

        if cfg.mod_queue_channel_id:
            channel = interaction.client.get_channel(cfg.mod_queue_channel_id)
            if channel is not None:
                embed = discord.Embed(
                    title=f"Forwarding report opened for {order_id}",
                    color=discord.Color.red(),
                    description=reason,
                )
                embed.add_field(name="Buyer Torn ID", value=torn_link(order["buyer_torn_id"]), inline=True)
                embed.add_field(
                    name="Claimed third party",
                    value=f"<@{order['forwarded_claimed_by_discord_id']}>" if order["forwarded_claimed_by_discord_id"] else "Unclaimed",
                    inline=True,
                )
                await channel.send(embed=embed)

        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            order_id,
            event="review",
        )
        await interaction.response.send_message(
            f"Report opened for order `{order_id}`. A moderator will review it.",
            ephemeral=True,
        )

    async def _submit_request(
        self,
        interaction: discord.Interaction,
        *,
        tier_value: str,
        target_value: str | None,
        revives_requested_value: str,
    ):
        await interaction.response.defer(ephemeral=True)
        buyer = await self._ensure_linked_and_active(interaction)
        if buyer is None:
            return

        try:
            tier_key = _parse_tier(tier_value)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        target_id = await self._resolve_target_id(interaction, buyer["torn_id"], target_value)
        if target_id is None:
            return

        revives_requested = await self._resolve_revives_requested(interaction, revives_requested_value)
        if revives_requested is None:
            return

        # Cheap hospital-status sanity check before we even queue the order.
        # NOTE: if target_id belongs to someone other than the buyer and isn't
        # separately linked/visible, this key lookup returns None and we skip
        # the check rather than block the order -- see the design-conversation
        # caveat on cross-faction target visibility. Tighten this once you've
        # decided whether to require targets to also be linked.
        status = await self._check_target_hospital(buyer["torn_id"], target_id)

        if status is not None and not status.in_hospital:
            await interaction.followup.send(
                f"Target {torn_link(target_id)} doesn't currently show as hospitalized — "
                "no order created. Try again once they're actually in hospital.",
                ephemeral=True,
            )
            return

        reviver = await assignment.pick_reviver(tier_key)
        initial_state = (
            OrderState.ASSIGNED.value if reviver else OrderState.QUEUED_NO_REVIVER.value
        )

        order_id = await db.create_order(
            buyer_torn_id=buyer["torn_id"],
            buyer_discord_id=str(interaction.user.id),
            target_torn_id=target_id,
            tier_requested=tier_key,
            revives_requested=revives_requested,
            initial_state=initial_state,
        )

        order = await db.get_order(order_id)
        if order is not None:
            target_closed = await notifications._close_order_if_target_left_hospital(
                interaction.client,
                order,
                event="closed_no_action",
            )
            if target_closed:
                await interaction.followup.send(
                    f"Order `{order_id}` was not assigned because the target left hospital before it could be routed.",
                    ephemeral=True,
                )
                return

        if reviver:
            await db.update_order(order_id, assigned_reviver_id=reviver["torn_id"], assigned_at=time.time())
            await db.record_reviver_assignment(reviver["torn_id"])
            await self._send_assigned_request_response(
                interaction,
                order_id,
                tier_key,
                target_id,
                buyer["torn_id"],
                revives_requested,
                reviver,
            )
        else:
            await self._send_queued_request_response(
                interaction,
                order_id,
                tier_key,
                buyer["torn_id"],
                target_id,
                revives_requested,
            )

    @app_commands.command(name="request", description="Request a revive from the storefront.")
    @app_commands.checks.has_any_role(*BUYER_ACCESS_ROLE_NAMES)
    @app_commands.describe(
        tier="Reviver tier (defaults to standard)",
        target_id="Torn ID of the player who needs reviving (defaults to self)",
        revives_requested="How many revives are being requested?",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="Standard", value=Tier.STANDARD.value),
            app_commands.Choice(name="75+", value=Tier.T75.value),
            app_commands.Choice(name="100", value=Tier.T100.value),
        ]
    )
    async def request(
        self,
        interaction: discord.Interaction,
        tier: str = Tier.STANDARD.value,
        target_id: int | None = None,
        revives_requested: int = 1,
    ):
        await self._submit_request(
            interaction,
            tier_value=tier,
            target_value=str(target_id) if target_id is not None else None,
            revives_requested_value=str(revives_requested),
        )

    @app_commands.command(name="panel", description="Post the revive request panel in this channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction):
        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I can't post the panel here.",
                ephemeral=True,
            )
            return

        if interaction.client.user is not None:
            await _delete_previous_panel_messages(
                interaction.channel,
                interaction.client.user.id,
                title=REQUEST_PANEL_TITLE,
            )

        await interaction.channel.send(
            embed=build_request_panel_embed(),
            view=ReviveRequestPanelView(),
        )
        await interaction.response.send_message(
            "Revive request panel posted.",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Check the status of one of your orders.")
    @app_commands.checks.has_any_role(*BUYER_ACCESS_ROLE_NAMES)
    async def status(self, interaction: discord.Interaction, order_id: str):
        order = await db.get_order(order_id)
        if order is None:
            await interaction.response.send_message(NO_ORDER_MESSAGE, ephemeral=True)
            return
        if order["buyer_discord_id"] != str(interaction.user.id):
            await interaction.response.send_message(
                NOT_YOUR_ORDER_MESSAGE, ephemeral=True
            )
            return

        state = order["state"]
        payment_window_expires_at = order["payment_window_expires_at"]
        dispute_window_expires_at = order["dispute_window_expires_at"]
        lines = {
            OrderState.REQUESTED.value: "Just created, being routed.",
            OrderState.ASSIGNED.value: "Assigned to a reviver, waiting on them to claim.",
            OrderState.CLAIMED.value: "A reviver has claimed this and is working on it.",
            OrderState.QUEUED_NO_REVIVER.value: "No reviver online yet — still open, unspecified delay.",
            OrderState.DELIVERED.value: (
                f"Delivered. Waiting for reviver confirmation until "
                f"<t:{int(payment_window_expires_at)}:R>."
                if payment_window_expires_at is not None
                else "Delivered. Waiting for reviver confirmation."
            ),
            OrderState.PAID.value: (
                f"Payment confirmed. Dispute window closes "
                f"<t:{int(dispute_window_expires_at)}:R>."
                if dispute_window_expires_at is not None
                else "Payment confirmed. Dispute window is open."
            ),
            OrderState.FLAGGED_FOR_REVIEW.value: "Under moderator review.",
            OrderState.CLOSED.value: "Closed.",
            OrderState.CLOSED_NO_ACTION.value: "Closed — target left hospital before assignment.",
        }
        await interaction.response.send_message(
            lines.get(state, f"State: {state}"), ephemeral=True
        )

    @app_commands.command(name="cancel", description="Cancel one of your active revive orders.")
    @app_commands.checks.has_any_role(*BUYER_ACCESS_ROLE_NAMES)
    async def cancel(self, interaction: discord.Interaction, order_id: str):
        order = await db.get_order(order_id)
        if order is None:
            await interaction.response.send_message(NO_ORDER_MESSAGE, ephemeral=True)
            return
        if order["buyer_discord_id"] != str(interaction.user.id):
            await interaction.response.send_message(
                NOT_YOUR_ORDER_MESSAGE, ephemeral=True
            )
            return
        if order["state"] not in ACTIVE_CANCELLABLE_STATES:
            await interaction.response.send_message(
                "That order can't be canceled in its current state.", ephemeral=True
            )
            return

        await db.transition_order(order_id, OrderState.CLOSED.value)

        if order["assigned_reviver_id"] is not None:
            assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])
            if assigned_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    order_id,
                    assigned_reviver["discord_id"],
                )

        await notifications.send_order_canceled_notice(
            interaction.client,
            order_id,
            order["buyer_torn_id"],
            order["target_torn_id"],
        )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            order_id,
            event="closed",
        )
        await interaction.response.send_message(
            f"Order `{order_id}` has been canceled and closed.",
            ephemeral=True,
        )

    @app_commands.command(
        name="dispute",
        description="Dispute a reviver's payment confirmation for one of your orders.",
    )
    @app_commands.checks.has_any_role(*BUYER_ACCESS_ROLE_NAMES)
    @app_commands.describe(reason="Why you think the payment confirmation is incorrect")
    async def dispute(self, interaction: discord.Interaction, order_id: str, reason: str):
        order = await db.get_order(order_id)
        if order is None:
            await interaction.response.send_message(NO_ORDER_MESSAGE, ephemeral=True)
            return
        if order["buyer_discord_id"] != str(interaction.user.id):
            await interaction.response.send_message(
                NOT_YOUR_ORDER_MESSAGE, ephemeral=True
            )
            return
        if order["state"] not in {OrderState.DELIVERED.value, OrderState.PAID.value}:
            await interaction.response.send_message(
                "That order can't be disputed in its current state.", ephemeral=True
            )
            return

        await db.update_order(
            order_id,
            disputed_at=time.time(),
            disputed_by_discord_id=str(interaction.user.id),
            dispute_reason=reason,
        )
        await db.transition_order(order_id, OrderState.FLAGGED_FOR_REVIEW.value)
        await db.open_mod_review(order_id, f"Buyer dispute: {reason}")

        if order["assigned_reviver_id"] is not None:
            assigned_reviver = await db.get_reviver(order["assigned_reviver_id"])
            if assigned_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    order_id,
                    assigned_reviver["discord_id"],
                )

        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            order_id,
            event="review",
        )

        if cfg.mod_queue_channel_id:
            channel = interaction.client.get_channel(cfg.mod_queue_channel_id)
            if channel is not None:
                embed = discord.Embed(
                    title=f"Dispute opened for {order_id}",
                    color=discord.Color.red(),
                    description=reason,
                )
                embed.add_field(name="Buyer Torn ID", value=torn_link(order["buyer_torn_id"]), inline=True)
                embed.add_field(
                    name="Assigned reviver",
                    value=torn_link(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else "Unassigned",
                    inline=True,
                )
                await channel.send(embed=embed)

        await interaction.response.send_message(
            f"Dispute opened for order `{order_id}`. A moderator will review it.",
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message(
                "You need the buyer role to use this command.",
                ephemeral=True,
            )
        elif isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Admin permissions are required for this command.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(BuyerCog(bot))
