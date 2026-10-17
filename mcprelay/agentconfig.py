"""Client profile generator for MCP hosts.

Produces the exact config each AI coding agent expects, and can optionally write
it (with a backup). Formats here were read off the agents' own config files
where they exist locally; everything else is documented schema.

Deliberately keeps credential material out of logs and out of generated file
*contents* beyond what the agent itself requires.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("mcprelay.agentconfig")

TransportForm = Literal["remote", "stdio-bridge"]


@dataclass(frozen=True)
class ClientProfile:
    """One MCP host: where its config lives and what shape it wants."""

    client_id: str
    display_name: str
    filename: str | None                 # None => print only, no file target
    default_path: str | None
    format: Literal["mcpServers", "vscode-servers", "opencode-mcp"]
    supports_http: bool
    supports_stdio: bool
    notes: str = ""
    path_hint: str = ""


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _resolve_existing(candidates: list[str]) -> str | None:
    """Return the first candidate that already exists.

    Several agents accept more than one config filename (OpenCode reads
    ``opencode.json`` *and* ``opencode.jsonc``). Writing to the wrong one
    creates a second file that the agent merges in, which leaves the stale entry
    in the file that is actually being read. Preferring the file that already
    exists avoids that split.
    """
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


PROFILES: dict[str, ClientProfile] = {
    "opencode": ClientProfile(
        client_id="opencode",
        display_name="OpenCode",
        filename="opencode.json",
        # OpenCode reads opencode.json *and* opencode.jsonc. resolved at
        # render time so we write into whichever already exists rather than
        # creating a competing file that leaves the real one stale.
        default_path=_resolve_existing(
            [
                str(_home() / ".config" / "opencode" / "opencode.jsonc"),
                str(_home() / ".config" / "opencode" / "opencode.json"),
                str(_home() / ".config" / "opencode" / "config.json"),
            ]
        )
        or str(_home() / ".config" / "opencode" / "opencode.json"),
        format="opencode-mcp",
        supports_http=True,
        supports_stdio=False,
        notes="Under the top-level 'mcp' key. Reads opencode.jsonc and opencode.json.",
    ),
    "cursor": ClientProfile(
        client_id="cursor",
        display_name="Cursor",
        filename="mcp.json",
        default_path=str(_home() / ".cursor" / "mcp.json"),
        format="mcpServers",
        supports_http=True,
        supports_stdio=False,
        notes="Cursor reads ~/.cursor/mcp.json.",
    ),
    "claude_code": ClientProfile(
        client_id="claude_code",
        display_name="Claude Code",
        filename="settings.json",
        default_path=str(_home() / ".claude" / "settings.json"),
        format="mcpServers",
        supports_http=True,
        supports_stdio=False,
        notes=(
            "Also accepts a project-local .mcp.json. Prefer the user scope; a "
            "committed .mcp.json would carry the gateway key into the repo."
        ),
        path_hint="~/.claude/settings.json  (or <project>/.mcp.json)",
    ),
    "claude_desktop": ClientProfile(
        client_id="claude_desktop",
        display_name="Claude Desktop",
        filename="claude_desktop_config.json",
        default_path="",
        format="mcpServers",
        supports_http=False,
        supports_stdio=True,
        notes=(
            "stdio-only. Needs a real path - find it via Help > Troubleshooting, "
            "or %APPDATA%\\Claude\\ on Windows. Must be fully quit before editing."
        ),
    ),
    "windsurf": ClientProfile(
        client_id="windsurf",
        display_name="Windsurf",
        filename="mcp_config.json",
        default_path=str(_home() / ".codeium" / "windsurf" / "mcp_config.json"),
        format="mcpServers",
        supports_http=True,
        supports_stdio=False,
    ),
    "vscode": ClientProfile(
        client_id="vscode",
        display_name="VS Code (Continue / Cline)",
        filename="mcp.json",
        default_path="",
        format="vscode-servers",
        supports_http=True,
        supports_stdio=False,
        notes=(
            "Uses a 'servers' key, not 'mcpServers'. Path depends on the "
            "extension: .continue/config.json, or a VS Code mcp.json."
        ),
        path_hint=".vscode/mcp.json  or  ~/.continue/config.json",
    ),
    "generic": ClientProfile(
        client_id="generic",
        display_name="Any Streamable HTTP MCP client",
        filename=None,
        default_path=None,
        format="mcpServers",
        supports_http=True,
        supports_stdio=False,
        notes="The common 'mcpServers' + url + headers shape.",
    ),
}


@dataclass
class RenderedConfig:
    client_id: str
    display_name: str
    transport: TransportForm
    server_key: str
    config: dict[str, Any]
    target_path: str | None
    notes: str
    supported: bool = True
    reason: str = ""

    def to_json(self) -> str:
        return json.dumps(self.config, indent=2)


def slugify(name: str) -> str:
    """Server key for the target agent: lowercase, underscores, no punctuation."""
    out = []
    prev_sep = False
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        elif not prev_sep:
            out.append("_")
            prev_sep = True
    return "".join(out).strip("_") or "mcprelay"


def render(
    client_id: str,
    server_name: str,
    url: str,
    api_key: str,
    *,
    radar: bool = False,
) -> RenderedConfig:
    """Build the config document one client expects."""
    profile = PROFILES.get(client_id)
    if profile is None:
        raise KeyError(
            f"unknown client {client_id!r}; known: {', '.join(sorted(PROFILES))}"
        )

    key = slugify(server_name)
    target_url = url.rstrip("/")
    if radar:
        target_url = f"{target_url}/radar"

    remote_entry: dict[str, Any] = {"url": target_url, "headers": {"X-API-Key": api_key}}

    if profile.format == "vscode-servers":
        entry: dict[str, Any] = {"type": "http", **remote_entry}
        config: dict[str, Any] = {"servers": {key: entry}}
    elif profile.format == "opencode-mcp":
        entry = {"type": "remote", **remote_entry, "enabled": True}
        config = {"mcp": {key: entry}}
    else:
        entry = dict(remote_entry)
        config = {"mcpServers": {key: entry}}

    supported, reason = True, ""
    if not profile.supports_http:
        supported, reason = False, (
            f"{profile.display_name} only speaks stdio; use the mcp-remote bridge "
            f"form (see --transport stdio-bridge)"
        )

    return RenderedConfig(
        client_id=client_id,
        display_name=profile.display_name,
        transport="remote",
        server_key=key,
        config=config,
        target_path=profile.default_path or None,
        notes=profile.notes,
        supported=supported,
        reason=reason,
    )


def render_stdio_bridge(
    client_id: str,
    server_name: str,
    url: str,
    api_key: str,
    *,
    radar: bool = False,
) -> RenderedConfig:
    """Bridge form for stdio-only hosts, via the ``mcp-remote`` npm package."""
    profile = PROFILES.get(client_id)
    if profile is None:
        raise KeyError(f"unknown client {client_id!r}")

    key = slugify(server_name)
    target_url = url.rstrip("/") + ("/radar" if radar else "")
    entry = {
        "command": "npx",
        "args": [
            "-y",
            "mcp-remote",
            target_url,
            "--header",
            f"X-API-Key: {api_key}",
        ],
    }
    if profile.format == "opencode-mcp":
        # OpenCode splits the command from its arguments.
        config = {
            "mcp": {
                key: {
                    "type": "local",
                    "command": entry["command"],
                    "arguments": entry["args"][1:],
                    "enabled": True,
                }
            }
        }
    elif profile.format == "vscode-servers":
        config = {
            "servers": {
                key: {
                    "type": "stdio",
                    "command": entry["command"],
                    "args": entry["args"],
                }
            }
        }
    else:
        config = {"mcpServers": {key: entry}}

    return RenderedConfig(
        client_id=client_id,
        display_name=profile.display_name,
        transport="stdio-bridge",
        server_key=key,
        config=config,
        target_path=profile.default_path or None,
        notes=profile.notes,
        supported=profile.supports_stdio,
        reason="" if profile.supports_stdio else "host does not support stdio",
    )


# ------------------------------------------------------------------- writing


def _strip_jsonc(text: str) -> str:
    """Best-effort removal of // and /* */ comments and trailing commas.

    Agent configs are frequently JSONC (OpenCode's is). A naive json.load would
    fail on them and we would clobber a file we could not otherwise parse.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1

    cleaned = "".join(out)
    # drop trailing commas before } or ]
    result: list[str] = []
    j = 0
    while j < len(cleaned):
        if cleaned[j] == ",":
            k = j + 1
            while k < len(cleaned) and cleaned[k] in " \t\r\n":
                k += 1
            if k < len(cleaned) and cleaned[k] in "}]":
                j += 1
                continue
        result.append(cleaned[j])
        j += 1
    return "".join(result)


def load_existing(path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse an existing config, tolerating JSONC.

    Returns ``(document, report)``. ``report["stripped"]`` is True when
    comments or trailing commas had to be removed - the caller should surface
    that, because we re-serialise from the parsed object and genuine ``//``
    comments will not come back.
    """
    report = {"stripped": False, "format": "json"}
    if not os.path.exists(path):
        return {}, report

    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    if not raw.strip():
        return {}, report

    try:
        return json.loads(raw), report
    except json.JSONDecodeError:
        pass

    cleaned = _strip_jsonc(raw)
    try:
        document = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not parseable as JSON or JSONC ({exc}). Refusing to "
            f"overwrite - fix or move the file first."
        ) from exc

    report["stripped"] = cleaned != raw
    report["format"] = "jsonc"
    return document, report


def merge_into(
    existing: dict[str, Any], rendered: RenderedConfig, profile: ClientProfile
) -> dict[str, Any]:
    """Insert the server entry, preserving everything already present."""
    merged = json.loads(json.dumps(existing))  # deep copy
    if profile.format == "opencode-mcp":
        merged.setdefault("mcp", {})
        merged["mcp"][rendered.server_key] = rendered.config["mcp"][rendered.server_key]
    elif profile.format == "vscode-servers":
        merged.setdefault("servers", {})
        merged["servers"][rendered.server_key] = rendered.config["servers"][rendered.server_key]
    else:
        merged.setdefault("mcpServers", {})
        merged["mcpServers"][rendered.server_key] = (
            rendered.config["mcpServers"][rendered.server_key]
        )
    return merged


def write_config(rendered: RenderedConfig, target_path: str | None = None) -> dict[str, Any]:
    """Write a rendered config to disk, backing up whatever was there.

    Backs up unconditionally when the destination already exists, so an
    accidental overwrite is always reversible.
    """
    profile = PROFILES[rendered.client_id]
    destination = target_path or rendered.target_path
    if not destination:
        raise ValueError(
            f"{profile.display_name} has no default config path - pass an explicit "
            f"target_path (see profile notes: {profile.notes})"
        )

    directory = os.path.dirname(os.path.abspath(destination))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)

    backup_path = None
    if os.path.exists(destination):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{destination}.mcprelay-backup-{stamp}"
        shutil.copy2(destination, backup_path)
        log.info("backed up %s -> %s", destination, backup_path)

    existing, strip_report = load_existing(destination)
    merged = merge_into(existing, rendered, profile)

    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")

    return {
        "path": destination,
        "backup_path": backup_path,
        "created": backup_path is None,
        "server_key": rendered.server_key,
        "preserved_top_level_keys": sorted(k for k in merged if k not in rendered.config),
        "jsonc_comments_dropped": strip_report.get("stripped", False),
    }


def client_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "client_id": p.client_id,
            "display_name": p.display_name,
            "format": p.format,
            "supports_http": p.supports_http,
            "supports_stdio": p.supports_stdio,
            "default_path": p.default_path or None,
            "path_hint": p.path_hint or None,
            "notes": p.notes,
        }
        for p in sorted(PROFILES.values(), key=lambda p: p.display_name)
    ]
