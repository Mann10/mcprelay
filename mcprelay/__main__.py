"""Command-line entry point.

    python -m mcprelay serve    --config config.yaml
    python -m mcprelay tools    --config config.yaml [--gateway dev]
    python -m mcprelay call     --config config.yaml --tool fs.read_file --arg path=...
    python -m mcprelay doctor   --config config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from .config import ConfigError, GatewayAppConfig
from .registry import Registry, UpstreamManager
from .upstream import Upstream

DEFAULT_CONFIG = os.environ.get("MCPRELAY_CONFIG", "config.yaml")


def _configure_logging(level: str | None) -> None:
    """Set up logging. A missing level means INFO."""
    resolved = (level or "info").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------- serve


def _port_owner(host: str, port: int) -> str | None:
    """Return a description of whatever already holds host:port, if anything.

    Checked before startup so a clash is reported in milliseconds rather than
    after every upstream has connected. uvicorn's own error for this is both
    late and unhelpful on Windows.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # SO_REUSEADDR would mask a real clash on some platforms; leave it off.
        try:
            probe.bind((host, port))
        except OSError:
            return "another process is already listening"
        return None


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import build_app

    config = GatewayAppConfig.load(args.config)
    _configure_logging(args.log_level or config.server.log_level)

    host = args.host or config.server.host
    port = args.port or config.server.port

    enabled = [k for k, c in config.upstreams.items() if c.enabled]
    disabled = [k for k, c in config.upstreams.items() if not c.enabled]

    conflict = _port_owner(host, port)
    if conflict:
        print(f"\n  error: cannot bind {host}:{port} - {conflict}.", file=sys.stderr)
        print(f"         Another mcprelay is probably already running.", file=sys.stderr)
        print(f"         Check:  http://{host}:{port}/healthz", file=sys.stderr)
        print(
            "         Stop it, or pick another port:\n"
            f"           python -m mcprelay serve -c {args.config} --port {port + 1}",
            file=sys.stderr,
        )
        return 2

    app = build_app(config, require_auth=not args.no_auth)

    print()
    print("  mcprelay - local MCP gateway")
    print(f"  {'-' * 58}")
    print(f"  config      {os.path.abspath(args.config)}")
    print(f"  upstreams   {', '.join(enabled) if enabled else '(none enabled)'}")
    if disabled:
        print(f"  disabled    {', '.join(disabled)}   (enabled: false in config)")
    for gid, gw in sorted(config.gateways.items()):
        print(f"  gateway     {gid:<12} {gw.path}   ({len(gw.upstreams)} upstreams)")
    print(f"  control     http://{host}:{port}/docs")
    print(f"  auth        {'DISABLED' if args.no_auth else 'X-API-Key'}")
    print()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=(args.log_level or config.server.log_level).lower(),
    )
    return 0


# ---------------------------------------------------------------- offline ops


async def _build_manager(config: GatewayAppConfig) -> UpstreamManager:
    manager = UpstreamManager(config.upstreams)
    await manager.start()
    await manager.wait_ready(timeout=45.0)
    return manager


def cmd_tools(args: argparse.Namespace) -> int:
    config = GatewayAppConfig.load(args.config)
    _configure_logging(args.log_level)

    async def run() -> int:
        manager = await _build_manager(config)
        try:
            registry = Registry(manager)
            gateways = (
                {args.gateway: config.gateways[args.gateway]}
                if args.gateway
                else config.gateways
            )
            for gid, gw in sorted(gateways.items()):
                view = registry.resolve(gw)
                print(f"\n=== gateway {gid} ({len(view.tools)} tools) ===")
                if view.unavailable_upstreams:
                    print(f"    ! unavailable upstreams: "
                          f"{', '.join(view.unavailable_upstreams)}")
                for tool in sorted(view.tools.values(), key=lambda t: t.qualified_name):
                    desc = (tool.description or "").splitlines()[0][:76]
                    print(f"  {tool.qualified_name:<44} {desc}")
            return 0
        finally:
            await manager.close()

    return asyncio.run(run())


