"""
Central configuration — loaded once at startup from environment / .env file.
All modules import from here; no os.getenv() calls scattered in business logic.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── LLM Provider Selection ──────────────────────────────────────────
    # llm_provider: "anthropic" | "azure" | "openai"
    # "azure" routes all calls through Azure OpenAI endpoints
    llm_provider: str = Field(default="anthropic")

    # ── Anthropic ───────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    anthropic_api_base: str = Field(default="")   # leave blank to use Anthropic's default
    primary_model: str = Field(default="claude-sonnet-4-6")
    fast_model: str = Field(default="claude-haiku-4-5-20251001")

    # ── OpenAI (direct) ────────────────────────────────────────────────
    openai_api_key: str = Field(default="")
    openai_api_base: str = Field(default="")      # leave blank to use OpenAI's default
    openai_primary_model: str = Field(default="gpt-4o")
    openai_fast_model: str = Field(default="gpt-4o-mini")

    # ── Azure OpenAI ────────────────────────────────────────────────────
    # All Azure calls go through LiteLLM: azure/<deployment_name>
    azure_openai_api_key: str = Field(default="")
    azure_openai_endpoint: str = Field(default="")          # https://<resource>.openai.azure.com/
    azure_openai_api_version: str = Field(default="2024-02-01")
    azure_primary_deployment: str = Field(default="gpt-4o")         # deployment name in Azure
    azure_fast_deployment: str = Field(default="gpt-4o-mini")       # fast/cheap deployment
    azure_embedding_deployment: str = Field(default="text-embedding-3-small")

    # ── Graph DB (FalkorDB) ─────────────────────────────────────────────
    falkordb_host: str = Field(default="localhost")
    falkordb_port: int = Field(default=6379)
    falkordb_password: str = Field(default="")

    # ── Postgres ────────────────────────────────────────────────────────
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="l1_support")
    postgres_user: str = Field(default="l1support")
    postgres_password: str = Field(default="l1support_secret")

    # ── Fernet encryption (for brand API credentials) ────────────────────
    # Must be set in production. If unset, raises error at startup.
    fernet_key: str = Field(default="")

    # ── Secrets that must be set in production ───────────────────────────
    admin_api_key: str = Field(default="")

    # ── Confidence thresholds (single source of truth) ──────────────────
    low_confidence_threshold: float = Field(default=0.45)
    csat_dissatisfied_threshold: float = Field(default=0.4)
    churn_alert_webhook_url: str = Field(default="")
    playbook_execution_webhook_url: str = Field(default="")
    playbook_failure_alert_webhook_url: str = Field(default="")

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn_sync(self) -> str:
        # Compose env: POSTGRES_HOST=postgres (service name) → host=postgres
        # Dev local: POSTGRES_HOST=localhost (default) → host=localhost
        # Explicit override: POSTGRES_HOST=db.myapp.com → host=db.myapp.com
        host = os.environ.get("POSTGRES_HOST", self.postgres_host)
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{host}:{self.postgres_port}/{self.postgres_db}"
        )

    def postgres_conn_params(self) -> dict:
        """Return psycopg connection kwargs — for raw psycopg connections."""
        return {
            "host": os.environ.get("POSTGRES_HOST", self.postgres_host),
            "port": int(os.environ.get("POSTGRES_PORT", str(self.postgres_port))),
            "dbname": self.postgres_db,
            "user": os.environ.get("POSTGRES_USER", self.postgres_user),
            "password": os.environ.get("POSTGRES_PASSWORD") or self.postgres_password,
            "autocommit": True,
        }

    def postgres_readonly_conn_params(self) -> dict:
        """Return psycopg connection kwargs for the read-only Text2Cypher user.

        Uses POSTGRES_READONLY_USER / POSTGRES_READONLY_PASSWORD env vars when set,
        falling back to the l1_readonly role credentials from the migration.
        """
        return {
            "host": os.environ.get("POSTGRES_HOST", self.postgres_host),
            "port": int(os.environ.get("POSTGRES_PORT", str(self.postgres_port))),
            "dbname": self.postgres_db,
            "user": os.environ.get("POSTGRES_READONLY_USER", "l1_readonly"),
            "password": os.environ.get("POSTGRES_READONLY_PASSWORD") or "l1_readonly_secret",
            "autocommit": True,
        }

    # ── Redis (session cache) ───────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6380/0")

    # ── MemPalace ───────────────────────────────────────────────────────
    mempalace_data_dir: str = Field(default="./data/mempalace")

    # ── Observability ───────────────────────────────────────────────────
    langsmith_api_key: str = Field(default="")
    langsmith_project: str = Field(default="l1-support-ai")
    langchain_tracing_v2: str = Field(default="false")
    otel_exporter_otlp_endpoint: str = Field(default="http://localhost:4317")

    # ── Crawling ────────────────────────────────────────────────────────
    firecrawl_api_key: str = Field(default="")

    # ── Voice / Telephony (Phase 3) ─────────────────────────────────────
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_phone_number: str = Field(default="")
    daily_api_key: str = Field(default="")
    deepgram_api_key: str = Field(default="")
    elevenlabs_api_key: str = Field(default="")

    # ── Chatwoot ────────────────────────────────────────────────────────
    chatwoot_api_url: str = Field(default="http://localhost:3000")
    chatwoot_api_key: str = Field(default="")
    chatwoot_account_id: int = Field(default=1)
    chatwoot_webhook_secret: str = Field(default="")

    # ── n8n ─────────────────────────────────────────────────────────────
    n8n_url: str = Field(default="http://localhost:5678")

    # ── Graphiti (temporal KG) ─────────────────────────────────────────
    # Graphiti uses FalkorDB — same connection as knowledge graph
    graphiti_graph_name: str = Field(default="l1_user_memory")

    # ── App ─────────────────────────────────────────────────────────────
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")
    environment: str = Field(default="development")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> Settings:
        """Fail fast if required secrets are missing in production."""
        if self.is_production:
            if not self.admin_api_key:
                raise ValueError("admin_api_key must be set in production (set ADMIN_API_KEY env var)")
            if not self.fernet_key:
                raise ValueError("fernet_key must be set in production (set FERNET_KEY env var)")
        return self

    @model_validator(mode="after")
    def _strip_trailing_slashes(self) -> Settings:
        if self.anthropic_api_base.endswith("/"):
            self.anthropic_api_base = self.anthropic_api_base.rstrip("/")
        if self.openai_api_base.endswith("/"):
            self.openai_api_base = self.openai_api_base.rstrip("/")
        return self



@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
