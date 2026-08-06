from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
import notifications

ADMIN_ONLY_MESSAGE = "Admin permissions are required to change bot destinations."

DESTINATION_CHOICES = [
    app_commands.Choice(name="Storefront guild", value="storefront_guild_id"),
    app_commands.Choice(name="Ops guild", value="ops_guild_id"),
    app_commands.Choice(name="Buyer channel", value="buyer_channel_id"),
    app_commands.Choice(name="Reviver ping channel", value="reviver_ping_channel_id"),
    app_commands.Choice(name="Forwarding channel", value="forwarding_channel_id"),
    app_commands.Choice(name="Ops channel", value="ops_channel_id"),
    app_commands.Choice(name="Mod queue channel", value="mod_queue_channel_id"),
]

GUILD_DESTINATIONS = {
    "storefront_guild_id",
    "ops_guild_id",
}

CHANNEL_DESTINATIONS = {
    "buyer_channel_id",
    "reviver_ping_channel_id",
    "forwarding_channel_id",
    "ops_channel_id",
    "mod_queue_channel_id",
}

DESTINATION_LABELS = {
    "storefront_guild_id": "storefront guild",
    "ops_guild_id": "ops guild",
    "buyer_channel_id": "buyer channel",
    "reviver_ping_channel_id": "reviver ping channel",
    "forwarding_channel_id": "forwarding channel",
    "ops_channel_id": "ops channel",
    "mod_queue_channel_id": "mod queue channel",
}


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="set_destination", description="Bind a bot destination to this guild or channel ID.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel to bind for channel destinations in this guild",
        channel_id="Raw channel ID to bind for destinations in another guild",
    )
    @app_commands.choices(target=DESTINATION_CHOICES)
    async def set_destination(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
        channel_id: str | None = None,
    ):
        setting_name = target.value
        label = DESTINATION_LABELS[setting_name]

        if setting_name in GUILD_DESTINATIONS:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This command has to be used inside the guild you want to bind.",
                    ephemeral=True,
                )
                return

            await db.upsert_setting(setting_name, interaction.guild.id)
            await interaction.response.send_message(
                f"Set {label} to {interaction.guild.name} ({interaction.guild.id}).",
                ephemeral=True,
            )
            return

        resolved_channel = channel
        if resolved_channel is None and channel_id:
            try:
                resolved_channel = await interaction.client.fetch_channel(int(str(channel_id).strip()))
            except (TypeError, ValueError):
                await interaction.response.send_message(
                    "The channel ID has to be a whole number.",
                    ephemeral=True,
                )
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message(
                    "I couldn't resolve that channel ID. Make sure the bot can see that channel.",
                    ephemeral=True,
                )
                return

        if resolved_channel is None:
            resolved_channel = interaction.channel
        if resolved_channel is None or not hasattr(resolved_channel, "id"):
            await interaction.response.send_message(
                "I need a guild text channel or raw channel ID to store that destination.",
                ephemeral=True,
            )
            return

        await db.upsert_setting(setting_name, int(resolved_channel.id))

        if setting_name == "buyer_channel_id":
            await notifications.refresh_online_revivers_list(interaction.client)
        elif setting_name == "reviver_ping_channel_id":
            await notifications.refresh_active_order_reminder(interaction.client)

        await interaction.response.send_message(
            f"Set {label} to <#{resolved_channel.id}>.",
            ephemeral=True,
        )

    @set_destination.error
    async def set_destination_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Admin permissions are required to change bot destinations.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
