"""
Single source of truth for the order state machine.

    REQUESTED
      -> ASSIGNED(reviver)            [reviver found online]
      -> QUEUED_NO_REVIVER            [nobody online]

    ASSIGNED
      -> CLAIMED                      [reviver claims]
      -> ASSIGNED (next reviver)      [reviver forwards, or claim timeout]
      -> QUEUED_NO_REVIVER            [no next reviver available]
      -> CLOSED                       [buyer cancels order]

    CLAIMED
      -> DELIVERED                    [reviver marks delivered; payment window opens]
      -> FLAGGED_FOR_REVIEW           [delivery timeout expires]
      -> CLOSED                       [buyer cancels order]
      -> CLOSED_NO_ACTION             [target leaves hospital before revive is completed]

    DELIVERED
      -> PAID                        [reviver confirms payment]
      -> FLAGGED_FOR_REVIEW           [payment window expires or buyer disputes]

    PAID
      -> CLOSED                       [buyer dispute window expires]
      -> FLAGGED_FOR_REVIEW           [buyer disputes the confirmation]

    QUEUED_NO_REVIVER
      -> ASSIGNED                     [a reviver comes online]
      -> CLOSED_NO_ACTION             [target leaves hospital before assignment]
      -> CLOSED                       [buyer cancels order]

    FLAGGED_FOR_REVIEW
      -> (mod resolves manually, see moderation.py)
          -> ASSIGNED / DELIVERED     [reinstate, order continues]
          -> CLOSED                   [resolved, e.g. late payment accepted]
          -> BLACKLISTED (buyer)      [buyer sanctioned]

    PAID -> CLOSED
"""
from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    # REQUESTED state is unused in current flow; orders are created directly in ASSIGNED or QUEUED_NO_REVIVER.
    # Kept here for documentation and potential future use if we implement a multi-stage request flow.
    REQUESTED = "requested"
    ASSIGNED = "assigned"
    CLAIMED = "claimed"
    FORWARDED_CLAIMED = "forwarded_claimed"
    DELIVERED = "delivered"
    PAID = "paid"
    QUEUED_NO_REVIVER = "queued_no_reviver"
    FLAGGED_FOR_REVIEW = "flagged_for_review"
    CLOSED = "closed"
    CLOSED_NO_ACTION = "closed_no_action"
    BLACKLISTED_ORDER = "blacklisted_order"  # order abandoned because buyer got blacklisted


class IncidentType(str, Enum):
    STALL_CLAIM = "stall_claim"          # reviver assigned but never claimed/forwarded in time
    STALL_DELIVERY = "stall_delivery"    # reviver claimed but never marked delivered in time
    NO_PAYMENT_MATCH = "no_payment_match"  # payment window expired with no match


class Tier(str, Enum):
    STANDARD = "standard"
    T75 = "75"
    T100 = "100"


# Ordered lowest -> highest. A reviver at a given tier is assumed capable of
# handling any request at that tier or below (a 100-skill reviver can cover
# a 75 or standard request), so assignment falls back *upward* through this
# list, never downward.
TIER_ORDER: list[str] = [Tier.STANDARD.value, Tier.T75.value, Tier.T100.value]


def tier_from_skill(skill_level: float) -> str:
    """Canonical skill -> tier mapping, shared by registration (linking.py)
    and periodic role sync (role_sync.py) so the DB `tier` column and the
    Discord tier roles can't independently drift from two separate copies
    of this threshold logic."""
    if skill_level >= 100:
        return Tier.T100.value
    if skill_level >= 75:
        return Tier.T75.value
    return Tier.STANDARD.value


# Valid transitions: {from_state: {to_state, ...}}
# Used as a guard rail in db.transition_order() so a bug elsewhere can't push
# an order into an invalid state silently.
VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    # REQUESTED is not used in current flow (orders created directly in ASSIGNED/QUEUED_NO_REVIVER)
    # but kept as a valid starting point for forward compatibility
    OrderState.REQUESTED: {OrderState.ASSIGNED, OrderState.QUEUED_NO_REVIVER},
    OrderState.ASSIGNED: {
        OrderState.CLAIMED,
        OrderState.ASSIGNED,  # reassigned to next reviver
        OrderState.QUEUED_NO_REVIVER,
      OrderState.CLOSED,
        OrderState.CLOSED_NO_ACTION,
        OrderState.FLAGGED_FOR_REVIEW,  # timeout or mod action
    },
    OrderState.CLAIMED: {
      OrderState.DELIVERED,
      OrderState.FLAGGED_FOR_REVIEW,
      OrderState.CLOSED,
      OrderState.CLOSED_NO_ACTION,
    },
    OrderState.FORWARDED_CLAIMED: {
        OrderState.QUEUED_NO_REVIVER,
        OrderState.FLAGGED_FOR_REVIEW,
        OrderState.CLOSED,
        OrderState.CLOSED_NO_ACTION,
    },
    OrderState.DELIVERED: {OrderState.PAID, OrderState.FLAGGED_FOR_REVIEW},
    OrderState.PAID: {OrderState.CLOSED, OrderState.FLAGGED_FOR_REVIEW},
    OrderState.QUEUED_NO_REVIVER: {OrderState.ASSIGNED, OrderState.CLOSED_NO_ACTION, OrderState.CLOSED},
    OrderState.FLAGGED_FOR_REVIEW: {
        OrderState.ASSIGNED,
        OrderState.DELIVERED,
        OrderState.CLOSED,
        OrderState.BLACKLISTED_ORDER,
        OrderState.QUEUED_NO_REVIVER,
    },
}


def is_valid_transition(current: OrderState, target: OrderState) -> bool:
    return target in VALID_TRANSITIONS.get(current, set())


def payment_window_seconds(revives_requested: int, cfg) -> int:
  return cfg.payment_window_multi_seconds if revives_requested > 1 else cfg.payment_window_single_seconds
