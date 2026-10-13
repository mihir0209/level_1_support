"""CSAT Survey Manager.

Manages automated post-session Customer Satisfaction (CSAT) surveys,
including dispatching questionnaires, parsing replies, persisting scores
to PostgreSQL support_sessions, and invalidating history caches.
"""

from __future__ import annotations

import re
import uuid
import datetime
import json
import structlog
from core.memory.session import get_redis, get_session, update_session
from core.crm.customer_history import invalidate_customer_cache

log = structlog.get_logger(__name__)

PENDING_PREFIX = "session:csat_pending"
SURVEY_QUESTION = "How would you rate your support experience today on a scale from 1 (poor) to 5 (excellent)?"
THANK_YOU_RESPONSE = "Thank you for your feedback!"


async def is_csat_pending(session_id: str) -> bool:
    """Check if a session is currently awaiting a CSAT survey response."""
    if not session_id:
        return False
    r = get_redis()
    return await r.exists(f"{PENDING_PREFIX}:{session_id}") > 0


async def schedule_csat_survey(
    session_id: str,
    customer_id: str | None,
    brand_id: str,
    channel: str,
    intent: str | None = None,
    outcome: str | None = "resolved",
    resolution_id: str | None = None,
    turn_count: int = 0,
) -> bool:
    """
    Saves the session state to PostgreSQL support_sessions, registers a
    pending CSAT survey in Redis, and dispatches the survey question.
    """
    r = get_redis()
    pending_key = f"{PENDING_PREFIX}:{session_id}"

    # Load session data to extract details
    empathy_mode_val = None
    chatwoot_conv_id = None
    try:
        sess_data = await get_session(session_id)
        if sess_data:
            empathy_mode_val = sess_data.get("empathy_mode")
            chatwoot_conv_id = sess_data.get("chatwoot_conv_id")
    except Exception:
        pass

    # Set pending flag in Redis (expires in 24 hours)
    customer_val = customer_id or ""
    await r.setex(pending_key, 86400, customer_val)

    # 1. Upsert session to PostgreSQL support_sessions
    try:
        from core.db.pool import get_async_pool

        pool = get_async_pool()
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Parse or default session ID to a valid UUID
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError:
            session_uuid = uuid.uuid4()

        meta_dict = {"csat_scheduled": True}
        if empathy_mode_val:
            meta_dict["empathy_mode"] = empathy_mode_val

        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO support_sessions (
                    id, brand_id, customer_id, channel, intent, outcome, 
                    resolution_id, turn_count, started_at, ended_at, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    turn_count = EXCLUDED.turn_count,
                    ended_at = EXCLUDED.ended_at
                """,
                (
                    session_uuid,
                    brand_id,
                    customer_id,
                    channel,
                    intent,
                    outcome,
                    resolution_id,
                    turn_count,
                    now - datetime.timedelta(minutes=5),  # started_at estimate
                    now,                                 # ended_at
                    json.dumps(meta_dict),
                ),
            )
            log.info("support_session_saved_for_csat", session_id=session_id, customer_id=customer_id)
    except Exception as exc:
        log.warning("save_session_for_csat_db_failed", session_id=session_id, error=str(exc))

    # 2. Dispatch the CSAT survey message to the channel
    try:
        if chatwoot_conv_id:
            from channels.chat.chatwoot_client import post_message
            posted = post_message(chatwoot_conv_id, SURVEY_QUESTION, private=False)
            log.info("csat_survey_dispatched_chatwoot", session_id=session_id, posted=posted)
            return posted
    except Exception as exc:
        log.warning("csat_dispatch_failed", session_id=session_id, error=str(exc))

    return False


async def process_csat_response(session_id: str, message: str) -> bool:
    """
    Parses message for a score 1-5. If found, updates Postgres and Redis,
    invalidates customer history cache, and returns True. Otherwise returns False.
    """
    if not message:
        return False

    # Extract single digit 1-5
    match = re.search(r"\b([1-5])\b", message.strip())
    if not match:
        return False

    score = int(match.group(1))
    r = get_redis()
    pending_key = f"{PENDING_PREFIX}:{session_id}"
    raw_customer_id = await r.get(pending_key) or ""
    if isinstance(raw_customer_id, bytes):
        customer_id = raw_customer_id.decode("utf-8", errors="ignore")
    else:
        customer_id = raw_customer_id

    # 1. Update PostgreSQL support_sessions satisfaction rating
    try:
        from core.db.pool import get_async_pool

        pool = get_async_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                UPDATE support_sessions
                SET satisfaction = %s
                WHERE id = %s
                """,
                (float(score), uuid.UUID(session_id)),
            )
            log.info("csat_score_saved_db", session_id=session_id, score=score)
    except Exception as exc:
        log.warning("csat_db_update_failed", session_id=session_id, error=str(exc))

    # 2. Clean up Redis pending state
    await r.delete(pending_key)

    # 3. Update Redis session data
    try:
        await update_session(session_id, {"satisfaction": float(score)})
    except Exception as exc:
        log.warning("csat_redis_update_failed", session_id=session_id, error=str(exc))

    # 4. Invalidate customer history cache
    if customer_id:
        try:
            await invalidate_customer_cache(customer_id)
        except Exception as exc:
            log.warning("csat_cache_invalidation_failed", customer_id=customer_id, error=str(exc))

    # 5. Reply to user and register RL feedback
    try:
        sess_data = await get_session(session_id)
        if sess_data:
            # Register CSAT reward to RL engine (Phase 31)
            chosen_persona = sess_data.get("empathy_mode")
            if chosen_persona:
                normalized_reward = (float(score) - 1.0) / 4.0
                try:
                    from core.orchestration.rl_engine import register_feedback
                    await register_feedback("empathy_mode", chosen_persona, normalized_reward)
                    log.info("csat_rl_reward_registered", session_id=session_id, persona=chosen_persona, reward=normalized_reward)
                except Exception as rl_err:
                    log.warning("csat_rl_reward_failed", session_id=session_id, error=str(rl_err))

            # Trigger Auto-Mitigation Playbook for low satisfaction rating (Phase 34)
            if float(score) < 3.0:
                try:
                    from core.crm.mitigation import generate_csat_mitigation
                    intent_val = sess_data.get("intent")
                    await generate_csat_mitigation(session_id, customer_id, float(score), intent_val)
                except Exception as mit_err:
                    log.warning("csat_mitigation_trigger_failed", session_id=session_id, error=str(mit_err))

            # Send thank you message
            if sess_data.get("chatwoot_conv_id"):
                chatwoot_conv_id = sess_data["chatwoot_conv_id"]
                from channels.chat.chatwoot_client import post_message
                post_message(chatwoot_conv_id, THANK_YOU_RESPONSE, private=False)
                log.info("csat_thank_you_dispatched", session_id=session_id)
    except Exception as exc:
        log.warning("csat_completion_flow_failed", session_id=session_id, error=str(exc))

    return True
