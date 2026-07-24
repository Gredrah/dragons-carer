from __future__ import annotations

import aiohttp
import json
import time

import discord
from discord import app_commands
from discord.ext import commands

import assignment
import db
from formatting import torn_link
import notifications
from queue_refresh import refresh_queued_orders
import torn_api
from state import OrderState, payment_window_seconds
from config import cfg
from permissions import BUYER_ACCESS_ROLE_NAMES, REVIVER_ROLE_NAMES, deny_if_missing_role


REVIVER_ONLY_BUTTON_MESSAGE = "This button is only available to revivers."
REVIVER_STATUS_PANEL_TITLE = "Reviver Status Panel"
REVIVER_STATUS_PANEL_MARKER = "reviver_status_panel"
FORWARDING_ONLY_BUTTON_MESSAGE = "This button is only available in the forwarding channel."
NO_ORDER_MESSAGE = "No order with that ID."

async def _refresh_order_views(interaction: discord.Interaction, order_id: str, *, event: str) -> None:
    await notifications.refresh_reviver_order_channel_log(interaction.client, order_id, event=event)
    await notifications.refresh_active_order_reminder(interaction.client)


def build_reviver_status_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=REVIVER_STATUS_PANEL_TITLE,
        description=(
            "Use the buttons below to mark yourself available or unavailable for revives.\n"
            "If something is off, use Contact Mods to flag it right away."
        ),
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="When you go online",
        value="Queued orders are refreshed so you can be assigned immediately if work is waiting.",
        inline=False,
    )
    embed.add_field(
        name="When you go offline",
        value="You will be skipped by the assignment queue until you mark yourself back online.",
        inline=False,
    )
    embed.set_footer(text=REVIVER_STATUS_PANEL_MARKER)
    return embed


class ReviverIssueModal(discord.ui.Modal, title="Flag an Issue"):
    def __init__(self, panel_view: "ReviverStatusPanelView"):
        super().__init__(timeout=None)
        self.panel_view = panel_view
        self.issue_details = discord.ui.TextInput(
            label="What should mods know?",
            placeholder="Explain the issue, and include an order ID if relevant.",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.issue_details)

    async def on_submit(self, interaction: discord.Interaction):
        await self.panel_view.submit_issue_report(interaction, self.issue_details.value)


