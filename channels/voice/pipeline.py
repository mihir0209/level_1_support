"""
Pipecat voice AI pipeline for L1 support.

Architecture:
  Twilio SIP → Daily WebRTC room → Pipecat pipeline
  Pipeline: Daily input → Deepgram STT → Anthropic LLM → ElevenLabs TTS → Daily output

Each call gets its own Daily room URL and Pipecat pipeline instance.
The LLM system prompt is brand-aware (brand_id passed at pipeline construction).

Silence/interruption is handled automatically by Pipecat's frame model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from config import settings

log = structlog.get_logger(__name__)

# ── Voice persona per brand tier ──────────────────────────────────────────────

_VOICE_PERSONAS: dict[str, dict[str, str]] = {
    # brand_id: {voice_id: ElevenLabs voice ID, tone: descriptor}
    "boohoo": {"voice_id": "EXAVITQu4vr4xnSDxMaL", "tone": "friendly and upbeat"},
    "prettylittlething": {"voice_id": "EXAVITQu4vr4xnSDxMaL", "tone": "trendy and energetic"},
    "nasty_gal": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "tone": "bold and confident"},
    "karen_millen": {"voice_id": "AZnzlk1XvdvUeBnXmlld", "tone": "sophisticated and professional"},
    "coast": {"voice_id": "AZnzlk1XvdvUeBnXmlld", "tone": "elegant and warm"},
    "warehouse": {"voice_id": "MF3mGyEYCl7XYWbV9V6O", "tone": "stylish and approachable"},
    "oasis": {"voice_id": "MF3mGyEYCl7XYWbV9V6O", "tone": "chic and helpful"},
    "boohooman": {"voice_id": "TxGEqnHWrfWFTfGW9XjX", "tone": "direct and casual"},
    "misspap": {"voice_id": "EXAVITQu4vr4xnSDxMaL", "tone": "fun and friendly"},
    "dorothy_perkins": {"voice_id": "MF3mGyEYCl7XYWbV9V6O", "tone": "warm and reassuring"},
    "burton": {"voice_id": "TxGEqnHWrfWFTfGW9XjX", "tone": "helpful and clear"},
    "wallis": {"voice_id": "AZnzlk1XvdvUeBnXmlld", "tone": "polished and caring"},
    "papaya": {"voice_id": "EXAVITQu4vr4xnSDxMaL", "tone": "bright and helpful"},
}

_DEFAULT_PERSONA = {"voice_id": "EXAVITQu4vr4xnSDxMaL", "tone": "friendly and professional"}


def _get_persona(brand_id: str) -> dict[str, str]:
    return _VOICE_PERSONAS.get(brand_id, _DEFAULT_PERSONA)


def _build_voice_system_prompt(brand_id: str, customer_name: str | None = None) -> str:
    persona = _get_persona(brand_id)
    greeting_name = f", {customer_name}" if customer_name else ""
    return f"""You are a {persona['tone']} voice assistant for {brand_id.replace('_', ' ').title()}'s customer support team.

VOICE RULES (critical — you are speaking, not writing):
- Keep every response under 2 sentences unless giving step-by-step instructions
- Never use bullet points, markdown, or special characters — they will be spoken aloud
- Spell out numbers: "twenty-eight days" not "28 days"
- Pause naturally: use commas where you would pause when speaking
- For order numbers, say each part: "B H dash one two three four five"
- If you don't understand, say "Could you repeat that?" not "I didn't catch that, could you please repeat your query?"

OPENING: "Hello{greeting_name}, you've reached {brand_id.replace('_', ' ').title()} customer support. How can I help you today?"

WORKFLOW:
1. Identify the issue (WISMO, return, refund, payment, account)
2. Ask for the order number if needed — spell out what format to give
3. Look up the information and give a clear, spoken answer
4. Confirm resolution: "Is there anything else I can help you with?"

ESCALATION TRIGGER: If the customer is very upset or the issue needs account access,
say: "I'm going to transfer you to a specialist who can help with this right away."
Then use the escalate tool."""


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline builder
# ═══════════════════════════════════════════════════════════════════════════

async def build_voice_pipeline(
    room_url: str,
    room_token: str,
    brand_id: str,
    session_id: str,
    customer_name: str | None = None,
) -> tuple[Any, Any]:
    """
    Build and return a (PipelineTask, PipelineRunner) pair for a voice call.

    Call runner.run(task) to start processing.
    Pipeline: Daily input → Deepgram STT → Anthropic LLM → ElevenLabs TTS → Daily output
    """
    from pipecat.transports.daily.transport import DailyTransport, DailyParams
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
    from pipecat.services.anthropic.llm import AnthropicLLMService
    from pipecat.processors.aggregators.llm_response import LLMFullResponseAggregator
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineTask, PipelineParams
    from pipecat.pipeline.runner import PipelineRunner

    persona = _get_persona(brand_id)
    system_prompt = _build_voice_system_prompt(brand_id, customer_name)

    # ── Transport (Daily WebRTC) ───────────────────────────────────────────
    transport = DailyTransport(
        room_url=room_url,
        token=room_token,
        bot_name=f"{brand_id.replace('_', ' ').title()} Support",
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            transcription_enabled=False,  # We use Deepgram directly
            vad_enabled=True,
            vad_stop_secs=0.6,
        ),
    )

    # ── STT (Deepgram) ────────────────────────────────────────────────────
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        live_options=None,
    )

    # ── LLM (Anthropic) ───────────────────────────────────────────────────
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}]
    )
    llm = AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        model=settings.primary_model,
    )

    # ── Context aggregator ────────────────────────────────────────────────
    context_aggregator = llm.create_context_aggregator(context)

    # ── TTS (ElevenLabs) ──────────────────────────────────────────────────
    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        voice_id=persona["voice_id"],
        model="eleven_turbo_v2_5",
        sample_rate=16000,
    )

    # ── Pipeline ──────────────────────────────────────────────────────────
    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        context_aggregator.assistant(),
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    runner = PipelineRunner()

    log.info(
        "voice_pipeline_built",
        brand_id=brand_id,
        session_id=session_id,
        room_url=room_url,
    )
    return task, runner


async def run_voice_call(
    room_url: str,
    room_token: str,
    brand_id: str,
    session_id: str,
    customer_name: str | None = None,
) -> None:
    """
    Run a complete voice call session.

    This blocks until the call ends (participant leaves or idle timeout).
    Call from an async background task.
    """
    task, runner = await build_voice_pipeline(
        room_url=room_url,
        room_token=room_token,
        brand_id=brand_id,
        session_id=session_id,
        customer_name=customer_name,
    )
    log.info("voice_call_start", session_id=session_id, brand_id=brand_id)
    try:
        await runner.run(task)
    except Exception as exc:
        log.error("voice_call_error", session_id=session_id, error=str(exc))
        raise
    finally:
        log.info("voice_call_ended", session_id=session_id)
