"""Tests for Session Archive Export & Compliance Reporting Engine."""

from __future__ import annotations

import pytest
from main import app
from core.reporting.exporter import generate_archive_export
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_generate_archive_export_formats(monkeypatch):
    """Verify that exporter parses postgres audit logs to CSV and JSONL formats."""
    mock_data = {
        "items": [
            {
                "session_id": "s_exp_1",
                "brand_id": "boohoo_uk",
                "customer_id": "cust_1",
                "compliance_score": 1.0,
                "compliance_violations": None,
                "created_at": "2026-07-04T12:00:00",
                "audit_summary": "Clean conversation."
            },
            {
                "session_id": "s_exp_2",
                "brand_id": "boohoo_uk",
                "customer_id": "cust_2",
                "compliance_score": 0.0,
                "compliance_violations": "prompt_injection",
                "created_at": "2026-07-04T12:05:00",
                "audit_summary": "Blocked jailbreak attempt."
            }
        ],
        "total": 2
    }

    monkeypatch.setattr(
        "core.reporting.exporter.get_all_audit_logs",
        lambda limit, brand_id: mock_data
    )

    # Test JSONL formatting
    jsonl_bytes, media_type = await generate_archive_export(format_type="jsonl")
    assert media_type == "application/x-jsonlines"
    lines = jsonl_bytes.decode("utf-8").split("\n")
    assert len(lines) == 2
    import json
    parsed = json.loads(lines[0])
    assert parsed["session_id"] == "s_exp_1"

    # Test CSV formatting
    csv_bytes, media_type_csv = await generate_archive_export(format_type="csv")
    assert media_type_csv == "text/csv"
    csv_str = csv_bytes.decode("utf-8")
    assert "session_id,brand_id" in csv_str
    assert "s_exp_1,boohoo_uk" in csv_str

    # Test filter by compliance violations
    filtered_bytes, _ = await generate_archive_export(
        violation_filter="prompt_injection",
        format_type="jsonl"
    )
    filtered_lines = filtered_bytes.decode("utf-8").split("\n")
    assert len(filtered_lines) == 1
    assert "s_exp_2" in filtered_lines[0]


@pytest.mark.asyncio
async def test_reporting_export_api_route(monkeypatch):
    """Verify endpoint generates correct filename, media headers and attachment payloads."""
    mock_data = {
        "items": [
            {
                "session_id": "s_exp_3",
                "brand_id": "boohoo_uk",
                "customer_id": "cust_3",
                "compliance_score": 1.0,
                "compliance_violations": None,
                "created_at": "2026-07-04T12:10:00",
                "audit_summary": "Test sync resolution"
            }
        ]
    }

    monkeypatch.setattr(
        "core.reporting.exporter.get_all_audit_logs",
        lambda limit, brand_id: mock_data
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET Export Endpoint
        res = await client.get(
            "/admin/reporting/export?format_type=csv&limit=10",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "attachment" in res.headers["content-disposition"]
        assert "session_export_" in res.headers["content-disposition"]
        assert "s_exp_3,boohoo_uk" in res.text
        assert "compliance_score,compliance_violations" in res.text
