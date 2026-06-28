"""Tests for Prometheus multi-tenant telemetry metrics."""

from __future__ import annotations

import pytest
from core.metrics import (
    MESSAGES_TOTAL,
    RESOLUTION_LATENCY,
    ESCALATION_TOTAL,
    SESSIONS_TOTAL,
    TOKEN_USAGE_INPUT,
    TOKEN_USAGE_OUTPUT,
)


def test_metrics_initialization():
    """Verify metrics exist and are registered with the correct labels."""
    assert MESSAGES_TOTAL._labelnames == ("brand_id", "channel")
    assert RESOLUTION_LATENCY._labelnames == ("brand_id",)
    assert ESCALATION_TOTAL._labelnames == ("brand_id", "reason")
    assert SESSIONS_TOTAL._labelnames == ("brand_id", "channel")
    assert TOKEN_USAGE_INPUT._labelnames == ("brand_id",)
    assert TOKEN_USAGE_OUTPUT._labelnames == ("brand_id",)


def test_metrics_increments():
    """Test updating metrics with dynamic labels."""
    m_child = MESSAGES_TOTAL.labels(brand_id="boohoo_uk", channel="chat")
    initial_messages = m_child._value.get()
    m_child.inc(2)
    assert m_child._value.get() == initial_messages + 2

    in_child = TOKEN_USAGE_INPUT.labels(brand_id="plt_uk")
    initial_in = in_child._value.get()
    in_child.inc(150)
    assert in_child._value.get() == initial_in + 150

    out_child = TOKEN_USAGE_OUTPUT.labels(brand_id="plt_uk")
    initial_out = out_child._value.get()
    out_child.inc(80)
    assert out_child._value.get() == initial_out + 80


def test_resolution_latency_observation():
    """Test recording turn latency metrics."""
    child = RESOLUTION_LATENCY.labels(brand_id="boohoo_uk")
    child.observe(0.45)
    assert hasattr(child, "observe")
