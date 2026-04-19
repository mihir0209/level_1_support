"""
Central configuration — loaded once at startup from environment / .env file.
All modules import from here; no os.getenv() calls scattered in business logic.
"""

from __future__ import annotations

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

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn_sync(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis (session cache) ───────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6380/0")

    # ── MemPalace ───────────────────────────────────────────────────────
    mempalace_data_dir: str = Field(default="./data/mempalace")

    # ── Observability ───────────────────────────────────────────────────
    langsmith_api_key: str = Field(default="")
    langsmith_project: str = Field(default="l1-support-ai")
    langchain_tracing_v2: str = Field(default="false")
    otel_exporter_otlp_endpoint: str = Field(default="http://localhost:4317")

    # ── Crawling / Search ───────────────────────────────────────────────
    firecrawl_api_key: str = Field(default="")
    exa_api_key: str = Field(default="", description="Exa neural search API — used for news/review/social discovery")

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

    # ── Graphiti (temporal KG) ─────────────────────────────────────────
    # Graphiti uses FalkorDB — same connection as knowledge graph
    graphiti_graph_name: str = Field(default="l1_user_memory")

    # ── App ─────────────────────────────────────────────────────────────
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")
    environment: str = Field(default="development")
    admin_api_key: str = Field(default="l1support-demo-key-2026")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

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
