"""The MCP gateway surface.

Uses the low-level ``Server`` with its ``on_list_tools`` / ``on_call_tool``
constructor hooks, which is the right shape for a gateway: the tool set is
dynamic - it depends on config, on which upstreams are healthy, and on the
per-gateway allow-list - so there is nothing to bind to a decorator.

One instance exists per gateway (declared or runtime-created).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from .config import GatewayConfig
from .naming import match_filter
from .registry import Registry, build_tool, to_wire_tool
from .upstream import UpstreamTool

log = logging.getLogger("mcprelay.server")

SEARCH_TOOL_NAME = "gateway_search_tools"

SEARCH_TOOL_DOC: dict[str, Any] = {
    "name": SEARCH_TOOL_NAME,
    "title": "Search gateway tools",
    "description": (
        "[gateway] Search this gateway's enabled tools by keyword and return full "
        "descriptions and input schemas for the matches. Use this first when the "
        "tool list is large, then call the tool you need directly. Returns at most "
        "'limit' matches."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring or keyword matched against tool names and descriptions.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Maximum number of tools to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _text_result(text: str, structured: dict[str, Any] | None = None) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=structured,
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


class GatewayMcpServer:
    """One MCP server instance bound to one gateway definition."""

    def __init__(
        self,
        gateway: GatewayConfig,
        registry: Registry,
        session_manager: Any = None,
    ) -> None:
        self.gateway = gateway
        self._registry = registry

        # Set by the owner after streamable_http_app() runs. Holding the
        # reference lets a runtime-created gateway start and stop without the
        # app having to track it in a list up front.
        self.session_manager = session_manager

        self._server: Server[Any] = Server(
            name=f"mcprelay-{gateway.id}",
            version="0.1.0",
            instructions=self._instructions(),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    def update_config(self, gateway: GatewayConfig) -> None:
        """Swap in a fresh gateway definition without remounting.

        Runtime mutations (tool_filter edits via the control plane) mutate the
        *store*, not this object - without this swap the mounted server would
        keep serving the filter it was created with until the next restart.
        """
        self.gateway = gateway

    def _instructions(self) -> str:
        gw = self.gateway
        lines = [gw.description or f"mcprelay gateway {gw.id}"]
        lines.append("")
        lines.append(
            f"Tools are namespaced as '<upstream>.<tool>'. "
            f"Enabled upstreams: {', '.join(gw.upstreams)}."
        )
        view = self._registry.resolve(gw)
        if view.tools:
            lines.append(f"{len(view.tools)} tool(s) available.")
        if view.unavailable_upstreams:
            lines.append(
                f"Currently unavailable upstreams (do not call these): "
                f"{', '.join(view.unavailable_upstreams)}."
            )
        return "\n".join(lines)

    @property
    def server(self) -> Server[Any]:
        return self._server

    # ---------------------------------------------------------------- handlers

    async def _on_list_tools(
        self, ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        gw = self.gateway
        view = self._registry.resolve(gw)
        budget = gw.tool_filter.description_max_chars

        # Validate against the surface model for the version this client
        # actually negotiated. Falls back to the oldest (strictest) surface
        # when the version is unknown - see registry.build_tool.
        negotiated = getattr(ctx, "protocol_version", None)

        tools: list[types.Tool] = []
        dropped_schema = 0
        dropped_tool = 0

        for tool in sorted(view.tools.values(), key=lambda t: t.qualified_name):
            built, note = build_tool(
                to_wire_tool(tool, budget,
                             include_output_schema=gw.expose_output_schemas),
                protocol_version=negotiated,
            )
            if built is None:
                dropped_tool += 1
                log.warning(
                    "gateway %r: omitting unusable tool %r (%s)",
                    gw.id, tool.qualified_name, note,
                )
                continue
            if note:
                dropped_schema += 1
                log.info(
                    "gateway %r: %s on %r (%s)",
                    gw.id, note, tool.qualified_name, tool.upstream_key,
                )
            tools.append(built)

        if dropped_schema or dropped_tool:
            log.warning(
                "gateway %r: served %d tool(s); %d lost outputSchema, %d omitted "
                "entirely. Set expose_output_schemas: true to try passing them "
                "through again, but expect the serialization failure to return "
                "for clients on older protocol versions.",
                gw.id, len(tools), dropped_schema, dropped_tool,
            )

        # Radar affordance: a search meta-tool so a client can explore a large
        # catalogue without pulling every schema into context. Subject to the
        # same allow-list as real tools.
        if match_filter(SEARCH_TOOL_NAME, gw.tool_filter.include).included:
            tools.append(types.Tool.model_validate(SEARCH_TOOL_DOC))

        return types.ListToolsResult(
            tools=tools,
            # SEP-2549. `cache_scope` is a Literal['public','private'].
            #
            # 'public' is technically accurate today - the merged list is
            # derived purely from config, so every caller of a given gateway
            # sees the same thing. 'private' is the default anyway, because
            # 'public' becomes a cross-tenant leak the moment anyone adds
            # per-caller tool filtering.
            ttl_ms=gw.tools_ttl_seconds * 1000,
            cache_scope=gw.cache_scope,
        )

    async def _on_call_tool(
        self, ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        name = params.name
        arguments = params.arguments or {}

        if name == SEARCH_TOOL_NAME:
            return self._handle_search(arguments)

        try:
            _upstream, result = await self._registry.call(self.gateway, name, arguments)
        except PermissionError as exc:
            return _error_result(str(exc))
        except KeyError:
            return _error_result(self._unknown_tool_message(name))
        except RuntimeError as exc:
            return _error_result(f"upstream unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
            log.exception("gateway %r: unhandled error calling %r", self.gateway.id, name)
            return _error_result(f"{type(exc).__name__}: {exc}")

        # Pass the upstream's CallToolResult through unchanged so structured
        # content, is_error and content blocks all survive the hop.
        return result

    # ------------------------------------------------------------ meta-tools

    def _unknown_tool_message(self, name: str) -> str:
        view = self._registry.resolve(self.gateway)
        known = ", ".join(view.names[:12]) or "(none)"
        return (
            f"unknown tool {name!r} on gateway {self.gateway.id!r}. "
            f"Tools are namespaced '<upstream>.<tool>'. "
            f"Currently available: {known}"
        )

    def _handle_search(self, arguments: dict[str, Any]) -> types.CallToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return _error_result("'query' is required")

        limit = _clamp_int(arguments.get("limit"), default=10, low=1, high=50)
        view = self._registry.resolve(self.gateway)

        needle = query.lower()
        scored: list[tuple[int, UpstreamTool]] = []
        for tool in view.tools.values():
            score = 0
            if needle in tool.qualified_name.lower():
                score += 3
            if needle in (tool.description or "").lower():
                score += 1
            if score:
                scored.append((score, tool))

        scored.sort(key=lambda pair: (-pair[0], pair[1].qualified_name))
        chosen = [tool for _score, tool in scored[:limit]]

        if not chosen:
            return _text_result(
                f"No tool on gateway {self.gateway.id!r} matched {query!r}.",
                {"query": query, "matches": 0, "tools": []},
            )

        lines = [
            f"{len(chosen)} match(es) for {query!r} on gateway {self.gateway.id!r}:",
            "",
        ]
        for tool in chosen:
            lines.append(f"### {tool.qualified_name}")
            lines.append(f"upstream: {tool.upstream_key}")
            if tool.description:
                lines.append(tool.description)
            lines.append("inputSchema:")
            lines.append(json.dumps(tool.input_schema, indent=2, sort_keys=True))
            lines.append("")

        return _text_result(
            "\n".join(lines),
            {
                "query": query,
                "matches": len(chosen),
                "tools": [
                    {
                        "name": t.qualified_name,
                        "upstream": t.upstream_key,
                        "description": t.description,
                        "inputSchema": t.input_schema,
                    }
                    for t in chosen
                ],
            },
        )


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