class AssignmentView(discord.ui.View):
    """Sent to a reviver when an order is assigned to them. Persistent (timeout=None)
    so it keeps working across bot restarts -- register with bot.add_view() on startup."""

    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        # Encode order_id into custom_id so a restarted bot can still route clicks.
        self.claim.custom_id = f"claim:{order_id}"
        self.forward.custom_id = f"forward:{order_id}"

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] != OrderState.ASSIGNED.value:
            await interaction.response.send_message(
                "This order is no longer available to claim.", ephemeral=True
            )
            return

        lookup_key = await db.get_api_key_for_torn_id(order["buyer_torn_id"])
        if lookup_key is not None:
            async with aiohttp.ClientSession() as session:
                try:
                    status = await torn_api.get_hospital_status(
                        session, order["target_torn_id"], lookup_key
                    )
                except Exception:
                    status = None

            if status is not None and not status.in_hospital:
                await db.transition_order(self.order_id, OrderState.CLOSED_NO_ACTION.value)
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    interaction.user.id,
                )
                await interaction.response.edit_message(
                    content=(
                        f"Order `{self.order_id}` closed: the target left hospital before the revive was claimed."
                    ),
                    view=None,
                )
                await notifications.send_dm(
                    interaction.client,
                    order["buyer_discord_id"],
                    (
                        f"Order `{self.order_id}` closed: target {torn_link(order['target_torn_id'])} left hospital "
                        "before a revive could be claimed."
                    ),
                )
                return

        await db.transition_order(self.order_id, OrderState.CLAIMED.value)
        await db.update_order(self.order_id, claimed_at=time.time())
        await notifications.refresh_reviver_order_dm(
            interaction.client,
            self.order_id,
            interaction.user.id,
        )
        await interaction.response.edit_message(
            content=f"Order `{self.order_id}` claimed by {interaction.user.mention}.",
            view=DeliveredView(self.order_id),
        )
        await _refresh_order_views(interaction, self.order_id, event="claimed")

    @discord.ui.button(label="Forward to next reviver", style=discord.ButtonStyle.secondary)
    async def forward(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] != OrderState.ASSIGNED.value:
            await interaction.response.send_message(
                "This order can no longer be forwarded.", ephemeral=True
            )
            return

        tried = set(json.loads(order["reviver_attempt_history"]))
        tried.add(order["assigned_reviver_id"])
        previous_reviver = await db.get_reviver(order["assigned_reviver_id"])

        next_reviver = await assignment.pick_reviver(order["tier_requested"], exclude_ids=tried)
        if next_reviver:
            await db.update_order(
                self.order_id,
                assigned_reviver_id=next_reviver["torn_id"],
                assigned_at=time.time(),
                reviver_attempt_history=json.dumps(list(tried)),
            )
            await db.record_reviver_assignment(next_reviver["torn_id"])
            await db.transition_order(self.order_id, OrderState.ASSIGNED.value)
            await interaction.response.edit_message(
                content=f"Order `{self.order_id}` forwarded to the next available reviver.",
                view=None,
            )
            if previous_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    previous_reviver["discord_id"],
                )
            # send a fresh AssignmentView ping to next_reviver's DM/channel.
            # Use discord_id if available, fall back to torn_id (revivers should always have discord_id from linking)
            reviver_identifier = next_reviver["discord_id"]
            await notifications.send_assignment_ping(
                interaction.client, reviver_identifier, self.order_id, view=AssignmentView(self.order_id)
            )
            await _refresh_order_views(interaction, self.order_id, event="available_again")
            await notifications.refresh_active_order_reminder(interaction.client)
        else:
            await db.update_order(
                self.order_id, reviver_attempt_history=json.dumps(list(tried))
            )
            await db.transition_order(self.order_id, OrderState.QUEUED_NO_REVIVER.value)
            await interaction.response.edit_message(
                content=f"Order `{self.order_id}` forwarded — no other reviver online, "
                        "queued until one is.",
                view=ResignalView(self.order_id),
            )
            await notifications.refresh_reviver_order_dm(
                interaction.client,
                self.order_id,
                interaction.user.id,
            )
            await _refresh_order_views(interaction, self.order_id, event="queued")


