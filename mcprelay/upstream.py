"""A single upstream MCP server, held open for the process lifetime.

Why a long-lived connection rather than connect-per-request:

* The `2026-07-28` spec removed protocol sessions, so a *modern* upstream
  would technically tolerate connect-per-request. But:
* stdio upstreams are subprocesses - one spawn per tool call is unusable;
* most upstreams in the wild are still legacy-era and need the handshake;
* a held connection amortises discovery and is what the SDK is built around.

So we pool. The cost is that a crashed upstream needs explicit recovery, which
:meth:`Upstream.reconnect` handles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .config import UpstreamConfig
from .naming import qualify, validate_upstream_key

log = logging.getLogger("mcprelay.upstream")


def describe_error(exc: BaseException) -> str:
    """Flatten an exception into a useful one-liner.

    anyio wraps transport failures in an ``ExceptionGroup``, whose message is
    the useless string "unhandled errors in a TaskGroup (1 sub-exception)".
    Surface the leaf cause instead, or debugging upstream failures is guesswork.
    """
    leaves: list[str] = []
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        nested = getattr(current, "exceptions", None)
        if nested:
            stack.extend(nested)
            continue
        text = str(current).strip() or current.__class__.__name__
        leaves.append(f"{current.__class__.__name__}: {text}")

    if not leaves:
        return exc.__class__.__name__
    if len(leaves) == 1:
        return leaves[0]
    return " | ".join(dict.fromkeys(leaves))


@dataclass
class UpstreamStatus:
    state: str = "pending"          # pending | ready | degraded | failed | disabled
    protocol_version: str | None = None
    server_name: str | None = None
    tool_count: int = 0
    last_error: str | None = None
    connect_attempts: int = 0


@dataclass
class UpstreamTool:
    """One tool as advertised by an upstream, plus how to call it."""

    qualified_name: str
    upstream_key: str
    original_name: str
    schema_doc: dict[str, Any]
    description: str | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.schema_doc.get("inputSchema") or {"type": "object", "properties": {}}

    @property
    def output_schema(self) -> dict[str, Any] | None:
        return self.schema_doc.get("outputSchema")


class Upstream:
    """Owns one client connection and the tool inventory it advertises."""

    def __init__(self, config: UpstreamConfig) -> None:
        validate_upstream_key(config.key)
        self.config = config
        self.status = UpstreamStatus(
            state="disabled" if not config.enabled else "pending"
        )
        self.tools: dict[str, UpstreamTool] = {}
        self._client: Client | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ build

    def _stdio_params(self) -> StdioServerParameters:
        # Pass through the parent environment so `npx`, `node`, `uvx` and any
        # provider CLI keep working, then layer our configured env on top.
        import os as _os

        env = dict(_os.environ)
        env.update(self.config.env)
        return StdioServerParameters(
            command=self.config.command or "",
            args=list(self.config.args),
            env=env,
            cwd=self.config.cwd,
        )

    def _http_target(self) -> Any:
        """Build the transport object, attaching headers when configured."""
        url = self.config.url or ""

        if self.config.transport == "sse":
            return sse_client(
                url,
                headers=dict(self.config.headers) or None,
                timeout=self.config.connect_timeout_seconds,
                sse_read_timeout=self.config.request_timeout_seconds,
            )

        if not self.config.headers:
            # Fast path: let the SDK build its own HTTP client.
            return streamable_http_client(url)

        import httpx2

        http_client = httpx2.AsyncClient(
            headers=dict(self.config.headers),
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
        )
        return streamable_http_client(url, http_client=http_client)

    def _build_client(self) -> Client:
        target: Any
        if self.config.transport == "stdio":
            target = self._stdio_params()
        else:
            target = self._http_target()
        # mode="auto" -> the SDK detects modern vs legacy upstream itself.
        return Client(
            target,
            read_timeout_seconds=self.config.request_timeout_seconds,
            client_info=None,
        )

    # -------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await self._close_locked()
            self.status.state = "pending"
            self.status.connect_attempts += 1
            try:
                stack = contextlib.AsyncExitStack()
                client = await stack.enter_async_context(self._build_client())
                self._stack = stack
                self._client = client

                result = await client.list_tools()
                self._ingest(result.tools)

                self.status.state = "ready"
                self.status.last_error = None
                self.status.tool_count = len(self.tools)
                self.status.protocol_version = _safe_attr(client.session, "protocol_version")
                info = _safe_attr(client.session, "server_info")
                self.status.server_name = getattr(info, "name", None)
                log.info(
                    "upstream %r ready (%s tools, protocol %s)",
                    self.config.key,
                    len(self.tools),
                    self.status.protocol_version,
                )
            except Exception as exc:  # noqa: BLE001 - upstream failures must not kill the gateway
                self.status.state = "degraded"
                self.status.last_error = describe_error(exc)
                log.warning("upstream %r failed to start: %s", self.config.key, describe_error(exc))
                await self._close_locked()

    async def _close_locked(self) -> None:
        stack, self._stack = self._stack, None
        self._client = None
        self.tools = {}
        self.status.tool_count = 0
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()
            self.status.state = "disabled"

    async def reconnect(self) -> None:
        log.info("reconnecting upstream %r", self.config.key)
        await self.connect()

    async def refresh_tools(self) -> int:
        """Re-list tools from a live upstream. Returns the new tool count."""
        client = self._client
        if client is None:
            await self.connect()
            return len(self.tools)
        try:
            result = await client.list_tools()
            self._ingest(result.tools)
            self.status.state = "ready"
            self.status.tool_count = len(self.tools)
            self.status.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.status.state = "degraded"
            self.status.last_error = describe_error(exc)
            raise
        return len(self.tools)

    def _ingest(self, tools: list[Any]) -> None:
        ingested: dict[str, UpstreamTool] = {}
        for tool in tools:
            original = tool.name
            doc = tool.model_dump(by_alias=True, exclude_none=True)
            ingested[qualify(self.config.key, original)] = UpstreamTool(
                qualified_name=qualify(self.config.key, original),
                upstream_key=self.config.key,
                original_name=original,
                schema_doc=doc,
                description=tool.description,
            )
        self.tools = ingested

    # ----------------------------------------------------------------- calls

    async def call_tool(
        self, original_name: str, arguments: dict[str, Any] | None
    ) -> Any:
        """Invoke one tool and return the raw ``CallToolResult``.

        A transport-level failure marks the upstream degraded and re-raises so
        the caller can decide whether to retry against a reconnected session.
        """
        client = self._client
        if client is None:
            raise RuntimeError(f"upstream {self.config.key!r} is not connected")

        try:
            return await client.call_tool(
                original_name,
                arguments or {},
                read_timeout_seconds=self.config.request_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self.status.state = "degraded"
            self.status.last_error = describe_error(exc)
            raise


def _safe_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None
