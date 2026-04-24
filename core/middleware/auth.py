"""
Admin auth middleware for FastAPI.

All routes under /admin/* are protected by X-ADMIN-KEY header.
Configured via ADMIN_API_KEY env var (defaults to 'l1support-demo-key-2026').
"""

from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings

admin_key_header = APIKeyHeader(name="X-ADMIN-KEY", auto_error=False)


async def verify_admin_key(key: str | None = Security(admin_key_header)) -> str:
    """
    Dependency — validate X-ADMIN-KEY header against ADMIN_API_KEY.

    Raises 401 if header is missing, 403 if the key is wrong.
    Returns the validated key on success.
    """
    expected = settings.admin_api_key
    if not key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-ADMIN-KEY header. Set ADMIN_API_KEY env var.",
        )
    if key != expected:
        raise HTTPException(status_code=403, detail="Invalid X-ADMIN-KEY.")
    return key