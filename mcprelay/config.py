"""Configuration model for the gateway.

Everything is file-driven: there is no database and no admin UI. Secrets are
referenced by environment variable rather than written literally.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

log = logging.getLogger("mcprelay.config")

TransportKind = Literal["stdio", "streamable-http", "sse"]


class ConfigError(Exception):
    """Raised when the configuration file is unusable."""


_ENV_PATTERN = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${env:VAR}`` / ``${VAR:default}`` references.

    A missing variable with no default is an error rather than a silent empty
    string, because a silently-empty API key produces a confusing 401 much
    later.
    """
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(
            f"environment variable {name!r} referenced in config is not set "
            f"(add it to your shell/.env, or write ${{{name}:default}})"
        )

    return _ENV_PATTERN.sub(repl, value)


def _expand_recursive(node: Any) -> Any:
    if isinstance(node, str):
        return expand_env(node)
    if isinstance(node, dict):
        return {k: _expand_recursive(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_recursive(v) for v in node]
    return node


@dataclass(frozen=True)
class UpstreamConfig:
    """One upstream MCP server the gateway fronts."""

    key: str
    """Stable short identifier. Becomes the tool-name namespace prefix."""

    transport: TransportKind
    enabled: bool = True

    # stdio
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    # streamable-http / sse
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    request_timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, key: str, raw: dict[str, Any]) -> "UpstreamConfig":
        transport = raw.get("transport", "stdio")
        if transport not in ("stdio", "streamable-http", "sse"):
            raise ConfigError(
                f"upstream {key!r}: transport must be one of "
                f"stdio | streamable-http | sse, got {transport!r}"
            )

        if transport == "stdio":
            if not raw.get("command"):
                raise ConfigError(f"upstream {key!r}: stdio transport requires 'command'")
        else:
            if not raw.get("url"):
                raise ConfigError(f"upstream {key!r}: {transport} transport requires 'url'")

        # Drop headers that ended up carrying no credential.
        #
        # Two cases matter, both easy to hit when one config is shared between
        # machines:
        #   Authorization: "${env:TOKEN:}"        -> ""            (whole value empty)
        #   Authorization: "Bearer ${env:TOKEN:}" -> "Bearer "     (scheme, no secret)
        # The second form is the dangerous one: httpx rejects it outright, and
        # the resulting error names the transport rather than the missing env
        # var. Drop both and warn, so the upstream simply runs unauthenticated
        # and reports degraded instead of failing with a misleading error.
        headers: dict[str, str] = {}
        for raw_name, raw_value in (raw.get("headers", {}) or {}).items():
            name, value = str(raw_name), str(raw_value).strip()
            if not value:
                continue
            if re.fullmatch(r"(?i)(bearer|token|basic|apikey)\s*", value):
                log.warning(
                    "upstream %r: header %r has no credential "
                    "(put the whole value in one env var, e.g. %s: \"${env:%s:}\"); "
                    "header dropped",
                    key, name, name, name.replace("-", "_").upper(),
                )
                continue
            headers[name] = value

        return cls(
            key=key,
            transport=transport,
            enabled=bool(raw.get("enabled", True)),
            command=raw.get("command"),
            args=list(raw.get("args", []) or []),
            cwd=raw.get("cwd"),
            env=dict(raw.get("env", {}) or {}),
            url=raw.get("url"),
            headers=headers,
            request_timeout_seconds=float(raw.get("request_timeout_seconds", 60.0)),
            connect_timeout_seconds=float(raw.get("connect_timeout_seconds", 30.0)),
        )