class ForwardedAssignmentView(discord.ui.View):
    def __init__(self, order_id: str, *, claimant_discord_id: str | None = None):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.claimant_discord_id = claimant_discord_id
        self.claim_forwarded.custom_id = f"forwarded_claim:{order_id}"
        self.release_forwarded.custom_id = f"forwarded_release:{order_id}"
        self.report_forwarded.custom_id = f"forwarded_report:{order_id}"

    def _is_forwarding_channel(self, interaction: discord.Interaction) -> bool:
        return bool(
            cfg.forwarding_channel_id
            and interaction.channel is not None
            and getattr(interaction.channel, "id", None) == cfg.forwarding_channel_id
        )

    async def _require_forwarding_channel(self, interaction: discord.Interaction) -> bool:
        if not self._is_forwarding_channel(interaction):
            await interaction.response.send_message(FORWARDING_ONLY_BUTTON_MESSAGE, ephemeral=True)
            return False
        return True

    def _claimed_by_current_user(self, interaction: discord.Interaction) -> bool:
        return self.claimant_discord_id is not None and self.claimant_discord_id == str(interaction.user.id)

    def _should_show_claim_controls(self) -> bool:
        return self.claimant_discord_id is None

    def _should_show_release_control(self, interaction: discord.Interaction) -> bool:
        return self.claimant_discord_id is not None and self._claimed_by_current_user(interaction)

    def _should_show_report_control(self) -> bool:
        return self.claimant_discord_id is None

    @discord.ui.button(label="Claim for this server", style=discord.ButtonStyle.success)
    async def claim_forwarded(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._should_show_claim_controls():
            await interaction.response.send_message("This order has already been claimed by a third party.", ephemeral=True)
            return
        if await deny_if_missing_role(
            interaction,
            BUYER_ACCESS_ROLE_NAMES,
            "You need buyer access to claim from the forwarding channel.",
        ):
            return
        if not await self._require_forwarding_channel(interaction):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] not in {OrderState.ASSIGNED.value, OrderState.QUEUED_NO_REVIVER.value}:
            await interaction.response.send_message("This order can no longer be claimed here.", ephemeral=True)
            return
        claimed_by = order["forwarding_claimed_by_discord_id"]
        if claimed_by is not None and claimed_by != str(interaction.user.id):
            await interaction.response.send_message("This order is already claimed by another third party.", ephemeral=True)
            return

        original_reviver = None
        if order["assigned_reviver_id"] is not None:
            original_reviver = await db.get_reviver(order["assigned_reviver_id"])

        await db.update_order(
            self.order_id,
            state=OrderState.FORWARDED_CLAIMED.value,
            forwarded_claimed_by_discord_id=str(interaction.user.id),
            forwarded_claimed_at=time.time(),
        )
        profile_embed = await notifications.build_user_profile_embed(
            interaction.client,
            interaction.user.id,
            title=f"Forwarded order claimed: {self.order_id}",
            description="A third party has claimed responsibility for this revive.",
        )
        await notifications.refresh_reviver_order_dm(
            interaction.client,
            self.order_id,
            interaction.user.id,
            view=DeliveredView(self.order_id),
        )
        if original_reviver is not None:
            await notifications.refresh_reviver_order_dm(
                interaction.client,
                self.order_id,
                original_reviver["discord_id"],
            )
        await interaction.response.edit_message(
            content=f"Order `{self.order_id}` claimed by {interaction.user.mention}.",
            embed=profile_embed,
            view=ForwardedAssignmentView(
                self.order_id,
                claimant_discord_id=str(interaction.user.id),
            ),
        )
        await notifications.refresh_forwarding_order_message(
            interaction.client,
            self.order_id,
            event="forwarded_claimed",
        )

    @discord.ui.button(label="Release back to pool", style=discord.ButtonStyle.secondary)
    async def release_forwarded(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._should_show_release_control(interaction):
            await interaction.response.send_message("Only the current third-party claimant can release this order.", ephemeral=True)
            return
        if await deny_if_missing_role(
            interaction,
            BUYER_ACCESS_ROLE_NAMES,
            "You need buyer access to release this order.",
        ):
            return
        if not await self._require_forwarding_channel(interaction):
            return
        order = await db.get_order(self.order_id)
        if order is None:
            await interaction.response.send_message("This order no longer exists.", ephemeral=True)
            return
        claimed_by = order["forwarded_claimed_by_discord_id"]
        if claimed_by is not None and claimed_by != str(interaction.user.id):
            await interaction.response.send_message("Only the current claimant can release this order.", ephemeral=True)
            return

        claimant_discord_id = order["forwarded_claimed_by_discord_id"]

        await db.update_order(
            self.order_id,
            state=OrderState.ASSIGNED.value if order["assigned_reviver_id"] is not None else OrderState.QUEUED_NO_REVIVER.value,
            forwarded_claimed_by_discord_id=None,
            forwarded_claimed_at=None,
        )
        refreshed_view = ForwardedAssignmentView(self.order_id)
        if claimant_discord_id is not None:
            await notifications.refresh_reviver_order_dm(
                interaction.client,
                self.order_id,
                claimant_discord_id,
            )
        if order["assigned_reviver_id"] is not None:
            original_reviver = await db.get_reviver(order["assigned_reviver_id"])
            if original_reviver is not None:
                await notifications.refresh_reviver_order_dm(
                    interaction.client,
                    self.order_id,
                    original_reviver["discord_id"],
                )
        await interaction.response.edit_message(
            content=f"Order `{self.order_id}` was released back into the forwarding pool.",
            embed=None,
            view=refreshed_view,
        )
        await notifications.refresh_forwarding_order_message(
            interaction.client,
            self.order_id,
            event="available_again",
        )

    @discord.ui.button(label="Report bad third party", style=discord.ButtonStyle.danger)
    async def report_forwarded(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._should_show_report_control():
            await interaction.response.send_message("This order is no longer awaiting a third-party claim.", ephemeral=True)
            return
        if await deny_if_missing_role(
            interaction,
            BUYER_ACCESS_ROLE_NAMES,
            "You need access to report this user.",
        ):
            return
        if not await self._require_forwarding_channel(interaction):
            return
        await interaction.response.send_modal(ForwardingReportModal(self.order_id))


class ForwardingReportModal(discord.ui.Modal, title="Report User"):
    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.details = discord.ui.TextInput(
            label="What happened?",
            placeholder="Explain why this user should be reviewed by mods.",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        order = await db.get_order(self.order_id)
        if order is None:
            await interaction.response.send_message(NO_ORDER_MESSAGE, ephemeral=True)
            return

        claimant = order["forwarded_claimed_by_discord_id"]
        report = (
            f"Forwarded order report opened by {interaction.user.mention} (Discord ID {interaction.user.id}).\n"
            f"Order `{self.order_id}` | buyer Torn ID {order['buyer_torn_id']} | target Torn ID {order['target_torn_id']} | tier `{order['tier_requested']}`\n"
            f"Claimed third party: {f'<@{claimant}>' if claimant else 'Unclaimed'}\n"
            f"Details: {self.details.value}"
        )
        sent = await notifications.send_mod_issue_report(interaction.client, report)
        if sent:
            await interaction.response.send_message(
                f"Your report for order `{self.order_id}` was sent to the mods.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "I couldn't find a mod queue channel to send that to. Please ping a mod directly.",
            ephemeral=True,
        )


class ResignalView(discord.ui.View):
    """Shown after a reviver forwards an order with nobody else available.
    Lets them remove themselves from the exclusion list and request a fresh
    reassessment if they still want the revive."""

    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.resignal_interest.custom_id = f"resignal:{order_id}"

    @discord.ui.button(label="Re-signal interest", style=discord.ButtonStyle.primary)
    async def resignal_interest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] != OrderState.QUEUED_NO_REVIVER.value:
            await interaction.response.send_message(
                "This order is no longer waiting for reassignment.", ephemeral=True
            )
            return

        reviver = await db.get_reviver_by_discord(str(interaction.user.id))
        if reviver is None:
            await interaction.response.send_message(
                "You aren't registered as a reviver on this bot.", ephemeral=True
            )
            return

        tried = {
            int(reviver_id)
            for reviver_id in json.loads(order["reviver_attempt_history"])
            if str(reviver_id).isdigit()
        }
        tried.discard(reviver["torn_id"])

        await db.update_order(
            self.order_id,
            reviver_attempt_history=json.dumps(list(tried)),
        )
        reassigned_count = await refresh_queued_orders(interaction.client)
        await notifications.refresh_active_order_reminder(interaction.client)
        await interaction.response.send_message(
            (
                "Your interest has been re-signaled and the queue was refreshed. "
                f"Reassigned {reassigned_count} order(s)."
            ),
            ephemeral=True,
        )


class DeliveredView(discord.ui.View):
    """Sent after a reviver claims -- lets them mark the revive as delivered,
    which starts the payment window clock."""

    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.mark_delivered.custom_id = f"deliver:{order_id}"
        self.buyer_bailed.custom_id = f"buyer_bailed:{order_id}"

    @discord.ui.button(label="Mark Delivered", style=discord.ButtonStyle.primary)
    async def mark_delivered(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] not in {OrderState.CLAIMED.value, OrderState.FORWARDED_CLAIMED.value}:
            await interaction.response.send_message(
                "This order isn't in a deliverable state.", ephemeral=True
            )
            return

        reviver_key = await db.get_api_key_for_torn_id(order["assigned_reviver_id"])
        if reviver_key is None:
            await interaction.response.send_message(
                "I can't verify this revive because the assigned reviver API key is unavailable.",
                ephemeral=True,
            )
            return

        async with aiohttp.ClientSession() as session:
            if (
                not cfg.debug_outgoing_revive_bypass_torn_id
                or order["buyer_torn_id"] != cfg.debug_outgoing_revive_bypass_torn_id
            ):
                try:
                    outgoing_count = await torn_api.count_outgoing_revives_since(
                        session,
                        reviver_key,
                        order["target_torn_id"],
                        int(order["created_at"]),
                    )
                except Exception:
                    await interaction.response.send_message(
                        "I couldn't verify the revive right now. Try again in a moment.",
                        ephemeral=True,
                    )
                    return

                if outgoing_count < int(order["revives_requested"]):
                    await interaction.response.send_message(
                        f"I can only see {outgoing_count} outgoing revive(s) for that target since the order was placed, "
                        f"but this order needs {int(order['revives_requested'])}. I won't mark it delivered yet.",
                        ephemeral=True,
                    )
                    return

        window = payment_window_seconds(int(order["revives_requested"]), cfg)
        expires_at = time.time() + window

        await db.transition_order(self.order_id, OrderState.DELIVERED.value)
        await db.update_order(
            self.order_id, delivered_at=time.time(), payment_window_expires_at=expires_at
        )
        await notifications.refresh_reviver_order_dm(
            interaction.client,
            self.order_id,
            interaction.user.id,
        )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            self.order_id,
            event="delivered",
        )
        await interaction.response.edit_message(
            content=(
                f"Order `{self.order_id}` marked delivered. Payment window closes "
                f"<t:{int(expires_at)}:R>. Confirm payment once the buyer pays."
            ),
            view=PaymentView(self.order_id),
        )
        await _refresh_order_views(interaction, self.order_id, event="delivered")
        # Send payment instructions to the buyer.
        await notifications.send_payment_instructions(
            interaction.client,
            order["buyer_discord_id"],
            self.order_id,
            order["assigned_reviver_id"],
            expires_at,
        )

    @discord.ui.button(label="Buyer bailed", style=discord.ButtonStyle.secondary)
    async def buyer_bailed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return

        order = await db.get_order(self.order_id)
        if order is None or order["state"] not in {OrderState.CLAIMED.value, OrderState.FORWARDED_CLAIMED.value}:
            await interaction.response.send_message(
                "This order can no longer be marked as buyer bailed.",
                ephemeral=True,
            )
            return

        current_assignee_discord_id = None
        if order["state"] == OrderState.CLAIMED.value:
            assigned_reviver = await db.get_reviver(order["assigned_reviver_id"]) if order["assigned_reviver_id"] is not None else None
            if assigned_reviver is None or str(assigned_reviver["discord_id"]) != str(interaction.user.id):
                await interaction.response.send_message(
                    "Only the assigned reviver can mark this order as buyer bailed.",
                    ephemeral=True,
                )
                return
            current_assignee_discord_id = str(assigned_reviver["discord_id"])
        else:
            claimant_discord_id = order["forwarded_claimed_by_discord_id"]
            if claimant_discord_id is None or claimant_discord_id != str(interaction.user.id):
                await interaction.response.send_message(
                    "Only the current claimant can mark this order as buyer bailed.",
                    ephemeral=True,
                )
                return
            current_assignee_discord_id = claimant_discord_id

        await db.transition_order(self.order_id, OrderState.CLOSED_NO_ACTION.value)
        await interaction.response.edit_message(
            content=(
                f"Order `{self.order_id}` closed without action after the buyer went with another revive method."
            ),
            view=None,
        )
        await notifications.refresh_reviver_order_dm(
            interaction.client,
            self.order_id,
            current_assignee_discord_id,
        )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            self.order_id,
            event="closed_no_action",
        )
        await notifications.send_dm(
            interaction.client,
            order["buyer_discord_id"],
            (
                f"Order `{self.order_id}` was closed without action because the buyer went with another revive method "
                "before delivery."
            ),
        )


