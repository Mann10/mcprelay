"""End-to-end: run the real TUI against the REALLY running mcprelay gateway
(127.0.0.1:8787), exercise navigation against live data, and save a PNG
screenshot of the dashboard + a text export of the key scenes."""

import asyncio
import sys

sys.path.insert(0, ".")

from textual.widgets import Input, ListItem
from mcprelay.tui import api as relay
from mcprelay.tui.app import McprelayApp


def flat(node) -> str:
    """All rendered content strings from a scene, in document order."""
    parts = []
    for w in node.walk_children():
        try:
            parts.append(str(w.content))
        except Exception:  # noqa: BLE001 - widgets without .content
            pass
    return "\n".join(p for p in parts if p.strip())


async def main() -> int:
    client = relay.RelayClient("http://127.0.0.1:8787")
    app = McprelayApp(client)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(2.5)

        dashboard = flat(app.query_one("#content"))
        print("== DASHBOARD ==")
        print(dashboard)
        assert "filesystem" in dashboard, "live upstreams missing from dashboard"
        assert "dashboard" in dashboard.lower()

        with open("var/tui-dashboard.svg", "w", encoding="utf-8") as f:
            f.write(app.export_screenshot())

        # upstreams live scene: reconnect column is real state
        await pilot.press("u")
        await pilot.pause(1.2)
        upstreams_text = " ".join(
            str(i.children[0].content) for i in app.query_one("#up-list").children
        )
        print("== UPSTREAMS (one line per item) ==")
        print(upstreams_text[:1200])
        assert "filesystem" in upstreams_text

        # gateways: open the first one and browse its tools live
        await pilot.press("g")
        await pilot.pause(1.0)
        gw_text = " ".join(
            str(i.children[0].content) for i in app.query_one("#gw-list").children
        )
        print("== GATEWAYS ==", "\n", gw_text[:600])
        from textual.widgets import ListItem

        listing = app.query_one("#gw-list")
        item = listing.highlighted_child
        assert item is not None, "no gateway to open"
        data = getattr(item, "data", {})
        gid = (data.get("gw") or {}).get("id")
        print("opening gateway:", gid)
        await pilot.press("enter")
        await pilot.pause(1.5)
        detail = flat(app.query_one("#content"))
        print("== GATEWAY DETAIL ==", "\n", detail[:900])
        assert f"gateway {gid}" in detail, detail[:200]

        await pilot.press("t")
        await pilot.pause(1.5)
        tools_text = flat(app.query_one("#content"))
        count_line = [l for l in tools_text.splitlines() if "tools" in l.lower()][:2]
        print("== TOOLS ==", "\n", count_line)
        # type a filter for realness
        app.query_one("#tb-filter", Input).value = "fs."
        await pilot.pause(1.0)
        filtered = flat(app.query_one("#content"))
        print("== TOOLS FILTERED 'fs.' ==")
        print(filtered[:900])
        with open("var/tui-tools.svg", "w", encoding="utf-8") as f:
            f.write(app.export_screenshot())

    await client.aclose()
    print("E2E PASSED — screenshots: gateway/var/tui-dashboard.svg, tui-tools.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
