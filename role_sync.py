from __future__ import annotations

import logging

import aiohttp
import discord
from discord.ext import commands

import db
from config import cfg
from permissions import BUYER_ROLE_NAMES, REVIVER_ROLE_NAMES
from state import tier_from_skill
import torn_api


LOGGER = logging.getLogger(__name__)
MANAGED_ROLE_NAMES = tuple(dict.fromkeys(BUYER_ROLE_NAMES + REVIVER_ROLE_NAMES))


def _managed_guild_ids() -> tuple[int, ...]:
    guild_ids = []
    if cfg.storefront_guild_id:
        guild_ids.append(cfg.storefront_guild_id)
    if cfg.ops_guild_id and cfg.ops_guild_id not in guild_ids:
        guild_ids.append(cfg.ops_guild_id)
    return tuple(guild_ids)


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


async def _sync_member_truth(
    member: discord.Member,
    *,
    has_buyer: bool,
    has_reviver: bool,
    reason: str,
) -> bool:
    desired_role_names = _desired_role_names(has_buyer, has_reviver)
    if has_reviver:
        reviver_row = await db.get_reviver_by_discord(str(member.id))
        skill_level = None
        stored_tier = None
        if reviver_row is not None:
            stored_tier = reviver_row["tier"]
            skill_level = await _get_revive_skill_level(reviver_row)
            if skill_level is not None:
                skill_tier = tier_from_skill(skill_level)
                if skill_tier != stored_tier:
                    # Keep the DB tier (what assignment.py actually routes on)
                    # in sync with the reviver's current skill, not just their
                    # Discord roles -- otherwise a reviver who's leveled up
                    # keeps getting routed at their old, stale tier.
                    await db.set_reviver_tier(reviver_row["torn_id"], skill_tier)
                    stored_tier = skill_tier
        tier_role_names = _reviver_tier_role_names_for_skill(skill_level)
        if not tier_role_names:
            tier_role_names = _reviver_tier_role_names_for_stored_tier(stored_tier)
        desired_role_names.update(tier_role_names)
    managed_roles = [role for role in member.roles if role.name.lower() in MANAGED_ROLE_NAMES]
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


async def sync_member_roles(bot: commands.Bot, discord_id: str, *, reason: str) -> None:
    buyer = await db.get_buyer_by_discord(discord_id)
    reviver = await db.get_reviver_by_discord(discord_id)
    has_buyer = buyer is not None
    has_reviver = reviver is not None

    for guild_id in _managed_guild_ids():
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
            reason=reason,
        )


async def sync_all_linked_roles(bot: commands.Bot) -> None:
    buyers = {str(row["discord_id"]): row for row in await db.list_buyers()}
    revivers = {str(row["discord_id"]): row for row in await db.list_revivers()}

    for guild_id in _managed_guild_ids():
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
            changed = await _sync_member_truth(
                member,
                has_buyer=has_buyer,
                has_reviver=has_reviver,
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