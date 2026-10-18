"""End-to-end probe of live tool-filter enforcement:

    1. start a throwaway gateway (fast python stdio upstream, no npx)
    2. create a runtime gateway via the control plane
    3. connect a REAL MCP client and call a tool  -> must succeed
    4. deny the tool via PATCH (what the TUI toggle-off does)
       -> same session: call must now be DENIED
    5. allow it again via PATCH (what the TUI toggle-on does)
       -> SAME session: call must SUCCEED again   <-- the user's scenario

Any failure prints exactly which step broke.
"""

from __future__ import annotations

import asyncio
import glob
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, ".")

from mcprelay.tui.api import RelayClient  # control-plane client

BASE = "http://127.0.0.1:8792"
CONFIG = Path("var/probe2-config.yaml")


def write_config() -> None:
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text(
        """
server: {host: 127.0.0.1, port: 8792, log_level: warning}
gateways_state_path: var/probe2-gateways.json
upstreams_state_path: var/probe2-upstreams.json
upstreams:
  echo: {transport: stdio, command: python, args: ["scripts/dummy_upstream.py"]}
gateways:
  dev: {api_key: probe-dev-key, description: probe, upstreams: [echo]}
""",
        encoding="utf-8",
    )


async def main() -> int:
    write_config()
    for p in ("var/probe2-gateways.json", "var/probe2-upstreams.json"):
        Path(p).unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "mcprelay", "serve", "-c", str(CONFIG)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=open("var/probe2.out.log", "w"),
        stderr=open("var/probe2.err.log", "w"),
    )
    try:
        up = False
        for i in range(60):
            try:
                httpx.get(f"{BASE}/healthz", timeout=2)
                up = True
                break
            except Exception:
                time.sleep(0.5)
        if not up:
            print("SERVER DID NOT START:")
            print(Path("var/probe2.out.log").read_text(errors="replace")[-2000:])
            print(Path("var/probe2.err.log").read_text(errors="replace")[-2000:])
            return 1
        print("[1] server up")

        control = RelayClient(BASE)
        created = await control.create_gateway(name="probe2", upstreams=["echo"])
        gid, key = created["id"], created["api_key"]
        print(f"[2] gateway {gid} key {key[:8]}…")

        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        import httpx2

        http = httpx2.AsyncClient(
            headers={"X-API-Key": key},
            timeout=30.0,
            follow_redirects=True,
        )
        target = streamable_http_client(f"{BASE}/gateway/{gid}/mcp", http_client=http)
        client = await Client(target).__aenter__()

        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("[3] tools/list:", names)
        assert set(names) == {"echo.echo", "echo.shout", "gateway_search_tools"}, names

        res = await client.call_tool("echo.echo", {"text": "hi"})
        assert not res.is_error, res.content
        print("[4] call echo BEFORE any filter ->", res.content[0].text)

        # deny echo (TUI toggle-off): PATCH !echo.echo
        await control.patch_gateway(gid, tool_filter=["!echo.echo", "*"])
        res = await client.call_tool("echo.echo", {"text": "hi"})
        print("[5] call echo after DENY      ->",
              "DENIED ✓" if res.is_error else f"ALLOWED ✗ ({res.content[0].text})")
        assert res.is_error, "deny did not take effect on the live path"

        # re-allow echo (TUI toggle-on): PATCH * — the user's exact scenario
        await control.patch_gateway(gid, tool_filter=["*"])
        res = await client.call_tool("echo.echo", {"text": "hi"})
        ok = not res.is_error
        print("[6] call echo after RE-ALLOW  ->",
              f"ALLOWED ✓ ({res.content[0].text})" if ok else
              f"DENIED ✗ ({res.content[0].text})")

        # and the list refreshes too, same session
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("[7] tools/list after RE-ALLOW ->", names)
        await client.__aexit__(None, None, None)   # same-task close (SDK requirement)

        await control.aclose()
        if ok and set(names) == {"echo.echo", "echo.shout", "gateway_search_tools"}:
            print("LIVE ENFORCEMENT PROBE PASSED")
            return 0
        return 1
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        for p in ("var/probe2-config.yaml", "var/probe2-gateways.json*",
                  "var/probe2-upstreams.json*", "var/probe2.*.log"):
            for f in glob.glob(p):
                try:
                    Path(f).unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
