from __future__ import annotations

import logging

import aiohttp
import discord
from discord.ext import commands

import db
from permissions import BUYER_ROLE_NAMES, REVIVER_ROLE_NAMES
from state import tier_from_skill
import torn_api
import notifications


LOGGER = logging.getLogger(__name__)
VERIFIED_ROLE_NAMES = ("verified",)
UNVERIFIED_ROLE_NAMES = ("un-verified",)
MANAGED_ROLE_NAMES = tuple(
    dict.fromkeys(BUYER_ROLE_NAMES + REVIVER_ROLE_NAMES + VERIFIED_ROLE_NAMES + UNVERIFIED_ROLE_NAMES)
)
NICKNAME_SUFFIX_TEMPLATE = " [{torn_id}]"


async def _managed_guild_ids(bot: commands.Bot | None = None) -> tuple[int, ...]:
    guild_ids = []
    storefront_guild_id = await db.get_setting_int("storefront_guild_id")
    if storefront_guild_id:
        guild_ids.append(storefront_guild_id)
    if guild_ids or bot is None:
        return tuple(guild_ids)
    return tuple(guild.id for guild in bot.guilds)


async def _nickname_guild_ids(bot: commands.Bot | None = None) -> tuple[int, ...]:
    guild_ids: list[int] = []
    storefront_guild_id = await db.get_setting_int("storefront_guild_id")
    if storefront_guild_id:
        guild_ids.append(storefront_guild_id)
    elif bot is not None:
        guild_ids.extend(guild.id for guild in bot.guilds)
    return tuple(guild_ids)


def format_torn_nickname(torn_name: str, torn_id: int, *, max_length: int = 32) -> str:
    suffix = NICKNAME_SUFFIX_TEMPLATE.format(torn_id=torn_id)
    cleaned_name = " ".join(str(torn_name).split()).strip()
    if len(cleaned_name) + len(suffix) <= max_length:
        return f"{cleaned_name}{suffix}"

    available = max_length - len(suffix)
    if available <= 0:
        return suffix.strip()
    return f"{cleaned_name[:available].rstrip()}{suffix}"


def _find_role(guild: discord.Guild, role_name: str) -> discord.Role | None:
    return discord.utils.find(lambda role: role.name.lower() == role_name.lower(), guild.roles)


async def _ensure_role(guild: discord.Guild, role_name: str) -> discord.Role | None:
    role = _find_role(guild, role_name)
    if role is not None:
        return role

    try:
        return await guild.create_role(name=role_name, reason="Create storefront-managed role")
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Failed to create role %s in guild %s", role_name, guild.id)
        return None


def _desired_role_names(has_buyer: bool, has_reviver: bool) -> set[str]:
    desired: set[str] = set()
    if has_buyer:
        desired.update(name.lower() for name in BUYER_ROLE_NAMES)
    if has_reviver:
        desired.add("reviver")
    return desired


def _verification_role_names(is_verified: bool) -> set[str]:
    if is_verified:
        return {name.lower() for name in VERIFIED_ROLE_NAMES}
    return {name.lower() for name in UNVERIFIED_ROLE_NAMES}


def _reviver_tier_role_names_for_skill(skill_level: float | None) -> set[str]:
    desired: set[str] = set()
    if skill_level is None:
        return desired
    if skill_level >= 75:
        desired.add("reviver75")
    if skill_level >= 100:
        desired.add("reviver100")
    return desired


def _reviver_tier_role_names_for_stored_tier(stored_tier: str | None) -> set[str]:
    desired: set[str] = set()
    normalized = (stored_tier or "").strip().lower()
    if normalized == "75":
        desired.add("reviver75")
    elif normalized == "100":
        desired.add("reviver75")
        desired.add("reviver100")
    return desired


async def _get_revive_skill_level(reviver_row) -> float | None:
    api_key_encrypted = reviver_row.get("api_key_encrypted") if hasattr(reviver_row, "get") else reviver_row["api_key_encrypted"]
    if not api_key_encrypted:
        return None

    try:
        api_key = db.decrypt_key(api_key_encrypted)
    except Exception:
        LOGGER.warning("Failed to decrypt reviver API key for %s", reviver_row["discord_id"])
        return None

    async with aiohttp.ClientSession() as session:
        try:
            return await torn_api.get_revive_skill(session, api_key)
        except torn_api.TornAPIError as exc:
            LOGGER.warning(
                "Failed to fetch revive skill for %s: Torn API %s %s",
                reviver_row["discord_id"],
                exc.code,
                exc.message,
            )
            return None


