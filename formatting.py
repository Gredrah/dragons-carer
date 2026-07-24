"""Text formatting helpers shared across cogs and notifications."""

from __future__ import annotations


def torn_link(torn_id: int | str) -> str:
    """Format a Torn ID as a clickable markdown link."""
    return f"[{torn_id}](https://www.torn.com/profiles.php?XID={torn_id})"
