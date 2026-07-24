# Revive Storefront Bot — Scaffold

A Discord bot that lets buyers request Torn revives, routes the request to an
available reviver, and tracks payment via the *reviver's* API key (never
custodial — the bot never touches money or items itself).

This is a **prototype skeleton**, sized for a solo "storefront of one" (you as
the only faction/reviver pool) that can grow into multi-reviver later without
a rewrite. Core pieces:

- `db.py` — SQLite schema + async helpers (orders, revivers, buyers, incidents)
- `torn_api.py` — thin wrapper around the Torn API (status checks, log/event polling)
- `state.py` — the order state machine (single source of truth for valid transitions)
- `bot.py` — bot entrypoint, background pollers (timeouts, hospital status, payment matching)
- `cogs/linking.py` — `/link` (self-serve, buyers) and `/link_reviver` (mod-only, assigns tier) — verifies a submitted API key against Torn and stores the Discord↔Torn ID mapping. Everything else depends on this.
- `cogs/buyer.py` — `/request`, `/status` slash commands
- `cogs/reviver.py` — `/available`, `/status_panel`, claim/forward/deliver buttons
- `cogs/moderation.py` — `/review` command + mod-queue notifications

## Not yet wired up (TODO, flagged inline in code)

- **Notifications.** Claim/forward pings, buyer DMs on assignment/delivery/
  closure, and mod-queue alerts are all left as explicit `# TODO` markers at
  the point they should fire — the state machine transitions and DB writes
  around them are real and working, this is just the "tell a human" layer.
- **Exact field name for player ID in the key-verification response**
  (`torn_api.verify_key_and_get_id`). It checks a few plausible locations
  but hasn't been confirmed against a live key — do that before `/link`
  goes out to real users, since a silent mismatch here means legit keys
  get rejected.
- **Cross-faction target visibility.** `/request` and the hospital-status
  poller both fall back gracefully (skip the check / stay queued) if the
  *target's* Torn ID isn't linked to a stored key — meaning right now only
  targets who are themselves buyers/revivers in your DB get the pre-flight
  hospital check and the auto-close-when-released behavior. Worth deciding
  whether to require targets to link too, or add a faction-key path.
- **Payment confirmation is now reviver-driven.** The bot no longer depends
  on polling Torn logs for a payment match. After delivery, the reviver
  presses a confirm button, then the buyer gets a short dispute window before
  the order closes automatically.
- **Encryption at rest for stored API keys.** `db.py` currently stores keys
  as plaintext columns for scaffold simplicity — swap in `cryptography`'s
  `Fernet` (or similar) before any real keys are stored. Marked with a TODO.
- **Rate limiting.** Torn allows 100 req/min per user across all keys. The
  poller intervals in `bot.py` are conservative defaults — tune based on how
  many reviver keys you're polling concurrently.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DISCORD_TOKEN, GUILD ids, etc.
python bot.py
```
