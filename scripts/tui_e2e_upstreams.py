"""Live E2E for runtime upstream CRUD: spawn a throwaway gateway on 8788
(isolated state files, no credentials), then drive the REAL TUI through:

    1. add upstream via the `a` modal (stdio, npx filesystem)
    2. verify it connects and shows as runtime in the list
    3. verify gateway creation accepts the new upstream (allow-list refresh)
    4. delete it via `x` and verify it is gone server-side

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
from textual.widgets import Input

from mcprelay.tui import api as relay
from mcprelay.tui.app import McprelayApp

BASE = "http://127.0.0.1:8788"
CONFIG = Path("var/tui-e2e-config.yaml")
KEY = "e2e-fs"


def write_config() -> None:
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text("""
server:
  host: 127.0.0.1
  port: 8788
  log_level: warning

gateways_state_path: var/tui-e2e-gateways.json
upstreams_state_path: var/tui-e2e-upstreams.json

upstreams:
  memory:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]

gateways:
  dev:
    api_key: tui-e2e-key
    description: tui-e2e test gateway
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
    # clear state from prior runs
    for p in ("var/tui-e2e-gateways.json", "var/tui-e2e-upstreams.json"):
        Path(p).unlink(missing_ok=True)

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcprelay", "serve", "-c", str(CONFIG)],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        health = await wait_health()
        print("server up:", health["status"], "| upstreams:",
              list(health["upstreams"]))
        assert KEY not in health["upstreams"]

        client = relay.RelayClient(BASE)
        app = McprelayApp(client)

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause(1.5)

            # --- upstreams scene, add via `a`
            await pilot.press("u")
            await pilot.pause(1.0)
            await pilot.press("a")
            await pilot.pause(0.5)
            modal = app.screen
            m = {i.id: i for i in modal.query(Input)}
            assert "u-key" in m, list(m)
            m["u-key"].value = KEY
            await pilot.press("enter")
            m["u-transport"].value = "stdio"
            await pilot.press("enter")
            m["u-command"].value = (
                "npx -y @modelcontextprotocol/server-filesystem C:/personal"
            )
            await pilot.press("enter")      # -> url
            await pilot.press("enter")      # -> headers
            await pilot.press("enter")      # CREATE (last field)
            await pilot.pause(15.0)         # npx spawn + handshake

            print("after add: scene",
                  [type(c).__name__ for c in app.query_one("#content").children],
                  "selected:", app.selected_upstream)
            assert app.selected_upstream == KEY, app.selected_upstream

            listing = app.query_one("#up-list")
            labels = " ".join(str(s.children[0].content) for s in listing.children)
            print("list:", [str(s.children[0].content) for s in listing.children])
            assert KEY in labels and "runtime" in labels, labels
            assert "ready" in labels, "e2e-fs did not reach ready"

            # --- server-side source + healthz
            live = (await client.upstreams())["upstreams"][KEY]
            print("server view:", {k: live.get(k) for k in ("state", "source", "tool_count")})
            assert live["source"] == "runtime" and live["state"] == "ready"

            # --- gateway creation accepts the new upstream
            created = await client.create_gateway(name="e2e-with-new-up",
                                                  upstreams=[KEY, "memory"])
            print("gateway created with new upstream:", created["id"],
                  created["tool_count"], "tools")
            assert created["tool_count"] > 0

            # --- delete via TUI `x`
            listing.index = [getattr(c, "data", {}).get("key") for c in listing.children].index(KEY)
            await pilot.press("x")
            await pilot.pause(2.0)
            labels_after = " ".join(str(s.children[0].content) for s in listing.children)
            print("after delete:", [str(s.children[0].content) for s in listing.children])
            assert KEY not in labels_after, labels_after

        # gone from healthz and rejected for creation
        health2 = await wait_health()
        assert KEY not in health2["upstreams"], list(health2["upstreams"])
        try:
            await client.create_gateway(name="e2e-should-fail", upstreams=[KEY])
            raise AssertionError("creating with the deleted upstream should fail")
        except relay.RelayError as exc:
            print("correctly refused:", exc)

        await client.aclose()
        print("LIVE UPSTREAM E2E PASSED")
        return 0
    finally:
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                proc.terminate()
        for p in ("var/tui-e2e-config.yaml", "var/tui-e2e-gateways.json*",
                  "var/tui-e2e-upstreams.json*"):
            for f in Path(".").glob(p):
                try:
                    f.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
