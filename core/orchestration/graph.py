"""
LangGraph state machine — the orchestration core.

Nodes:
  assemble_context   → Context Engineering Engine (8-step pipeline)
  route_to_agent    → Execute specialist agent (WISMO/Returns/Refund/etc.)
  execute_tools     → Run tool calls from specialist
  generate_response → Specialist output or primary LLM fallback
  check_resolution  → Decide: next turn | escalate | close
  escalate_session  → Write to MemPalace + Graphiti, create Chatwoot ticket
  close_session     → Write to MemPalace + Graphiti, trigger feedback loop

Edge routing:
  START → assemble_context → route_to_agent
  route_to_agent → generate_response (specialist output)
  generate_response → END (specialist path) | tools | escalate | close | END
  tools → generate_response
  escalate → END
  close → END
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt.tool_node import RunnableCallable

from config import settings
from core.context.engine import assemble_context
from core.ontology.models import Channel, Customer, SessionOutcome
from core.orchestration.llm import get_llm_client
from core.orchestration.state import L1SupportState
from core.tools.registry import ALL_TOOLS, TOOL_REGISTRY
from core.memory.mempalace_client import mempalace


# ─────────────────────────────────────────────────────────────────────────
# STATEFUL TOOL NODE — resolves tools from state at invoke time
# ─────────────────────────────────────────────────────────────────────────

class StatefulToolNode(RunnableCallable):
    """
    Tool executor that reads available tools from L1SupportState at invoke time,
    rather than being pre-bound at graph-build time. This allows the active tool
    set to differ per brand and per turn (static TOOL_REGISTRY + dynamic API tools).

    Inherits from RunnableCallable so it is accepted as a proper langgraph Runnable.
    """

    def __init__(self, static_tools: list | None = None):
        self._static_tools = static_tools or ALL_TOOLS
        super().__init__(func=self._sync_invoke, afunc=self._async_invoke, name="tools")

    def _resolve_tools(self, state: L1SupportState) -> list:
        """Resolve the full tool list from state: static registry + dynamic tools."""
        manifest = state.context_package.tool_manifest if state.context_package else []
        tool_map: dict[str, Any] = {t.name: t for t in self._static_tools}

        # Overlay dynamic tools (brand-API tools keyed by brand_id_* prefix)
        if state.dynamic_tools:
            for t in state.dynamic_tools:
                name = getattr(t, "name", "") or ""
                tool_map[name] = t
                if "_" in name:
                    stripped = name.split("_", 1)[1]
                    if stripped not in tool_map:
                        tool_map[stripped] = t

        return [tool_map[n] for n in manifest if n in tool_map]

    async def _self_correct_tool_args(
        self,
        tool_name: str,
        tool_args: dict,
        error_msg: str,
        tool_schema: dict
    ) -> dict | None:
        """
        Ask the LLM to reflect on incorrect tool arguments and output corrected arguments.
        """
        from core.orchestration.llm import get_llm_client
        llm = get_llm_client()

        prompt = (
            f"You attempted to call the tool '{tool_name}' with the following arguments:\n"
            f"{json.dumps(tool_args, indent=2)}\n\n"
            f"However, execution failed with the error:\n"
            f"{error_msg}\n\n"
            f"The correct arguments schema for this tool is:\n"
            f"{json.dumps(tool_schema, indent=2)}\n\n"
            f"Please output the corrected JSON arguments matching the schema. Respond with ONLY the raw JSON object, do not include any markdown fences or explanation."
        )
        try:
            res_text = await llm.generate(
                prompt=prompt,
                system="You are a precise JSON auto-corrector. Output ONLY valid JSON arguments.",
                fast=True,
                max_tokens=256,
            )
            clean_res = res_text.strip()
            if clean_res.startswith("```"):
                lines = clean_res.split("\n")
                if lines[0].startswith("```json") or lines[0].startswith("```"):
                    clean_res = "\n".join(lines[1:-1]).strip()
            return json.loads(clean_res)
        except Exception as e:
            log.warning("tool_args_self_correction_failed", tool_name=tool_name, error=str(e))
        return None

    def _sync_invoke(self, state: L1SupportState) -> dict[str, Any]:
        """Execute tool calls from the last AIMessage in state (sync entry point)."""
        available = self._resolve_tools(state)

        last_ai: AIMessage | None = None
        for msg in reversed(state.messages):
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                last_ai = msg
                break

        if not last_ai or not last_ai.tool_calls:
            return {"messages": []}

        tool_messages: list[ToolMessage] = []
        for call in last_ai.tool_calls:
            tool_name = call.get("name")
            tool_args = call.get("args") or {}
            tool_id = call.get("id", "")

            selected = next((t for t in available if getattr(t, "name", "") == tool_name), None)
            if not selected:
                tool_messages.append(ToolMessage(
                    content=f"Tool '{tool_name}' not found.",
                    tool_call_id=tool_id,
                    name=tool_name,
                ))
                continue

            try:
                result = selected.invoke(tool_args)
                content = result if isinstance(result, str) else json.dumps(result) if isinstance(result, (dict, list)) else str(result)
            except Exception as exc:
                content = f"Tool execution failed: {exc}"

            tool_messages.append(ToolMessage(
                content=content,
                tool_call_id=tool_id,
                name=tool_name,
            ))

        return {"messages": tool_messages}

    async def _async_invoke(self, state: L1SupportState) -> dict[str, Any]:
        """Async entry point — execute tools, run self-healing retries, and sync to Chatwoot."""
        available = self._resolve_tools(state)

        last_ai: AIMessage | None = None
        for msg in reversed(state.messages):
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                last_ai = msg
                break

        if not last_ai or not last_ai.tool_calls:
            return {"messages": []}

        tool_messages: list[ToolMessage] = []
        for call in last_ai.tool_calls:
            tool_name = call.get("name")
            tool_args = call.get("args") or {}
            tool_id = call.get("id", "")

            selected = next((t for t in available if getattr(t, "name", "") == tool_name), None)
            if not selected:
                tool_messages.append(ToolMessage(
                    content=f"Tool '{tool_name}' not found.",
                    tool_call_id=tool_id,
                    name=tool_name,
                ))
                continue

            # Check if the tool is high-risk and requires human approval
            is_high_risk = tool_name in ("return_init", "cancel_order", "order_cancel", "initiate_return")
            if is_high_risk:
                if state.human_approval is None:
                    log.info("high_risk_tool_paused_awaiting_approval", tool_name=tool_name, args=tool_args)
                    pending = {
                        "tool_name": tool_name,
                        "args": tool_args,
                        "id": tool_id
                    }
                    tool_messages.append(ToolMessage(
                        content=f"Action '{tool_name}' requires human authorization. Pausing execution.",
                        tool_call_id=tool_id,
                        name=tool_name,
                    ))
                    # PAUSE: Return updated messages, set pending_action and is_paused
                    return {
                        "messages": tool_messages,
                        "pending_action": pending,
                        "is_paused": True
                    }
                elif state.human_approval == "rejected":
                    log.info("high_risk_tool_rejected_by_human", tool_name=tool_name, feedback=state.human_feedback)
                    tool_messages.append(ToolMessage(
                        content=f"Action rejected by support representative: {state.human_feedback or 'No feedback provided'}",
                        tool_call_id=tool_id,
                        name=tool_name,
                    ))
                    # Trigger escalation
                    return {
                        "messages": tool_messages,
                        "outcome": SessionOutcome.ESCALATED,
                        "escalation_reason": f"Action rejected: {state.human_feedback}",
                        "pending_action": None,
                        "human_approval": None,
                        "is_paused": False
                    }
                elif state.human_approval == "approved":
                    log.info("high_risk_tool_approved_executing", tool_name=tool_name)

            # First attempt
            try:
                if hasattr(selected, "ainvoke"):
                    result = await selected.ainvoke(tool_args)
                else:
                    result = selected.invoke(tool_args)
                content = result if isinstance(result, str) else json.dumps(result) if isinstance(result, (dict, list)) else str(result)
            except Exception as exc:
                error_msg = str(exc)
                log.warning("tool_execution_failed_attempting_self_healing", tool_name=tool_name, args=tool_args, error=error_msg)

                # Fetch schema args
                schema_args = getattr(selected, "args", {})
                corrected_args = await self._self_correct_tool_args(tool_name, tool_args, error_msg, schema_args)

                if corrected_args:
                    log.info("tool_args_self_corrected", tool_name=tool_name, old_args=tool_args, corrected_args=corrected_args)
                    try:
                        if hasattr(selected, "ainvoke"):
                            result = await selected.ainvoke(corrected_args)
                        else:
                            result = selected.invoke(corrected_args)
                        content = result if isinstance(result, str) else json.dumps(result) if isinstance(result, (dict, list)) else str(result)
                    except Exception as retry_exc:
                        content = f"Tool execution failed after self-correction retry: {retry_exc}"
                else:
                    content = f"Tool execution failed: {exc}"

            tool_messages.append(ToolMessage(
                content=content,
                tool_call_id=tool_id,
                name=tool_name,
            ))

        if tool_messages:
            try:
                from core.memory.session import get_session
                session_data = await get_session(state.session_id)
                if session_data:
                    chatwoot_conv_id = session_data.get("chatwoot_conv_id")
                    if chatwoot_conv_id:
                        from channels.chat.chatwoot_client import post_message
                        for msg in tool_messages:
                            note_content = f"🔧 Tool Execution:\n• Name: {msg.name}\n• Result: {msg.content}"
                            post_message(chatwoot_conv_id, note_content, private=True)
            except Exception as e:
                log.warning("chatwoot_tool_sync_failed", error=str(e))

        return {
            "messages": tool_messages,
            "pending_action": None,
            "human_approval": None,
            "human_feedback": None,
            "is_paused": False
        }


log = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# ESCALATION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────
MAX_RESOLUTION_ATTEMPTS = 10
MAX_TURNS = 10

# ─────────────────────────────────────────────────────────────────────────
# NODE: assemble_context
# ─────────────────────────────────────────────────────────────────────────

async def node_assemble_context(state: L1SupportState) -> dict[str, Any]:
    """
    Step 1-8 of the Context Engineering pipeline.
    Runs on every turn. Returns updated state fields.
    """
    llm = get_llm_client()

    # Extract last user message
    last_user_message = ""
    for msg in reversed(state.messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_message = msg.get("content", "")
            break
        elif isinstance(msg, HumanMessage):
            last_user_message = msg.content
            break

    # Detect user language
    locale = "en"
    translated_message = last_user_message
    if last_user_message:
        locale = await llm.detect_language(last_user_message)
        if locale != "en":
            translated_message = await llm.translate_content(last_user_message, "en")
            log.info("message_translated_to_english", original=last_user_message, translated=translated_message, locale=locale)

    # Build conversation history list for context engine
    history = []
    for m in state.messages[:-1]:  # exclude current message (already in last_user_message)
        if isinstance(m, ToolMessage):
            continue
        role = "user" if isinstance(m, HumanMessage) else "assistant"
        content = m.content if hasattr(m, "content") else str(m)
        if locale != "en" and content:
            content = await llm.translate_content(content, "en")
        history.append({"role": role, "content": content})

    # Get order date for temporal policy lookup (from first order if available)
    order_placed_at = None
    if state.customer and state.customer.recent_orders:
        order_placed_at = state.customer.recent_orders[0].placed_at

    context_package = await assemble_context(
        message=translated_message,
        session_id=state.session_id,
        brand_id=state.brand_id,
        channel=state.channel,
        conversation_history=history,
        customer=state.customer,
        llm_client=llm,
        customer_order_placed_at=order_placed_at,
    )

    log.info(
        "context_assembled",
        session_id=state.session_id,
        locale=locale,
        intent=context_package.intent.issue_type if context_package.intent else "unknown",
        latency_ms=round(context_package.assembly_latency_ms, 1),
    )

    intent_conf = context_package.intent.confidence if context_package.intent else 0.0
    effective_conf = max(intent_conf, context_package.retrieval_confidence)

    # ── Load dynamic brand tools once per session ──────────────────────
    # Loaded on first turn and stored in the state field for reuse across turns.
    dynamic_tools: list[Any] = []
    if not state.dynamic_tools:
        try:
            from core.tools.dynamic_builder import load_tools_for_brand
            dynamic_tools = load_tools_for_brand(state.brand_id)
            log.info(
                "dynamic_tools_loaded",
                session_id=state.session_id,
                brand_id=state.brand_id,
                count=len(dynamic_tools),
            )
        except Exception as e:
            log.warning(
                "dynamic_tools_load_failed",
                session_id=state.session_id,
                brand_id=state.brand_id,
                error=str(e),
            )

    return {
        "context_package": context_package,
        "intent": context_package.intent,
        "entities": context_package.entities,
        "confidence": effective_conf,
        "turn_count": state.turn_count + 1,
        "dynamic_tools": dynamic_tools,
        "locale": locale,
    }


# ─────────────────────────────────────────────────────────────────────────
# NODE: route_to_agent
# ─────────────────────────────────────────────────────────────────────────

async def node_route_to_agent(state: L1SupportState) -> dict[str, Any]:
    """
    Route to the appropriate specialist agent and execute it.
    The agent handles tools internally and returns a final text response.
    We skip tool execution in the LangGraph ToolNode — the specialist
    agent owns that loop up to its max_loops limit.
    """
    from core.agents.specialists import get_agent_for_intent
    from core.agents.base import run_specialist

    intent_id = state.intent.issue_type if state.intent else "unknown"

    # Build task description from context
    last_msg = ""
    for msg in reversed(state.messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_msg = msg.get("content", "")
            break
        elif hasattr(msg, "content"):
            last_msg = msg.content
            break

    if state.locale != "en" and last_msg:
        from core.orchestration.llm import get_llm_client
        llm = get_llm_client()
        last_msg = await llm.translate_content(last_msg, "en")

    entities_dict = state.entities.model_dump() if state.entities else {}
    entity_str = ", ".join(f"{k}={v}" for k, v in entities_dict.items() if v is not None) if entities_dict else "none"
    task = (
        f"Customer message: \"{last_msg}\"\n"
        f"Brand: {state.brand_id}\n"
        f"Extracted entities: {entity_str}\n"
        f"Policy context: {state.context_package.policy_block if state.context_package else 'none'}\n"
        f"Resolution steps available: {state.context_package.resolution_block if state.context_package else 'none'}"
    )

    agent_name = "supervisor_agent"
    try:
        from core.agents.simple import HighRiskActionPaused, HighRiskActionRejected
        specialist = get_agent_for_intent(intent_id, state.brand_id)
        if hasattr(specialist, "name"):
            agent_name = specialist.name
        response = await run_specialist(
            agent=specialist,
            task=task,
            brand_id=state.brand_id,
            customer_id=state.customer_id,
            intent_id=intent_id,
            state=state,
        )
        log.info(
            "specialist_agent_ran",
            session_id=state.session_id,
            intent=intent_id,
            agent=agent_name,
            response_len=len(response),
        )
        return {
            "routed_to_agent": agent_name,
            "specialist_response": response,
            "specialist_used": True,
            "is_paused": False,
            "pending_action": None,
            "human_approval": None,
            "human_feedback": None,
        }
    except HighRiskActionPaused as pause_exc:
        log.info("specialist_agent_paused_awaiting_approval", agent=agent_name, tool_name=pause_exc.tool_name)
        from langchain_core.messages import ToolMessage
        pause_msg = ToolMessage(
            content=f"Action '{pause_exc.tool_name}' requires human authorization. Pausing execution.",
            tool_call_id=pause_exc.tool_call_id,
            name=pause_exc.tool_name,
        )
        pending = {
            "tool_name": pause_exc.tool_name,
            "args": pause_exc.tool_args,
            "id": pause_exc.tool_call_id
        }
        return {
            "messages": [pause_msg],
            "specialist_response": f"Action {pause_exc.tool_name} requires human authorization. Pausing execution.",
            "specialist_used": True,
            "pending_action": pending,
            "is_paused": True,
        }
    except HighRiskActionRejected as reject_exc:
        log.info("specialist_agent_rejected_by_human", agent=agent_name, tool_name=reject_exc.tool_name)
        return {
            "specialist_response": f"Action rejected by support representative: {reject_exc.feedback or 'No feedback provided'}",
            "specialist_used": True,
            "outcome": SessionOutcome.ESCALATED,
            "escalation_reason": f"Action rejected: {reject_exc.feedback}",
            "pending_action": None,
            "human_approval": None,
            "is_paused": False,
        }
    except Exception as e:
        log.warning(
            "specialist_agent_failed_fallback",
            session_id=state.session_id,
            intent=intent_id,
            agent=agent_name,
            error=str(e),
        )
        # Fall back to direct LLM in node_generate_response
        return {
            "routed_to_agent": agent_name,
            "specialist_response": None,
            "specialist_used": False,
        }


# ─────────────────────────────────────────────────────────────────────────
# NODE: generate_response
# ─────────────────────────────────────────────────────────────────────────

async def _build_llm_messages(state: L1SupportState) -> list:
    """
    Construct the message list passed to the LLM.
    Injects ContextPackage blocks as the system message.
    """
    ctx = state.context_package
    if not ctx:
        return [HumanMessage(content="Hello, I need help.")]

    # Merge static manifest names with dynamically-loaded tool base names
    manifest_names = list(ctx.tool_manifest) if ctx.tool_manifest else []
    if state.dynamic_tools:
        for t in state.dynamic_tools:
            name = getattr(t, "name", "") or ""
            stripped = name.split("_", 1)[1] if "_" in name else name
            if stripped not in manifest_names and name not in manifest_names:
                manifest_names.append(stripped)

    # Build structured system message from ContextPackage blocks
    system_content = "\n\n".join(filter(None, [
        ctx.system_prompt,
        "---",
        ctx.user_context_block,
        "---",
        "IMPORTANT POLICY RULES:",
        "  - When a customer asks to return an item, ALWAYS check the 'Excluded items' list in the policy block first.",
        "  - If the item category is listed in excluded items, state clearly that it CANNOT be returned before any other information.",
        "  - Always give accurate timelines. Do NOT guess days — use the numbers in the policy block.",
        "---",
        ctx.policy_block,
        ctx.shipping_block or None,
        "---",
        ctx.resolution_block,
        "---",
        f"AVAILABLE TOOLS: {', '.join(manifest_names)}",
        "---",
        "CURRENT SESSION CONTEXT:",
        ctx.session_block,
        "---",
        "ESCALATION RULES: Escalate if: (1) resolution_attempts >= 10, "
        "(2) customer explicitly asks for a human agent, "
        "(3) refund is overdue (>21 days since return was received), "
        "(4) confidence in resolution is below 45%.",
        "---",
        "RESOLUTION SIGNAL: When the customer's issue is resolved, your response MUST end with the line:",
        "[RESOLVED]",
        "Do NOT add this signal unless the issue is genuinely resolved. "
        "If the customer thanks you or confirms they're satisfied, the issue is resolved.",
    ]))

    messages = [SystemMessage(content=system_content)]

    from core.orchestration.llm import get_llm_client
    llm = get_llm_client()

    # Append conversation history (avoid re-adding the system prompt's session context)
    for msg in state.messages:
        if isinstance(msg, (HumanMessage, AIMessage)):
            content = msg.content
            if state.locale != "en" and content:
                content = await llm.translate_content(content, "en")
            klass = HumanMessage if isinstance(msg, HumanMessage) else AIMessage
            messages.append(klass(content=content))
        elif isinstance(msg, ToolMessage):
            # Tool messages are metadata and should not be translated
            messages.append(msg)
        elif isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content", "")
            if state.locale != "en" and role in ("user", "assistant") and content:
                content = await llm.translate_content(content, "en")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

    return messages


async def node_generate_response(state: L1SupportState) -> dict[str, Any]:
    """
    Generate the AI response.

    If a specialist agent ran (node_route_to_agent), use its response directly —
    the specialist already executed its tool loop. Otherwise fall back to a direct
    primary-model call with tool binding.
    """
    # Specialist response path — agent handled everything, return it verbatim
    if state.specialist_used and state.specialist_response:
        original_response = state.specialist_response
        updates: dict[str, Any] = {
            "resolution_attempts": state.resolution_attempts + 1,
        }
        if "[resolved]" in original_response.lower():
            updates["current_resolution_id"] = (
                state.context_package.intent.issue_type if state.context_package and state.context_package.intent else "specialist"
            )

        # Translate specialist response back to user native language if needed
        if state.locale != "en" and original_response:
            llm = get_llm_client()
            translated_text = await llm.translate_content(original_response, state.locale)
        else:
            translated_text = original_response

        response = AIMessage(content=translated_text)
        updates["messages"] = [response]
        log.info(
            "response_from_specialist",
            session_id=state.session_id,
            agent=state.routed_to_agent,
            locale=state.locale,
        )
        return updates

    # Direct LLM path — specialist was not used or failed
    llm = get_llm_client()
    lc_client = llm.get_lc_client(fast=False)

    available_tool_names = state.context_package.tool_manifest if state.context_package else []
    available_tools = [t for name, t in TOOL_REGISTRY.items() if name in available_tool_names]

    # Merge in dynamic brand-API tools; their names in the manifest are the base
    # spec names (e.g. "get_order") — dynamic tools are prefixed at binding time
    # so we match by checking if a dynamic tool's base name appears in manifest.
    if state.dynamic_tools:
        for dyn_t in state.dynamic_tools:
            base_name = getattr(dyn_t, "name", "") or ""
            # Strip brand_id prefix if present: "boohoo_uk_get_order" → "get_order"
            if "_" in base_name:
                stripped = base_name.split("_", 1)[1]
            else:
                stripped = base_name
            if stripped in available_tool_names or base_name in available_tool_names:
                available_tools.append(dyn_t)

    if available_tools:
        model_with_tools = lc_client.bind_tools(available_tools)
    else:
        model_with_tools = lc_client

    messages = await _build_llm_messages(state)

    try:
        response = await model_with_tools.ainvoke(messages)
    except Exception as e:
        log.error("llm_invoke_failed", session_id=state.session_id, error=str(e))
        response = AIMessage(
            content="I'm experiencing a technical issue. Let me connect you with a member of our team.",
        )

    # Translate direct LLM response text back to user native language if there are no tool calls
    if not getattr(response, "tool_calls", None) and state.locale != "en" and response.content:
        translated_content = await llm.translate_content(response.content, state.locale)
        response.content = translated_content

    log.info(
        "response_generated",
        session_id=state.session_id,
        agent=state.routed_to_agent,
        has_tool_calls=bool(getattr(response, "tool_calls", None)),
        locale=state.locale,
    )

    updates = {"messages": [response]}

    if not getattr(response, "tool_calls", None):
        updates["resolution_attempts"] = state.resolution_attempts + 1
        if state.context_package and state.context_package.intent:
            updates["current_resolution_id"] = state.context_package.intent.issue_type

    return updates


# ─────────────────────────────────────────────────────────────────────────
# NODE: check_resolution  (routing decision node)
# ─────────────────────────────────────────────────────────────────────────

def check_resolution_routing(state: L1SupportState) -> Literal["tools", "escalate", "close", "continue", "end"]:
    """
    Conditional edge: decide what happens after generate_response.

    If a specialist agent ran this turn, return its response verbatim to the
    customer and end the graph turn — the next customer message starts a fresh cycle.

    Otherwise:
      "tools"    → there are tool calls to execute
      "escalate" → we should create a human ticket
      "close"    → session is resolved
      "continue" → wait for next customer message (happy path)
    """
    # Specialist path — one-shot, return to customer and wait for next message
    if state.specialist_used:
        return "end"

    last_ai_message = None
    for msg in reversed(state.messages):
        if isinstance(msg, AIMessage):
            last_ai_message = msg
            break

    # If model made tool calls → execute them
    if last_ai_message and getattr(last_ai_message, "tool_calls", None):
        return "tools"

    # Escalation conditions
    content = (last_ai_message.content or "").lower() if last_ai_message else ""
    explicit_escalation = any(phrase in content for phrase in [
        "connect you with", "human agent", "member of our team", "specialist",
        "ticket_id"
    ])

    if explicit_escalation:
        return "escalate"
    if state.resolution_attempts >= MAX_RESOLUTION_ATTEMPTS:
        return "escalate"
    if state.turn_count >= MAX_TURNS:
        return "escalate"
    if state.confidence < settings.low_confidence_threshold and state.turn_count > 1:
        return "escalate"

    # No tool calls available AND confidence too low to self-resolve on first response:
    # escalate immediately rather than looping with unhelpful replies.
    tool_manifest = (state.context_package.tool_manifest if state.context_package else [])
    if not tool_manifest and state.confidence < settings.low_confidence_threshold:
        return "escalate"

    # Agent explicitly marked as resolved via [RESOLVED] signal in response
    if "[resolved]" in content:
        return "close"

    return "continue"


# ─────────────────────────────────────────────────────────────────────────
# GRAPHITI EPISODE BUILDER
# ─────────────────────────────────────────────────────────────────────────

def _build_episode_text(state: L1SupportState, outcome: str, extra: str = "") -> str:
    """Compose a human-readable episode summary for Graphiti ingestion."""
    intent_str = state.intent.issue_type if state.intent else "unknown"
    category = state.intent.category.value if state.intent else "unknown"
    entities = state.entities.model_dump() if state.entities else {}

    parts = [
        f"Customer contacted support via {state.channel.value} about {intent_str} ({category}).",
        f"Session lasted {state.turn_count} turn(s).",
        f"Outcome: {outcome}.",
    ]
    if entities.get("order_id"):
        parts.append(f"Order referenced: {entities['order_id']}.")
    if entities.get("return_reason"):
        parts.append(f"Return reason: {entities['return_reason']}.")
    if entities.get("product_sku"):
        parts.append(f"Item SKU: {entities['product_sku']}.")
    if state.current_resolution_id and outcome == "resolved":
        parts.append(f"Resolution applied: {state.current_resolution_id}.")
    if extra:
        parts.append(extra)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# NODE: escalate_session
# ─────────────────────────────────────────────────────────────────────────

async def node_escalate(state: L1SupportState) -> dict[str, Any]:
    """
    Escalate to human agent. Creates Chatwoot ticket (Phase 1: mock).
    Writes escalation episode to Graphiti Tier 3 memory.
    Writes verbatim transcript to MemPalace Tier 2 memory.
    """
    import asyncio

    reason = state.escalation_reason or f"Auto-escalate after {state.resolution_attempts} attempt(s)"
    log.info(
        "session_escalated",
        session_id=state.session_id,
        brand_id=state.brand_id,
        reason=reason,
        turn_count=state.turn_count,
    )

    # Build transcript for MemPalace
    transcript_lines = []
    for msg in state.messages:
        if isinstance(msg, dict):
            role = "Customer" if msg.get("role") == "user" else "Agent"
            transcript_lines.append(f"{role}: {msg.get('content', '')}")
        elif hasattr(msg, "content"):
            role = "Agent" if isinstance(msg, AIMessage) else "Customer"
            transcript_lines.append(f"{role}: {msg.content}")

    loop = asyncio.get_event_loop()

    # Serialize LangChain messages to plain dicts before passing to feedback engine.
    # Plain dicts serialize reliably to JSONB; BaseMessage objects may not.
    msg_serialized = [
        m.model_dump() if hasattr(m, "model_dump") else {"content": getattr(m, "content", str(m))}
        for m in state.messages
    ]

    # Increment Prometheus escalation counter
    try:
        from core.metrics import ESCALATION_TOTAL
        ESCALATION_TOTAL.labels(brand_id=state.brand_id, reason=reason).inc()
    except Exception as e:
        log.warning("metrics_escalation_inc_failed", error=str(e))

    # Feedback loop via feedback engine
    try:
        from feedback.engine import on_session_escalate
        on_session_escalate(
            session_id=state.session_id,
            brand_id=state.brand_id,
            resolution_id=state.current_resolution_id,
            intent=state.intent.issue_type if state.intent else "unknown",
            reason=reason,
            conversation={"messages": msg_serialized},
        )
        log.info("feedback_engine_escalate_triggered", session_id=state.session_id)
    except Exception as e:
        log.warning("feedback_loop_failed", error=str(e))

    # Build transcript once — used by MemPalace
    transcript = "\n".join(transcript_lines)

    # Tier 2: verbatim session → MemPalace
    if state.customer_id:
        try:
            await loop.run_in_executor(
                None,
                lambda: mempalace.remember(
                    brand_id=state.brand_id,
                    customer_id=state.customer_id,
                    session_id=state.session_id,
                    content=transcript,
                    metadata={
                        "outcome": "escalated",
                        "channel": state.channel.value,
                        "turns": state.turn_count,
                    },
                ),
            )
            log.info("mempalace_stored", session_id=state.session_id, outcome="escalated")
        except Exception as e:
            log.warning("mempalace_write_failed", session_id=state.session_id, error=str(e))

    # Chatwoot: create a real ticket / update existing one (non-fatal if unavailable)
    chatwoot_conv_id: str | None = None
    try:
        from core.memory.session import get_session
        session_data = await get_session(state.session_id)
        if session_data:
            chatwoot_conv_id = session_data.get("chatwoot_conv_id")

        from brands.registry import get_brand_by_id
        from core.integrations.chatwoot import create_conversation
        from channels.chat.chatwoot_client import assign_team, add_label, post_message
        import os

        brand_entry = get_brand_by_id(state.brand_id)
        inbox_id = brand_entry.chatwoot_inbox_id if brand_entry else None
        
        # Build escalation summary message
        summary_lines = [
            "🚨 SESSION ESCALATED",
            f"Session ID: {state.session_id}",
            f"Intent: {state.intent.issue_type if state.intent else 'unknown'}",
            f"Confidence: {state.confidence:.0%}",
            f"Turns: {state.turn_count}",
            f"Reason: {reason}",
            "",
            "Transcript:",
            transcript,
        ]
        summary_msg = "\n".join(summary_lines)

        if chatwoot_conv_id:
            # Existing Chatwoot conversation — post private note summary
            post_message(chatwoot_conv_id, summary_msg, private=True)
            log.info("chatwoot_existing_conv_escalated", session_id=state.session_id, conversation_id=chatwoot_conv_id)
        elif inbox_id:
            # Create a new conversation (e.g., from voice channel escalation)
            chatwoot_conv_id = create_conversation(
                brand_id=state.brand_id,
                inbox_id=inbox_id,
                customer_name=None,
                customer_email=None,
                initial_message=summary_msg,
                session_id=state.session_id,
                metadata={
                    "intent": state.intent.issue_type if state.intent else "unknown",
                    "confidence": state.confidence,
                    "turn_count": state.turn_count,
                },
            )

        if chatwoot_conv_id:
            # 1. Assign to L2 Support team (configured via env or default to 1)
            l2_team_id_str = os.environ.get("CHATWOOT_L2_TEAM_ID") or os.environ.get("CHATWOOT_TEAM_ID", "1")
            if l2_team_id_str.isdigit():
                assign_team(chatwoot_conv_id, int(l2_team_id_str))
            
            # 2. Tag conversation with intent label
            if state.intent and state.intent.issue_type:
                add_label(chatwoot_conv_id, state.intent.issue_type)
            else:
                add_label(chatwoot_conv_id, "escalated")
    except Exception as e:
        log.warning("chatwoot_ticket_failed", session_id=state.session_id, error=str(e))

    # Tier 3: write escalation episode → Graphiti
    if state.customer_id:
        try:
            from core.memory.graphiti_client import graphiti_memory
            episode = _build_episode_text(state, outcome="escalated", extra=reason)
            await graphiti_memory.add_episode(
                customer_id=state.customer_id,
                brand_id=state.brand_id,
                content=episode,
                metadata={
                    "channel": state.channel.value,
                    "outcome": "escalated",
                    "reason": reason[:200],
                    "intent": state.intent.issue_type if state.intent else "unknown",
                },
                episode_name=f"support_escalated:{state.session_id}",
            )
            log.info("graphiti_episode_written", session_id=state.session_id, outcome="escalated")
        except Exception as e:
            log.warning("graphiti_write_failed", session_id=state.session_id, error=str(e))

    # Trigger n8n escalation webhook (in background executor)
    try:
        from core.utils.http import http_request
        from config import settings
        n8n_webhook_url = f"{settings.n8n_url.rstrip('/')}/webhook/escalation"
        
        customer_email = None
        customer_name = None
        if state.customer:
            if isinstance(state.customer, dict):
                customer_email = state.customer.get("email")
                customer_name = state.customer.get("name")
            else:
                customer_email = getattr(state.customer, "email", None)
                customer_name = getattr(state.customer, "name", None)

        n8n_payload = {
            "session_id": state.session_id,
            "brand_id": state.brand_id,
            "intent": state.intent.issue_type if state.intent else "unknown",
            "reason": reason,
            "transcript": transcript,
            "chatwoot_conv_id": chatwoot_conv_id,
            "customer_id": state.customer_id,
            "customer_email": customer_email,
            "customer_name": customer_name,
        }

        loop.run_in_executor(
            None,
            lambda: http_request(
                "POST",
                n8n_webhook_url,
                json_body=n8n_payload,
                timeout=5,
                max_retries=1
            )
        )
        log.info("n8n_escalation_webhook_triggered", session_id=state.session_id, url=n8n_webhook_url)
    except Exception as e:
        log.warning("n8n_escalation_webhook_failed", error=str(e))

    return {
        "is_resolved": False,
        "outcome": SessionOutcome.ESCALATED,
        "escalation_reason": reason,
        "chatwoot_conversation_id": chatwoot_conv_id,
    }


# ─────────────────────────────────────────────────────────────────────────
# NODE: close_session
# ─────────────────────────────────────────────────────────────────────────

async def node_close_session(state: L1SupportState) -> dict[str, Any]:
    """
    Session resolved. Trigger feedback loop — update Resolution.successRate in graph,
    write verbatim transcript to MemPalace Tier 2, and episode to Graphiti Tier 3.
    """
    import asyncio

    log.info(
        "session_closed",
        session_id=state.session_id,
        brand_id=state.brand_id,
        outcome="resolved",
        turns=state.turn_count,
    )

    # Build transcript for MemPalace
    transcript_lines = []
    for msg in state.messages:
        if isinstance(msg, dict):
            role = "Customer" if msg.get("role") == "user" else "Agent"
            transcript_lines.append(f"{role}: {msg.get('content', '')}")
        elif hasattr(msg, "content"):
            role = "Agent" if isinstance(msg, AIMessage) else "Customer"
            transcript_lines.append(f"{role}: {msg.content}")

    loop = asyncio.get_event_loop()

    # Serialize LangChain messages to plain dicts before passing to feedback engine.
    msg_serialized = [
        m.model_dump() if hasattr(m, "model_dump") else {"content": getattr(m, "content", str(m))}
        for m in state.messages
    ]

    # Feedback loop via feedback engine
    try:
        from feedback.engine import on_session_close
        on_session_close(
            session_id=state.session_id,
            brand_id=state.brand_id,
            resolution_id=state.current_resolution_id,
            satisfaction=state.satisfaction,
            intent=state.intent.issue_type if state.intent else "unknown",
            confidence=state.confidence,
            conversation={"messages": msg_serialized},
        )
        log.info("feedback_engine_triggered", session_id=state.session_id)
    except Exception as e:
        log.warning("feedback_loop_failed", error=str(e))

    transcript = "\n".join(transcript_lines)

    # Tier 2: verbatim session → MemPalace
    if state.customer_id:
        try:
            await loop.run_in_executor(
                None,
                lambda: mempalace.remember(
                    brand_id=state.brand_id,
                    customer_id=state.customer_id,
                    session_id=state.session_id,
                    content=transcript,
                    metadata={
                        "outcome": "resolved",
                        "channel": state.channel.value,
                        "turns": state.turn_count,
                    },
                ),
            )
            log.info("mempalace_stored", session_id=state.session_id, outcome="resolved")
        except Exception as e:
            log.warning("mempalace_write_failed", session_id=state.session_id, error=str(e))

    # Tier 3: write resolved episode → Graphiti
    if state.customer_id:
        try:
            from core.memory.graphiti_client import graphiti_memory
            intent_str = state.intent.issue_type if state.intent else "unknown"
            episode = _build_episode_text(state, outcome="resolved")
            await graphiti_memory.add_episode(
                customer_id=state.customer_id,
                brand_id=state.brand_id,
                content=episode,
                metadata={
                    "channel": state.channel.value,
                    "outcome": "resolved",
                    "intent": intent_str,
                    "turns": state.turn_count,
                },
                episode_name=f"support_resolved:{state.session_id}",
            )
            log.info("graphiti_episode_written", session_id=state.session_id, outcome="resolved")
        except Exception as e:
            log.warning("graphiti_write_failed", session_id=state.session_id, error=str(e))

    # Mark Chatwoot conversation as resolved if it is linked
    try:
        from core.memory.session import get_session
        session_data = await get_session(state.session_id)
        if session_data:
            chatwoot_conv_id = session_data.get("chatwoot_conv_id")
            if chatwoot_conv_id:
                from core.integrations.chatwoot import resolve_conversation
                resolve_conversation(chatwoot_conv_id)
    except Exception as e:
        log.warning("chatwoot_resolve_conversation_failed", error=str(e))

    return {
        "is_resolved": True,
        "outcome": SessionOutcome.RESOLVED,
    }


# ─────────────────────────────────────────────────────────────────────────
# GRAPH CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Assemble and compile the LangGraph state machine.
    Returns a compiled graph ready for .invoke() / .ainvoke().
    """
    builder = StateGraph(L1SupportState)

    # ── Add nodes ────────────────────────────────────────────────────────
    builder.add_node("assemble_context", node_assemble_context)
    builder.add_node("route_to_agent", node_route_to_agent)
    builder.add_node("generate_response", node_generate_response)
    builder.add_node("tools", StatefulToolNode(static_tools=ALL_TOOLS))
    builder.add_node("escalate", node_escalate)
    builder.add_node("close", node_close_session)

    # ── Add edges ─────────────────────────────────────────────────────────
    builder.add_edge(START, "assemble_context")
    builder.add_edge("assemble_context", "route_to_agent")
    builder.add_edge("route_to_agent", "generate_response")

    # Conditional routing after response generation
    builder.add_conditional_edges(
        "generate_response",
        check_resolution_routing,
        {
            "end":      END,       # specialist handled — end this turn; next message restarts
            "tools":    "tools",
            "escalate": "escalate",
            "close":    "close",
            "continue": END,        # no tool calls, wait for next customer message
        },
    )

    # After tool execution, re-generate response with tool results
    builder.add_edge("tools", "generate_response")

    # Terminal nodes
    builder.add_edge("escalate", END)
    builder.add_edge("close", END)

    return builder.compile()


# Module-level compiled graph singleton
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
