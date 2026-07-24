"""Shared constants used across the revive bot package."""

from __future__ import annotations

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR

TORN_BASE_URL = "https://api.torn.com"
TORN_V2_PATH = "/v2/torn"
TORN_V2_USER_PATH = "/v2/user"
TORN_V2_USERS_PATH = "/v2/users/"

# Torn's revive scoring community estimate uses a 24 hour decay window.
REVIVE_SCORE_DECAY_WINDOW_SECONDS = SECONDS_PER_DAY