async def _resolve_reviver_tier_role_names(reviver_row) -> set[str]:
    stored_tier = reviver_row["tier"]
    skill_level = await _get_revive_skill_level(reviver_row)
    if skill_level is None:
        return _reviver_tier_role_names_for_stored_tier(stored_tier)

    skill_tier = tier_from_skill(skill_level)
    if skill_tier != stored_tier:
        # Keep the DB tier (what assignment.py actually routes on)
        # in sync with the reviver's current skill, not just their
        # Discord roles -- otherwise a reviver who's leveled up
        # keeps getting routed at their old, stale tier.
        await db.set_reviver_tier(reviver_row["torn_id"], skill_tier)
        stored_tier = skill_tier

    return _reviver_tier_role_names_for_skill(skill_level) or _reviver_tier_role_names_for_stored_tier(stored_tier)


async def _get_linked_identity(discord_id: str) -> torn_api.TornIdentity | None:
    api_key = await db.get_api_key_for_discord(discord_id)
    if not api_key:
        return None

    async with aiohttp.ClientSession() as session:
        try:
            return await torn_api.verify_key_and_get_identity(session, api_key)
        except torn_api.TornAPIError as exc:
            LOGGER.warning("Failed to verify Torn identity for %s: Torn API %s %s", discord_id, exc.code, exc.message)
            return None
        except Exception:
            LOGGER.warning("Failed to verify Torn identity for %s", discord_id)
            return None


async def _get_identity_from_api_key(api_key: str) -> torn_api.TornIdentity | None:
    if not api_key:
        return None

    async with aiohttp.ClientSession() as session:
        try:
            return await torn_api.verify_key_and_get_identity(session, api_key)
        except torn_api.TornAPIError as exc:
            LOGGER.warning("Failed to verify Torn identity from provided key: Torn API %s %s", exc.code, exc.message)
            return None
        except Exception:
            LOGGER.warning("Failed to verify Torn identity from provided key")
            return None


