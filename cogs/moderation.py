from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

import db
import notifications
from config import cfg
from state import OrderState
from formatting import torn_link
from permissions import deny_if_not_admin


ADMIN_ONLY_MESSAGE = "Admin permissions are required for moderation actions."


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="review", description="Review a flagged order (mod only).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def review(self, interaction: discord.Interaction, order_id: str):
        order = await db.get_order(order_id)
        if order is None:
            await interaction.response.send_message("No such order.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Order {order_id}", color=discord.Color.orange())
        embed.add_field(name="State", value=order["state"], inline=True)
        embed.add_field(name="Buyer (Torn ID)", value=torn_link(order["buyer_torn_id"]), inline=True)
        embed.add_field(name="Target (Torn ID)", value=torn_link(order["target_torn_id"]), inline=True)
        embed.add_field(name="Tier", value=order["tier_requested"], inline=True)
        embed.add_field(
            name="Assigned reviver",
            value=torn_link(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else "Unassigned",
            inline=True,
        )
        if order["forwarded_claimed_by_discord_id"]:
            claimant_embed = await notifications.build_user_profile_embed(
                interaction.client,
                order["forwarded_claimed_by_discord_id"],
                title=f"Third party claimant for {order_id}",
                description="This user claimed the revive in the forwarding channel.",
            )
            embed.description = claimant_embed.description
            if claimant_embed.thumbnail and claimant_embed.thumbnail.url:
                embed.set_thumbnail(url=claimant_embed.thumbnail.url)
            for field in claimant_embed.fields:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)
        await interaction.response.send_message(embed=embed, view=ResolutionView(order_id))

    @review.error
    async def review_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Admin permissions are required for moderation commands.", ephemeral=True
            )


class ResolutionView(discord.ui.View):
    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.reinstate.custom_id = f"review:{order_id}:reinstate"
        self.blacklist.custom_id = f"review:{order_id}:blacklist"
        self.reviver_fault.custom_id = f"review:{order_id}:reviver_fault"

    @discord.ui.button(label="Reinstate buyer / close order", style=discord.ButtonStyle.success)
    async def reinstate(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_not_admin(interaction, ADMIN_ONLY_MESSAGE):
            return
        order = await db.get_order(self.order_id)
        await db.transition_order(self.order_id, OrderState.CLOSED.value)
        await db.resolve_mod_review(self.order_id, str(interaction.user.id), "reinstated")
        if order is not None and order["assigned_reviver_id"] is not None:
            reviver = await db.get_reviver(order["assigned_reviver_id"])
            if reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    reviver["discord_id"],
                )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            self.order_id,
            event="closed",
        )
        await interaction.response.edit_message(
            content=f"Order `{self.order_id}` closed, buyer reinstated.", embed=None, view=None
        )

    @discord.ui.button(label="Blacklist buyer", style=discord.ButtonStyle.danger)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_not_admin(interaction, ADMIN_ONLY_MESSAGE):
            return
        order = await db.get_order(self.order_id)
        if order["forwarded_claimed_by_discord_id"]:
            await db.set_buyer_status(order["buyer_torn_id"], "blacklisted", reason=f"third party discord {order['forwarded_claimed_by_discord_id']} on order {self.order_id}")
        else:
            await db.set_buyer_status(order["buyer_torn_id"], "blacklisted", reason=f"order {self.order_id}")
        await db.transition_order(self.order_id, OrderState.BLACKLISTED_ORDER.value)
        await db.resolve_mod_review(self.order_id, str(interaction.user.id), "blacklisted")
        if order is not None and order["assigned_reviver_id"] is not None:
            reviver = await db.get_reviver(order["assigned_reviver_id"])
            if reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    reviver["discord_id"],
                )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            self.order_id,
            event="blacklisted",
        )
        await interaction.response.edit_message(
            content=f"Buyer blacklisted, order `{self.order_id}` closed.", embed=None, view=None
        )

    @discord.ui.button(label="Reviver at fault — reassign", style=discord.ButtonStyle.secondary)
    async def reviver_fault(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_not_admin(interaction, ADMIN_ONLY_MESSAGE):
            return
        order = await db.get_order(self.order_id)
        previous_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
        import state as state_mod
        await db.log_incident(
            order["assigned_reviver_id"], self.order_id, state_mod.IncidentType.STALL_DELIVERY.value
        )

        import assignment
        import json as json_mod

        tried = set()
        try:
            tried = {
                int(reviver_id) for reviver_id in json_mod.loads(order["reviver_attempt_history"])
            }
        except Exception:
            tried = set()
        if order["assigned_reviver_id"] is not None:
            tried.add(order["assigned_reviver_id"])

        next_reviver = await assignment.pick_reviver(order["tier_requested"], exclude_ids=tried)

        if next_reviver is not None:
            await db.update_order(
                self.order_id,
                assigned_reviver_id=next_reviver["torn_id"],
                assigned_at=time.time(),
                reviver_attempt_history=json_mod.dumps(list(tried)),
            )
            await db.record_reviver_assignment(next_reviver["torn_id"])
            await db.transition_order(self.order_id, OrderState.ASSIGNED.value)
            await db.resolve_mod_review(self.order_id, str(interaction.user.id), "reviver_fault_reassigned")

            from cogs.reviver import AssignmentView

            if previous_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    previous_reviver["discord_id"],
                )
            reviver_identifier = next_reviver["discord_id"]
            await notifications.send_assignment_ping(
                interaction.client,
                reviver_identifier,
                self.order_id,
                view=AssignmentView(self.order_id),
            )
            await notifications.refresh_reviver_order_channel_log(
                interaction.client,
                self.order_id,
                event="available_again",
            )
            await interaction.response.edit_message(
                content=(
                    f"Incident logged against the previous reviver. Order `{self.order_id}` "
                    "reassigned to a new reviver and pinged."
                ),
                embed=None,
                view=None,
            )
        else:
            await db.update_order(
                self.order_id, reviver_attempt_history=json_mod.dumps(list(tried))
            )
            await db.transition_order(self.order_id, OrderState.QUEUED_NO_REVIVER.value)
            await db.resolve_mod_review(self.order_id, str(interaction.user.id), "reviver_fault_queued")
            if previous_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    previous_reviver["discord_id"],
                )
            await notifications.refresh_reviver_order_channel_log(
                interaction.client,
                self.order_id,
                event="queued",
            )
            await interaction.response.edit_message(
                content=(
                    f"Incident logged against the previous reviver. No other reviver is currently "
                    f"online for order `{self.order_id}` -- it's queued until one is."
                ),
                embed=None,
                view=None,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
