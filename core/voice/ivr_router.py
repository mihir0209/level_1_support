"""Inbound Voice IVR Webhook and Router."""

from __future__ import annotations

import json
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)


def generate_inbound_twiml(action_url: str) -> str:
    """Compile the initial TwiML greeting asking caller to choose options."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'    <Gather action="{action_url}" numDigits="1" timeout="10">\n'
        '        <Say>Welcome to L1 E-Commerce Support. Press 1 for Returns and Exchanges. '
        'Press 2 to speak to a Security Representative. Press 3 for any other questions.</Say>\n'
        '    </Gather>\n'
        '    <Say>We did not receive any input. Goodbye.</Say>\n'
        '    <Hangup/>\n'
        '</Response>'
    )


def generate_gather_twiml(digits: str) -> str:
    """Compile response TwiML based on digits entered by the caller."""
    if digits == "1":
        msg = "Connecting you to Returns and Exchanges."
        target = "+1234567890"
    elif digits == "2":
        msg = "Connecting you to a Security Representative."
        target = "+1999999999"
    else:
        msg = "Connecting you to General FAQ line."
        target = "+1555555555"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'    <Say>{msg}</Say>\n'
        f'    <Dial>{target}</Dial>\n'
        '</Response>'
    )


async def register_inbound_call(call_sid: str, from_number: str) -> None:
    """Add active inbound call connection metadata into Redis."""
    r = get_redis()
    call = {
        "call_sid": call_sid,
        "from_number": from_number,
        "selected_digit": None,
        "status": "connected"
    }
    await r.hset("voice:inbound:calls", call_sid, json.dumps(call))
    log.info("inbound_call_registered", call_sid=call_sid, from_number=from_number)


async def update_inbound_call_digit(call_sid: str, digit: str) -> None:
    """Update active inbound call's keypress selection digit."""
    r = get_redis()
    raw = await r.hget("voice:inbound:calls", call_sid)
    if raw:
        call = json.loads(raw)
        call["selected_digit"] = digit
        await r.hset("voice:inbound:calls", call_sid, json.dumps(call))
        log.info("inbound_call_digit_updated", call_sid=call_sid, digit=digit)


async def remove_inbound_call(call_sid: str) -> None:
    """Remove active inbound call metadata on hangup."""
    r = get_redis()
    await r.hdel("voice:inbound:calls", call_sid)
    log.info("inbound_call_removed", call_sid=call_sid)


async def get_active_inbound_calls() -> list[dict]:
    """Retrieve all tracked inbound calls."""
    r = get_redis()
    raw_calls = await r.hgetall("voice:inbound:calls")
    calls = []
    for raw in raw_calls.values():
        try:
            calls.append(json.loads(raw))
        except Exception:
            pass
    return calls
