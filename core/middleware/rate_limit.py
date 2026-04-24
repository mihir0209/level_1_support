"""
Redis-based rate limiter for API endpoints.

Uses sliding window via Redis INCR + EXPIRE to enforce per-endpoint
rate limits defined in `registered_api_endpoints.rate_limit_per_min`.

Falls back to a default limit if no per-endpoint config exists.
Configured via settings.redis_url.
"""

from __future__ import annotations

import time
from typing import Callable

import redis.asyncio as aioredis
import structlog
from fastapi import HTTPException, Request

from config import settings

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 60  # requests per minute

_redis_pool: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(settings.redis_url, decode_responses=False)
    return _redis_pool


def _rate_limit_key(endpoint: str, client_id: str) -> str:
    return f"ratelimit:{endpoint}:{client_id}"


async def check_rate_limit(
    endpoint: str,
    client_id: str,
    limit: int,
    window_seconds: int = 60,
) -> tuple[bool, int, int]:
    """
    Check (and increment) rate limit counter using Redis sliding window.

    Returns (allowed, remaining, reset_in_seconds).
    If Redis is unavailable, returns (True, limit, 0) — fail open.
    """
    try:
        r = await _get_redis()
        key = _rate_limit_key(endpoint, client_id)
        now = time.time()
        window_start = now - window_seconds

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds + 1)
        results = await pipe.execute()

        # zcard (results[1]) counts entries BEFORE this request is added.
        # After zadd, we have (current_count + 1) entries total.
        current_count = results[1]
        total_after_add = current_count + 1

        if total_after_add > limit:
            return False, 0, window_seconds

        remaining = limit - total_after_add
        return True, remaining, window_seconds

    except Exception as exc:
        log.warning("rate_limit_redis_unavailable", error=str(exc))
        return True, limit, 0


async def rate_limit_middleware(request: Request, call_next: Callable):
    """
    FastAPI middleware — enforce rate limits on all incoming requests.

    Rate limit key = client IP + path.
    Default limit: 60 req/min, configurable via DEFAULT_RATE_LIMIT env var.
    """
    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"
    client_id = f"{client_ip}:{request.app.title}"

    default_limit = int(
        getattr(settings, "default_rate_limit", None)
        or _get_default_limit_from_env()
        or DEFAULT_LIMIT
    )

    allowed, remaining, reset_in = await check_rate_limit(path, client_id, default_limit)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "limit": default_limit,
                "window_seconds": 60,
                "retry_after": reset_in,
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(default_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(int(time.time() + reset_in))
    return response


def _get_default_limit_from_env() -> int | None:
    import os
    val = os.environ.get("DEFAULT_RATE_LIMIT")
    return int(val) if val else None


async def rate_limit_dependency(
    request: Request,
    endpoint: str | None = None,
    limit: int | None = None,
) -> None:
    """
    FastAPI dependency — per-endpoint rate limiting.

    Use in routes:
        @router.get("/expensive")
        async def expensive( _: None = Depends(rate_limit_dependency(limit=10))):
            ...

    Args:
        endpoint: override the auto-detected path (useful for grouped routes)
        limit: override the per-endpoint limit from DB config
    """
    path = endpoint or request.url.path
    client_ip = request.client.host if request.client else "unknown"
    client_id = f"{client_ip}"

    actual_limit = limit or DEFAULT_LIMIT
    allowed, remaining, reset_in = await check_rate_limit(path, client_id, actual_limit)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "limit": actual_limit,
                "window_seconds": 60,
                "retry_after": reset_in,
            },
        )