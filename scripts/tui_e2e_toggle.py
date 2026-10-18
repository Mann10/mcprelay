"""Live E2E for tool enable/disable toggling: spawn a throwaway gateway on
8789, create a runtime gateway via the control plane, then drive the REAL TUI:

    1. tools browser shows on/off chips from the allow-list
    2. enter toggles a tool off  -> server-side tool_filter gains `!tool`
    3. enter again toggles it on -> filter entry removed
    4. a declared-in-config gateway PATCH is refused with a clear error

Cleans up its own server process and artifacts.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import httpx
from textual.widgets import ListView

from mcprelay.tui import api as relay
from mcprelay.tui.app import McprelayApp

BASE = "http://127.0.0.1:8789"
CONFIG = Path("var/tui-toggle-config.yaml")


def write_config() -> None:
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text("""
server:
  host: 127.0.0.1
  port: 8789
  log_level: warning

gateways_state_path: var/tui-toggle-gateways.json
upstreams_state_path: var/tui-toggle-upstreams.json

upstreams:
  memory:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]

gateways:
  dev:
    api_key: tui-e2e-key
    description: declared gateway (read-only)
    upstreams: [memory]
""", encoding="utf-8")


async def wait_health(deadline_s: float = 90.0) -> dict:
    async with httpx.AsyncClient(timeout=2.0) as http:
        end = time.monotonic() + deadline_s
        last = None
        while time.monotonic() < end:
            try:
                r = await http.get(f"{BASE}/healthz")
                return r.json()
            except Exception as exc:  # noqa: BLE001 - still booting
                last = exc
            await asyncio.sleep(1.0)
        raise RuntimeError(f"server never came up: {last}")


async def main() -> int:
    write_config()
    for p in ("var/tui-toggle-gateways.json", "var/tui-toggle-upstreams.json"):
        Path(p).unlink(missing_ok=True)

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcprelay", "serve", "-c", str(CONFIG)],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await wait_health()
        print("server up")

        client = relay.RelayClient(BASE)
        created = await client.create_gateway(name="toggle-me", upstreams=["memory"])
        gid = created["id"]
        print("runtime gateway:", gid, created["tool_count"], "tools")

        choices = await client.tool_choices(gid)
        tools = [t for ts in choices["upstreams"].values() for t in ts]
        assert tools and all(t["allowed"] for t in tools), tools
        target = tools[0]["name"]
        print("target tool:", target)

        app = McprelayApp(client)
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause(1.5)
            await pilot.press("g")
            await pilot.pause(1.0)
            listing = app.query_one("#gw-list", ListView)
            idx = [getattr(c, "data", {}).get("gw", {}).get("id")
                   for c in listing.children].index(gid)
            listing.index = idx
            await pilot.press("enter")           # open detail
            await pilot.pause(1.0)
            await pilot.press("t")               # tools browser
            await pilot.pause(1.0)

            tb = app.query_one("#tb-list", ListView)
            rows = [str(s.children[0].content) for s in tb.children]
            assert rows and all("on" in r for r in rows), rows
            total = created["tool_count"]
            assert f"{total}/{total} enabled" in str(app.query_one("#tb-summary").render())

            # toggle the target off
            tb.index = [getattr(c, "data", {}).get("name") for c in tb.children].index(target)
            await pilot.press("enter")
            await pilot.pause(1.2)
            rows = [str(s.children[0].content) for s in tb.children]
            assert any(target in r and "off" in r for r in rows), rows

            # server side: filter gained the deny rule, choices agree
            gw = await client.gateway(gid)
            assert f"!{target}" in gw["tool_filter"], gw["tool_filter"]
            choices = await client.tool_choices(gid)
            flat = {t["name"]: t["allowed"] for ts in choices["upstreams"].values() for t in ts}
            assert flat[target] is False
            print("toggle off OK ->", gw["tool_filter"])

            # toggle back on
            await pilot.press("enter")
            await pilot.pause(1.2)
            gw = await client.gateway(gid)
            assert f"!{target}" not in gw["tool_filter"], gw["tool_filter"]
            print("toggle on OK  ->", gw["tool_filter"])

        # declared-in-config gateway must be refused (409)
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.patch(f"{BASE}/api/v1/gateways/dev", json={"tool_filter": ["*"]})
            assert r.status_code == 409, (r.status_code, r.text)
            assert "declared in the config file" in r.json()["error"]
        print("config-declared gateway correctly refused (409)")

        await client.aclose()
        print("LIVE TOGGLE E2E PASSED")
        return 0
    finally:
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.terminate()
        for p in ("var/tui-toggle-config.yaml", "var/tui-toggle-gateways.json*",
                  "var/tui-toggle-upstreams.json*"):
            for f in Path(".").glob(p):
                try:
                    f.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
