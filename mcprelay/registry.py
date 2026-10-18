"""Aggregated tool registry and per-gateway dispatch.

Holds every enabled upstream and produces the merged, filtered tool view for one
gateway. Enforces the allow-list on *both* ``tools/list`` and ``tools/call`` -
filtering the listing alone would leave the call path open.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig, UpstreamConfig
from .naming import MatchOutcome, match_filter, split, truncate
from .upstream import Upstream, UpstreamTool, describe_error

log = logging.getLogger("mcprelay.registry")


@dataclass
class GatewayTools:
    """The resolved tool set for one gateway."""

    tools: dict[str, UpstreamTool]
    denied: list[str]
    unavailable_upstreams: list[str]

    @property
    def names(self) -> list[str]:
        return sorted(self.tools)


class UpstreamManager:
    """Owns every upstream and the connect/reconnect lifecycle."""

    def __init__(self, configs: dict[str, UpstreamConfig]) -> None:
        self._configs = configs
        self._upstreams: dict[str, Upstream] = {}
        self._ready = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.start_upstreams(k for k, c in self._configs.items() if c.enabled)

    async def start_upstreams(self, keys: Any) -> None:
        """Connect the named upstreams concurrently.

        One failing upstream must never prevent the others from serving, so
        each connection is isolated and failures are recorded, not raised.
        """
        wanted = [k for k in keys if k in self._configs]
        for key in wanted:
            cfg = self._configs[key]
            if key not in self._upstreams:
                self._upstreams[key] = Upstream(cfg)

        results = await asyncio.gather(
            *(self._upstreams[k].connect() for k in wanted),
            return_exceptions=True,
        )
        for key, outcome in zip(wanted, results):
            if isinstance(outcome, BaseException):
                up = self._upstreams[key]
                up.status.state = "failed"
                up.status.last_error = describe_error(outcome)
                log.error("upstream %r connect raised: %s", key, describe_error(outcome))
        self._ready.set()

    async def close(self) -> None:
        for up in list(self._upstreams.values()):
            await up.close()

    async def register(self, config: UpstreamConfig, *, connect: bool = True) -> Upstream:
        """Attach one new upstream at runtime and (optionally) connect it."""
        self._configs = {**self._configs, config.key: config}
        upstream = self._upstreams.get(config.key)
        if upstream is None or upstream.config != config:
            upstream = Upstream(config)
            self._upstreams[config.key] = upstream
        if connect and config.enabled:
            await upstream.connect()
        self._ready.set()
        return upstream

    async def remove(self, key: str) -> bool:
        """Detach one runtime upstream: disconnect and forget it."""
        self._configs.pop(key, None)
        upstream = self._upstreams.pop(key, None)
        if upstream is None:
            return False
        await upstream.close()
        return True

    async def wait_ready(self, timeout: float = 30.0) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("upstreams not fully ready after %.1fs; serving what is up", timeout)

    def upstream(self, key: str) -> Upstream | None:
        return self._upstreams.get(key)

    def health(self) -> dict[str, Any]:
        return {
            key: {
                "state": up.status.state,
                "tool_count": up.status.tool_count,
                "protocol_version": up.status.protocol_version,
                "server_name": up.status.server_name,
                "connect_attempts": up.status.connect_attempts,
                "last_error": up.status.last_error,
            }
            for key, up in sorted(self._upstreams.items())
        }


class Registry:
    """Builds per-gateway tool views from the live upstream inventory."""

    def __init__(self, manager: UpstreamManager) -> None:
        self._manager = manager

    def resolve(self, gateway: GatewayConfig) -> GatewayTools:
        allowed: dict[str, UpstreamTool] = {}
        denied: list[str] = []
        unavailable: list[str] = []

        for key in gateway.upstreams:
            upstream = self._manager.upstream(key)
            if upstream is None:
                unavailable.append(key)
                continue
            if upstream.status.state not in ("ready", "pending"):
                # Degraded/failed/disabled upstreams still expose their cached
                # tool list if we have one, so clients keep a stable schema.
                if not upstream.tools:
                    unavailable.append(key)
                    continue
                unavailable.append(key)

            for qualified, tool in upstream.tools.items():
                outcome = match_filter(qualified, gateway.tool_filter.include)
                if outcome.included:
                    allowed[qualified] = tool
                else:
                    denied.append(qualified)

        return GatewayTools(
            tools=allowed,
            denied=sorted(denied),
            unavailable_upstreams=sorted(set(unavailable)),
        )

    # -------------------------------------------------------------- dispatch

    async def call(
        self,
        gateway: GatewayConfig,
        qualified_name: str,
        arguments: dict[str, Any] | None,
    ) -> tuple[Upstream, Any]:
        """Route one qualified tool call to its upstream.

        Raises ``PermissionError`` when the name is outside the gateway's
        allow-list - the enforcement point that a listing-only filter would
        miss.
        """
        view = self.resolve(gateway)

        if qualified_name not in view.tools:
            if qualified_name in view.denied:
                raise PermissionError(
                    f"tool {qualified_name!r} exists upstream but is not enabled on "
                    f"gateway {gateway.id!r}"
                )
            # Distinguish "unknown entirely" from "known but disabled" so the
            # client gets an actionable error.
            upstream_key, _ = split(qualified_name) if "." in qualified_name else ("", "")
            if upstream_key and self._manager.upstream(upstream_key) is not None:
                raise PermissionError(
                    f"unknown tool {qualified_name!r} on gateway {gateway.id!r} "
                    f"(upstream {upstream_key!r} is attached but the tool was not found)"
                )
            raise KeyError(qualified_name)

        tool = view.tools[qualified_name]
        upstream = self._manager.upstream(tool.upstream_key)
        if upstream is None or upstream.status.state == "disabled":
            raise RuntimeError(f"upstream {tool.upstream_key!r} is not available")

        result = await upstream.call_tool(tool.original_name, arguments)
        return upstream, result


def to_wire_tool(
    tool: UpstreamTool,
    description_max_chars: int,
    *,
    include_output_schema: bool = False,
) -> dict[str, Any]:
    """Render an UpstreamTool as an MCP ``Tool`` dict for ``tools/list``.

    ``outputSchema`` is opt-in and **off by default**. It is a `2026-07-28`
    field (SEP-2106), and a gateway routinely fronts upstreams that are newer
    than the client connected to it. When a client negotiates `2025-11-25` and
    the SDK downgrades the result, a proxied `outputSchema` gets rewritten into
    a shape the serializer rejects - and because the failure surfaces while
    serializing the *whole* ``ListToolsResult``, one odd schema takes down
    every tool on the gateway, not just its own.

    Output schemas are optional metadata, so dropping them costs nothing and
    removing the failure mode entirely is worth far more than the field.
    """
    doc: dict[str, Any] = {
        "name": tool.qualified_name,
        "inputSchema": tool.input_schema,
    }

    description = truncate(tool.description, description_max_chars)
    if description:
        doc["description"] = description

    # Annotate provenance so a model can tell where a tool came from.
    prefix = f"[{tool.upstream_key}] "
    if description and not description.startswith(prefix):
        doc["description"] = prefix + description
    elif not description:
        doc["description"] = (
            f"[{tool.upstream_key}] Tool {tool.original_name!r} exposed by upstream "
            f"{tool.upstream_key!r}."
        )

    passthrough = ("title", "annotations", "icons", "execution")
    if include_output_schema:
        passthrough = ("outputSchema",) + passthrough
    for field in passthrough:
        value = tool.schema_doc.get(field)
        if value:
            doc[field] = value

    return doc


def _strict_surfaces() -> dict[str, Any]:
    """Per-version ``tools/list`` Tool models from the SDK, for validation.

    ``mcp.types.Tool`` types ``output_schema`` as ``dict[str, Any] | None`` and
    so accepts anything. The *versioned* surface models the SDK actually
    serializes against are far stricter: through ``2025-11-25`` the
    ``OutputSchema`` model requires ``type`` to be the literal ``'object'``.
    Validating against the loose model therefore cannot catch a schema that
    will fail at serialize time - it has to be the surface model.

    Returns ``{protocol_version: ToolModel}``.
    """
    import typing

    try:
        from mcp_types.methods import SERVER_RESULTS
    except Exception:  # noqa: BLE001
        return {}

    out: dict[str, Any] = {}
    for (method, version), result in SERVER_RESULTS.items():
        if method != "tools/list":
            continue
        annotation = result.model_fields.get("tools")
        if annotation is None:
            continue
        for arg in typing.get_args(annotation.annotation):
            for candidate in typing.get_args(arg) or (arg,):
                if hasattr(candidate, "model_fields") and (
                    "output_schema" in candidate.model_fields
                ):
                    out[version] = candidate
    return out


_STRICT_SURFACES: dict[str, Any] | None = None


def strict_surfaces() -> dict[str, Any]:
    """Cached :func:`_strict_surfaces`. Tests clear this to force a reload."""
    global _STRICT_SURFACES
    if _STRICT_SURFACES is None:
        _STRICT_SURFACES = _strict_surfaces()
    return _STRICT_SURFACES


def build_tool(
    doc: dict[str, Any],
    *,
    protocol_version: str | None = None,
) -> tuple[Any | None, str | None]:
    """Validate one tool dict, degrading instead of failing.

    Returns ``(tool, None)`` on success, ``(tool_without_output_schema, reason)``
    if only that field was at fault, or ``(None, reason)`` if the tool is
    unusable. Never raises: a gateway must not lose its whole tool list because
    one upstream advertised something odd.

    ``protocol_version`` selects which surface to validate against. When omitted,
    the strictest known surface is used, because a tool has to survive the
    *oldest* client that may connect - not just the one that happens to be
    talking right now.
    """
    from mcp import types

    surfaces = strict_surfaces()
    strict = surfaces.get(protocol_version or "")
    if strict is None and surfaces:
        # No exact match (unknown or omitted version): validate against the
        # oldest surface, which is the one with the narrowest OutputSchema.
        strict = surfaces[min(surfaces)]

    validator = strict if strict is not None else types.Tool

    def accept(candidate: dict[str, Any]) -> tuple[Any | None, str]:
        # Validate against the version's surface model - that is the only
        # model strict enough to predict whether serialization will succeed.
        # Then hand back a plain `mcp.types.Tool`, because that is what
        # `ListToolsResult` is built from; a versioned surface model is not
        # accepted there.
        try:
            validator.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        try:
            return types.Tool.model_validate(candidate), ""
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    built, text = accept(doc)
    if built is not None:
        return built, None

    if "output_schema" not in text and "outputSchema" not in text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return None, lines[0] if lines else "unknown"

    stripped = {k: v for k, v in doc.items() if k != "outputSchema"}
    built, detail = accept(stripped)
    if built is None:
        first = [ln.strip() for ln in text.splitlines() if ln.strip()]
        head = first[0] if first else "unknown"
        return None, f"{head}; still invalid without outputSchema: {detail.splitlines()[0] if detail else ''}"
    return built, "dropped outputSchema"
