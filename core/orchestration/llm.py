"""
LLM client — supports three providers via LiteLLM:
  • anthropic  →  claude-sonnet-4-6 / claude-haiku-4-5
  • azure      →  Azure OpenAI deployment names
  • openai     →  gpt-4o / gpt-4o-mini (or any OpenAI-compatible proxy)

Provider is selected by LLM_PROVIDER env var.
All LiteLLM calls use explicit custom_llm_provider so the backend format
is never inferred from the model name — safe for custom proxy setups.
"""

from __future__ import annotations

from typing import Any

import litellm
import structlog

from config import settings

log = structlog.get_logger(__name__)

litellm.set_verbose = False


# ── Unified param builder ──────────────────────────────────────────────────

def _litellm_params(fast: bool = False) -> tuple[str, dict[str, Any]]:
    """
    Return (model_name, kwargs) for litellm.acompletion / aembedding.
    custom_llm_provider is always set explicitly — never inferred from model name.
    """
    p = settings.llm_provider.lower()
    if p == "azure":
        model = settings.azure_fast_deployment if fast else settings.azure_primary_deployment
        return model, {
            "custom_llm_provider": "azure",
            "api_key": settings.azure_openai_api_key,
            "api_base": settings.azure_openai_endpoint,
            "api_version": settings.azure_openai_api_version,
        }
    elif p == "openai":
        model = settings.openai_fast_model if fast else settings.openai_primary_model
        kwargs: dict[str, Any] = {
            "custom_llm_provider": "openai",
            "api_key": settings.openai_api_key,
        }
        if settings.openai_api_base:
            kwargs["api_base"] = settings.openai_api_base
        return model, kwargs
    else:  # anthropic
        model = settings.fast_model if fast else settings.primary_model
        kwargs = {
            "custom_llm_provider": "anthropic",
            "api_key": settings.anthropic_api_key,
        }
        if settings.anthropic_api_base:
            kwargs["api_base"] = settings.anthropic_api_base.rstrip("/")
        return model, kwargs


def _lc_client(fast: bool = False):
    """
    Return a LangChain chat model compatible with LangGraph tool-calling.
    Selects the right provider/model based on LLM_PROVIDER.
    """
    p = settings.llm_provider.lower()

    if p == "azure":
        from langchain_openai import AzureChatOpenAI
        deployment = settings.azure_fast_deployment if fast else settings.azure_primary_deployment
        return AzureChatOpenAI(
            azure_deployment=deployment,
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            max_tokens=512 if fast else 4096,
            temperature=0,
        )
    elif p == "openai":
        from langchain_openai import ChatOpenAI
        model = settings.openai_fast_model if fast else settings.openai_primary_model
        kwargs = {
            "model": model,
            "api_key": settings.openai_api_key,
            "max_tokens": 512 if fast else 4096,
            "temperature": 0,
        }
        if settings.openai_api_base:
            kwargs["base_url"] = settings.openai_api_base
        return ChatOpenAI(**kwargs)
    else:  # anthropic
        from langchain_anthropic import ChatAnthropic
        model = settings.fast_model if fast else settings.primary_model
        kwargs = {
            "model": model,
            "api_key": settings.anthropic_api_key,
            "max_tokens": 512 if fast else 4096,
        }
        if settings.anthropic_api_base:
            kwargs["base_url"] = settings.anthropic_api_base
        return ChatAnthropic(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════

class LLMClient:
    """
    Single async-first LLM client.
    generate()     → raw text via LiteLLM (intent classification, extraction)
    get_lc_client() → LangChain client for LangGraph tool-calling nodes
    provider        → active provider name for logging
    """

    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()
        log.info("llm_client_init", provider=self.provider)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
        fast: bool = True,
    ) -> str:
        """Raw async text completion."""
        target_model, extra = _litellm_params(fast=fast)
        if model:
            target_model = model  # explicit override keeps provider kwargs intact

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = await litellm.acompletion(
                model=target_model,
                messages=messages,
                max_tokens=max_tokens,
                **extra,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            log.error("llm_generate_failed", model=target_model, provider=self.provider, error=str(e))
            raise

    def get_lc_client(self, fast: bool = False):
        """LangChain-compatible client for LangGraph nodes with tool binding."""
        return _lc_client(fast=fast)

    # ── Convenience: embedding (used by vector fallback search) ───────────
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a piece of text."""
        p = self.provider
        if p == "azure":
            resp = await litellm.aembedding(
                model=settings.azure_embedding_deployment,
                input=text,
                custom_llm_provider="azure",
                api_key=settings.azure_openai_api_key,
                api_base=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
            )
        else:
            # openai provider (or anthropic fallback — Anthropic has no embedding API)
            if not settings.openai_api_key:
                log.warning("embed_no_key", msg="No embedding key; returning zero vector")
                return [0.0] * 1536
            kwargs: dict[str, Any] = {
                "model": "text-embedding-3-small",
                "input": text,
                "custom_llm_provider": "openai",
                "api_key": settings.openai_api_key,
            }
            if settings.openai_api_base:
                kwargs["api_base"] = settings.openai_api_base
            resp = await litellm.aembedding(**kwargs)
        return resp.data[0]["embedding"]


# ── Singleton ─────────────────────────────────────────────────────────────
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
