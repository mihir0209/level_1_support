"""
Structured logging configuration for L1 Support.

Adds correlation fields (request_id, brand_id, session_id, latency_ms)
to every log entry automatically via bound context.

Format:
- Development: human-readable with key sorting (structlog.ConsoleRenderer)
- Production: JSON (structlog.JSONRenderer)

Usage:
    from core.logging_config import configure_logging, get_logger
    configure_logging(settings)  # once at startup

    log = get_logger(__name__)
    log.info("tool_called", tool_name="get_order", brand_id="boohoo_uk", latency_ms=42)
"""

from __future__ import annotations

import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# ── Context vars for correlation ──────────────────────────────────────────

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
brand_id_var: ContextVar[str] = ContextVar("brand_id", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")


def bind_correlation(request_id: str | None = None, brand_id: str | None = None, session_id: str | None = None) -> None:
    """Bind correlation IDs to log context. Call per-request."""
    if request_id:
        request_id_var.set(request_id)
    if brand_id:
        brand_id_var.set(brand_id)
    if session_id:
        session_id_var.set(session_id)


def clear_correlation() -> None:
    """Clear correlation IDs — call at end of request."""
    request_id_var.set("")
    brand_id_var.set("")
    session_id_var.set("")


# ── Timestamp / level addons ────────────────────────────────────────────────

def add_log_level(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["level"] = method_name.upper()
    return event_dict


def add_timestamp(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    event_dict["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    return event_dict


def add_correlation_fields(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Inject correlation IDs from context vars into every log entry."""
    rid = request_id_var.get()
    bid = brand_id_var.get()
    sid = session_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    if bid:
        event_dict["brand_id"] = bid
    if sid:
        event_dict["session_id"] = sid
    return event_dict


# ── Exception filter ────────────────────────────────────────────────────────

def _drop_event_key(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Remove internal '_record' / '_from_msg' keys added by structlog."""
    for key in ("_record", "_from_msg", "stack_info", "exc_info"):
        event_dict.pop(key, None)
    return event_dict


# ── Configure ──────────────────────────────────────────────────────────────

def configure_logging(
    log_level: str = "INFO",
    environment: str = "development",
) -> None:
    """
    Call once at application startup (before any log calls).

    - Development → ConsoleRenderer (colored, key-sorted)
    - Production → JSONRenderer (structured, parseable by ELK/Grafana)

    The root logger is configured to forward to structlog,
    so log statements in third-party libraries (requests, psycopg,
    falkordb, etc.) also appear in structured format.
    """
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        add_timestamp,
        add_correlation_fields,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _drop_event_key,
    ]

    if environment == "production":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                sort_keys=True,
                exception_formatter=structlog.dev.plain_traceback,
            )
        )

    # Route all library logs through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Silence noisy third-party loggers
    noisy = ["httpx", "httpcore", "falkordb", "urllib3", "charset_normalizer", "botocore"]
    for name in noisy:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a bound logger for a module."""
    return structlog.get_logger(name)