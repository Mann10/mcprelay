"""FastAPI application: control plane + per-gateway MCP endpoints.

Lifecycle note
--------------
``Server.streamable_http_app()`` builds a ``StreamableHTTPSessionManager`` but
does **not** start it. The manager owns the task group that serves requests and
enters the MCP server lifespan, so the host application must enter
``manager.run()`` exactly once per instance. Forgetting that yields
``RuntimeError: Task group is not initialized. Make sure to use run().`` on the
first request - which is what this module does in its lifespan.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.responses import JSONResponse as StarletteJSONResponse

from . import agentconfig
from .auth import ApiKeyAuthMiddleware
from .config import GatewayAppConfig, GatewayConfig, ToolFilterConfig
from .mcp_server import SEARCH_TOOL_NAME, GatewayMcpServer
from .naming import match_filter
from .registry import Registry, UpstreamManager, to_wire_tool
from .store import GatewayStore, ManagedGateway, StoreError

log = logging.getLogger("mcprelay.app")

_STARTED_AT = time.time()


def _base_url(cfg: GatewayAppConfig, gw: GatewayConfig) -> str:
    """Public base URL for a gateway's MCP endpoint.

    Derived from the configured bind host. That is correct for the normal case
    (127.0.0.1:port on the same machine) but wrong if you sit behind a tunnel or
    reverse proxy - pass an explicit base_url on install in that case.
    """
    return f"http://{cfg.server.host}:{cfg.server.port}{gw.path}"


def _server_label(gw: GatewayConfig) -> str:
    """A short, human-friendly name for the server key in client configs."""
    if gw.description:
        cleaned = gw.description.removeprefix("mcprelay gateway ").strip()
        if cleaned:
            return cleaned
    return f"mcprelay-{gw.id}"


def _rendered_payload(rendered) -> dict[str, Any]:
    return {
        "client_id": rendered.client_id,
        "display_name": rendered.display_name,
        "transport": rendered.transport,
        "supported": rendered.supported,
        "reason": rendered.reason,
        "server_key": rendered.server_key,
        "target_path": rendered.target_path,
        "notes": rendered.notes,
        "config": rendered.config,
        "config_json": rendered.to_json(),
    }


def _managed_to_config(managed: ManagedGateway, cfg: GatewayAppConfig) -> GatewayConfig:
    """Adapt a runtime-managed gateway into the immutable config shape.

    Everything downstream (Registry, GatewayMcpServer) works against
    GatewayConfig, so runtime and YAML gateways share one code path.
    """
    return GatewayConfig(
        id=managed.id,
        description=managed.description,
        api_key=managed.api_key,
        upstreams=tuple(managed.upstream_keys),
        tool_filter=ToolFilterConfig(
            include=tuple(managed.tool_filter),
            description_max_chars=managed.description_max_chars,
        ),
        tools_ttl_seconds=managed.tools_ttl_seconds,
        expose_output_schemas=managed.expose_output_schemas,
    )


class RewritePath:
    """ASGI wrapper that swaps a trailing path segment before delegating.

    Exposes ``/gateway/<id>/radar`` on the same mounted app that serves
    ``/gateway/<id>/mcp`` without building a second server.
    """

    def __init__(self, app: Any, from_suffix: str, to_suffix: str) -> None:
        self.app = app
        self.from_suffix = from_suffix
        self.to_suffix = to_suffix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope.get("type") == "http" and path.endswith(self.from_suffix):
            scope = dict(scope)
            scope["path"] = path[: -len(self.from_suffix)] + self.to_suffix
        await self.app(scope, receive, send)


def build_app(
    config: GatewayAppConfig,
    *,
    require_auth: bool = True,
    state_path: str | None = None,
) -> Any:
    upstream_manager = UpstreamManager(config.upstreams)
    registry = Registry(upstream_manager)
    store = GatewayStore(
        state_path or config.gateways_state_path,
        allowed_upstreams=set(config.upstreams),
    )

    # ---- build every gateway's MCP app before wiring the lifespan ----------
    mounts: list[tuple[str, Any, str]] = []          # (prefix, asgi app, gateway id)
    session_managers: list[Any] = []
    auth_rules: list[tuple[str, str, str]] = []

    for gid, gw in sorted(config.gateways.items()):
        mcp_server = GatewayMcpServer(gw, registry)
        mcp_app: Starlette = mcp_server.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,          # 2026-07-28: no session store required
            session_idle_timeout=None,
            max_sessions=None,
            debug=False,
        )
        mcp_server.session_manager = mcp_server.server.session_manager
        # Start the per-gateway session manager in the app lifespan below.
        session_managers.append(mcp_server.session_manager)
        mounts.append((f"/gateway/{gid}", RewritePath(mcp_app, "/radar", "/mcp"), gid))
        auth_rules.append((f"/gateway/{gid}", gw.api_key, gid))
        log.info("gateway %-10s -> %s  (+ %s)", gid, gw.path, gw.radar_path)

    runtime_stack: contextlib.AsyncExitStack | None = None
    # Set below, after the routes are registered. Rotate-key needs it to swap a
    # gateway's expected key in place; declared before so the closure can see it.
    auth_middleware: ApiKeyAuthMiddleware | None = None

    async def _build_runtime_gateway(gw) -> None:
        """Attach a runtime-created gateway: build, start, mount, register auth.

        Mounting after startup works because Starlette matches routes in order
        and a later ``mount()`` is simply appended to the router.

        Must be awaited: entering the session manager's ``run()`` context is
        what actually starts the task group serving this gateway's MCP traffic.
        """
        assert runtime_stack is not None, "runtime_stack is only set inside lifespan"
        mcp_server = GatewayMcpServer(gw, registry)
        mcp_app = mcp_server.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            session_idle_timeout=None,
            max_sessions=None,
            debug=False,
        )
        mcp_server.session_manager = mcp_server.server.session_manager
        prefix = f"/gateway/{gw.id}"

        @contextlib.asynccontextmanager
        async def _run_and_release():
            # Manager.run() creates the task group that serves MCP traffic and
            # enters the server lifespan. Skip it and every request fails with
            # "Task group is not initialized".
            async with mcp_server.session_manager.run():
                yield

        await runtime_stack.enter_async_context(_run_and_release())

        app.mount(prefix, RewritePath(mcp_app, "/radar", "/mcp"), name=f"gw-{gw.id}")
        auth_rules.append((prefix, gw.api_key, gw.id))
        log.info("runtime gateway %s mounted at %s", gw.id, prefix)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal runtime_stack
        async with contextlib.AsyncExitStack() as stack:
            runtime_stack = stack
            # Must happen before any request: this creates the task group that
            # serves MCP traffic and enters each server's lifespan.
            for sm in session_managers:
                await stack.enter_async_context(sm.run())

            store.load()
            for managed in store.list():
                try:
                    await _build_runtime_gateway(_managed_to_config(managed, config))
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "failed to restore runtime gateway %s: %s", managed.id, exc
                    )

            log.info(
                "connecting upstreams: %s",
                ", ".join(k for k, c in config.upstreams.items() if c.enabled)
                or "(none enabled)",
            )
            await upstream_manager.start()
            await upstream_manager.wait_ready(timeout=30.0)
            for key, state in upstream_manager.health().items():
                log.info("  %-20s %s (%s tools)", key, state["state"], state["tool_count"])
            disabled = [k for k, c in config.upstreams.items() if not c.enabled]
            if disabled:
                log.info("  (skipped, enabled: false: %s)", ", ".join(disabled))
            try:
                yield
            finally:
                log.info("shutting down upstreams")
                await upstream_manager.close()

    app = FastAPI(
        title="mcprelay",
        version="0.1.0",
        description=(
            "Local MCP gateway. Aggregates upstream MCP servers behind one "
            "endpoint with namespaced tools and a per-gateway allow-list."
        ),
        lifespan=lifespan,
    )
    app.state.manager = upstream_manager
    app.state.registry = registry
    app.state.config = config

    # ------------------------------------------------------------ control plane

    @app.get("/healthz", tags=["control"])
    async def healthz() -> dict[str, Any]:
        upstreams = upstream_manager.health()
        ready = sum(1 for s in upstreams.values() if s["state"] == "ready")
        return {
            "status": "ok" if ready else "degraded",
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "upstreams_ready": ready,
            "upstreams_total": len(upstreams),
            "upstreams": upstreams,
            "gateways": {
                gid: {
                    "endpoint": gw.path,
                    "radar_endpoint": gw.radar_path,
                    "tools": len(registry.resolve(gw).tools),
                    "source": "runtime" if store.get(gid) else "config",
                }
                for gid, gw in sorted(_all_gateways().items())
            },
        }

    @app.get("/api/v1/upstreams", tags=["control"])
    async def list_upstreams() -> dict[str, Any]:
        tools: dict[str, list[str]] = {}
        for key in config.upstreams:
            up = upstream_manager.upstream(key)
            tools[key] = sorted(up.tools) if up else []
        return {"upstreams": upstream_manager.health(), "tools": tools}

    @app.post("/api/v1/upstreams/{key}/reconnect", tags=["control"])
    async def reconnect_upstream(key: str) -> Any:
        if key not in config.upstreams:
            return JSONResponse({"error": "unknown upstream", "key": key}, status_code=404)
        if not config.upstreams[key].enabled:
            return JSONResponse(
                {"error": "upstream disabled in config", "key": key}, status_code=409
            )
        await upstream_manager.start_upstreams([key])
        up = upstream_manager.upstream(key)
        return {
            "key": key,
            "state": up.status.state if up else "unknown",
            "tools": up.status.tool_count if up else 0,
            "last_error": up.status.last_error if up else None,
        }

    @app.get("/api/v1/gateways", tags=["control"])
    async def list_gateways() -> dict[str, Any]:
        out = []
        for gid, gw in sorted(_all_gateways().items()):
            view = registry.resolve(gw)
            managed = store.get(gid)
            out.append(
                {
                    "id": gid,
                    "description": gw.description,
                    "endpoint": gw.path,
                    "radar_endpoint": gw.radar_path,
                    "upstreams": list(gw.upstreams),
                    "tool_count": len(view.tools),
                    "unavailable_upstreams": view.unavailable_upstreams,
                    "auth": "X-API-Key" if require_auth else "disabled",
                    "source": "runtime" if managed else "config",
                }
            )
        return {"gateways": out}

    @app.get("/api/v1/gateways/{gid}/tools", tags=["control"])
    async def gateway_tools(gid: str, full: bool = Query(False)) -> Any:
        gw = _lookup(gid)
        if gw is None:
            return JSONResponse({"error": "unknown gateway", "id": gid}, status_code=404)
        view = registry.resolve(gw)
        items = [
            to_wire_tool(t, gw.tool_filter.description_max_chars if full else 0)
            for t in sorted(view.tools.values(), key=lambda t: t.qualified_name)
        ]
        return {
            "gateway": gid,
            "count": len(items),
            "search_tool": SEARCH_TOOL_NAME,
            "denied": view.denied,
            "unavailable_upstreams": view.unavailable_upstreams,
            "tools": items,
        }

    # ------------------------------------------------ gateway lifecycle (CRUD)
    # Declared gateways are read-only from here; runtime gateways are created,
    # deleted and re-keyed. Both are reachable at the same paths.

    def _all_gateways() -> dict[str, GatewayConfig]:
        merged: dict[str, GatewayConfig] = dict(config.gateways)
        for managed in store.list():
            merged[managed.id] = _managed_to_config(managed, config)
        return merged

    def _lookup(gateway_id: str) -> GatewayConfig | None:
        return _all_gateways().get(gateway_id)

    @app.get("/api/v1/clients", tags=["clients"])
    async def list_clients() -> dict[str, Any]:
        return {"clients": agentconfig.client_catalogue()}

    @app.get("/api/v1/gateways/{gid}/tool-choices", tags=["clients"])
    async def gateway_tool_choices(gid: str) -> Any:
        """Tools the caller could select when building a gateway."""
        gw = _lookup(gid)
        if gw is None:
            return JSONResponse({"error": "unknown gateway", "id": gid}, status_code=404)
        view = registry.resolve(gw)
        by_upstream: dict[str, list[dict[str, Any]]] = {}
        for tool in sorted(view.tools.values(), key=lambda t: t.qualified_name):
            by_upstream.setdefault(tool.upstream_key, []).append(
                {
                    "name": tool.qualified_name,
                    "original_name": tool.original_name,
                    "description": tool.description,
                    "allowed": match_filter(tool.qualified_name, gw.tool_filter.include).included,
                }
            )
        return {"gateway": gid, "upstreams": by_upstream}

    @app.post("/api/v1/gateways", tags=["gateways"], status_code=201)
    async def create_gateway(body: dict[str, Any]) -> Any:
        upstreams = body.get("upstreams") or []
        if not isinstance(upstreams, list):
            return JSONResponse({"error": "'upstreams' must be a list"}, status_code=400)
        try:
            managed = store.create(
                name=str(body.get("name", "")),
                upstreams=[str(u) for u in upstreams],
                description=str(body.get("description", "")),
                tool_filter=[str(p) for p in (body.get("tool_filter") or ["*"])],
                description_max_chars=int(body.get("description_max_chars", 400)),
                tools_ttl_seconds=int(body.get("tools_ttl_seconds", 300)),
                radar_enabled=bool(body.get("radar_enabled", True)),
                expose_output_schemas=bool(
                    body.get("expose_output_schemas", False)
                ),
            )
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        gw = _managed_to_config(managed, config)
        try:
            await _build_runtime_gateway(gw)
        except Exception as exc:  # noqa: BLE001
            store.delete(managed.id)
            log.error("rolled back gateway %s after mount failure: %s", managed.id, exc)
            return JSONResponse(
                {"error": f"gateway created but could not be mounted: {exc}"},
                status_code=500,
            )

        view = registry.resolve(gw)
        return {
            **managed.to_public(),
            "endpoint": managed.path,
            "radar_endpoint": managed.radar_path,
            "tool_count": len(view.tools),
            # Returned once, at creation. Not retrievable through any listing.
            "api_key": managed.api_key,
            "api_key_note": "shown once; GET /api/v1/gateways never returns it",
        }

    @app.get("/api/v1/gateways/{gid}", tags=["gateways"])
    async def get_gateway(gid: str, include_key: bool = Query(False)) -> Any:
        managed = store.get(gid)
        gw = _lookup(gid)
        if gw is None:
            return JSONResponse({"error": "unknown gateway", "id": gid}, status_code=404)
        view = registry.resolve(gw)
        payload: dict[str, Any] = {
            "id": gw.id,
            "description": gw.description,
            "endpoint": gw.path,
            "radar_endpoint": gw.radar_path,
            "upstreams": list(gw.upstreams),
            "tool_count": len(view.tools),
            "tools": view.names,
            "unavailable_upstreams": view.unavailable_upstreams,
            "denied": view.denied,
            "tool_filter": list(gw.tool_filter.include),
            "source": "runtime" if managed else "config",
        }
        if include_key and managed is not None:
            payload["api_key"] = managed.api_key
        return payload

    @app.post("/api/v1/gateways/{gid}/rotate-key", tags=["gateways"])
    async def rotate_key(gid: str) -> Any:
        try:
            managed = store.rotate_key(gid)
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

        # The auth rule captured the old key at creation time, so it must be
        # replaced in place. Without this the gateway becomes unusable: the new
        # key would be rejected while the revoked one kept working.
        if auth_middleware is not None:
            prefix = f"/gateway/{managed.id}"
            auth_middleware.remove_rule(prefix)
            auth_middleware.add_rule(prefix, managed.api_key, managed.id)

        return {
            "gateway": managed.id,
            "api_key": managed.api_key,
            "note": "previous key is now invalid",
        }

    @app.patch("/api/v1/gateways/{gid}", tags=["gateways"])
    async def patch_gateway(gid: str, body: dict[str, Any]) -> Any:
        try:
            managed = store.update_tools(
                gid,
                tool_filter=[str(p) for p in body["tool_filter"]]
                if "tool_filter" in body
                else None,
                description_max_chars=body.get("description_max_chars"),
            )
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"gateway": managed.id, **managed.to_public()}

    @app.delete("/api/v1/gateways/{gid}", tags=["gateways"])
    async def delete_gateway(gid: str) -> Any:
        if gid in config.gateways:
            return JSONResponse(
                {"error": f"gateway {gid!r} is declared in the config file; "
                          f"remove it there rather than at runtime"},
                status_code=409,
            )
        try:
            store.delete(gid)
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return {"deleted": gid, "note": "process restart needed to unmount cleanly"}

    # ------------------------------------------------- client configuration

    @app.get("/api/v1/gateways/{gid}/client-config/{client_id}", tags=["clients"])
    async def client_config(gid: str, client_id: str) -> Any:
        """Preview the exact config for one agent. Never writes anything."""
        gw = _lookup(gid)
        if gw is None:
            return JSONResponse({"error": "unknown gateway", "id": gid}, status_code=404)
        try:
            rendered = agentconfig.render(
                client_id, _server_label(gw), _base_url(config, gw), gw.api_key
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return _rendered_payload(rendered)

    @app.post("/api/v1/gateways/{gid}/install/{client_id}", tags=["clients"])
    async def install_client(
        gid: str,
        client_id: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Write the config into an agent's config file, with a backup."""
        body = body or {}
        gw = _lookup(gid)
        if gw is None:
            return JSONResponse({"error": "unknown gateway", "id": gid}, status_code=404)

        bridge = bool(body.get("transport") == "stdio-bridge")
        radar = bool(body.get("radar", False))
        target_path = body.get("target_path")
        dry_run = bool(body.get("dry_run", False))

        # `base_url` must be the full MCP endpoint (ending in /mcp). Useful when
        # the gateway sits behind a tunnel or reverse proxy, where the bound
        # host is not what clients can reach.
        base = str(body.get("base_url") or _base_url(config, gw)).strip()
        if not base.rstrip("/").endswith("/mcp"):
            base = base.rstrip("/") + "/mcp"

        try:
            rendered = (
                agentconfig.render_stdio_bridge(
                    client_id, _server_label(gw), base, gw.api_key, radar=radar,
                )
                if bridge
                else agentconfig.render(
                    client_id, _server_label(gw), base, gw.api_key, radar=radar,
                )
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

        payload = _rendered_payload(rendered)
        if dry_run:
            return {**payload, "dry_run": True, "written": False}

        try:
            written = agentconfig.write_config(rendered, target_path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return {**payload, "written": True, **written}

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": "mcprelay",
            "control_plane": "/docs",
            "health": "/healthz",
            "create_gateway": "POST /api/v1/gateways",
            "install_client": "POST /api/v1/gateways/{id}/install/{client_id}",
            "clients": "/api/v1/clients",
            "gateways": {
                g: w.path for g, w in sorted(_all_gateways().items())
            },
        }

    # ----------------------------------------------------------- MCP endpoints

    for prefix, asgi_app, gid in mounts:
        app.mount(prefix, asgi_app, name=f"gw-{gid}")

    if require_auth and auth_rules:
        # Outermost wrapper so it runs before FastAPI's route dispatch. It holds
        # a live reference to `auth_rules`, so gateways registered later (at
        # runtime) are authenticated too.
        auth_middleware = ApiKeyAuthMiddleware(app, auth_rules, enabled=True)
        app.state.auth = auth_middleware
        return auth_middleware
    return app
