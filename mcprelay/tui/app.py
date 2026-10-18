"""The mcprelay TUI itself.

Look-and-feel notes (mirroring opencode's terminal UI):

* a brand line ``<mcprelay> v0.1.0`` at the top, server URL to its right
* a dark, low-contrast surface with a single amber accent
* a bottom key-hint bar in the ``key action`` style
* hard failure on a non-TTY stdout, exactly like ``opencode mini``

Structure: the app keeps ONE ``#content`` container and swaps scene
widgets into it. Scenes are ordinary widgets, so each owns its own data
loading via ``run_worker``, and key handlers on a scene receive keys that
bubble from whichever child has focus. App-level bindings cover the
always-available switches (d/u/g/n/?/q).
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from ..naming import match_filter
from . import api as relay

# The radar search meta-tool: exposed by every gateway unless the allow-list
# denies it by name (see mcp_server.SEARCH_TOOL_NAME).
SEARCH_TOOL_NAME = "gateway_search_tools"

ACCENT = "#e8a33d"      # the one accent, opencode-style
FG = "#d5d9de"
DIM = "#6b7280"
BORDER = "#2b303b"
SURFACE = "#0d1117"
SURFACE2 = "#161b22"
OK = "#3fb27f"
BAD = "#e35b5f"

_STATE_COLOR = {
    "ready": OK,
    "connecting": ACCENT,
    "error": BAD,
    "stopped": DIM,
}

CSS = f"""
Screen {{ background: {SURFACE}; color: {FG}; }}

#brandline {{ height: 1; padding: 0 1; background: {SURFACE2}; }}
#brandline .brand {{ color: {ACCENT}; text-style: bold; }}
#brandline .server {{ color: {DIM}; }}

#statusbar {{
    dock: bottom;
    height: 1;
    padding: 0 1;
    color: {DIM};
    background: {SURFACE2};
}}

#content {{ margin: 0 1; }}

.sceneheader {{
    height: 2;
    padding: 1 0 0 0;
    color: {ACCENT};
    text-style: bold;
}}
.sceneheader .sub {{ color: {DIM}; }}

.keyhint {{
    dock: bottom;
    height: 1;
    padding: 0 1;
    color: {DIM};
    background: {SURFACE2};
}}

Panel {{
    border: round {BORDER};
    padding: 1 2;
    margin-bottom: 1;
    height: auto;
    background: {SURFACE};
}}

ListView {{
    border: round {BORDER};
    background: {SURFACE};
    height: auto;
    max-height: 14;
    margin-bottom: 1;
}}
ListItem {{ padding: 0 1; }}

Input {{
    border: round {BORDER};
    background: {SURFACE};
    color: {FG};
    height: 3;
    margin-bottom: 1;
}}
Input:focus {{ border: round {ACCENT}; }}