async def _resolve_member(guild: discord.Guild, discord_id: str) -> discord.Member | None:
    try:
        member_id = int(discord_id)
    except (TypeError, ValueError):
        return None

    member = guild.get_member(member_id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(member_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _sync_member_nickname(
    member: discord.Member,
    identity: torn_api.TornIdentity,
    *,
    reason: str,
) -> bool:
    desired_nick = format_torn_nickname(identity.name, identity.torn_id)
    if member.nick == desired_nick:
        return False

    bot_member = member.guild.me
    if bot_member is not None:
        blockers = []
        if not bot_member.guild_permissions.manage_nicknames:
            blockers.append("missing Manage Nicknames permission")
        if member.guild.owner_id != bot_member.id and bot_member.top_role <= member.top_role:
            blockers.append(
                f"bot top role {bot_member.top_role} is not above target top role {member.top_role}"
            )
        if blockers:
            LOGGER.warning(
                "Failed to sync nickname for %s in %s to %r: %s (bot top role=%s, target top role=%s, target roles=%s)",
                member.id,
                member.guild.id,
                desired_nick,
                "; ".join(blockers),
                bot_member.top_role,
                member.top_role,
                ", ".join(role.name for role in member.roles),
            )
            return False

    try:
        await member.edit(nick=desired_nick, reason=reason)
    except (discord.Forbidden, discord.HTTPException) as exc:
        LOGGER.warning(
            "Failed to sync nickname for %s in %s to %r: %s (bot top role=%s, target top role=%s, target roles=%s)",
            member.id,
            member.guild.id,
            desired_nick,
            exc,
            bot_member.top_role if bot_member is not None else None,
            member.top_role,
            ", ".join(role.name for role in member.roles),
        )
        return False

    return True


async def _sync_named_roles(
    member: discord.Member,
    desired_role_names: set[str],
    *,
    reason: str,
    managed_role_names: tuple[str, ...] = MANAGED_ROLE_NAMES,
) -> bool:
    managed_role_lookup = {name.lower() for name in managed_role_names}
    managed_roles = [role for role in member.roles if role.name.lower() in managed_role_lookup]
    desired_roles = []
    for role_name in desired_role_names:
        role = await _ensure_role(member.guild, role_name)
        if role is not None:
            desired_roles.append(role)

    to_add = [role for role in desired_roles if role not in member.roles]
    to_remove = [role for role in managed_roles if role.name.lower() not in desired_role_names]

    if not to_add and not to_remove:
        return False

    try:
        if to_add:
            await member.add_roles(*to_add, reason=reason)
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.warning("Failed to sync roles for %s in %s", member.id, member.guild.id)
        return False

    return True


async def _sync_member_truth(
    member: discord.Member,
    *,
    has_buyer: bool,
    has_reviver: bool,
    is_verified: bool,
    reason: str,
) -> bool:
    desired_role_names = _desired_role_names(has_buyer, has_reviver)
    desired_role_names.update(_verification_role_names(is_verified))
    if has_reviver:
        reviver_row = await db.get_reviver_by_discord(str(member.id))
        if reviver_row is not None:
            desired_role_names.update(await _resolve_reviver_tier_role_names(reviver_row))
    return await _sync_named_roles(member, desired_role_names, reason=reason)


async def sync_member_roles(bot: commands.Bot, discord_id: str, *, reason: str) -> None:
    buyer = await db.get_buyer_by_discord(discord_id)
    reviver = await db.get_reviver_by_discord(discord_id)
    has_buyer = buyer is not None
    has_reviver = reviver is not None
    is_verified = has_buyer or has_reviver

    for guild_id in await _managed_guild_ids(bot):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        member = await _resolve_member(guild, discord_id)
        if member is None:
            continue
        await _sync_member_truth(
            member,
            has_buyer=has_buyer,
            has_reviver=has_reviver,
            is_verified=is_verified,
            reason=reason,
        )


async def sync_linked_member_state(
    member: discord.Member,
    *,
    reason: str,
    identity: torn_api.TornIdentity | None,
    has_buyer: bool,
    has_reviver: bool,
    verified: bool,
) -> bool:
    changed = False
    if identity is not None and await _sync_member_nickname(member, identity, reason=reason):
        changed = True
    if await _sync_member_truth(
        member,
        has_buyer=has_buyer,
        has_reviver=has_reviver,
        is_verified=verified,
        reason=reason,
    ):
        changed = True
    return changed


async def sync_linked_member_from_db(member: discord.Member, *, reason: str) -> bool:
    buyer = await db.get_buyer_by_discord(str(member.id))
    reviver = await db.get_reviver_by_discord(str(member.id))
    if buyer is None and reviver is None:
        return False

    identity = await _get_linked_identity(str(member.id))
    if identity is None:
        api_key = await db.get_api_key_for_discord(str(member.id))
        if api_key is not None:
            identity = await _get_identity_from_api_key(api_key)

    has_buyer = buyer is not None
    has_reviver = reviver is not None
    verified = has_buyer or has_reviver
    return await sync_linked_member_state(
        member,
        reason=reason,
        identity=identity,
        has_buyer=has_buyer,
        has_reviver=has_reviver,
        verified=verified,
    )


async def sync_member_verification(bot: commands.Bot, discord_id: str, *, reason: str, verified: bool) -> None:
    for guild_id in await _managed_guild_ids(bot):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        member = await _resolve_member(guild, discord_id)
        if member is None:
            continue

        desired_role_names = _verification_role_names(verified)
        await _sync_named_roles(
            member,
            desired_role_names,
            reason=reason,
            managed_role_names=VERIFIED_ROLE_NAMES + UNVERIFIED_ROLE_NAMES,
        )



async def sync_member_nickname(
    bot: commands.Bot,
    discord_id: str,
    *,
    reason: str,
    api_key: str | None = None,
    identity: torn_api.TornIdentity | None = None,
) -> bool:
    linked_identity = identity
    if linked_identity is None and api_key is not None:
        linked_identity = await _get_identity_from_api_key(api_key)
    if linked_identity is None:
        linked_identity = await _get_linked_identity(discord_id)
    if linked_identity is None:
        return False

    changed = False
    for guild_id in await _nickname_guild_ids(bot):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        member = await _resolve_member(guild, discord_id)
        if member is None:
            continue

        if await _sync_member_nickname(member, linked_identity, reason=reason):
            changed = True

    return changed


async def sync_all_linked_nicknames(bot: commands.Bot) -> None:
    linked_discord_ids = {
        str(row["discord_id"]) for row in await db.list_buyers()
    } | {
        str(row["discord_id"]) for row in await db.list_revivers()
    }

    for discord_id in linked_discord_ids:
        await sync_member_nickname(
            bot,
            discord_id,
            reason="periodic linked-name verification",
        )


async def sync_all_linked_roles(bot: commands.Bot) -> None:
    buyers = {str(row["discord_id"]): row for row in await db.list_buyers()}
    revivers = {str(row["discord_id"]): row for row in await db.list_revivers()}

    for guild_id in await _managed_guild_ids(bot):
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        synced_members = 0
        changed_members = 0
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue

            discord_id = str(member.id)
            has_buyer = discord_id in buyers
            has_reviver = discord_id in revivers
            is_verified = has_buyer or has_reviver
            changed = await _sync_member_truth(
                member,
                has_buyer=has_buyer,
                has_reviver=has_reviver,
                is_verified=is_verified,
                reason="periodic linked-role sync",
            )
            synced_members += 1
            if changed:
                changed_members += 1

        LOGGER.info(
            "Role sync complete for guild %s: %s member(s) checked, %s member(s) updated",
            guild_id,
            synced_members,
            changed_members,
        )

    await notifications.refresh_online_revivers_list(bot)