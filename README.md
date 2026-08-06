# Revive Storefront Bot

A Discord bot for running a Torn revive storefront. It handles buyer and seller/medic registration, revive requests, reviver assignment, order tracking, and moderation review flow.

The bot is built around a few core ideas:

- Torn API keys are verified against Torn before a Discord account is linked.
- Stored Torn API keys are encrypted at rest with Fernet.
- Discord nicknames are synced to `{Torn Name} [{ID}]` during registration and re-verified on a daily loop.
- Buyer and reviver roles are synced automatically from the stored link data.
- Payment confirmation is reviver-driven after delivery; the bot does not rely on polling Torn logs for payment detection.

## Project Layout

- `bot.py` - bot entrypoint and background loops
- `db.py` - SQLite schema and async helpers for buyers, revivers, orders, incidents, and sticky messages
- `torn_api.py` - Torn API wrapper for identity verification, hospital status, revive skill, and revive history checks
- `role_sync.py` - role reconciliation and nickname verification for linked members
- `state.py` - order and incident state definitions
- `notifications.py` - Discord DM/channel notification helpers and persistent message refresh logic
- `assignment.py` - reviver selection and fairness logic
- `cogs/linking.py` - buyer and seller/medic registration and unregister flow
- `cogs/buyer.py` - revive request flow and buyer order actions
- `cogs/reviver.py` - reviver availability, assignment controls, and delivery/payment actions
- `cogs/moderation.py` - moderation review actions for flagged orders

## Features

- Buyer registration with `/link`
- Seller/medic registration with `/link_reviver` or the registration panel
- Buyer and seller/medic role syncing
- Torn nickname syncing on registration and daily re-checks
- Revive request creation with tier selection and optional target ID
- Assignment, forwarding, claim, delivery, and payment confirmation flow
- Reviver status toggling with `/available`
- Reviver status panel with reporting controls
- Moderation review for flagged orders with `/review`
- Persistent Discord views that survive bot restarts

## Commands

### Linking and registration

- `/link` - self-serve buyer registration from a Torn API key
- `/link_reviver` - mod-only seller/medic registration for a specific member
- `/register_panel` - post the buyer or seller/medic registration panel
- `/unregister` - remove buyer, seller/medic, or both links

### Buyer flow

- `/request` - create a revive order
- `/panel` - post the revive request panel
- `/status` - check one of your orders
- `/cancel` - cancel an active order you own

### Reviver flow

- `/available` - mark yourself online or offline
- `/status_panel` - post the reviver status panel

### Moderation

- `/review` - review a flagged order as a moderator

## Runtime Behavior

The bot runs several background loops:

- role sync for linked accounts
- nickname verification for linked accounts
- timeout sweep for assigned, claimed, delivered, and paid orders
- queue reassignment when revivers come online
- active order reminder refreshes
- hospital-status polling for active orders

The nickname verification uses the Torn API key provided at registration whenever possible. The daily loop falls back to the stored linked key so names stay aligned even if the user changes their Torn name later.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy and fill the environment file:

```bash
cp .env.example .env
```

On Windows PowerShell, use:

```powershell
Copy-Item .env.example .env
```

3. Set the required values in `.env`:

- `DISCORD_TOKEN`
- `DB_ENCRYPTION_KEY`
- Guild IDs and channel IDs for your storefront, ops, forwarding, mod queue, and reviver ping channels
- Optional revive price fields used in the revive request panel

4. Run the bot:

```bash
python bot.py
```

## Testing

Run the available unit tests with:

```bash
py -m unittest
```

## Environment Variables

The most important values are listed below. See `.env.example` for the complete set.

- `DISCORD_TOKEN` - bot token from the Discord Developer Portal
- `STOREFRONT_GUILD_ID` - guild used for buyer-facing commands and panels
- `OPS_GUILD_ID` - ops/mod guild used for role and nickname sync
- `FORWARDING_GUILD_ID` - forwarding guild ID if used by your Discord setup
- `MOD_QUEUE_CHANNEL_ID` - channel used for moderation alerts
- `REVIVER_PING_CHANNEL_ID` - channel watched for active order reminder refreshes
- `FORWARDING_CHANNEL_ID` - channel used for forwarded order claims
- `OPS_CHANNEL_ID` - ops alert channel
- `FORWARDING_JOIN_LINK` - optional footer text for forwarded order embeds
- `DB_ENCRYPTION_KEY` - Fernet key for encrypting stored Torn API keys
- `DB_PATH` - SQLite database path
- `STANDARD_REVIVE_PRICE`, `T75_REVIVE_PRICE`, `T100_REVIVE_PRICE` - values shown in the revive request panel

Other settings control timeouts, poll intervals, UI limits, and assignment balancing. The defaults are tuned for a small storefront and can be adjusted in `.env`.

## Notes

- The bot verifies Torn API keys against Torn before linking a Discord account.
- Nickname syncing is designed so the registration path can later be extracted into a dedicated verification panel.
- Some Discord actions may fail if the bot lacks permission to manage nicknames or roles in the target guilds.
- Torn API rate limits still matter; keep the poll intervals conservative if you operate with multiple linked reviver accounts.
