"""MCP server — exposes L1 Support AI tools via the Model Context Protocol.

Run standalone:
    python -m core.mcp.server

The server exposes:
  - Tools: all agent tools (order lookup, return init, etc.)
  - Resources: brands, sessions, knowledge graph snapshot
  - Prompts: structured prompts for common support scenarios
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    Tool,
    TextContent,
    Resource,
    ResourceTemplate,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ListPromptsResult,
    Prompt,
    PromptMessage,
)

from brands.registry import BRAND_REGISTRY
from config import settings
from core.memory.session import get_session, get_redis
from core.ontology.schema import get_graph_client, verify_connectivity
from core.tools.registry import TOOL_REGISTRY


# ── Server instance ───────────────────────────────────────────────────────────
server = Server("l1-support-mcp")


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════════════

# Expose each tool from the registry as an MCP tool
_MCP_TOOLS: list[Tool] = []
for name, fn in TOOL_REGISTRY.items():
    schema = fn.input_schema if isinstance(fn.input_schema, dict) else {}
    _MCP_TOOLS.append(
        Tool(
            name=name,
            description=fn.description or f"Tool: {name}",
            inputSchema={
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            },
        )
    )


@server.list_tools()
async def list_tools() -> ListToolsResult:
    """List all available L1 agent tools."""
    return ListToolsResult(tools=_MCP_TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Execute an L1 agent tool."""
    if name not in TOOL_REGISTRY:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Unknown tool: {name}")],
            isError=True,
        )

    fn = TOOL_REGISTRY[name]
    try:
        # Detect required parameters from function signature
        import inspect
        sig = inspect.signature(fn.invoke)
        params = {}
        for pname, param in sig.parameters.items():
            if pname in ("name", "kwargs"):
                continue
            val = arguments.get(pname)
            if val is not None:
                params[pname] = val

        result = fn.invoke(params)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, default=str))],
        )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Tool error: {e}")],
            isError=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# RESOURCES
# ══════════════════════════════════════════════════════════════════════════════

@server.list_resources()
async def list_resources() -> ListResourcesResult:
    """Static resources: system manifest and brand registry."""
    manifest = {
        "system": "L1 Support AI",
        "version": "0.1.0",
        "brands": [b.brand_id for b in BRAND_REGISTRY],
        "brand_count": len(BRAND_REGISTRY),
        "tools": list(TOOL_REGISTRY.keys()),
        "falkordb_host": settings.falkordb_host,
        "postgres_host": settings.postgres_host,
        "api_base": f"http://localhost:{settings.app_port}",
    }
    return ListResourcesResult(resources=[
        Resource(
            uri="l1://system/manifest",
            name="System Manifest",
            description="L1 Support AI system overview — version, brands, tools",
            mimeType="application/json",
            text=json.dumps(manifest, indent=2),
        ),
        Resource(
            uri="l1://graph/schema",
            name="Knowledge Graph Schema",
            description="Node labels, relationship types, property keys",
            mimeType="application/json",
            text=_fetch_graph_schema(),
        ),
    ])


@server.list_resource_templates()
async def list_resource_templates() -> ListResourceTemplatesResult:
    """Dynamic resource templates for sessions and brands."""
    return ListResourceTemplatesResult(resourceTemplates=[
        ResourceTemplate(
            uriTemplate="l1://brand/{brand_id}",
            name="Brand Detail",
            description="Full brand entry for a given brand_id",
            mimeType="application/json",
        ),
        ResourceTemplate(
            uriTemplate="l1://session/{session_id}",
            name="Session State",
            description="Full session state including message history",
            mimeType="application/json",
        ),
    ])


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Read a dynamic resource."""
    if uri == "l1://graph/schema":
        return _fetch_graph_schema()

    if uri.startswith("l1://brand/"):
        brand_id = uri.split("/")[-1]
        entry = next((b for b in BRAND_REGISTRY if b.brand_id == brand_id), None)
        if not entry:
            raise ValueError(f"Brand not found: {brand_id}")
        return json.dumps({
            "brand_id": entry.brand_id,
            "name": entry.name,
            "domains": entry.domains,
            "locales": entry.locales,
            "default_region": entry.default_region,
            "chatwoot_inbox_id": entry.chatwoot_inbox_id,
        }, indent=2)

    if uri.startswith("l1://session/"):
        session_id = uri.split("/")[-1]
        data = await get_session(session_id)
        if not data:
            raise ValueError(f"Session not found: {session_id}")
        return json.dumps(data, default=str)

    raise ValueError(f"Unknown resource: {uri}")


# ── Helper: fetch graph schema ────────────────────────────────────────────────
def _fetch_graph_schema() -> str:
    try:
        _, graph = get_graph_client()
        labels = graph.query("CALL db.labels()").result_set
        rels = graph.query("CALL db.relationshipTypes()").result_set
        props = graph.query("CALL db.propertyKeys()").result_set
        schema = {
            "node_labels": sorted([r[0] for r in labels]),
            "relationship_types": sorted([r[0] for r in rels]),
            "property_keys": sorted([r[0] for r in props]),
        }
        return json.dumps(schema, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

@server.list_prompts()
async def list_prompts() -> ListPromptsResult:
    """Structured prompts for common support scenarios."""
    return ListPromptsResult(prompts=[
        Prompt(
            name="new_order_issue",
            description="Start a support session for a new order status inquiry",
            arguments=[
                {"name": "brand_id", "required": True, "description": "Brand ID (e.g. boohoo_uk)"},
                {"name": "order_id", "required": True, "description": "Order ID to look up"},
            ],
        ),
        Prompt(
            name="return_request",
            description="Start a support session for a return initiation",
            arguments=[
                {"name": "brand_id", "required": True, "description": "Brand ID"},
                {"name": "order_id", "required": True, "description": "Order ID"},
                {"name": "items", "required": True, "description": "Items to return (comma-separated SKUs)"},
            ],
        ),
        Prompt(
            name="escalation",
            description="Escalate a session to a human agent with context",
            arguments=[
                {"name": "session_id", "required": True, "description": "Session ID"},
                {"name": "reason", "required": True, "description": "Reason for escalation"},
                {"name": "priority", "required": False, "description": "P1/P2/P3"},
            ],
        ),
    ])


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, Any] | None = None) -> PromptMessage:
    """Render a structured prompt."""
    args = arguments or {}
    if name == "new_order_issue":
        return PromptMessage(
            role="user",
            content=TextContent(
                type="text",
                text=f"I need help with my order {args['order_id']} from {args['brand_id']}. Can you look it up for me?",
            ),
        )
    if name == "return_request":
        return PromptMessage(
            role="user",
            content=TextContent(
                type="text",
                text=f"I would like to return item(s) {args['items']} from order {args['order_id']} ({args['brand_id']}). Could you please initiate the return for me?",
            ),
        )
    if name == "escalation":
        return PromptMessage(
            role="user",
            content=TextContent(
                type="text",
                text=f"I need to speak with a human agent. Session {args['session_id']} requires escalation: {args['reason']}",
            ),
        )
    raise ValueError(f"Unknown prompt: {name}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    import asyncio
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
