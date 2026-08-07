from __future__ import annotations

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import db
from config import cfg
from role_sync import (
    format_torn_nickname,
    sync_linked_member_state,
    sync_member_nickname,
    sync_member_roles,
    sync_member_verification,
)
import torn_api
from formatting import torn_link
from state import Tier, tier_from_skill


BUYER_API_KEY_URL = "https://www.torn.com/preferences.php#tab=api?step=addNewKey&title=ReviveStorefront-Buyer&user=basic,revivesfull"
REVIVER_API_KEY_URL = "https://www.torn.com/preferences.php#tab=api?step=addNewKey&title=ReviveStorefront-Reviver&user=basic,revivesfull,skills"
TORN_API_UNAVAILABLE_MESSAGE = "Couldn't reach the Torn API right now — try again in a moment."
BUYER_REGISTRATION_PANEL_TITLE = "Buyer Registration Panel"
REVIVER_REGISTRATION_PANEL_TITLE = "Seller / Medic Registration Panel"
BUYER_REGISTRATION_PANEL_MARKER = "buyer_registration_panel"
REVIVER_REGISTRATION_PANEL_MARKER = "reviver_registration_panel"


def _parse_tier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"standard", "std"}:
        return Tier.STANDARD.value
    if normalized in {"75", "75+", "t75"}:
        return Tier.T75.value
    if normalized in {"100", "100+", "t100"}:
        return Tier.T100.value
    raise ValueError("tier must be one of: standard, 75, 100")


def _normalize_faction_name(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def _format_faction_requirement_label(required_faction_id: int, required_faction_name: str) -> str:
    normalized_name = required_faction_name.strip()
    if normalized_name and required_faction_id:
        return f"{normalized_name} (Torn faction ID {required_faction_id})"
    if normalized_name:
        return normalized_name
    if required_faction_id:
        return f"Torn faction ID {required_faction_id}"
    return "the configured Torn faction"


def _identity_matches_required_faction(
    identity_faction_id: int | None,
    identity_faction_name: str | None,
    required_faction_id: int,
    required_faction_name: str,
) -> bool:
    if required_faction_id and identity_faction_id == required_faction_id:
        return True

    required_name = _normalize_faction_name(required_faction_name)
    if required_name and _normalize_faction_name(identity_faction_name) == required_name:
        return True

    return False


def build_buyer_registration_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=BUYER_REGISTRATION_PANEL_TITLE,
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
    embed.set_footer(text=BUYER_REGISTRATION_PANEL_MARKER)
    return embed


def build_reviver_registration_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=REVIVER_REGISTRATION_PANEL_TITLE,
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
    if cfg.reviver_registration_faction_match_enabled:
        embed.add_field(
            name="Faction requirement",
            value=(
                "Only members of "
                f"{_format_faction_requirement_label(cfg.reviver_registration_faction_id, cfg.reviver_registration_faction_name)} "
                "can register as seller / medic."
            ),
            inline=False,
        )
    embed.set_footer(text=REVIVER_REGISTRATION_PANEL_MARKER)
    return embed


def _panel_marker_for_embed(embed: discord.Embed) -> str:
    return getattr(getattr(embed, "footer", None), "text", "") or ""


def _is_registration_panel_message(message: discord.Message, bot_id: int) -> bool:
    if getattr(message.author, "id", None) != bot_id:
        return False
    if not message.embeds:
        return False

    embed = message.embeds[0]
    marker = _panel_marker_for_embed(embed)
    return marker in {BUYER_REGISTRATION_PANEL_MARKER, REVIVER_REGISTRATION_PANEL_MARKER} or embed.title in {
        BUYER_REGISTRATION_PANEL_TITLE,
        REVIVER_REGISTRATION_PANEL_TITLE,
    }


def _registration_panel_payload(embed: discord.Embed) -> tuple[discord.Embed, discord.ui.View]:
    marker = _panel_marker_for_embed(embed)
    if marker == BUYER_REGISTRATION_PANEL_MARKER or embed.title == BUYER_REGISTRATION_PANEL_TITLE:
        return build_buyer_registration_panel_embed(), BuyerRegistrationPanelView()
    return build_reviver_registration_panel_embed(), ReviverRegistrationPanelView()


async def _collect_registration_panel_messages(
    channel: discord.abc.Messageable,
    bot_id: int,
) -> list[discord.Message]:
    if not hasattr(channel, "history"):
        return []

    matches: list[discord.Message] = []
    try:
        async for message in channel.history(limit=None, oldest_first=False):
            if _is_registration_panel_message(message, bot_id):
                matches.append(message)
    except (discord.Forbidden, discord.HTTPException):
        return []

    return matches


async def refresh_registration_panels(bot: discord.Client) -> int:
    """Refresh all buyer/reviver registration panel messages across guild channels."""
    if bot.user is None:
        return 0

    refreshed = 0
    for guild in bot.guilds:
        for channel in guild.text_channels:
            messages = await _collect_registration_panel_messages(channel, bot.user.id)
            for message in messages:
                embed = message.embeds[0]
                next_embed, next_view = _registration_panel_payload(embed)
                try:
                    await message.edit(embed=next_embed, view=next_view)
                    refreshed += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
    return refreshed


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
        if isinstance(interaction.user, discord.Member):
            await sync_linked_member_state(
                interaction.user,
                reason="buyer registration synced linked state",
                identity=identity,
                has_buyer=True,
                has_reviver=False,
                verified=True,
            )
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

        if cfg.reviver_registration_faction_match_enabled and not _identity_matches_required_faction(
            identity.faction_id,
            identity.faction_name,
            cfg.reviver_registration_faction_id,
            cfg.reviver_registration_faction_name,
        ):
            requirement = _format_faction_requirement_label(
                cfg.reviver_registration_faction_id,
                cfg.reviver_registration_faction_name,
            )
            current_faction = identity.faction_name or "no faction"
            if identity.faction_id is not None:
                current_faction = f"{current_faction} (Torn faction ID {identity.faction_id})"
            await interaction.followup.send(
                f"This seller / medic registration panel is restricted to {requirement}. Your Torn account is currently tied to {current_faction}, so registration was denied.",
                ephemeral=True,
            )
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
        if isinstance(interaction.user, discord.Member):
            await sync_linked_member_state(
                interaction.user,
                reason="reviver registration synced linked state",
                identity=identity,
                has_buyer=False,
                has_reviver=True,
                verified=True,
            )
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
        await notifications.refresh_online_revivers_list(interaction.client)
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
        title = BUYER_REGISTRATION_PANEL_TITLE if is_buyer else REVIVER_REGISTRATION_PANEL_TITLE
        marker = BUYER_REGISTRATION_PANEL_MARKER if is_buyer else REVIVER_REGISTRATION_PANEL_MARKER

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
        await notifications.refresh_online_revivers_list(interaction.client)
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