ModalScreen {{ background: {SURFACE}; }}
#modalbody {{ padding: 1 2; }}
"""


def _state_chip(state: str) -> str:
    color = _STATE_COLOR.get(state, ACCENT)
    return f"[{color}]● {state}[/{color}]"


def hintline(*pairs: tuple[str, str]) -> str:
    """``r reconnect   esc back`` style bottom-bar content."""
    return "  " + "  ".join(f"[dim]{key}[/] [b]{label}[/]" for key, label in pairs)


# -------------------------------------------------------------------- widgets


class Panel(Static):
    """A labelled round-bordered box — the recurring surface of this UI."""


# -------------------------------------------------------------------- scenes


class DashboardScene(Static):
    """One glance: upstream health + gateways."""

    def compose(self) -> ComposeResult:
        yield Static(f"[{ACCENT}]dashboard[/]", classes="sceneheader")
        yield Panel(id="dash-upstreams")
        yield Panel(id="dash-gateways")
        yield Static(
            hintline(("r", "refresh"), ("u", "upstreams"), ("g", "gateways"),
                     ("n", "new gateway"), ("?", "help"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self.app.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        ups = self.query_one("#dash-upstreams", Panel)
        gws = self.query_one("#dash-gateways", Panel)
        try:
            health = await self.app.client.health()
        except Exception as exc:  # noqa: BLE001
            ups.content = f"[{BAD}]control plane unreachable: {exc}[/]"
            return

        upstreams = health.get("upstreams", {})
        lines = [
            f"[{ACCENT}] Upstreams[/]  "
            f"{health.get('upstreams_ready', 0)} of {health.get('upstreams_total', 0)} ready",
        ]
        for key, state in sorted(upstreams.items()):
            count = state.get("tool_count") or 0
            lines.append(f"  {_state_chip(state.get('state', '?'))}  {key:<22}[dim]{count} tools[/]")
            error = state.get("last_error")
            if error and state.get("state") != "ready":
                lines.append(f"      [dim]└─[/] [{BAD}]{error.splitlines()[-1][:90]}[/]")
        if not upstreams:
            lines.append("  [dim](none configured)[/]")
        ups.content = "\n".join(lines)

        gateways = health.get("gateways", {})
        glines = [f"[{ACCENT}] Gateways[/]  ({len(gateways)})"]
        for gid, info in sorted(gateways.items()):
            glines.append(
                f"  {gid:<20} [dim]{info.get('endpoint', '?')}  ·  "
                f"{info.get('tools', '?')} tools  ·  {info.get('source', '?')}[/]"
            )
        if not gateways:
            glines.append("  [dim](none — press n to create one)[/]")
        gws.content = "\n".join(glines)


class UpstreamsScene(Static):
    """All upstreams; reconnect the highlighted one, add new ones with a."""

    AUTO_FOCUS = "#up-list"
    def compose(self) -> ComposeResult:
        yield Static(f"[{ACCENT}]upstreams[/]", classes="sceneheader")
        yield ListView(id="up-list")
        yield Static("", id="up-detail")
        yield Static(
            hintline(("enter", "details"), ("r", "reconnect"), ("a", "add upstream"),
                     ("x", "delete runtime"), ("esc", "back"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        self.refresh_list()
        self.query_one("#up-list", ListView).focus()

    def refresh_list(self) -> None:
        self.app.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        listing = self.query_one("#up-list", ListView)
        detail = self.query_one("#up-detail", Static)
        try:
            data = await self.app.client.upstreams()
        except Exception as exc:  # noqa: BLE001
            detail.content = f"[{BAD}]{exc}[/]"
            return
        states = data.get("upstreams", {})
        tools_by_key: dict[str, list[str]] = data.get("tools", {})
        listing.clear()
        keep_selected = self.app.selected_upstream
        for key, state in sorted(states.items()):
            source = state.get("source", "config")
            src_tag = "[dim]yaml[/]" if source == "config" else f"[{ACCENT}]runtime[/]"
            label = (
                f"{key:<20} {_state_chip(state.get('state', '?'))}"
                f"  [dim]{len(tools_by_key.get(key, []))} tools[/]  {src_tag}"
            )
            item = ListItem(Static(label))
            item.data = {"key": key, "state": state, "tools": tools_by_key.get(key, [])}  # type: ignore[attr-defined]
            await listing.append(item)
        if listing.children:
            # keep the user's selection when it survives the reload
            preferred = [
                i
                for i, child in enumerate(listing.children)
                if getattr(child, "data", {}).get("key")  # type: ignore[attr-defined]
                == keep_selected
            ]
            listing.index = preferred[0] if preferred else 0
        if not listing.children:
            detail.content = "[dim](no upstreams — press a to connect one)[/]"

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._show_detail(event.item)

    def on_list_view_submitted(self, event: ListView.Submitted) -> None:
        # enter on the list behaves like selecting the item
        self._show_detail(event.item)

    def _focused_item(self) -> ListItem | None:
        return self.query_one("#up-list", ListView).highlighted_child

    def _show_detail(self, item: ListItem) -> None:
        info = getattr(item, "data", None) or {}
        state = info.get("state", {})
        self.app.selected_upstream = info.get("key")
        lines = [
            f"[{ACCENT}]{info.get('key', '?')}[/]",
            f"  state    {_state_chip(state.get('state', '?'))}",
            f"  source   [dim]{'config file (edit YAML to change)' if state.get('source', 'config') == 'config' else 'runtime — added via the TUI, persists in var/upstreams.json'}[/]",
            f"  tools    [dim]{state.get('tool_count', len(info.get('tools') or []))}[/]",
        ]
        if state.get("last_error"):
            lines.append(f"  error    [{BAD}]{state['last_error'].splitlines()[-1][:90]}[/]")
        self.query_one("#up-detail", Static).content = "\n".join(lines)

    def action_reconnect(self) -> None:
        item = self._focused_item()
        if item is None:
            return
        key: str | None = getattr(item, "data", {}).get("key")  # type: ignore[attr-defined]
        if not key:
            return

        async def do() -> None:
            try:
                await self.app.client.reconnect_upstream(key)
                self.app.notify(f"reconnect requested: {key}")
            except relay.RelayError as exc:
                self.app.notify(str(exc), severity="error")

        self.app.run_worker(do(), exclusive=True)

    def action_add_upstream(self) -> None:
        def _added(result: dict[str, Any] | None) -> None:
            if not result:
                return
            self.app.selected_upstream = result.get("key")
            self.refresh_list()
            state = (result.get("state") or "?")
            if state == "ready":
                self.app.notify(
                    f"upstream {result.get('key')} connected "
                    f"({result.get('tool_count', 0)} tools)"
                )
            else:
                self.app.notify(
                    f"upstream {result.get('key')} added but {state}; "
                    "check the detail pane",
                    severity="warning",
                )

        self.app.push_screen(CreateUpstreamScreen(), _added)

    def action_delete_upstream(self) -> None:
        item = self._focused_item()
        if item is None:
            return
        info = getattr(item, "data", {}) or {}  # type: ignore[attr-defined]
        key = info.get("key", "")
        source = (info.get("state") or {}).get("source", "config")

        async def do() -> None:
            try:
                if source == "config":
                    self.app.notify(
                        f"{key} is declared in the config file — remove it in the YAML",
                        severity="error",
                    )
                    return
                await self.app.client.delete_upstream(key)
                self.app.notify(f"removed upstream {key}")
                self.refresh_list()
            except relay.RelayError as exc:
                self.app.notify(str(exc), severity="error")

        self.app.run_worker(do(), exclusive=True)

    # esc/q/a/x live at app level (scene context-aware), not per-scene


class GatewaysScene(Static):
    """Gateway list; enter opens the detail scene."""

    AUTO_FOCUS = "#gw-list"
    def compose(self) -> ComposeResult:
        yield Static(f"[{ACCENT}]gateways[/]", classes="sceneheader")
        yield ListView(id="gw-list")
        yield Static(
            hintline(("enter", "open"), ("n", "new"), ("x", "delete"),
                     ("esc", "back"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        self.refresh_list()
        self.query_one("#gw-list", ListView).focus()

    def refresh_list(self) -> None:
        self.app.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        listing = self.query_one("#gw-list", ListView)
        listing.clear()
        try:
            gateways = await self.app.client.gateways()
        except Exception as exc:  # noqa: BLE001
            await listing.append(ListItem(Static(f"[{BAD}]{exc}[/]")))
            return
        for gw in gateways:
            unavailable = gw.get("unavailable_upstreams") or []
            warn = f"  [{ACCENT}]! unavailable: {', '.join(unavailable)}[/]" if unavailable else ""
            label = (
                f"{gw['id']:<18} [dim]{gw.get('endpoint', '')}[/]"
                f"  [dim]{gw.get('tool_count', 0)} tools · {gw.get('source', 'config')}[/]{warn}"
            )
            item = ListItem(Static(label))
            item.data = {"gw": gw}  # type: ignore[attr-defined]
            await listing.append(item)
        if gateways:
            listing.index = 0
        if not gateways:
            await listing.append(ListItem(Static("[dim](none — press n to create one)[/]")))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._open_gateway(event.item)

    def on_list_view_submitted(self, event: ListView.Submitted) -> None:
        # enter opens the gateway just like clicking it
        self._open_gateway(event.item)

    def _open_gateway(self, item: ListItem) -> None:
        gw = getattr(item, "data", {}).get("gw") or {}  # type: ignore[attr-defined]
        if not gw.get("id"):
            return
        self.app.selected_gateway = gw["id"]
        self.app.show_scene("gateway-detail")

    def key_x(self) -> None:
        item = self.query_one("#gw-list", ListView).highlighted_child
        if item is None:
            return
        gw = getattr(item, "data", {}).get("gw") or {}  # type: ignore[attr-defined]
        gid = gw.get("id", "")

        async def do() -> None:
            try:
                await self.app.client.delete_gateway(gid)
                self.app.notify(f"deleted {gid}")
            except relay.RelayError as exc:
                self.app.notify(str(exc), severity="error")

        self.app.run_worker(do(), exclusive=True)

    # x/esc/q live at app level (scene context-aware), not per-scene


class GatewayDetailScene(Static):
    """One gateway: endpoint, allow-list overview, install entry."""

    def compose(self) -> ComposeResult:
        yield Static("", id="gw-title", classes="sceneheader")
        yield VerticalScroll(
            Panel(id="gw-detail"),
            Panel(id="gw-choices"),
        )
        yield Static(
            hintline(("t", "browse tools"), ("i", "install client"), ("k", "rotate key"),
                     ("g", "gateways"), ("esc", "back"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self.app.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        gid = self.app.selected_gateway
        if not gid:
            return
        self.query_one("#gw-title", Static).content = f"[{ACCENT}]gateway {gid}[/]"
        detail = self.query_one("#gw-detail", Panel)
        choices_panel = self.query_one("#gw-choices", Panel)
        try:
            gw = await self.app.client.gateway(gid)
        except relay.RelayError as exc:
            detail.content = f"[{BAD}]{exc}[/]"
            return
        filter_exp = ", ".join(gw.get("tool_filter", ["*"]))
        lines = [
            f"  endpoint     [{ACCENT}]{gw.get('endpoint')}[/]",
            f"  radar        [dim]{gw.get('radar_endpoint')}[/]",
            f"  description  [dim]{gw.get('description') or '-'}[/]",
            f"  upstreams    [dim]{', '.join(gw.get('upstreams', [])) or '-'}[/]",
            f"  filter       [dim]{filter_exp}[/]",
            f"  tools        [dim]{gw.get('tool_count')}[/]",
            f"  source       [dim]{gw.get('source')}[/]",
        ]
        if gw.get("unavailable_upstreams"):
            lines.append(f"  unavailable  [{BAD}]{', '.join(gw['unavailable_upstreams'])}[/]")
        detail.content = "\n".join(lines)

        try:
            choices = await self.app.client.tool_choices(gid)
        except Exception:  # noqa: BLE001 - detail side already works
            choices_panel.content = ""
            return
        blocks = [f"[{ACCENT}] tool choices[/]  [dim](allowed/total per upstream)[/]"]
        for upstream, items in sorted((choices.get("upstreams") or {}).items()):
            allowed = sum(1 for t in items if t.get("allowed"))
            blocks.append(f"  {upstream:<20} [dim]{allowed}/{len(items)}[/]")
        choices_panel.content = "\n".join(blocks)

    def _rotate(self) -> None:
        gid = self.app.selected_gateway or ""

        async def do() -> None:
            try:
                data = await self.app.client.rotate_key(gid)
            except relay.RelayError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.push_screen(KeyRevealScreen(
                title="key rotated — shown once",
                endpoint=self.app.client.base + f"/gateway/{gid}/mcp",
                api_key=data.get("api_key", ""),
            ))

        self.app.run_worker(do(), exclusive=True)


class ToolBrowserScene(Static):
    """Every tool the gateway's upstreams offer — search, toggle on/off.

    Toggles rewrite the gateway's tool allow-list (first-match-wins globs):
    disabling prepends ``!tool.name``; enabling removes that exact rule.
    Declared-in-config gateways are read-only and say so.
    """

    AUTO_FOCUS = "#tb-list"
    BINDINGS: ClassVar = [Binding("slash", "focus_filter", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("", id="tb-title", classes="sceneheader")
        yield Input(placeholder="filter by substring…  (tab or / returns to the list)", id="tb-filter")
        yield ListView(id="tb-list")
        yield Static("", id="tb-summary")
        yield Static(
            hintline(("enter", "toggle on/off"), ("tab /", "filter"), ("esc", "back"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        gid = self.app.selected_gateway
        self.query_one("#tb-title", Static).content = f"[{ACCENT}]tools[/]  [dim]{gid}[/]"
        self.query_one("#tb-list", ListView).focus()
        self._choices: list[dict[str, Any]] = []
        self.app.run_worker(self._reload(), exclusive=True)

    def action_focus_filter(self) -> None:
        self.query_one("#tb-filter", Input).focus()

    async def _reload(self, keep: str | None = None) -> None:
        gid = self.app.selected_gateway or ""
        summary = self.query_one("#tb-summary", Static)
        try:
            payload = await self.app.client.tool_choices(gid)
        except relay.RelayError as exc:
            summary.content = f"[{BAD}]{exc}[/]"
            return

        self._choices = [
            {**tool, "upstream": upstream}
            for upstream, tools in sorted((payload.get("upstreams") or {}).items())
            for tool in tools
        ]
        # The built-in search meta-tool is not an upstream tool, so the
        # tool-choices endpoint never lists it - but it IS allow-listed.
        # Derive its state from the gateway filter so it can be toggled too.
        try:
            gw = await self.app.client.gateway(gid)
            flt = [str(p) for p in gw.get("tool_filter", ["*"])]
            self._choices.insert(0, {
                "name": SEARCH_TOOL_NAME,
                "upstream": "gateway",
                "description": "built-in search meta-tool (radar)",
                "allowed": match_filter(SEARCH_TOOL_NAME, flt).included,
            })
        except Exception:  # noqa: BLE001 - detail row is best-effort
            pass

        listing = self.query_one("#tb-list", ListView)
        needle = self.query_one("#tb-filter", Input).value.lower().strip()
        enabled = sum(1 for t in self._choices if t.get("allowed"))
        listing.clear()
        shown = 0
        restore_to: int | None = None
        for tool in self._choices:
            name = tool.get("name", "?")
            desc = (tool.get("description") or "").replace("\n", " ")
            if needle and needle not in name.lower() and needle not in desc.lower():
                continue
            chip = f"[{OK}]on [/]" if tool.get("allowed") else f"[{BAD}]off[/]"
            item = ListItem(Static(f"  {chip}  {name:<46} [dim]{desc[:62]}[/]"))
            item.data = dict(tool)  # type: ignore[attr-defined]
            await listing.append(item)
            if keep and name == keep:
                restore_to = shown
            shown += 1
        if listing.children:
            listing.index = restore_to if restore_to is not None else 0

        title = f"{enabled}/{len(self._choices)} enabled"
        if needle:
            title += f"   [dim]matching {needle!r}[/]"
        summary.content = f"[{ACCENT}] {title}[/]  [dim]enter toggles the highlighted tool[/]"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tb-filter":
            self.app.run_worker(self._reload(), exclusive=True)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.list_view.highlighted_child
        if item is not None:
            self._toggle(item)

    def on_list_view_submitted(self, event: ListView.Submitted) -> None:
        # older/newer Textual versions may post Submitted instead of Selected
        item = event.list_view.highlighted_child
        if item is not None:
            self._toggle(item)

    def _toggle(self, item: ListItem) -> None:
        info = getattr(item, "data", {}) or {}  # type: ignore[attr-defined]
        name = info.get("name", "")
        was_on = bool(info.get("allowed"))
        gid = self.app.selected_gateway or ""

        async def do() -> None:
            try:
                gw = await self.app.client.gateway(gid)
                flt = [str(p) for p in gw.get("tool_filter", ["*"])]
                rule = f"!{name}"
                if was_on:
                    flt = [rule, *flt]          # deny rules must come first
                else:
                    if rule in flt:
                        flt.remove(rule)
                    else:
                        self.app.notify(
                            f"{name} is denied by a broader pattern in the "
                            "allow-list — edit that pattern instead",
                            severity="warning",
                        )
                        return
                await self.app.client.patch_gateway(gid, tool_filter=flt)
            except relay.RelayError as exc:
                self.app.notify(str(exc), severity="error")
                return
            except Exception as exc:  # noqa: BLE001
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"{name} {'disabled' if was_on else 'enabled'}")
            await self._reload(keep=name)

        self.app.run_worker(do(), exclusive=True)


class InstallScene(Static):
    """Agent-config screen: pick a client, preview what is written, then write."""

    def compose(self) -> ComposeResult:
        yield Static("", id="inst-title", classes="sceneheader")
        yield VerticalScroll(Static("", id="inst-clients"))
        yield Input(placeholder="client id (tab through the list above)", id="inst-id")
        yield Static("", id="inst-output")
        yield Static(
            hintline(("enter", "preview"), ("ctrl+w", "write config"),
                     ("esc", "back"), ("q", "quit")),
            classes="keyhint",
        )

    def on_mount(self) -> None:
        gid = self.app.selected_gateway
        self.query_one("#inst-title", Static).content = (
            f"[{ACCENT}]install[/]  [dim]{gid}[/]  [dim](previews first — nothing written until ctrl+w)[/]"
        )
        self.query_one("#inst-id", Input).focus()
        self.app.run_worker(self._load_clients(), exclusive=True)

    async def _load_clients(self) -> None:
        try:
            clients = await self.app.client.clients()
        except Exception as exc:  # noqa: BLE001
            self.query_one("#inst-clients", Static).content = f"[{BAD}]{exc}[/]"
            return
        rows = [f"[{ACCENT}] clients[/]"]
        for c in clients:
            path = c.get("default_path") or c.get("path_hint") or "copy manually"
            rows.append(f"  {c['client_id']:<18} [dim]{c['display_name']} · {path}[/]")
        self.query_one("#inst-clients", Static).content = "\n".join(rows)

    async def _run(self, write: bool) -> None:
        gid = self.app.selected_gateway or ""
        client_id = self.query_one("#inst-id", Input).value.strip()
        output = self.query_one("#inst-output", Static)
        if not client_id:
            output.content = f"[{ACCENT}]type a client id first — e.g. opencode[/]"
            return
        try:
            if write:
                data = await self.app.client.install_client(gid, client_id, write=True)
                note = f"[{OK}]WROTE[/] {data.get('path') or '(no default path on this machine)'}"
                if data.get("backup_path"):
                    note += f"   [dim]backup: {data['backup_path']}[/]"
                output.content = note
            else:
                data = await self.app.client.client_config(gid, client_id)
                output.content = (
                    f"[{DIM}]preview only — nothing written[/]\n"
                    f"[{ACCENT}]{data.get('display_name')}[/] "
                    f"[dim]({data.get('transport')}, {client_id})[/]\n"
                    + (data.get("config_json") or json.dumps(data.get("config"), indent=2))
                )
        except Exception as exc:  # noqa: BLE001
            output.content = f"[{BAD}]{exc}[/]"

    @on(Input.Submitted)
    async def _submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "inst-id":
            await self._run(write=False)

    def key_ctrl_w(self) -> None:
        self.app.run_worker(self._run(write=True), exclusive=True)


# --------------------------------------------------------------------- modals


class CreateGatewayScreen(ModalScreen[dict[str, Any] | None]):
    """Name + upstream keys + optional tool-filter globs."""

    AUTO_FOCUS = "Input"
    BINDINGS: ClassVar = [Binding("escape", "cancel", "close")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"[{ACCENT}]new gateway[/]", classes="sceneheader"),
            Input(placeholder="name            e.g. dev", id="c-name"),
            Input(placeholder="upstream keys   comma-separated, e.g. filesystem, github", id="c-ups"),
            Input(placeholder="tool filter     globs, e.g. fs.*   (empty = allow *)", id="c-filter"),
            Static("", id="c-note"),
            Static(hintline(("enter", "create"), ("esc", "cancel")), classes="keyhint"),
            id="modalbody",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    async def _submitted(self, event: Input.Submitted) -> None:
        inputs = {i.id: i for i in self.query(Input)}
        order = ["c-name", "c-ups", "c-filter"]
        position = order.index(event.input.id)
        if position < len(order) - 1:
            inputs[order[position + 1]].focus()
            return
        note = self.query_one("#c-note", Static)
        name = inputs["c-name"].value.strip()
        upstreams = [u.strip() for u in inputs["c-ups"].value.split(",") if u.strip()]
        tool_filter = [p.strip() for p in inputs["c-filter"].value.split(",") if p.strip()]
        if not name or not upstreams:
            note.content = f"[{BAD}]name and at least one upstream are required[/]"
            return
        try:
            created = await self.app.client.create_gateway(
                name=name, upstreams=upstreams, tool_filter=tool_filter or None,
            )
        except relay.RelayError as exc:
            note.content = f"[{BAD}]{exc}[/]"
            return
        except Exception as exc:  # noqa: BLE001
            note.content = f"[{BAD}]{exc}[/]"
            return
        self.dismiss({
            "id": created.get("id"),
            "endpoint": created.get("endpoint"),
            "api_key": created.get("api_key", ""),
        })


class CreateUpstreamScreen(ModalScreen[dict[str, Any] | None]):
    """Add one upstream at runtime.

    Fields adapt by transport:
      * stdio            -> command (with args), optional cwd, env
      * streamable-http/sse -> url, optional headers
    """

    AUTO_FOCUS = "Input"
    BINDINGS: ClassVar = [Binding("escape", "cancel", "close")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"[{ACCENT}]add upstream[/]  [dim]transport: stdio (npx …) or "
                "streamable-http / sse (url)[/]",
                classes="sceneheader",
            ),
            Input(placeholder="key            e.g. Notion (prefix of tool names)", id="u-key"),
            Input(placeholder="transport      stdio | streamable-http | sse", id="u-transport"),
            Input(placeholder="command        stdio only, e.g. npx -y @mcp/server", id="u-command"),
            Input(placeholder="url            remote only, e.g. https://…/mcp", id="u-url"),
            Input(placeholder="headers        remote only, e.g. X-API-Key: ${env:NOTION_KEY}", id="u-headers"),
            Static("", id="u-note"),
            Static(hintline(("enter", "add + connect"), ("esc", "cancel")), classes="keyhint"),
            id="modalbody",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    async def _submitted(self, event: Input.Submitted) -> None:
        inputs = {i.id: i for i in self.query(Input)}
        order = ["u-key", "u-transport", "u-command", "u-url", "u-headers"]
        position = order.index(event.input.id)
        if position < len(order) - 1:
            inputs[order[position + 1]].focus()
            return
        note = self.query_one("#u-note", Static)
        key = inputs["u-key"].value.strip()
        transport = inputs["u-transport"].value.strip() or "stdio"
        if transport not in ("stdio", "streamable-http", "sse"):
            note.content = f"[{BAD}]transport must be stdio, streamable-http or sse[/]"
            return
        spec: dict[str, Any] = {"transport": transport}
        if transport == "stdio":
            command = inputs["u-command"].value.strip()
            if command:
                import shlex

                try:
                    parts = shlex.split(command)
                except ValueError as exc:
                    note.content = f"[{BAD}]{exc}[/]"
                    return
                spec["command"] = parts[0]
                if len(parts) > 1:
                    spec["args"] = parts[1:]
            else:
                note.content = f"[{BAD}]stdio needs a command (e.g. npx -y @modelcontextprotocol/server-memory)[/]"
                return
        else:
            url = inputs["u-url"].value.strip()
            if not url:
                note.content = f"[{BAD}]{transport} needs a url[/]"
                return
            spec["url"] = url
            headers_raw = inputs["u-headers"].value.strip()
            headers: dict[str, str] = {}
            for pair in headers_raw.split(","):
                if ":" in pair:
                    name, value = pair.split(":", 1)
                    headers[name.strip()] = value.strip()
            if headers:
                spec["headers"] = headers

        try:
            data = await self.app.client.add_upstream({"key": key, **spec})
        except relay.RelayError as exc:
            note.content = f"[{BAD}]{exc}[/]"
            return
        except Exception as exc:  # noqa: BLE001
            note.content = f"[{BAD}]{exc}[/]"
            return
        self.dismiss({
            "key": data.get("key", key),
            "state": data.get("state"),
            "tool_count": data.get("tool_count", 0),
        })


class KeyRevealScreen(ModalScreen[None]):
    """A once-only API key. ctrl+y copies, any of esc/enter closes."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "close"),
        Binding("enter", "close", "close"),
        Binding("ctrl+y", "copy_key", "copy key"),
    ]

    def __init__(self, title: str, endpoint: str, api_key: str) -> None:
        super().__init__()
        self._title = title
        self._endpoint = endpoint
        self._api_key = api_key

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"[{ACCENT}]{self._title}[/]", classes="sceneheader"),
            Panel(
                f"  endpoint    [{FG}]{self._endpoint}[/]\n"
                f"  api key     [{OK}]{self._api_key}[/]\n\n"
                f"  [{DIM}]This is the only time it is shown — copy it now.[/]",
            ),
            Static(hintline(("ctrl+y", "copy key"), ("enter", "close")), classes="keyhint"),
            id="modalbody",
        )

    def action_copy_key(self) -> None:
        self.app.copy_to_clipboard(self._api_key)
        self.notify("API key copied to clipboard")

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [Binding("escape", "close", "back")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"[{ACCENT}]keys[/]", classes="sceneheader"),
            Panel("\n".join([
                "  [b]d[/]      dashboard    [dim]health overview: upstreams + gateways[/]",
                "  [b]u[/]      upstreams    [dim]connect / reconnect MCP servers[/]",
                "  [b]a[/]      add          [dim]add a new upstream server (in upstreams)[/]",
                "  [b]g[/]      gateways     [dim]compose and manage endpoints[/]",
                "  [b]n[/]      new          [dim]create a gateway[/]",
                "  [b]t[/]      tools        [dim]browse exposed tools (in a gateway)[/]",
                "  [b]i[/]      install      [dim]write client config (in a gateway)[/]",
                "  [b]k[/]      rotate key   [dim]regenerate a gateway key[/]",
                "  [b]r[/]      refresh      [dim]reload what is on screen[/]",
                "  [b]?[/]      help         [dim]this panel[/]",
                "  [b]q[/]      quit         [dim]leave the gateway running[/]",
                "",
                f"  [{DIM}]The gateway server keeps running after you quit — "
                "your agents stay connected.[/]",
            ])),
            Static(hintline(("esc", "back")), classes="keyhint"),
            id="modalbody",
        )

    def action_close(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------- the app


class McprelayApp(App):
    """``mcprelay`` — the terminal UI for the local MCP gateway."""

    CSS = CSS
    TITLE = "mcprelay"

    SCENES: ClassVar[dict[str, str]] = {
        "dashboard": "DashboardScene",
        "upstreams": "UpstreamsScene",
        "gateways": "GatewaysScene",
        "gateway-detail": "GatewayDetailScene",
        "tools": "ToolBrowserScene",
        "install": "InstallScene",
    }

    BINDINGS: ClassVar = [
        Binding("d", "show_scene('dashboard')", "dashboard", show=True),
        Binding("u", "show_scene('upstreams')", "upstreams", show=True),
        Binding("g", "show_scene('gateways')", "gateways", show=True),
        Binding("n", "new_gateway", "new gateway", show=True),
        Binding("r", "refresh_scene", "refresh", show=True),
        Binding("question_mark", "help", "help", show=True),
        Binding("q", "quit", "quit", show=True),
        # context keys: only act where they make sense (scene-aware actions)
        Binding("t", "tool_browser", show=False),
        Binding("i", "install_client", show=False),
        Binding("k", "rotate_key", show=False),
        Binding("x", "scene_delete", show=False),
        Binding("a", "scene_add_upstream", show=False),
        Binding("escape", "scene_back", show=False),
    ]

    def __init__(self, client: relay.RelayClient, *, version: str = "0.1.0") -> None:
        super().__init__()
        self.client = client
        self.version = version
        self.selected_upstream: str | None = None
        self.selected_gateway: str | None = None

    def compose(self) -> ComposeResult:
        brand = f"[{ACCENT}][b]<mcprelay>[/b][/{ACCENT}] [dim]v{self.version} · {self.client.base}[/dim]"
        yield Static(brand, id="brandline")
        with Vertical(id="content"):
            yield DashboardScene()
        yield Static(
            f"[dim]{self.client.base} ·[/] [b]d[/b] [dim]dashboard[/]  "
            "[b]u[/b] [dim]upstreams[/]  [b]g[/b] [dim]gateways[/]  "
            "[b]n[/b] [dim]new[/]  [b]?[/b] [dim]help[/]  [b]q[/b] [dim]quit[/]",
            id="statusbar",
        )

    # ------------------------------------------------------------- switching

    def show_scene(self, name: str) -> None:
        self.query_one("#content").remove_children()
        scene_cls = {
            "dashboard": DashboardScene,
            "upstreams": UpstreamsScene,
            "gateways": GatewaysScene,
            "gateway-detail": GatewayDetailScene,
            "tools": ToolBrowserScene,
            "install": InstallScene,
        }[name]
        self.query_one("#content").mount(scene_cls())

    # ------------------------------------------------- context-aware actions

    def _current_scene(self) -> Static | None:
        children = self.query_one("#content").children
        return children[0] if children else None

    def action_tool_browser(self) -> None:
        if isinstance(self._current_scene(), (GatewayDetailScene, ToolBrowserScene)):
            self.show_scene("tools")

    def action_install_client(self) -> None:
        if isinstance(self._current_scene(), GatewayDetailScene):
            self.show_scene("install")

    def action_rotate_key(self) -> None:
        if isinstance(self._current_scene(), GatewayDetailScene):
            self._current_scene()._rotate()  # type: ignore[attr-defined]

    def action_scene_delete(self) -> None:
        scene = self._current_scene()
        if isinstance(scene, GatewaysScene):
            scene.key_x()  # type: ignore[attr-defined]
        elif isinstance(scene, UpstreamsScene):
            scene.action_delete_upstream()

    def action_scene_add_upstream(self) -> None:
        scene = self._current_scene()
        if isinstance(scene, UpstreamsScene):
            scene.action_add_upstream()

    def action_scene_back(self) -> None:
        scene = self._current_scene()
        if not scene:
            return
        back: dict[type, str] = {
            UpstreamsScene: "dashboard",
            GatewaysScene: "dashboard",
            GatewayDetailScene: "gateways",
            ToolBrowserScene: "gateway-detail",
            InstallScene: "gateway-detail",
        }
        parent = back.get(type(scene))
        if parent:
            self.show_scene(parent)

    def action_show_scene(self, name: str) -> None:
        self.show_scene(name)

    def action_refresh_scene(self) -> None:
        content = self.query_one("#content")
        for child in content.children:
            for watcher_name in ("refresh_data", "refresh_list"):
                watcher = getattr(child, watcher_name, None)
                if callable(watcher):
                    watcher()
                    return

    def action_new_gateway(self) -> None:
        def _created(result: dict[str, Any] | None) -> None:
            if not result:
                return
            if result.get("id"):
                self.selected_gateway = result["id"]
                self.show_scene("gateway-detail")
            if result.get("api_key"):
                self.push_screen(KeyRevealScreen(
                    title="gateway created — key shown once",
                    endpoint=str(result.get("endpoint") or ""),
                    api_key=str(result.get("api_key")),
                ))

        self.push_screen(CreateGatewayScreen(), _created)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


def run(base: str | None = None, config: str = "config.yaml", *, spawn: bool = True) -> int:
    """Entry point used by the ``mcprelay tui`` command and the bare command."""
    # opencode parity: refuse to run a TUI into a pipe, and fail in milliseconds.
    if not sys.stdout.isatty():
        print("error: mcprelay requires a TTY stdout", file=sys.stderr)
        return 2

    import asyncio

    import httpx

    async def _boot() -> tuple[str, dict[str, Any]]:
        async with httpx.AsyncClient() as http:
            found, health = await relay.discover(http, base, config, spawn=spawn)
            return found, health

    try:
        found, _health = asyncio.run(_boot())
    except relay.RelayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    McprelayApp(relay.RelayClient(found)).run()
    return 0
