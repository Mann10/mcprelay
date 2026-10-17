"""Probe GitHub's hosted MCP endpoint directly, bypassing mcprelay.

Separates two questions that were tangled together:
  A. does GitHub answer tools/list at all?
  B. do its outputSchemas survive a round trip through mcp_types?

The gateway traceback was (B). Whether GitHub is also (A) was unclear, because
doctor reported it degraded with 0 tools.

Run:  python tools/probe_github_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def token() -> str:
    value = os.environ.get("GITHUB_AUTHORIZATION")
    if not value:
        import subprocess

        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('GITHUB_AUTHORIZATION','User')"],
            capture_output=True, text=True, timeout=30,
        )
        value = (out.stdout or "").strip()
    if not value:
        print("GITHUB_AUTHORIZATION is not set. Nothing to probe.")
        sys.exit(2)
    return value


async def probe() -> int:
    import httpx2
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client

    auth = token()
    url = "https://api.githubcopilot.com/mcp/"

    async with httpx2.AsyncClient(
        headers={"Authorization": auth}, timeout=60.0, follow_redirects=True
    ) as hc:
        async with streamable_http_client(url, http_client=hc) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                print("=" * 68)
                print("A. connectivity")
                print("=" * 68)
                init = await session.initialize()
                print(f"  OK  server   = {getattr(init.server_info, 'name', '?')}")
                print(f"  OK  protocol = {init.protocol_version}")

                print()
                print("=" * 68)
                print("B. tools/list")
                print("=" * 68)
                try:
                    result = await session.list_tools()
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED: {type(exc).__name__}: {exc}")
                    return 1

                tools = result.tools
                print(f"  OK  {len(tools)} tools")

                print()
                print("=" * 68)
                print("C. do the outputSchemas validate as mcp Tool?")
                print("=" * 68)
                bad = []
                for t in tools:
                    doc = t.model_dump(by_alias=True, exclude_none=True)
                    try:
                        types.Tool.model_validate(doc)
                    except Exception as exc:  # noqa: BLE001
                        first = str(exc).splitlines()
                        reason = next(
                            (ln.strip() for ln in first if "outputSchema" in ln),
                            first[0].strip() if first else "?",
                        )
                        bad.append((t.name, reason))

                print(f"  {len(tools) - len(bad)}/{len(tools)} round-trip cleanly")
                if bad:
                    print(f"  {len(bad)} FAIL - and one of them kills the whole")
                    print("  tools/list response for the client:")
                    for name, reason in bad[:6]:
                        print(f"    {name}")
                        print(f"        {reason[:110]}")
                    if len(bad) > 6:
                        print(f"    ... and {len(bad) - 6} more")

                print()
                print("=" * 68)
                print("D. sample of the offending schema shapes")
                print("=" * 68)
                seen = set()
                for name, _reason in bad[:40]:
                    tool = next((t for t in tools if t.name == name), None)
                    if tool is None:
                        continue
                    schema = tool.model_dump(by_alias=True, exclude_none=True).get(
                        "outputSchema", {}
                    )
                    shape = json.dumps(schema)[:150]
                    if shape not in seen:
                        seen.add(shape)
                        print(f"  {name}")
                        print(f"    {shape}")
                    if len(seen) >= 4:
                        break
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(probe()))
