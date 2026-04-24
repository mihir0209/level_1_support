"""
OpenTelemetry tracing for L1 Support.

Wires OTLP exporter and adds trace spans on:
- HTTP tool calls (dynamic_builder)
- Graph queries (FalkorDB)
- LLM invocations (orchestration/llm.py)
- Ingestion pipeline stages

Context is propagated via HTTP headers (W3C TraceContext).
Configured via OTEL_* env vars (OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_ENDPOINT, etc.)
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

# ── Resource ────────────────────────────────────────────────────────────────

_resource = Resource.create({
    "service.name": os.environ.get("OTEL_SERVICE_NAME", "l1_support"),
    "service.version": "0.1.0",
    "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "development"),
})


def _init_tracer() -> trace.Tracer:
    """Initialize the global tracer provider with OTLP exporter if configured."""
    provider = TracerProvider(resource=_resource)

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    return trace.get_tracer(__name__)


_tracer = _init_tracer()


# ── Public tracer ───────────────────────────────────────────────────────────

def get_tracer() -> trace.Tracer:
    return _tracer


# ── Span helpers ───────────────────────────────────────────────────────────

def start_span(name: str, attributes: dict[str, Any] | None = None) -> Span:
    """Start a named span with optional attributes."""
    span = _tracer.start_span(name)
    if attributes:
        for k, v in attributes.items():
            span.set_attribute(k, v)
    return span


def start_active_span(
    name: str,
    fn: Callable,
    attributes: dict[str, Any] | None = None,
) -> Any:
    """Context-manager wrapper: run fn inside a named span, close on exit."""
    with _tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        return fn()


def record_exception(span: Span, exc: Exception, msg: str = "") -> None:
    """Record an exception on a span with the error message."""
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, msg or str(exc)))


def set_span_attributes(span: Span, **attrs: Any) -> None:
    """Set multiple attributes on a span in one call."""
    for k, v in attrs.items():
        if v is not None:
            span.set_attribute(k, v)


# ── Decorator ───────────────────────────────────────────────────────────────

def traced(
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
):
    """
    Decorator — wrap a sync or async function in a trace span.

    Usage:
        @traced("db.query", {"db.system": "postgresql"})
        def query(sql: str) -> list:
            ...

        @traced("agent.invoke")
        async def invoke_agent(state: L1SupportState) -> L1SupportState:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__name__

        @wraps(fn)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(span_name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    record_exception(span, exc)
                    raise

        @wraps(fn)
        def _sync(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(span_name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    record_exception(span, exc)
                    raise

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return _async
        return _sync

    return decorator


# ── Context injection for downstream calls ─────────────────────────────────

def inject_trace_context() -> dict[str, str]:
    """Inject trace context into outgoing HTTP headers (W3C TraceContext)."""
    carrier: dict[str, str] = {}
    from opentelemetry.propagate import inject
    inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str]) -> trace.Context | None:
    """Extract trace context from incoming HTTP headers."""
    from opentelemetry.propagate import extract
    return extract(carrier) if carrier else None