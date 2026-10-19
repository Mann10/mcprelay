"""Headless smoke tests for the mcprelay TUI.

Runs the app through Textual's pilot (a scripted virtual terminal) against
a *mock* control plane, so no gateway has to be running. Verifies:

    1. the app mounts and renders the opencode-style brand line
    2. the dashboard shows upstream + gateway state from /healthz
    3. key navigation reaches each scene
    4. an install preview shows the rendered client config
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from textual.app import App
from textual.widgets import Input

sys.path.insert(0, ".")

from mcprelay.tui import api as relay  # noqa: E402
from mcprelay.tui.app import McprelayApp  # noqa: E402

HEALTH = {
    "status": "ok",
    "uptime_seconds": 1042.1,
    "upstreams_ready": 2,
    "upstreams_total": 3,
    "upstreams": {
        "filesystem": {"state": "ready", "tool_count": 11},
        "openai": {"state": "error", "tool_count": 0, "transport": "streamable-http",
                   "last_error": "401 Unauthorized (missing header)"},
        "weather": {"state": "connecting", "tool_count": 4},
    },
    "gateways": {
        "dev": {"endpoint": "/gateway/dev/mcp", "radar_endpoint": "/gateway/dev/radar",
                "tools": 15, "source": "config"},
        "research": {"endpoint": "/gateway/research/mcp", "radar_endpoint": "/gateway/research/radar",
                     "tools": 7, "source": "runtime"},
    },
}

GATEWAYS = {"gateways": [
    {"id": "dev", "description": "mcprelay gateway dev", "endpoint": "/gateway/dev/mcp",
     "radar_endpoint": "/gateway/dev/radar", "upstreams": ["filesystem", "weather"],
     "tool_count": 15, "unavailable_upstreams": [], "auth": "X-API-Key", "source": "config"},
    {"id": "research", "description": "mcprelay gateway research", "endpoint": "/gateway/research/mcp",
     "radar_endpoint": "/gateway/research/radar", "upstreams": ["openai"],
     "tool_count": 7, "unavailable_upstreams": ["openai"], "auth": "X-API-Key", "source": "runtime"},
]}

GATEWAY_DEV = dict(GATEWAYS["gateways"][0], tools=["filesystem.read_file", "weather.forecast"],
                   tool_filter=["*"], source="config")

GATEWAY_RESEARCH = dict(GATEWAYS["gateways"][1], tools=["openai.ask"],
                        tool_filter=["!openai.delete_all", "*"], source="runtime")

RESEARCH_CHOICES = {"gateway": "research", "upstreams": {
    "openai": [
        {"name": "openai.ask", "original_name": "ask",
         "description": "Ask a model", "allowed": True},
        {"name": "openai.delete_all", "original_name": "delete_all",
         "description": "Wipe everything", "allowed": False},
    ],
}}

TOOL_CHOICES = {"gateway": "dev", "upstreams": {
    "filesystem": [{"name": "filesystem.read_file", "original_name": "read_file",
                    "description": "Read", "allowed": True}],
    "weather": [{"name": "weather.forecast", "original_name": "forecast",
                 "description": "Forecast", "allowed": True}],
}}

TOOLS = {
    "gateway": "dev", "count": 2, "search_tool": "gateway_search_tools",
    "denied": [], "unavailable_upstreams": [],
    "tools": [
        {"name": "filesystem.read_file", "original_name": "read_file",
         "description": "Read one file from disk", "inputSchema": {}},
        {"name": "weather.forecast", "original_name": "forecast",
         "description": "Weather for a city", "inputSchema": {}},
    ],
}

CLIENT_CONFIG = {
    "client_id": "opencode", "display_name": "OpenCode",
    "transport": "remote", "supported": True, "reason": None,
    "server_key": "mcprelay-dev", "target_path": None, "notes": ["test"],
    "config": {"mcp": {"mcprelay-dev": {"type": "remote",
                                        "url": "http://127.0.0.1:8787/gateway/dev/mcp",
                                        "headers": {"X-API-Key": "rk_test"}}},
               "config_json": json.dumps({"mcp": {"mcprelay-dev": {}}}, indent=2)},
}


class MockTransport(httpx.AsyncBaseTransport):
    """Routes TUI requests to canned payloads, catching anything unexpected."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.upstreams: dict[str, dict[str, Any]] = dict(HEALTH["upstreams"])
        self.upstream_sources: dict[str, str] = {k: "config" for k in self.upstreams}
        self.research_filter: list[str] = list(GATEWAY_RESEARCH["tool_filter"])

    def _research_choices(self) -> dict[str, Any]:
        """tool-choices for `research`, allowed flags derived from the filter."""
        import copy

        choices = copy.deepcopy(RESEARCH_CHOICES)
        for tool in choices["upstreams"]["openai"]:
            tool["allowed"] = f"!{tool['name']}" not in self.research_filter
        return choices

    def _upstreams_payload(self) -> dict[str, Any]:
        return {
            key: {**state, "source": self.upstream_sources.get(key, "config")}
            for key, state in self.upstreams.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        path = request.url.path
        method = request.method
        if (method, path) == ("GET", "/healthz"):
            return httpx.Response(200, json=HEALTH)
        if (method, path) == ("GET", "/api/v1/upstreams"):
            return httpx.Response(200, json={
                "upstreams": self._upstreams_payload(),
                "tools": {"filesystem": ["a", "b"], "openai": [], "weather": ["c"]},
            })
        if (method, path) == ("POST", "/api/v1/upstreams"):
            body = json.loads(request.content)
            key = body["key"]
            self.upstreams[key] = {"state": "ready", "tool_count": 42}
            self.upstream_sources[key] = "runtime"
            return httpx.Response(201, json={
                "key": key, "state": "ready", "tool_count": 42,
            })
        if method == "DELETE" and path.startswith("/api/v1/upstreams/"):
            key = path.rsplit("/", 1)[-1]
            if self.upstream_sources.get(key) != "runtime":
                return httpx.Response(
                    409, json={"error": "declared in the config file"}
                )
            self.upstreams.pop(key)
            self.upstream_sources.pop(key)
            return httpx.Response(200, json={"deleted": key})
        if (method, path) == ("GET", "/api/v1/gateways"):
            return httpx.Response(200, json=GATEWAYS)
        if (method, path) == ("POST", "/api/v1/gateways"):
            body = json.loads(request.content)
            gid = body["name"]
            upstreams = [str(u) for u in body.get("upstreams", [])]
            missing = [u for u in upstreams if u not in self.upstreams]
            if missing:
                return httpx.Response(400, json={
                    "error": f"unknown upstream(s): {', '.join(missing)}",
                })
            return httpx.Response(201, json={
                "id": gid,
                "endpoint": f"/gateway/{gid}/mcp",
                "radar_endpoint": f"/gateway/{gid}/radar",
                "upstreams": upstreams,
                "tool_filter": list(body.get("tool_filter", ["*"])),
                "tool_count": 7,
                "source": "runtime",
                "api_key": "gw_demo_key",
            })
        if (method, path) == ("GET", "/api/v1/gateways/dev"):
            return httpx.Response(200, json=GATEWAY_DEV)
        if (method, path) == ("GET", "/api/v1/gateways/research"):
            return httpx.Response(200, json={
                **GATEWAY_RESEARCH, "tool_filter": list(self.research_filter),
            })
        if (method, path) == ("GET", "/api/v1/gateways/dev/tool-choices"):
            return httpx.Response(200, json=TOOL_CHOICES)
        if (method, path) == ("GET", "/api/v1/gateways/research/tool-choices"):
            return httpx.Response(200, json=self._research_choices())
        if (method, path) == ("PATCH", "/api/v1/gateways/dev"):
            return httpx.Response(409, json={
                "error": "gateway 'dev' is declared in the config file; "
                         "edit its tool_filter there rather than at runtime",
            })
        if (method, path) == ("PATCH", "/api/v1/gateways/research"):
            body = json.loads(request.content)
            self.research_filter = [str(p) for p in body.get("tool_filter", ["*"])]
            return httpx.Response(200, json={
                "gateway": "research", "tool_filter": list(self.research_filter),
            })
        if (method, path) == ("GET", "/api/v1/gateways/dev/tools"):
            return httpx.Response(200, json=TOOLS)
        if (method, path) == ("GET", "/api/v1/clients"):
            return httpx.Response(200, json={"clients": [
                {"client_id": "opencode", "display_name": "OpenCode",
                 "supports_http": True, "supports_stdio": True,
                 "default_path": "~/.config/opencode/opencode.json", "path_hint": None,
                 "notes": ""},
            ]})
        if (method, path) == ("GET", "/api/v1/gateways/dev/client-config/opencode"):
            return httpx.Response(200, json=CLIENT_CONFIG)
        return httpx.Response(404, json={"error": f"mock miss: {method} {path}"})


async def main() -> int:
    app = McprelayApp(relay.RelayClient("http://mock"))
    app.client.http._transport = MockTransport()  # type: ignore[attr-defined]

    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause(0.6)

        # 1 - brand line, in the opencode mold
        brand = str(app.query_one("#brandline").render())
        assert "<mcprelay>" in brand, brand
        assert "v0.1.0" in brand and "http://mock" in brand, brand

        # 2 - dashboard data made it on screen
        ups_panel = str(app.query_one("#dash-upstreams").render())
        assert "filesystem" in ups_panel and "weather" in ups_panel, ups_panel
        assert "ready" in ups_panel, ups_panel
        gw_panel = str(app.query_one("#dash-gateways").render())
        assert "dev" in gw_panel and "research" in gw_panel, gw_panel

        # 3 - navigation: upstreams scene lists all three
        await pilot.press("u")
        await pilot.pause(0.6)
        up_list_text = " ".join(
            str(s.children[0].content) for s in app.query_one("#up-list").children
        )
        assert "filesystem" in up_list_text and "openai" in up_list_text, up_list_text

        # gateways scene
        await pilot.press("g")
        await pilot.pause(0.6)
        gw_list_text = " ".join(
            str(s.children[0].content) for s in app.query_one("#gw-list").children
        )
        assert "research" in gw_list_text, gw_list_text

        # open the first gateway's detail
        await pilot.press("enter")
        await pilot.pause(0.8)
        gw_detail_text = str(app.query_one("#gw-detail").render())
        assert "/gateway/dev/mcp" in gw_detail_text, gw_detail_text

        # 4 - tools scene on the runtime gateway: on/off chips + toggling
        await pilot.press("escape")          # back to the gateways list
        await pilot.pause(0.5)
        await pilot.press("down")            # highlight `research` (runtime)
        await pilot.press("enter")
        await pilot.pause(0.8)
        await pilot.press("t")
        await pilot.pause(0.8)
        tb_listing = app.query_one("#tb-list")
        rows = [str(s.children[0].content) for s in tb_listing.children]
        assert any("openai.ask" in r and "on" in r for r in rows), rows
        assert any("openai.delete_all" in r and "off" in r for r in rows), rows
        # the built-in search meta-tool shows up as its own toggleable row
        assert any("gateway_search_tools" in r and "on" in r for r in rows), rows
        assert "2/3 enabled" in str(app.query_one("#tb-summary").render())

        # toggle openai.ask off, then back on
        tb_listing.index = 1
        await pilot.press("enter")
        await pilot.pause(0.8)
        rows = [str(s.children[0].content) for s in tb_listing.children]
        assert any("openai.ask" in r and "off" in r for r in rows), rows
        assert "1/3 enabled" in str(app.query_one("#tb-summary").render())
        await pilot.press("enter")
        await pilot.pause(0.8)
        rows = [str(s.children[0].content) for s in tb_listing.children]
        assert any("openai.ask" in r and "on" in r for r in rows), rows
        assert "2/3 enabled" in str(app.query_one("#tb-summary").render())

        # the meta-tool itself can be denied as well
        tb_listing.index = 0
        await pilot.press("enter")
        await pilot.pause(0.8)
        rows = [str(s.children[0].content) for s in tb_listing.children]
        assert any("gateway_search_tools" in r and "off" in r for r in rows), rows
        await pilot.press("enter")
        await pilot.pause(0.8)
        await pilot.press("escape")          # back to gateway detail
        await pilot.pause(0.4)

        # 5 - add a runtime upstream through the modal, then delete it again
        await pilot.press("u")
        await pilot.pause(0.6)
        await pilot.press("a")
        await pilot.pause(0.5)
        modal = app.screen
        m_inputs = {i.id: i for i in modal.query(Input)}
        assert "u-key" in m_inputs, list(m_inputs)
        m_inputs["u-key"].value = "secrets"
        await pilot.press("enter")          # focus + open the transport dropdown
        await pilot.pause(0.3)
        await pilot.press("down")           # highlight streamable-http
        await pilot.press("enter")          # confirm -> focus moves to u-url
        await pilot.pause(0.3)
        m_inputs["u-url"].value = "https://example.invalid/mcp"
        await pilot.press("enter")          # u-url -> u-headers
        # then submit from the last (headers) field to create
        m_inputs["u-headers"].value = ""
        await pilot.press("enter")          # u-headers -> CREATE
        await pilot.pause(0.8)
        # modal gone, back on upstreams scene with new entry highlighted
        assert app.selected_upstream == "secrets", app.selected_upstream
        await pilot.pause(1.0)
        listing = app.query_one("#up-list")
        labels = " ".join(str(s.children[0].content) for s in listing.children)
        assert "secrets" in labels and "runtime" in labels, labels

        # delete only works for runtime upstreams
        await pilot.press("x")
        await pilot.pause(0.8)
        labels_after = " ".join(str(s.children[0].content) for s in listing.children)
        assert "secrets" not in labels_after, labels_after
        # deleting a config-declared one is refused by the server (409 -> notify, stays)
        app.selected_upstream = "filesystem"
        listing.index = ([getattr(c, "data", {}).get("key") for c in listing.children]
                         .index("filesystem"))
        await pilot.press("x")
        await pilot.pause(0.8)
        assert "filesystem" in " ".join(
            str(s.children[0].content) for s in listing.children
        )

        # help modal and back
        await pilot.press("d")
        await pilot.pause(0.3)
        await pilot.press("q")  # exits the app

    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
