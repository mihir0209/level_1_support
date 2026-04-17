"""
LLM client — supports three providers via LiteLLM:
  • anthropic  →  claude-sonnet-4-6 / claude-haiku-4-5
  • azure      →  azure/<deployment> (Azure OpenAI)
  • openai     →  gpt-4o / gpt-4o-mini

Provider is selected by LLM_PROVIDER env var.
All provider-specific routing is encapsulated here;
no other module knows which backend is active.
"""

from __future__ import annotations

import os
from typing import Any

import litellm
import structlog

from config import settings

log = structlog.get_logger(__name__)

litellm.set_verbose = False

# ── LiteLLM model strings per provider ────────────────────────────────────

def _litellm_model(fast: bool = False) -> str:
    """Return the LiteLLM model string for the active provider."""
    p = settings.llm_provider.lower()
    if p == "azure":
        dep = settings.azure_fast_deployment if fast else settings.azure_primary_deployment
        return f"azure/{dep}"
    elif p == "openai":
        return settings.openai_fast_model if fast else settings.openai_primary_model
    else:  # anthropic (default)
        return settings.fast_model if fast else settings.primary_model


def _litellm_kwargs() -> dict[str, Any]:
    """Extra kwargs LiteLLM needs for the active provider."""
    p = settings.llm_provider.lower()
    if p == "azure":
        return {
            "api_key": settings.azure_openai_api_key,
            "api_base": settings.azure_openai_endpoint,
            "api_version": settings.azure_openai_api_version,
        }
    elif p == "openai":
        return {"api_key": settings.openai_api_key}
    else:
        return {"api_key": settings.anthropic_api_key}


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
        return ChatOpenAI(
            model=model,
            api_key=settings.openai_api_key,
            max_tokens=512 if fast else 4096,
            temperature=0,
        )
    else:  # anthropic
        from langchain_anthropic import ChatAnthropic
        model = settings.fast_model if fast else settings.primary_model
        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            max_tokens=512 if fast else 4096,
        )


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
        model: str | None = None,          # override; else uses fast model
        max_tokens: int = 1024,
        system: str | None = None,
        fast: bool = True,
    ) -> str:
        """Raw async text completion."""
        target_model = model or _litellm_model(fast=fast)
        extra = _litellm_kwargs()

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
                model=f"azure/{settings.azure_embedding_deployment}",
                input=text,
                api_key=settings.azure_openai_api_key,
                api_base=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
            )
        elif p == "openai":
            resp = await litellm.aembedding(
                model="text-embedding-3-small",
                input=text,
                api_key=settings.openai_api_key,
            )
        else:
            # Anthropic has no embedding API — fall back to OpenAI-compatible
            # If no OpenAI key, return zero vector (vector search gracefully degrades)
            if not settings.openai_api_key:
                log.warning("embed_no_key", msg="No embedding key; returning zero vector")
                return [0.0] * 1536
            resp = await litellm.aembedding(
                model="text-embedding-3-small",
                input=text,
                api_key=settings.openai_api_key,
            )
        return resp.data[0]["embedding"]


# ── Singleton ─────────────────────────────────────────────────────────────
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
