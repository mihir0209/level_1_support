"""
Voice call router — handles inbound call webhooks and Daily room provisioning.

Flow:
  1. Twilio receives call → sends webhook to POST /voice/incoming
  2. Router provisions a Daily room (or uses pre-warmed pool)
  3. Returns TwiML to connect call to Daily via SIP
  4. Spins up Pipecat pipeline in background for the room
  5. Twilio hangs up → Daily room expires → pipeline stops

For development/testing: POST /voice/start creates a room directly (no Twilio).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from config import settings
from core.memory.session import create_session

log = structlog.get_logger(__name__)


# ── Daily room provisioning ───────────────────────────────────────────────────

async def _create_daily_room(brand_id: str, session_id: str) -> dict[str, str]:
    """
    Create a Daily room for a voice call.

    Returns: {room_url, room_name, token}
    """
    if not settings.daily_api_key:
        # Dev mode: return mock room info
        mock_name = f"l1-{brand_id}-{session_id[:8]}"
        return {
            "room_url": f"https://l1support.daily.co/{mock_name}",
            "room_name": mock_name,
            "token": "dev_token",
        }

    room_name = f"l1-{brand_id}-{session_id[:8]}"
    # Room expires 2 hours after creation
    exp = int(datetime.now(timezone.utc).timestamp()) + 7200

    async with httpx.AsyncClient() as client:
        # Create room
        resp = await client.post(
            "https://api.daily.co/v1/rooms",
            headers={"Authorization": f"Bearer {settings.daily_api_key}"},
            json={
                "name": room_name,
                "properties": {
                    "exp": exp,
                    "enable_chat": False,
                    "start_audio_off": False,
                    "max_participants": 2,  # customer + bot
                },
            },
            timeout=10,
        )
        resp.raise_for_status()
        room_data = resp.json()
        room_url = room_data["url"]

        # Create bot token (owner-level so bot can join without knock)
        token_resp = await client.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers={"Authorization": f"Bearer {settings.daily_api_key}"},
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": True,
                    "exp": exp,
                }
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["token"]

    log.info("daily_room_created", room_url=room_url, session_id=session_id)
    return {"room_url": room_url, "room_name": room_name, "token": token}


# ── Twilio TwiML builder ──────────────────────────────────────────────────────

def _build_twiml_dial_sip(room_url: str, token: str) -> str:
    """Build TwiML to connect Twilio call to Daily via SIP."""
    # Daily SIP endpoint: sip:<room-name>@<daily-domain>
    # See: https://docs.daily.co/guides/products/phone-call-transfer/sip
    sip_uri = room_url.replace("https://", "sip:").replace(".daily.co/", "@sip.daily.co;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>
    <Sip>{sip_uri}?token={token}</Sip>
  </Dial>
</Response>"""


# ── Active call tracker ───────────────────────────────────────────────────────

_active_calls: dict[str, dict[str, Any]] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Public router functions (called from FastAPI endpoints)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_incoming_call(
    brand_id: str,
    from_number: str,
    call_sid: str,
) -> str:
    """
    Handle inbound Twilio webhook for a new call.

    1. Creates a session
    2. Provisions a Daily room
    3. Starts Pipecat pipeline in background
    4. Returns TwiML to connect the call

    Returns: TwiML XML string
    """
    session_id = await create_session(
        brand_id=brand_id,
        channel="voice",
        customer_id=from_number,  # use phone as customer ID for voice
    )

    room_info = await _create_daily_room(brand_id, session_id)

    # Track this call
    _active_calls[call_sid] = {
        "session_id": session_id,
        "brand_id": brand_id,
        "from_number": from_number,
        "room_url": room_info["room_url"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # Start Pipecat pipeline in background
    asyncio.create_task(
        _run_pipeline_bg(
            room_url=room_info["room_url"],
            room_token=room_info["token"],
            brand_id=brand_id,
            session_id=session_id,
            call_sid=call_sid,
        )
    )

    twiml = _build_twiml_dial_sip(room_info["room_url"], room_info["token"])
    log.info(
        "call_connected",
        call_sid=call_sid,
        brand_id=brand_id,
        session_id=session_id,
        room_url=room_info["room_url"],
    )
    return twiml


async def _run_pipeline_bg(
    room_url: str,
    room_token: str,
    brand_id: str,
    session_id: str,
    call_sid: str,
) -> None:
    """Run the Pipecat pipeline for a call (background task)."""
    from channels.voice.pipeline import run_voice_call
    try:
        await run_voice_call(
            room_url=room_url,
            room_token=room_token,
            brand_id=brand_id,
            session_id=session_id,
        )
    except Exception as exc:
        log.error("pipeline_bg_error", call_sid=call_sid, error=str(exc))
    finally:
        _active_calls.pop(call_sid, None)
        log.info("call_cleaned_up", call_sid=call_sid)


async def start_dev_call(brand_id: str) -> dict[str, str]:
    """
    Start a voice call session without Twilio (dev/testing).

    Returns room_url and token for direct browser join.
    """
    session_id = await create_session(
        brand_id=brand_id,
        channel="voice",
        customer_id=f"dev_{uuid.uuid4().hex[:8]}",
    )
    room_info = await _create_daily_room(brand_id, session_id)

    call_sid = f"dev_{session_id[:8]}"
    _active_calls[call_sid] = {
        "session_id": session_id,
        "brand_id": brand_id,
        "room_url": room_info["room_url"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    asyncio.create_task(
        _run_pipeline_bg(
            room_url=room_info["room_url"],
            room_token=room_info["token"],
            brand_id=brand_id,
            session_id=session_id,
            call_sid=call_sid,
        )
    )

    return {
        "session_id": session_id,
        "room_url": room_info["room_url"],
        "call_sid": call_sid,
        "join_url": room_info["room_url"],  # open in browser to join
    }


def get_active_calls() -> list[dict[str, Any]]:
    """List all active voice calls (for admin monitoring)."""
    return list(_active_calls.values())


async def end_call(call_sid: str) -> bool:
    """Force-end a call (admin action)."""
    if call_sid not in _active_calls:
        return False
    _active_calls.pop(call_sid)
    log.info("call_force_ended", call_sid=call_sid)
    return True
