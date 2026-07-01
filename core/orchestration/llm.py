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
            
            # Increment Prometheus billing / token usage counters
            if hasattr(resp, "usage") and resp.usage:
                try:
                    from core.logging_config import get_active_brand_id
                    from core.metrics import TOKEN_USAGE_INPUT, TOKEN_USAGE_OUTPUT
                    brand_id = get_active_brand_id() or "unknown"
                    
                    p_tok = getattr(resp.usage, "prompt_tokens", 0) or 0
                    c_tok = getattr(resp.usage, "completion_tokens", 0) or 0
                    
                    if p_tok > 0:
                        TOKEN_USAGE_INPUT.labels(brand_id=brand_id).inc(p_tok)
                    if c_tok > 0:
                        TOKEN_USAGE_OUTPUT.labels(brand_id=brand_id).inc(c_tok)
                except Exception as ex:
                    log.warning("metrics_token_usage_failed", error=str(ex))

            return resp.choices[0].message.content or ""
        except Exception as e:
            log.error("llm_generate_failed", model=target_model, provider=self.provider, error=str(e))
            raise

    async def detect_language(self, text: str) -> str:
        """
        Detect language of a given text.
        Returns ISO 639-1 language code (e.g. 'en', 'es', 'fr', 'de').
        """
        # Strip common punctuation and whitespaces
        clean_text = (text or "").strip()
        if not clean_text:
            return "en"

        # Check if text consists only of basic ascii chars and is short/simple -> default to english
        # to avoid invoking the LLM for trivial statements (saves latency/tokens)
        if len(clean_text) < 15 and all(ord(c) < 128 for c in clean_text):
            return "en"

        prompt = f"Analyze the language of the following user message. Respond with ONLY the 2-letter ISO 639-1 language code (e.g. 'en', 'es', 'fr', 'de', 'it').\n\nMessage: {clean_text}"
        try:
            res = await self.generate(
                prompt=prompt,
                system="You are a precise language classifier. Output ONLY the 2-letter ISO code.",
                fast=True,
                max_tokens=5,
            )
            code = res.strip().lower()[:2]
            if len(code) == 2 and code.isalpha():
                return code
        except Exception as e:
            log.warning("language_detection_failed", error=str(e))
        return "en"

    async def translate_content(self, text: str, target_lang: str) -> str:
        """
        Translate the given content to the target language locale.
        """
        if not text or not target_lang:
            return text

        if target_lang.lower() == "en":
            # If target is English, and text consists only of basic English characters, return directly
            if all(ord(c) < 128 for c in text):
                return text

        # Map 2-letter codes to friendly names for better LLM quality
        lang_names = {
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "pt": "Portuguese",
            "nl": "Dutch",
        }
        target_name = lang_names.get(target_lang.lower(), target_lang)

        prompt = f"Translate the following text into {target_name}. Respond with ONLY the translated text, preserving all formatting and placeholders (like HTML, links, or order IDs). Do not include any explanations or conversational chatter.\n\nText: {text}"
        try:
            res = await self.generate(
                prompt=prompt,
                system=f"You are a professional translator translating content to {target_name}. Output ONLY the raw translation.",
                fast=True,
                max_tokens=2048,
            )
            return res.strip()
        except Exception as e:
            log.warning("translation_failed", target_lang=target_lang, error=str(e))
        return text

    async def self_heal_json(self, bad_json: str, error_msg: str) -> dict | None:
        """
        Attempt to self-heal malformed JSON using a lightweight reflection LLM call.
        """
        prompt = f"You produced the following text which was expected to be valid JSON, but failed parsing:\n{bad_json}\n\nThe parser returned the error:\n{error_msg}\n\nPlease clean and correct this JSON structure so that it is valid. Respond with ONLY the raw JSON object. Do not include any explanations, markdown code blocks, or chatter."
        try:
            res = await self.generate(
                prompt=prompt,
                system="You are a precise JSON corrector. Output ONLY valid JSON.",
                fast=True,
                max_tokens=512,
            )
            clean_res = res.strip()
            if clean_res.startswith("```"):
                lines = clean_res.split("\n")
                if lines[0].startswith("```json") or lines[0].startswith("```"):
                    clean_res = "\n".join(lines[1:-1]).strip()
            import json
            return json.loads(clean_res)
        except Exception as e:
            log.warning("json_self_healing_failed", error=str(e))
        return None

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
