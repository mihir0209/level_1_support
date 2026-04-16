"""
LLM client wrapper — thin abstraction over LiteLLM.
Provides both completion and structured-output helpers used across the system.
"""

from __future__ import annotations

import json
from typing import Any

import litellm
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from config import settings

log = structlog.get_logger(__name__)

# Configure LiteLLM
litellm.set_verbose = False


class LLMClient:
    """
    Async-first LLM client.
    Uses LiteLLM for raw completions (intent classification, entity extraction).
    Uses LangChain ChatAnthropic for tool-calling in LangGraph nodes.
    """

    def __init__(
        self,
        primary_model: str = settings.primary_model,
        fast_model: str = settings.fast_model,
    ):
        self.primary_model = primary_model
        self.fast_model = fast_model

        # LangChain-compatible client (used in LangGraph nodes with tool_choice)
        self.lc_primary = ChatAnthropic(
            model=primary_model,
            api_key=settings.anthropic_api_key,
            max_tokens=4096,
        )
        self.lc_fast = ChatAnthropic(
            model=fast_model,
            api_key=settings.anthropic_api_key,
            max_tokens=512,
        )

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
    ) -> str:
        """Raw async text generation via LiteLLM."""
        target_model = model or self.fast_model
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await litellm.acompletion(
                model=f"anthropic/{target_model}",
                messages=messages,
                max_tokens=max_tokens,
                api_key=settings.anthropic_api_key,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            log.error("llm_generate_failed", model=target_model, error=str(e))
            raise

    def get_lc_client(self, fast: bool = False):
        """Return LangChain-compatible client for LangGraph tool-calling nodes."""
        return self.lc_fast if fast else self.lc_primary


# Module-level singleton
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
