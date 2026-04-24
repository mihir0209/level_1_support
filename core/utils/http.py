"""
HTTP utilities — retry with exponential backoff and configurable timeouts.

All outbound HTTP calls in the application route through http_request()
to get consistent retry behavior, timeout enforcement, and structured logging.
"""

from __future__ import annotations

import time
from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT = 15  # seconds
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 1.5  # multiply delay by this each retry


def http_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    data: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
) -> requests.Response:
    """
    Make an HTTP request with built-in retry + timeout.

    Retries on 5xx responses up to max_retries times with exponential backoff.
    Raises requests.RequestException on final failure or client errors (4xx).

    Returns the Response object — call .json(), .text, etc. on it.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=timeout,
            )
            if not (500 <= resp.status_code < 600):
                # Success or client error — don't retry
                return resp

            # 5xx — retry
            last_exc = requests.HTTPError(f"{resp.status_code} Server Error", response=resp)
            if attempt < max_retries:
                delay = backoff_factor ** attempt
                log.warning(
                    "http_retry",
                    url=url,
                    method=method,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay=round(delay, 2),
                    status_code=resp.status_code,
                )
                time.sleep(delay)
                continue
            # Exhausted retries
            return resp

        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = backoff_factor ** attempt
                log.warning(
                    "http_retry_network",
                    url=url,
                    method=method,
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    error=str(exc),
                )
                time.sleep(delay)
            else:
                break

    raise last_exc from None