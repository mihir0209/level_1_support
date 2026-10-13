"""Proactive Churn Risk Alert Broadcaster (Phase 32)."""

from __future__ import annotations

import datetime
import httpx
import structlog
from config import settings

log = structlog.get_logger(__name__)


async def broadcast_churn_alert(
    session_id: str,
    customer_id: str | None,
    brand_id: str,
    average_csat: float
) -> None:
    """
    Asynchronously broadcasts an alert payload when a high churn-risk customer
    initiates a session. If the webhook destination URL matches hooks.slack.com,
    it formats the payload using Slack Block Kit elements.
    """
    webhook_url = settings.churn_alert_webhook_url
    if not webhook_url:
        log.info("churn_alert_webhook_skipped_no_url", session_id=session_id)
        return

    is_slack = "hooks.slack.com" in webhook_url
    timestamp_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if is_slack:
        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ *Churn Risk Alert: High-Priority SLA Active*"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Session ID:*\n`{session_id}`"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Brand:*\n{brand_id}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Customer ID:*\n`{customer_id or 'Unknown'}`"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Avg CSAT:*\n{average_csat:.2f} / 5.0"
                        }
                    ]
                }
            ]
        }
    else:
        payload = {
            "event": "churn_risk_sla_alert",
            "timestamp": timestamp_str,
            "session_id": session_id,
            "brand_id": brand_id,
            "customer_id": customer_id,
            "average_csat": average_csat,
            "turn_count": 1
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=payload, timeout=5.0)
            if response.status_code >= 400:
                log.warning(
                    "churn_alert_webhook_http_error",
                    session_id=session_id,
                    status_code=response.status_code,
                    body=response.text
                )
            else:
                log.info("churn_alert_webhook_delivered", session_id=session_id, is_slack=is_slack)
    except Exception as exc:
        log.warning("churn_alert_webhook_delivery_failed", session_id=session_id, error=str(exc))
