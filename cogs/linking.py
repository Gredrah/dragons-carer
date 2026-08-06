from __future__ import annotations

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import db
from role_sync import format_torn_nickname, sync_member_nickname, sync_member_roles, sync_member_verification
import torn_api
from formatting import torn_link
from state import Tier, tier_from_skill


BUYER_API_KEY_URL = "https://www.torn.com/preferences.php#tab=api?step=addNewKey&title=ReviveStorefront-Buyer&user=basic,revivesfull"
REVIVER_API_KEY_URL = "https://www.torn.com/preferences.php#tab=api?step=addNewKey&title=ReviveStorefront-Reviver&user=basic,revivesfull,skills"
TORN_API_UNAVAILABLE_MESSAGE = "Couldn't reach the Torn API right now — try again in a moment."


def _parse_tier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"standard", "std"}:
        return Tier.STANDARD.value
    if normalized in {"75", "75+", "t75"}:
        return Tier.T75.value
    if normalized in {"100", "100+", "t100"}:
        return Tier.T100.value
    raise ValueError("tier must be one of: standard, 75, 100")


def build_buyer_registration_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Buyer Registration Panel",
        description=(
            "Register as a buyer to get access to revive requests.\n"
            "If you need to remove a link later, use `/unregister`."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Instructions",
        value=(
            f"Use [this Torn API key generator]({BUYER_API_KEY_URL}) to create the buyer key, "
            "then link it below. The bot will grant the @buyer role."
        ),
        inline=False,
    )
    embed.set_footer(text="buyer_registration_panel")
    return embed


def build_reviver_registration_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Seller / Medic Registration Panel",
        description=(
            "Register as a seller/medic to get the reviver roles used by the bot.\n"
            "If you need to remove a link later, use `/unregister`."
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Instructions",
        value=(
            f"Use [this Torn API key generator]({REVIVER_API_KEY_URL}) to create the reviver key, "
            "then link it below. The bot will detect your tier from your revive skill."
        ),
        inline=False,
    )
    embed.set_footer(text="reviver_registration_panel")
    return embed


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


class BuyerRegistrationModal(discord.ui.Modal, title="Buyer Registration"):
    def __init__(self, linking_cog: "LinkingCog"):
        super().__init__(timeout=None)
        self.linking_cog = linking_cog
        self.api_key = discord.ui.TextInput(
            label="Torn API key",
            placeholder="Paste a Torn key with basic and revivesFull selections enabled",
            required=True,
            max_length=120,
        )
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction):
        await self.linking_cog._register_buyer(interaction, self.api_key.value)


class SellerRegistrationModal(discord.ui.Modal, title="Seller / Medic Registration"):
    def __init__(self, linking_cog: "LinkingCog"):
        super().__init__(timeout=None)
        self.linking_cog = linking_cog
        self.api_key = discord.ui.TextInput(
            label="Torn API key",
            placeholder="Paste a Torn key with basic, revivesFull, and Skills selections enabled",
            required=True,
            max_length=120,
        )
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction):
        await self.linking_cog._register_reviver(interaction, self.api_key.value)


class BuyerRegistrationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Generate Buyer API Key",
                style=discord.ButtonStyle.link,
                url=BUYER_API_KEY_URL,
            )
        )

    @discord.ui.button(label="Register as Buyer", style=discord.ButtonStyle.success, custom_id="registration_panel:buyer")
    async def register_buyer(self, interaction: discord.Interaction, button: discord.ui.Button):
        linking_cog = interaction.client.get_cog("LinkingCog")
        if linking_cog is None:
            await interaction.response.send_message(
                "The registration system is not ready yet. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(BuyerRegistrationModal(linking_cog))


class ReviverRegistrationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Generate Seller / Medic API Key",
                style=discord.ButtonStyle.link,
                url=REVIVER_API_KEY_URL,
            )
        )

    @discord.ui.button(label="Register as Seller / Medic", style=discord.ButtonStyle.primary, custom_id="registration_panel:seller")
    async def register_seller(self, interaction: discord.Interaction, button: discord.ui.Button):
        linking_cog = interaction.client.get_cog("LinkingCog")
        if linking_cog is None:
            await interaction.response.send_message(
                "The registration system is not ready yet. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SellerRegistrationModal(linking_cog))


class LinkingCog(commands.Cog):
    """
    Everything else in this bot (/request, /status, /available, payment
    polling) depends on being able to answer "what Torn ID/key belongs to
    this Discord user?" -- this cog is what actually answers that question.

    Design choice: we verify the key against Torn's own API rather than
    trusting a self-reported Torn ID, so nobody can place orders or claim
    reviver status as someone else.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _verify_torn_identity(self, api_key: str) -> torn_api.TornIdentity:
        async with aiohttp.ClientSession() as session:
            return await torn_api.verify_key_and_get_identity(session, api_key)

    async def _register_buyer(self, interaction: discord.Interaction, api_key: str) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            identity = await self._verify_torn_identity(api_key)
        except torn_api.TornAPIError as e:
            await interaction.followup.send(
                f"Torn rejected that key: {e.message}. Double-check it's active (keys can be regenerated/invalidated from Torn's preferences page).",
                ephemeral=True,
            )
            return
        except Exception:
            await interaction.followup.send(TORN_API_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        await db.upsert_buyer(identity.torn_id, str(interaction.user.id), api_key)
        nickname = format_torn_nickname(identity.name, identity.torn_id)
        nickname_synced = await sync_member_nickname(
            interaction.client,
            str(interaction.user.id),
            reason="buyer registration synced nickname",
            api_key=api_key,
            identity=identity,
        )
        await sync_member_roles(
            interaction.client,
            str(interaction.user.id),
            reason="buyer registration synced roles",
        )
        await sync_member_verification(
            interaction.client,
            str(interaction.user.id),
            reason="buyer registration synced verification roles",
            verified=True,
        )
        nickname_message = f" Your nickname is now {nickname}." if nickname_synced else ""
        await interaction.followup.send(
            f"Linked. Your Discord account is now tied to Torn ID {torn_link(identity.torn_id)} as a buyer.{nickname_message} The @buyer role has been synced where available.",
            ephemeral=True,
        )

    async def _register_reviver(
        self,
        interaction: discord.Interaction,
        api_key: str,
        discord_id: str | None = None,
        display_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Self-service path (seller/medic registration panel): no explicit
        # member is passed, so register the invoking user themselves. The
        # /link_reviver slash command still passes an explicit member for the
        # mod-driven case. Self-service is safe here because tier is derived
        # from get_revive_skill (the person's own key, verified ownership)
        # rather than being a free-form claim, and the panel embed itself is
        # only meant to be posted in mod/medic-restricted channels.
        if discord_id is None:
            discord_id = str(interaction.user.id)
        if display_name is None:
            display_name = interaction.user.mention

        try:
            identity = await self._verify_torn_identity(api_key)
        except torn_api.TornAPIError as e:
            await interaction.followup.send(f"Torn rejected that key: {e.message}.", ephemeral=True)
            return
        except Exception:
            await interaction.followup.send(TORN_API_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            try:
                skill_level = await torn_api.get_revive_skill(session, api_key)
            except torn_api.TornAPIError as e:
                await interaction.followup.send(
                    f"Torn rejected the skill lookup: {e.message}.",
                    ephemeral=True,
                )
                return
            except Exception:
                await interaction.followup.send(TORN_API_UNAVAILABLE_MESSAGE, ephemeral=True)
                return

        tier = tier_from_skill(skill_level)

        await db.upsert_reviver(identity.torn_id, discord_id, api_key, tier)
        nickname = format_torn_nickname(identity.name, identity.torn_id)
        nickname_synced = await sync_member_nickname(
            interaction.client,
            discord_id,
            reason="reviver registration synced nickname",
            api_key=api_key,
            identity=identity,
        )
        await sync_member_roles(
            interaction.client,
            discord_id,
            reason="reviver registration synced roles",
        )
        await sync_member_verification(
            interaction.client,
            discord_id,
            reason="reviver registration synced verification roles",
            verified=True,
        )
        nickname_message = f" Your nickname is now {nickname}." if nickname_synced else ""
        await interaction.followup.send(
            f"Registered {display_name} as a **{tier}** reviver (Torn ID {torn_link(identity.torn_id)}) based on revive skill.{nickname_message} Synced the reviver roles where available.",
            ephemeral=True,
        )

    @app_commands.command(
        name="link",
        description="Link your Discord account as a buyer using your Torn API key.",
    )
    @app_commands.describe(
        api_key="A Torn API key with at least the 'basic' selection enabled."
    )
    async def link_buyer(self, interaction: discord.Interaction, api_key: str):
        await self._register_buyer(interaction, api_key)

    @app_commands.command(
        name="link_reviver",
        description="Link a Discord account as a reviver using the account's revive skill.",
    )
    @app_commands.describe(
        member="The Discord member to register.",
        api_key="A Torn API key with at least the 'basic' and 'skills' selections enabled.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def link_reviver(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        api_key: str,
    ):
        # Mod-gated and takes an explicit `member` because tier assignment is a
        # trust decision you make about someone else, not a self-service claim
        # -- deliberately different shape from /link above.
        await self._register_reviver(interaction, api_key, str(member.id), member.mention)

    @app_commands.command(name="register_panel", description="Post a registration panel in this channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(panel_type="Which registration panel to post")
    @app_commands.choices(
        panel_type=[
            app_commands.Choice(name="Buyer", value="buyer"),
            app_commands.Choice(name="Seller / Medic", value="reviver"),
        ]
    )
    async def register_panel(self, interaction: discord.Interaction, panel_type: app_commands.Choice[str]):
        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I can't post the panel here.",
                ephemeral=True,
            )
            return

        is_buyer = panel_type.value == "buyer"
        title = "Buyer Registration Panel" if is_buyer else "Seller / Medic Registration Panel"
        marker = "buyer_registration_panel" if is_buyer else "reviver_registration_panel"

        if interaction.client.user is not None:
            await _delete_previous_panel_messages(
                interaction.channel,
                interaction.client.user.id,
                title=title,
                marker=marker,
            )

        embed = build_buyer_registration_panel_embed() if is_buyer else build_reviver_registration_panel_embed()
        view = BuyerRegistrationPanelView() if is_buyer else ReviverRegistrationPanelView()

        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"{panel_type.name} registration panel posted.",
            ephemeral=True,
        )

    @app_commands.command(name="unregister", description="Remove your buyer and/or reviver link from the bot.")
    @app_commands.describe(
        registration="Which link should be removed?",
    )
    @app_commands.choices(
        registration=[
            app_commands.Choice(name="Buyer", value="buyer"),
            app_commands.Choice(name="Seller / Medic", value="seller"),
            app_commands.Choice(name="Both", value="all"),
        ]
    )
    async def unregister(self, interaction: discord.Interaction, registration: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        removed: list[str] = []

        if registration.value in {"buyer", "all"}:
            if await db.get_buyer_by_discord(user_id) is not None:
                await db.delete_buyer_by_discord(user_id)
                removed.append("buyer")

        if registration.value in {"seller", "all"}:
            if await db.get_reviver_by_discord(user_id) is not None:
                await db.delete_reviver_by_discord(user_id)
                removed.append("seller / medic")

        if not removed:
            await interaction.followup.send(
                "You don't have a linked buyer or seller / medic account to remove.",
                ephemeral=True,
            )
            return

        still_verified = (
            await db.get_buyer_by_discord(user_id) is not None
            or await db.get_reviver_by_discord(user_id) is not None
        )

        await sync_member_roles(
            interaction.client,
            user_id,
            reason="user unregistered from storefront roles",
        )
        await sync_member_verification(
            interaction.client,
            user_id,
            reason="user unregistered from storefront verification roles",
            verified=still_verified,
        )
        await interaction.followup.send(
            f"Removed: {', '.join(removed)}. Your roles were synced afterward.",
            ephemeral=True,
        )

    @link_reviver.error
    async def link_reviver_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Registering revivers is mod-only — ask a moderator to run this for you.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkingCog(bot))