"""Session Archive Export & Reporting Engine."""

from __future__ import annotations

import io
import csv
import json
from feedback.engine import get_all_audit_logs


async def generate_archive_export(
    brand_id: str | None = None,
    violation_filter: str | None = None,
    format_type: str = "jsonl",
    limit: int = 200
) -> tuple[bytes, str]:
    """
    Queries session audit logs and serializes them as either CSV or JSONL.
    Returns: (bytes_content, media_type)
    """
    res = get_all_audit_logs(limit=limit, brand_id=brand_id)
    items = res.get("items", [])

    if violation_filter:
        items = [i for i in items if i.get("compliance_violations") == violation_filter]

    if format_type.lower() == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        # Header
        writer.writerow([
            "session_id",
            "brand_id",
            "customer_id",
            "compliance_score",
            "compliance_violations",
            "created_at",
            "audit_summary"
        ])
        # Rows
        for i in items:
            writer.writerow([
                i.get("session_id", ""),
                i.get("brand_id", ""),
                i.get("customer_id", ""),
                i.get("compliance_score", 1.0),
                i.get("compliance_violations" or ""),
                i.get("created_at", ""),
                i.get("audit_summary", "")
            ])
        return output.getvalue().encode("utf-8"), "text/csv"

    else:
        # Default JSON Lines (JSONL)
        lines = []
        for i in items:
            # Flatten or format dictionary cleanly
            data = {
                "session_id": i.get("session_id"),
                "brand_id": i.get("brand_id"),
                "customer_id": i.get("customer_id"),
                "compliance_score": i.get("compliance_score"),
                "compliance_violations": i.get("compliance_violations"),
                "created_at": i.get("created_at"),
                "audit_summary": i.get("audit_summary"),
                "tools_executed": i.get("tools_executed"),
                "policies_matched": i.get("policies_matched")
            }
            lines.append(json.dumps(data))
        return "\n".join(lines).encode("utf-8"), "application/x-jsonlines"
