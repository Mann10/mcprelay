"""Isolate tools/list construction failures without HTTP in the way."""
import asyncio
import json
import traceback

from mcp import types

import mcprelay.config as cfg
from mcprelay.mcp_server import GatewayMcpServer, _search_tool_doc
from mcprelay.registry import Registry, UpstreamManager

print("=== enum / field constraints ===")
for name in ("CacheScope", "CacheHint", "Role"):
    obj = getattr(types, name, None)
    print(f"{name}: {obj}")
    if obj is not None:
        ann = getattr(obj, "__annotations__", {})
        print(f"   annotations: {ann}")

print("\nListToolsResult field constraints:")
for fname, f in types.ListToolsResult.model_fields.items():
    print(f"  {fname}: {f.annotation}  default={f.default!r}")

print("\nTool field constraints:")
for fname, f in types.Tool.model_fields.items():
    print(f"  {fname}: {f.annotation}")

print("\n=== try a bare ListToolsResult ===")
for scope in (None, "organization", "user", "public"):
    for ttl in (None, 300000):
        try:
            r = types.ListToolsResult(tools=[], ttl_ms=ttl, cache_scope=scope)
            print(f"  OK   ttl={ttl} scope={scope!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL ttl={ttl} scope={scope!r}: {type(exc).__name__}: {exc}")

print("\n=== try the search tool doc through Tool.model_validate ===")
try:
    t = types.Tool.model_validate(_search_tool_doc())
    print("  OK search tool validates")
except Exception:  # noqa: BLE001
    traceback.print_exc()


async def main() -> None:
    conf = cfg.GatewayAppConfig.load("config.local.yaml")
    manager = UpstreamManager(conf.upstreams)
    await manager.start()
    await manager.wait_ready(45)
    registry = Registry(manager)

    gw = conf.gateways["dev"]
    server = GatewayMcpServer(gw, registry)

    print("\n=== call _on_list_tools directly ===")
    try:
        result = await server._on_list_tools(None, None)
        print(f"  OK  {len(result.tools)} tools, ttl_ms={result.ttl_ms}, "
              f"cache_scope={result.cache_scope}")
        for t in result.tools[:3]:
            print(f"     {t.name}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    print("\n=== call _on_call_tool directly (routing) ===")
    try:
        res = await server._on_call_tool(
            None,
            types.CallToolRequestParams(
                name="sequential-thinking.sequentialthinking",
                arguments={
                    "thought": "routing probe",
                    "nextThoughtNeeded": False,
                    "totalThoughts": 1,
                },
            ),
        )
        print(f"  OK is_error={res.is_error} -> {str(res.content[0].text)[:70]!r}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    print("\n=== call _on_call_tool directly (search meta-tool) ===")
    try:
        res = await server._on_call_tool(
            None,
            types.CallToolRequestParams(
                name="gateway_search_tools", arguments={"query": "file", "limit": 3}
            ),
        )
        print(f"  OK is_error={res.is_error}")
        print("  " + str(res.content[0].text).splitlines()[0][:80])
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    await manager.close()


asyncio.run(main())
