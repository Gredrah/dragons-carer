# Dragon Care API Host

This folder contains the browser userscript and a small FastAPI service that receives the revive request payload from it and stores it in a local SQLite queue.

The userscript is the primary buyer submission path. Discord's `/request` command remains a manual fallback, but routine buyer orders should go through the script.

## Routes

- `POST /api/revive`
- `POST /api/dashboard/revive/script`
- `GET /healthz`

## Expected payload

```json
{
  "apiKey": "ABC123DEF456GHI7",
  "type": "Cash",
  "price": "2m",
  "skill": "Full (100%)",
  "scriptVersion": "1.3.1"
}
```

## Setup

```bash
pip install -r requirements.txt
```

Optional environment variables:

- `DRAGON_CARE_DB_PATH` - SQLite database path, defaults to `dragon_care.db`
- `DRAGON_CARE_VERIFY_TORN_KEYS` - set to `true` to validate Torn API keys against Torn
- `DRAGON_CARE_TORN_API_TIMEOUT_SECONDS` - Torn API timeout, defaults to `10`
- `DRAGON_CARE_DISCORD_WEBHOOK_URL` - optional webhook used to forward queued requests
- `DRAGON_CARE_REQUEST_API_TOKEN` - optional request token expected in the `X-Dragon-Care-Token` header
- `DRAGON_CARE_HOST` - bind host, defaults to `0.0.0.0`
- `DRAGON_CARE_PORT` - bind port, defaults to `8000`

## Run

```bash
python main.py
```

The service stores each request in the `revive_requests` table and returns a small success payload containing the generated queue ID.