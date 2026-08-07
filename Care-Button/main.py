from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


LOG = logging.getLogger("dragon-care-api")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DRAGON_CARE_DB_PATH", BASE_DIR / "dragon_care.db"))
VERIFY_TORN_KEYS = os.getenv("DRAGON_CARE_VERIFY_TORN_KEYS", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TORN_API_TIMEOUT_SECONDS = float(os.getenv("DRAGON_CARE_TORN_API_TIMEOUT_SECONDS", "10"))
DISCORD_WEBHOOK_URL = os.getenv("DRAGON_CARE_DISCORD_WEBHOOK_URL", "").strip()
REQUEST_API_TOKEN = os.getenv("DRAGON_CARE_REQUEST_API_TOKEN", "").strip()


class ReviveRequest(BaseModel):
    apiKey: str = Field(..., min_length=16, max_length=16, description="Torn public API key")
    type: Literal["Cash", "Xanax"]
    price: str = Field(..., min_length=1, max_length=32)
    skill: str = Field(..., min_length=1, max_length=32)
    scriptVersion: str = Field(..., min_length=1, max_length=32)


class EnrichedRequest(ReviveRequest):
    tornName: str | None = None
    tornId: int | None = None
    receivedAt: float


app = FastAPI(title="Dragon Care Revive API")


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS revive_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_suffix TEXT NOT NULL,
                type TEXT NOT NULL,
                price TEXT NOT NULL,
                skill TEXT NOT NULL,
                script_version TEXT NOT NULL,
                torn_name TEXT,
                torn_id INTEGER,
                received_at REAL NOT NULL,
                raw_payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
            )
            """
        )
        db.commit()


def _mask_api_key(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= 4 else api_key


async def _verify_torn_key(api_key: str) -> tuple[str | None, int | None]:
    if not VERIFY_TORN_KEYS:
        return None, None

    url = f"https://api.torn.com/user/?selections=basic&key={api_key}"
    timeout = httpx.Timeout(TORN_API_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict) and payload.get("error"):
        raise HTTPException(status_code=401, detail="Invalid Torn API key")

    torn_name = None
    torn_id = None
    if isinstance(payload, dict):
        torn_name = payload.get("name")
        torn_id = payload.get("player_id") or payload.get("user")

    try:
        torn_id = int(torn_id) if torn_id is not None else None
    except (TypeError, ValueError):
        torn_id = None

    return torn_name, torn_id


async def _maybe_forward_to_webhook(request: EnrichedRequest) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    content_lines = [
        "🐲 **Dragon Care revive request**",
        f"**Type:** {request.type}",
        f"**Price:** {request.price}",
        f"**Skill:** {request.skill}",
        f"**Script version:** {request.scriptVersion}",
        f"**API key suffix:** ...{_mask_api_key(request.apiKey)}",
    ]
    if request.tornName or request.tornId is not None:
        identity = request.tornName or "Unknown"
        if request.tornId is not None:
            identity = f"{identity} [{request.tornId}]"
        content_lines.insert(1, f"**Requester:** {identity}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": "\n".join(content_lines)})


def _store_request(request: EnrichedRequest) -> int:
    raw_payload = request.model_dump(mode="json")
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            """
            INSERT INTO revive_requests (
                api_key_suffix, type, price, skill, script_version,
                torn_name, torn_id, received_at, raw_payload, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
            """,
            (
                _mask_api_key(request.apiKey),
                request.type,
                request.price,
                request.skill,
                request.scriptVersion,
                request.tornName,
                request.tornId,
                request.receivedAt,
                json.dumps(raw_payload, separators=(",", ":")),
            ),
        )
        db.commit()
        return int(cursor.lastrowid)


async def _process_request(request: ReviveRequest) -> dict[str, object]:
    torn_name, torn_id = await _verify_torn_key(request.apiKey)
    enriched = EnrichedRequest(
        **request.model_dump(),
        tornName=torn_name,
        tornId=torn_id,
        receivedAt=time.time(),
    )
    request_id = await _store_request(enriched)
    await _maybe_forward_to_webhook(enriched)
    return {
        "status": "success",
        "message": "Revive request queued successfully.",
        "requestId": request_id,
        "tornName": torn_name,
        "tornId": torn_id,
    }


@app.on_event("startup")
async def _startup() -> None:
    _init_db()
    LOG.info("Dragon Care API initialized at %s", DB_PATH)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/revive")
@app.post("/api/dashboard/revive/script")
async def create_revive_request(
    request: ReviveRequest,
    x_dragon_care_token: str | None = Header(default=None, alias="X-Dragon-Care-Token"),
) -> dict[str, object]:
    if REQUEST_API_TOKEN and x_dragon_care_token != REQUEST_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid request token")

    try:
        return await _process_request(request)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        LOG.exception("Failed to verify or forward revive request")
        raise HTTPException(status_code=502, detail="Upstream request failed") from exc


def main() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("DRAGON_CARE_HOST", "0.0.0.0"),
        port=int(os.getenv("DRAGON_CARE_PORT", "8000")),
        reload=os.getenv("DRAGON_CARE_RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"},
    )


if __name__ == "__main__":
    main()