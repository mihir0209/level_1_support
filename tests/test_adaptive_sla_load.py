"""Tests for Load-Aware Adaptive SLA Grace-Period Extension (Phase 33)."""

from __future__ import annotations

import pytest
from core.orchestration.sla_monitor import get_grace_offsets, check_sla_status


def test_get_grace_offsets():
    """Verify offset values returned for normal, medium, and high loads."""
    # 1. Normal Load (< 10)
    assert get_grace_offsets(5, is_priority=False) == (0, 0)
    assert get_grace_offsets(5, is_priority=True) == (0, 0)

    # 2. Medium Load (10 to 25)
    # Standard: warning +1, breach +2
    assert get_grace_offsets(15, is_priority=False) == (1, 2)
    # Priority: warning +0, breach +0
    assert get_grace_offsets(15, is_priority=True) == (0, 0)

    # 3. High Load (> 25)
    # Standard: warning +3, breach +4
    assert get_grace_offsets(30, is_priority=False) == (3, 4)
    # Priority: warning +1, breach +1
    assert get_grace_offsets(30, is_priority=True) == (1, 1)


@pytest.mark.asyncio
async def test_check_sla_status_with_load_offsets(monkeypatch):
    """Verify check_sla_status incorporates dynamic Redis load grace offsets."""
    # Mock get_session to return standard customer
    async def mock_get_session(sid):
        return {"session_id": sid, "customer_id": None}
    monkeypatch.setattr("core.memory.session.get_session", mock_get_session)

    # Mock Redis keys scan to simulate High Load (30 sessions)
    redis_keys = [f"session:s{i}".encode() for i in range(30)]
    class FakeRedis:
        async def scan_iter(self, match):
            for k in redis_keys:
                yield k
    monkeypatch.setattr("core.memory.session.get_redis", lambda: FakeRedis())

    # Standard thresholds: warning = 5, breach = 8.
    # At high load: warning gets +3 (total 8), breach gets +4 (total 12).
    # Turn 7 should be 'ok' under high load (normally warning).
    status_7 = await check_sla_status("sess_load_test", turn_count=7, escalation_risk=0.10)
    assert status_7 == "ok"

    # Turn 9 should be 'warning' under high load (normally breached).
    status_9 = await check_sla_status("sess_load_test", turn_count=9, escalation_risk=0.10)
    assert status_9 == "warning"

    # Turn 13 should be 'breached' under high load.
    status_13 = await check_sla_status("sess_load_test", turn_count=13, escalation_risk=0.10)
    assert status_13 == "breached"