def cmd_call(args: argparse.Namespace) -> int:
    config = GatewayAppConfig.load(args.config)
    _configure_logging(args.log_level)

    arguments: dict[str, Any] = {}
    for item in args.arg or []:
        if "=" not in item:
            print(f"error: --arg must be key=value, got {item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        arguments[key] = _coerce(value)

    async def run() -> int:
        manager = await _build_manager(config)
        try:
            registry = Registry(manager)
            gateway = config.gateways[args.gateway]
            try:
                _upstream, result = await registry.call(gateway, args.tool, arguments)
            except PermissionError as exc:
                # An allow-list rejection is a normal outcome, not a crash.
                print(f"denied: {exc}", file=sys.stderr)
                return 3
            except KeyError:
                view = registry.resolve(gateway)
                known = ", ".join(view.names[:12]) or "(none)"
                print(
                    f"unknown tool {args.tool!r} on gateway {args.gateway!r}. "
                    f"Tools are namespaced '<upstream>.<tool>'. Available: {known}",
                    file=sys.stderr,
                )
                return 4
            for block in getattr(result, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    print(text)
                elif getattr(block, "type", "") == "image":
                    print(f"[image: {getattr(block, 'mimeType', '?')}]")
            if getattr(result, "structured_content", None):
                print("\n--- structured ---")
                print(json.dumps(result.structured_content, indent=2, default=str))
            return 1 if getattr(result, "is_error", False) else 0
        finally:
            await manager.close()

    return asyncio.run(run())


def _server_base(args: argparse.Namespace) -> str:
    """Resolve the control-plane base URL: --base wins, else the config's bind."""
    if args.base:
        return args.base.rstrip("/")
    try:
        config = GatewayAppConfig.load(args.config)
    except ConfigError:
        return "http://127.0.0.1:8787"
    return f"http://{config.server.host}:{config.server.port}"


def cmd_create(args: argparse.Namespace) -> int:
    """Create a gateway against a running server and print its key."""
    import httpx

    base = _server_base(args)
    try:
        with httpx.Client(timeout=30.0) as http:
            response = http.post(
                f"{base}/api/v1/gateways",
                json={
                    "name": args.name,
                    "upstreams": args.upstream or [],
                    "description": args.description or f"mcprelay gateway {args.name}",
                    "tool_filter": args.tool_filter or ["*"],
                },
            )
    except httpx.HTTPError as exc:
        print(f"error: cannot reach the gateway server at {base}: {exc}", file=sys.stderr)
        return 2

    if response.status_code >= 400:
        print(f"error ({response.status_code}): {response.text}", file=sys.stderr)
        return 1

    gw = response.json()
    print(f"\n  gateway created: {gw['id']}")
    print(f"  name      {gw['name']}")
    print(f"  endpoint  {gw['endpoint']}")
    print(f"  radar     {gw['radar_endpoint']}")
    print(f"  upstreams {', '.join(gw['upstreams'])}")
    print(f"  tools     {gw['tool_count']}")
    print(f"  key       {gw['api_key']}")
    print("\n  This key is shown once. Store it now.")
    print("  Give it to a client with:")
    print(f"    python -m mcprelay install -g {gw['id']} -c cursor")
    print()
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Print (and optionally write) the config for one agent."""
    import httpx

    base = _server_base(args)
    payload = {"transport": args.transport, "radar": args.radar,
               "target_path": args.target_path, "dry_run": not args.write}

    try:
        with httpx.Client(timeout=30.0) as http:
            response = http.post(
                f"{base}/api/v1/gateways/{args.gateway}/install/{args.client}",
                json=payload,
            )
    except httpx.HTTPError as exc:
        print(f"error: cannot reach the gateway server at {base}: {exc}", file=sys.stderr)
        return 2

    if response.status_code >= 400:
        print(f"error ({response.status_code}): {response.text}", file=sys.stderr)
        return 1

    data = response.json()
    if not data.get("supported", True):
        print(f"note: {data.get('reason')}")

    print(f"\n  {data['display_name']}  ({data['transport']})")
    print(f"  server key : {data['server_key']}")
    print(f"  target     : {data['target_path'] or '(none - copy manually)'}")
    print()
    print(data["config_json"])
    if data.get("written"):
        print(f"\n  WROTE {data['path']}")
        if data.get("backup_path"):
            print(f"  backup {data['backup_path']}")
        if data.get("preserved_top_level_keys"):
            print(f"  preserved: {', '.join(data['preserved_top_level_keys'])}")
        print("  Restart the client to pick it up.")
    else:
        print("  Preview only. Re-run with --write to apply.")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    import httpx

    base = _server_base(args)
    try:
        with httpx.Client(timeout=20.0) as http:
            response = http.get(f"{base}/api/v1/gateways")
    except httpx.HTTPError as exc:
        print(f"error: cannot reach the gateway server at {base}: {exc}", file=sys.stderr)
        return 2

    if response.status_code >= 400:
        print(f"error ({response.status_code}): {response.text}", file=sys.stderr)
        return 1

    for gw in response.json().get("gateways", []):
        print(f"  {gw['id']:<14} {gw['tool_count']:>3} tools  {gw['endpoint']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = GatewayAppConfig.load(args.config)
    _configure_logging("warning")

    async def run() -> int:
        manager = await _build_manager(config)
        try:
            health = manager.health()
            payload = {
                "server": {"host": config.server.host, "port": config.server.port},
                "upstreams": health,
                "gateways": {},
            }
            registry = Registry(manager)
            for gid, gw in sorted(config.gateways.items()):
                view = registry.resolve(gw)
                payload["gateways"][gid] = {
                    "endpoint": gw.path,
                    "upstreams": list(gw.upstreams),
                    "tools": len(view.tools),
                    "unavailable": view.unavailable_upstreams,
                }
            print(json.dumps(payload, indent=2))
            healthy = all(s["state"] == "ready" for s in health.values())
            return 0 if healthy else 1
        finally:
            await manager.close()

    return asyncio.run(run())


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so `-c` works both before and
    # after the subcommand: `mcprelay -c x doctor` and `mcprelay doctor -c x`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=DEFAULT_CONFIG)
    common.add_argument("--log-level", default=None)

    parser = argparse.ArgumentParser(
        prog="mcprelay",
        description="Local MCP gateway.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the gateway HTTP server", parents=[common])
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-auth", action="store_true",
                         help="disable X-API-Key (localhost testing only)")
    p_serve.set_defaults(func=cmd_serve)

    p_tools = sub.add_parser("tools", help="list tools a gateway would expose", parents=[common])
    p_tools.add_argument("--gateway", "-g", default=None)
    p_tools.set_defaults(func=cmd_tools)

    p_call = sub.add_parser("call", help="invoke one namespaced tool", parents=[common])
    p_call.add_argument("--gateway", "-g", required=True)
    p_call.add_argument("--tool", "-t", required=True)
    p_call.add_argument("--arg", "-a", action="append",
                        help="key=value (repeatable); value is JSON-coerced")
    p_call.set_defaults(func=cmd_call)

    p_doctor = sub.add_parser("doctor", help="check upstream connectivity", parents=[common])
    p_doctor.set_defaults(func=cmd_doctor)

    p_create = sub.add_parser("create", help="create a gateway on a running server",
                              parents=[common])
    p_create.add_argument("--name", "-n", required=True)
    p_create.add_argument("--upstream", "-u", action="append",
                          help="upstream key (repeatable)")
    p_create.add_argument("--description", "-d", default=None)
    p_create.add_argument("--tool-filter", action="append",
                          help="allow-list glob (repeatable, default: *)")
    p_create.add_argument("--base", default=None, help="gateway server base URL")
    p_create.set_defaults(func=cmd_create)

    p_install = sub.add_parser("install", help="print/write config for an agent",
                               parents=[common])
    p_install.add_argument("--gateway", "-g", required=True)
    p_install.add_argument("--client", "--for", dest="client", required=True,
                           help="opencode | cursor | claude_code | claude_desktop | "
                                "windsurf | vscode | generic")
    p_install.add_argument("--transport", choices=["remote", "stdio-bridge"],
                           default="remote")
    p_install.add_argument("--radar", action="store_true", help="use the /radar path")
    p_install.add_argument("--target-path", default=None)
    p_install.add_argument("--write", action="store_true",
                           help="actually write the file (backs up first)")
    p_install.add_argument("--base", default=None)
    p_install.set_defaults(func=cmd_install)

    p_list = sub.add_parser("ls", help="list gateways on a running server", parents=[common])
    p_list.add_argument("--base", default=None)
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