class PaymentView(discord.ui.View):
    """Shown after delivery so the reviver can confirm payment manually.
    Once confirmed, the buyer gets a short dispute window before the order
    auto-closes."""

    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.confirm_payment.custom_id = f"confirm_payment:{order_id}"

    @discord.ui.button(label="Confirm Payment", style=discord.ButtonStyle.success)
    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        order = await db.get_order(self.order_id)
        if order is None or order["state"] != OrderState.DELIVERED.value:
            await interaction.response.send_message(
                "This order can no longer be confirmed.", ephemeral=True
            )
            return

        now = time.time()
        dispute_window_expires_at = now + cfg.payment_dispute_window_seconds

        await db.transition_order(self.order_id, OrderState.PAID.value)
        await db.update_order(
            self.order_id,
            paid_confirmed_at=now,
            dispute_window_expires_at=dispute_window_expires_at,
        )
        await notifications.refresh_reviver_order_dm(
            interaction.client,
            self.order_id,
            interaction.user.id,
        )
        await notifications.refresh_reviver_order_channel_log(
            interaction.client,
            self.order_id,
            event="paid",
        )
        await interaction.response.edit_message(
            content=(
                f"Payment confirmed for order `{self.order_id}`. "
                f"Buyer dispute window closes <t:{int(dispute_window_expires_at)}:R>."
            ),
            view=None,
        )
        await _refresh_order_views(interaction, self.order_id, event="paid")
        # Notify the buyer that payment was confirmed.
        await notifications.send_payment_confirmed_notification(
            interaction.client,
            order["buyer_discord_id"],
            self.order_id,
            dispute_window_expires_at,
        )


class ReviverStatusPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.set_online.custom_id = "reviver_status:online"
        self.set_offline.custom_id = "reviver_status:offline"
        self.contact_mods.custom_id = "reviver_status:contact_mods"

    async def _set_status(self, interaction: discord.Interaction, status: str) -> None:
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return

        reviver = await db.get_reviver_by_discord(str(interaction.user.id))
        if reviver is None:
            await interaction.response.send_message(
                "You're not registered as a reviver yet. Contact a mod to get set up.",
                ephemeral=True,
            )
            return

        await db.set_reviver_status(reviver["torn_id"], status)
        message = f"You're now marked **{status}**."
        if status == "online":
            reassigned_count = await refresh_queued_orders(interaction.client)
            if reassigned_count:
                message += f" Refreshed and reassigned {reassigned_count} queued order(s)."
            else:
                message += " No queued orders were ready for reassignment."

        await interaction.response.send_message(message, ephemeral=True)

    async def submit_issue_report(self, interaction: discord.Interaction, issue_details: str) -> None:
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        reviver = await db.get_reviver_by_discord(str(interaction.user.id))
        status = reviver["status"] if reviver is not None else "unknown"
        torn_id = reviver["torn_id"] if reviver is not None else "unlinked"

        report = (
            f"Reviver issue reported by {interaction.user.mention} (Discord ID {interaction.user.id}, Torn ID {torn_id}, status `{status}`).\n"
            f"Details: {issue_details}"
        )
        sent = await notifications.send_mod_issue_report(interaction.client, report)
        if sent:
            await interaction.followup.send("Your issue was sent to the mods.", ephemeral=True)
            return

        await interaction.followup.send(
            "I couldn't find a mod queue channel to send that to. Please ping a mod directly.",
            ephemeral=True,
        )


    @discord.ui.button(label="Set Online", style=discord.ButtonStyle.success)
    async def set_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "online")

    @discord.ui.button(label="Set Offline", style=discord.ButtonStyle.secondary)
    async def set_offline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "offline")

    @discord.ui.button(label="Contact Mods", style=discord.ButtonStyle.danger)
    async def contact_mods(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_if_missing_role(
            interaction,
            REVIVER_ROLE_NAMES,
            REVIVER_ONLY_BUTTON_MESSAGE,
        ):
            return
        await interaction.response.send_modal(ReviverIssueModal(self))


class ReviverCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="available", description="Toggle your reviver availability.")
    @app_commands.checks.has_any_role(*REVIVER_ROLE_NAMES)
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Online", value="online"),
            app_commands.Choice(name="Offline", value="offline"),
        ]
    )
    async def available(self, interaction: discord.Interaction, status: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        reviver = await db.get_reviver_by_discord(str(interaction.user.id))
        if reviver is None:
            await interaction.followup.send(
                "You're not registered as a reviver yet. Contact a mod to get set up.",
                ephemeral=True,
            )
            return
        await db.set_reviver_status(reviver["torn_id"], status.value)
        message = f"You're now marked **{status.value}**."
        if status.value == "online":
            reassigned_count = await refresh_queued_orders(interaction.client)
            if reassigned_count:
                message += f" Refreshed and reassigned {reassigned_count} queued order(s)."
            else:
                message += " No queued orders were ready for reassignment."
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="status_panel", description="Post the reviver status panel in this channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def status_panel(self, interaction: discord.Interaction):
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
                title=REVIVER_STATUS_PANEL_TITLE,
                marker=REVIVER_STATUS_PANEL_MARKER,
            )

        await interaction.channel.send(
            embed=build_reviver_status_panel_embed(),
            view=ReviverStatusPanelView(),
        )
        await interaction.response.send_message(
            "Reviver status panel posted.",
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message(
                "You need a reviver role to use this command.",
                ephemeral=True,
            )


async def _delete_previous_panel_messages(channel: discord.abc.Messageable, bot_id: int, *, title: str, marker: str) -> None:
    if not hasattr(channel, "history"):
        return

    async for message in channel.history(limit=None, oldest_first=False):
        if getattr(message.author, "id", None) != bot_id:
            continue

        if not message.embeds:
            continue

        embed = message.embeds[0]
        footer_text = getattr(getattr(embed, "footer", None), "text", "") or ""
        if embed.title == title or footer_text == marker:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(ReviverCog(bot))