@dataclass(frozen=True)
class ToolFilterConfig:
    """Per-gateway allow-list, evaluated first-match-wins.

    Patterns are globs; a leading ``!`` negates. A tool matched by no pattern is
    **denied** - there is no implicit allow-all, so attaching a new upstream
    never silently exposes every tool it carries.

    Examples::

        ["*"]                              allow everything
        ["!memory.delete_*", "*"]          everything except memory deletions
        ["github.*", "notion.search"]      only those
    """

    include: tuple[str, ...] = ("*",)
    description_max_chars: int = 400
    """Truncate proxied descriptions to bound tools/list size (context rot)."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolFilterConfig":
        raw = raw or {}
        include = raw.get("include")
        if include is None:
            include = ["*"]
        if not isinstance(include, list) or not include:
            raise ConfigError("gateway tool_filter.include must be a non-empty list of globs")
        return cls(
            include=tuple(str(p) for p in include),
            description_max_chars=int(raw.get("description_max_chars", 400)),
        )


def _validate_cache_scope(gateway_id: str, value: Any) -> Literal["public", "private"]:
    if value not in ("public", "private"):
        raise ConfigError(
            f"gateway {gateway_id!r}: cache_scope must be 'public' or 'private', "
            f"got {value!r} (mcp.types.ListToolsResult.cache_scope is a "
            f"Literal['public','private'])"
        )
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class GatewayConfig:
    """A gateway: a named, key-protected MCP endpoint with its own tool set."""

    id: str
    description: str = ""
    api_key: str = ""
    upstreams: tuple[str, ...] = ()
    tool_filter: ToolFilterConfig = field(default_factory=ToolFilterConfig)
    tools_ttl_seconds: int = 300
    """Advertised ``ttlMs`` on tools/list so clients can cache it. 0 disables."""

    cache_scope: Literal["public", "private"] = "private"
    """SEP-2549 cache scope for tools/list. See mcp_server._on_list_tools."""

    expose_output_schemas: bool = False
    """Proxy upstream ``outputSchema`` (SEP-2106) to clients.

    Off by default. ``outputSchema`` only exists in ``2026-07-28``; a gateway
    routinely fronts upstreams newer than the connected client, and the SDK's
    downgrade path rewrites a proxied schema into a shape its own serializer
    then rejects - failing the entire ``tools/list`` for every tool, not just
    the offending one. Turn it on only if all your clients negotiate
    ``2026-07-28``.
    """

    @property
    def path(self) -> str:
        return f"/gateway/{self.id}/mcp"

    @property
    def radar_path(self) -> str:
        return f"/gateway/{self.id}/radar"

    @classmethod
    def from_dict(cls, gateway_id: str, raw: dict[str, Any]) -> "GatewayConfig":
        api_key = raw.get("api_key", "")
        if not api_key:
            raise ConfigError(
                f"gateway {gateway_id!r}: api_key is required "
                f"(set it to ${{env:MCPRELAY_KEY}} rather than a literal)"
            )
        if raw.get("radar_enabled") is False:
            log.info("gateway %r: RADAR endpoint disabled", gateway_id)
        return cls(
            id=gateway_id,
            description=str(raw.get("description", "")),
            api_key=api_key,
            upstreams=tuple(raw.get("upstreams", []) or []),
            tool_filter=ToolFilterConfig.from_dict(raw.get("tool_filter")),
            tools_ttl_seconds=int(raw.get("tools_ttl_seconds", 300)),
            cache_scope=_validate_cache_scope(
                gateway_id, raw.get("cache_scope", "private")
            ),
            expose_output_schemas=bool(raw.get("expose_output_schemas", False)),
        )


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "info"
    enable_control_plane: bool = True


DEFAULT_STATE_PATH = "var/gateways.json"


@dataclass(frozen=True)
class GatewayAppConfig:
    server: ServerConfig
    upstreams: dict[str, UpstreamConfig]
    gateways: dict[str, GatewayConfig]
    gateways_state_path: str = DEFAULT_STATE_PATH

    @classmethod
    def load(cls, path: str) -> "GatewayAppConfig":
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file is not valid YAML: {path}: {exc}") from exc

        raw = _expand_recursive(raw)

        server = ServerConfig(**(raw.get("server", {}) or {}))

        upstreams: dict[str, UpstreamConfig] = {}
        for key, spec in (raw.get("upstreams", {}) or {}).items():
            if not isinstance(spec, dict):
                raise ConfigError(f"upstream {key!r} must be a mapping")
            upstreams[key] = UpstreamConfig.from_dict(key, spec)

        gateways: dict[str, GatewayConfig] = {}
        for gid, spec in (raw.get("gateways", {}) or {}).items():
            if not isinstance(spec, dict):
                raise ConfigError(f"gateway {gid!r} must be a mapping")
            gateways[gid] = GatewayConfig.from_dict(gid, spec)

        if not gateways:
            raise ConfigError("at least one gateway must be defined under 'gateways:'")

        # Referential integrity: a gateway may only reference declared upstreams.
        for gw in gateways.values():
            unknown = [u for u in gw.upstreams if u not in upstreams]
            if unknown:
                raise ConfigError(
                    f"gateway {gw.id!r} references undeclared upstream(s): {', '.join(unknown)}"
                )
            if not gw.upstreams:
                raise ConfigError(f"gateway {gw.id!r} must list at least one upstream")

        # Referential integrity: no two gateways may share a key.
        seen: dict[str, str] = {}
        for gw in gateways.values():
            if gw.api_key in seen:
                raise ConfigError(
                    f"gateways {seen[gw.api_key]!r} and {gw.id!r} share the same api_key; "
                    f"each gateway needs its own key"
                )
            seen[gw.api_key] = gw.id

        return cls(
            server=server,
            upstreams=upstreams,
            gateways=gateways,
            gateways_state_path=str(raw.get("gateways_state_path", DEFAULT_STATE_PATH)),
        )
