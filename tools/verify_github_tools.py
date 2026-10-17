"""Replay the exact failure from the user's terminal.

They connected a real client (OpenCode), which negotiated `2025-11-25`, and
`tools/list` died with 31 ValidationErrors on `outputSchema.type`. Reproduce
that path and prove it is fixed.

Run:  python tools/verify_github_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GATEWAY_ID = sys.argv[1] if len(sys.argv) > 1 else "77732c7475e7"
BASE = "http://127.0.0.1:8787"


def gateway_key(gateway_id: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "var", "gateways.json")
    data = json.load(open(path, encoding="utf-8"))
    for gw in data.get("gateways", []):
        if gw["id"] == gateway_id:
            return gw["api_key"]
    raise SystemExit(f"gateway {gateway_id} not found in {path}")


async def main() -> int:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    key = gateway_key(GATEWAY_ID)
    url = f"{BASE}/gateway/{GATEWAY_ID}/mcp"

    failures = 0
    async with httpx2.AsyncClient(
        headers={"X-API-Key": key}, timeout=90.0, follow_redirects=True
    ) as hc:
        async with streamable_http_client(url, http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                init = await session.initialize()
                print(f"  negotiated protocol : {init.protocol_version}")
                print(f"  server             : {getattr(init.server_info, 'name', '?')}")

                print("\n  calling tools/list (this is what threw 31 errors before)...")
                try:
                    result = await session.list_tools()
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED: {type(exc).__name__}: {exc}")
                    return 1

                tools = result.tools
                print(f"  OK - {len(tools)} tools returned")

                by_upstream: dict[str, int] = {}
                for t in tools:
                    key_ = t.name.split(".")[0] if "." in t.name else "other"
                    by_upstream[key_] = by_upstream.get(key_, 0) + 1
                for upstream, n in sorted(by_upstream.items()):
                    print(f"    {upstream:<12} {n:>3} tools")

                if by_upstream.get("github"):
                    print(f"\n  PASS - github tools survived tools/list")
                else:
                    print(f"\n  FAIL - no github tools present")
                    failures += 1

                leaking = [t.name for t in tools if getattr(t, "output_schema", None)]
                print(f"\n  tools carrying outputSchema: {len(leaking)} "
                      f"(0 expected - see registry.to_wire_tool)")

                print("\n  calling one github tool to prove the round trip works...")
                target = next(
                    (t.name for t in tools if t.name.startswith("github.")), None
                )
                if not target:
                    print("  SKIP - no github tool to call")
                    return 1 if failures else 0

                try:
                    res = await session.call_tool(target, {}, read_timeout_seconds=60)
                    text = str(res.content[0].text)[:100] if res.content else ""
                    print(f"  {target} -> is_error={res.is_error}")
                    print(f"    {text}")
                    if res.is_error:
                        # Argument errors are fine; transport/schema errors are not.
                        low = text.lower()
                        if "schema" in low or "validation" in low:
                            print("  FAIL - schema/validation error in the tool call")
                            failures += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED: {type(exc).__name__}: {exc}")
                    failures += 1

    print(f"\n{'PASS' if not failures else 'FAIL'} - {1 - failures}/1 check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
