"""
Pydantic models for admin API request/response validation.

All brand_id values are validated: alphanumeric + underscores only.
All UUIDs are validated with Pydantic's UUID type.
HTTP methods are restricted to GET/POST/PUT/DELETE/PATCH.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


# ── HTTP method enum ───────────────────────────────────────────────────────────

class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


# ── brand_id validation ───────────────────────────────────────────────────────

BRAND_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


def validate_brand_id(brand_id: str) -> str:
    """Validate and return brand_id — raises ValueError if invalid."""
    if not brand_id or len(brand_id) > 64 or not BRAND_ID_PATTERN.match(brand_id):
        raise ValueError(f"Invalid brand_id '{brand_id}': must be alphanumeric + underscores, max 64 chars")
    return brand_id


class BrandId(str):
    """Validated brand_id — alphanumeric + underscores only, max 64 chars."""
    def __new__(cls, value: str) -> "BrandId":
        if not BRAND_ID_PATTERN.match(value):
            raise ValueError(
                f"brand_id must be alphanumeric + underscores only, max 64 chars. Got: {value!r}"
            )
        return str.__new__(cls, value)


# ── Brand models ──────────────────────────────────────────────────────────────

class BrandBase(BaseModel):
    brand_id: Annotated[str, Field(min_length=1, max_length=64)]
    base_url: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("brand_id")
    @classmethod
    def validate_brand_id(cls, v: str) -> str:
        if not BRAND_ID_PATTERN.match(v):
            raise ValueError(
                "brand_id must be alphanumeric + underscores only (max 64 chars)"
            )
        return v


class EndpointCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: str | None = Field(default=None, max_length=500)
    method: HttpMethod
    path: Annotated[str, Field(max_length=500)]
    query_params: list[dict] = Field(default_factory=list)
    body_schema: dict | None = None
    response_schema: dict | None = None
    rate_limit_per_min: Annotated[int | None, Field(ge=1, le=10000)] = None
    requires_auth: bool = False
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_\-]{1,128}$", v):
            raise ValueError("name must be alphanumeric, underscores, or hyphens")
        return v


class CredentialCreate(BaseModel):
    key_type: str = Field(pattern=r"^(api_key|bearer_token|basic_auth|oauth2)$")
    key_name: Annotated[str, Field(min_length=1, max_length=64)]
    value: str = Field(min_length=1)

    @field_validator("key_name")
    @classmethod
    def validate_key_name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_\- ]{1,64}$", v):
            raise ValueError("key_name must be alphanumeric, underscores, spaces, or hyphens")
        return v


# ── Graph query models ────────────────────────────────────────────────────────

class GraphQueryRequest(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        # Block obvious destructive Cypher keywords in raw query endpoint
        dangerous = ["DROP ", "DELETE ", "DETACH DELETE", "REMOVE "]
        upper = v.upper()
        for kw in dangerous:
            if kw in upper:
                raise ValueError(f"Query contains forbidden keyword: {kw.strip()}")
        return v


# ── Session models ─────────────────────────────────────────────────────────────

class SessionFilter(BaseModel):
    brand_id: str | None = None
    customer_id: str | None = None
    channel: str | None = Field(default=None, pattern=r"^(chat|email|voice|whatsapp)$")
    intent: str | None = None
    outcome: str | None = Field(default=None, pattern=r"^(resolved|escalated|abandoned)$")
    limit: Annotated[int, Field(ge=1, le=500)] = 50


# ── Collection / ingestion models ─────────────────────────────────────────────

class CollectRequest(BaseModel):
    base_url: str = Field(max_length=500)
    max_pages: Annotated[int, Field(ge=1, le=500)] = 50
    concurrency: Annotated[int, Field(ge=1, le=20)] = 3


# ── Response models ────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class SuccessResponse(BaseModel):
    success: bool = True
    message: str
    data: dict | None = None