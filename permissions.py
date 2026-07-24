from __future__ import annotations

from collections.abc import Iterable

import discord

import db

BUYER_ROLE_NAMES = ("buyer",)
BUYER_ACCESS_ROLE_NAMES = ("buyer", "reviver", "reviver75", "reviver100")
REVIVER_ROLE_NAMES = ("reviver", "reviver75", "reviver100")


def member_has_any_role(member: discord.abc.User | discord.Member, role_names: Iterable[str]) -> bool:
    roles = getattr(member, "roles", None)
    if roles is None:
        return False
    target_names = {name.lower() for name in role_names}
    return any(getattr(role, "name", "").lower() in target_names for role in roles)


async def deny_if_missing_role(
    interaction: discord.Interaction,
    role_names: Iterable[str],
    message: str,
) -> bool:
    if interaction.user is not None and member_has_any_role(interaction.user, role_names):
        return False

    user_id = str(getattr(interaction.user, "id", ""))
    if user_id:
        target_roles = {name.lower() for name in role_names}
        if target_roles.issubset({name.lower() for name in REVIVER_ROLE_NAMES}):
            if await db.get_reviver_by_discord(user_id) is not None:
                return False
        if target_roles.issubset({name.lower() for name in BUYER_ROLE_NAMES}):
            if await db.get_buyer_by_discord(user_id) is not None:
                return False
        if target_roles.issubset({name.lower() for name in BUYER_ACCESS_ROLE_NAMES}):
            if await db.get_buyer_by_discord(user_id) is not None:
                return False
            if await db.get_reviver_by_discord(user_id) is not None:
                return False

    await interaction.response.send_message(message, ephemeral=True)
    return True


async def deny_if_missing_buyer_access(interaction: discord.Interaction, message: str) -> bool:
    if interaction.user is not None and member_has_any_role(interaction.user, BUYER_ACCESS_ROLE_NAMES):
        return False

    user_id = str(getattr(interaction.user, "id", ""))
    if not user_id:
        await interaction.response.send_message(message, ephemeral=True)
        return True

    if await db.get_buyer_by_discord(user_id) is not None:
        return False
    if await db.get_reviver_by_discord(user_id) is not None:
        return False

    await interaction.response.send_message(message, ephemeral=True)
    return True


async def deny_if_not_admin(interaction: discord.Interaction, message: str) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return False
    await interaction.response.send_message(message, ephemeral=True)
    return True