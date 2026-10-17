"""Rebuild folder "03" of the collection as a clear 3-step flow.

Folder 03 previously held five requests, two of which were byte-identical
(GET client-config and POST install with dry_run:true), and none of the names
said whether they wrote anything. That ambiguity is the whole problem.

This replaces it with three numbered steps where the verb tells you the blast
radius:

    1. WHO      list clients        read-only, informational
    2. PREVIEW  preview config      read-only, shows JSON, writes nothing
    3. WRITE    write config        BUILDS UP A FILE (backs up first)

The Claude Desktop stdio-bridge case is folded into step 3 as a body option
rather than being its own near-duplicate request.

Run:  python rebuild_folder03.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, "mcprelay.postman_collection.json")

FOLDER_NAME = "03 - Attach an agent"


def folder() -> dict:
    return {
        "name": FOLDER_NAME,
        "description": (
            "Point an AI coding agent at a gateway. Three steps, and the verb "
            "tells you the blast radius.\n\n"
            "| # | Request | Reads? | Writes? |\n|---|---|---|---|\n"
            "| 1 | List supported clients | yes | no |\n"
            "| 2 | PREVIEW config | yes | **no** |\n"
            "| 3 | WRITE config | yes | **YES - agent config file** |\n\n"
            "Steps 2 and 3 take the same `client_id` path parameter, so step 2 is "
            "always safe and step 3 is the only one that changes anything.\n\n"
            "### Decision tree\n\n"
            "```\n"
            "Need to know which agents are supported?   -> 1\n"
            "Want to see the JSON before committing?     -> 2\n"
            "Happy with it?                              -> 3, then restart the agent\n"
            "```\n\n"
            "### Which client_id?\n\n"
            "`opencode`, `cursor`, `claude_code`, `claude_desktop`, `windsurf`, "
            "`vscode`, `generic`. Run step 1 for the current list.\n\n"
            "### Claude Desktop is different\n\n"
            "It is stdio-only, so step 3 needs `\"transport\": \"stdio-bridge\"` "
            "in the body. That generates an `npx -y mcp-remote` bridge instead of "
            "a direct URL. It also has no default config path, so you must supply "
            "`target_path` - and quit the app first, or it overwrites your change "
            "on exit."
        ),
        "item": [
            # ---------------------------------------------------------- 1
            {
                "name": "1. List supported clients",
                "event": [
                    {
                        "listen": "test",
                        "script": {
                            "type": "text/javascript",
                            "exec": [
                                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                                "const body = pm.response.json();",
                                "",
                                "console.log('--- agents you can target ---');",
                                "body.clients.forEach(c => {",
                                "    const path = c.default_path || c.path_hint || '(needs target_path)';",
                                "    const caps = [",
                                "        c.supports_http ? 'remote' : null,",
                                "        c.supports_stdio ? 'stdio' : null",
                                "    ].filter(Boolean).join(' or ');",
                                "    console.log(`  ${c.client_id.padEnd(16)} ${caps.padEnd(11)} ${c.format}`);",
                                "    console.log(`      path: ${path}`);",
                                "    if (c.notes) console.log(`      note: ${c.notes}`);",
                                "});",
                                "",
                                "// Point step 2/3 at the first agent that needs no target_path.",
                                "const easy = body.clients.find(c => c.default_path);",
                                "if (easy) pm.collectionVariables.set('client_id', easy.client_id);",
                                "",
                                "console.log('\\nset {{client_id}} = ' +",
                                "    pm.collectionVariables.get('client_id'));",
                            ],
                        },
                    }
                ],
                "request": {
                    "method": "GET",
                    "header": [],
                    "url": {
                        "raw": "{{base_url}}/api/v1/clients",
                        "host": ["{{base_url}}"],
                        "path": ["api", "v1", "clients"],
                    },
                    "description": (
                        "**Step 1 - reads only.** Which agents mcprelay can generate a "
                        "config for, the format each expects, and where its config "
                        "lives.\n\n"
                        "The test script prints that as a table and sets "
                        "`{{client_id}}` to the first agent with a known default path, "
                        "so steps 2 and 3 usually need no editing."
                    ),
                    "descriptionFormat": "markdown",
                },
                "response": [
                    {
                        "name": "ok",
                        "originalRequest": {
                            "method": "GET",
                            "url": {
                                "raw": "{{base_url}}/api/v1/clients",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "clients"],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "clients": [
                                        {
                                            "client_id": "opencode",
                                            "display_name": "OpenCode",
                                            "format": "opencode-mcp",
                                            "supports_http": True,
                                            "supports_stdio": False,
                                            "default_path": "C:\\Users\\you\\.config\\opencode\\opencode.jsonc",
                                            "notes": "Under the top-level 'mcp' key. Reads opencode.jsonc and opencode.json.",
                                        },
                                        {
                                            "client_id": "cursor",
                                            "display_name": "Cursor",
                                            "format": "mcpServers",
                                            "supports_http": True,
                                            "supports_stdio": False,
                                            "default_path": "C:\\Users\\you\\.cursor\\mcp.json",
                                            "notes": "Cursor reads ~/.cursor/mcp.json.",
                                        },
                                        {
                                            "client_id": "vscode",
                                            "display_name": "VS Code (Continue / Cline)",
                                            "format": "vscode-servers",
                                            "supports_http": True,
                                            "supports_stdio": False,
                                            "default_path": None,
                                            "path_hint": ".vscode/mcp.json  or  ~/.continue/config.json",
                                            "notes": "Uses a 'servers' key, not 'mcpServers'.",
                                        },
                                        {
                                            "client_id": "claude_desktop",
                                            "display_name": "Claude Desktop",
                                            "format": "mcpServers",
                                            "supports_http": False,
                                            "supports_stdio": True,
                                            "default_path": "",
                                            "notes": "stdio-only. Needs a real path. Must be fully quit before editing.",
                                        },
                                    ]
                                },
                                indent=2,
                            ),
                        },
                    }
                ],
            },
            # ---------------------------------------------------------- 2
            {
                "name": "2. PREVIEW config (safe)",
                "event": [
                    {
                        "listen": "test",
                        "script": {
                            "type": "text/javascript",
                            "exec": [
                                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                                "const body = pm.response.json();",
                                "",
                                "pm.test('this request cannot write', function () {",
                                "    pm.expect(body).to.not.have.property('written');",
                                "});",
                                "",
                                "console.log('='.repeat(62));",
                                "console.log('PREVIEW ONLY - nothing was written');",
                                "console.log('='.repeat(62));",
                                "console.log('agent     : ' + body.display_name);",
                                "console.log('transport : ' + body.transport);",
                                "console.log('server key: ' + body.server_key);",
                                "console.log('would write to: ' +",
                                "    (body.target_path || '(none - set target_path in step 3)'));",
                                "if (!body.supported) {",
                                "    console.log('');",
                                "    console.log('!! ' + body.reason);",
                                "}",
                                "console.log('');",
                                "console.log(body.config_json);",
                                "",
                                "console.log('Happy with it? Run step 3 with the same",
                                "    client_id and dry_run: false.');",
                            ],
                        },
                    }
                ],
                "request": {
                    "method": "GET",
                    "header": [],
                    "url": {
                        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}/client-config/{{client_id}}",
                        "host": ["{{base_url}}"],
                        "path": [
                            "api", "v1", "gateways", "{{active_gateway_id}}",
                            "client-config", "{{client_id}}",
                        ],
                    },
                    "description": (
                        "**Step 2 - reads only. Cannot write anything.**\n\n"
                        "Shows the exact JSON an agent would receive, for the "
                        "gateway and agent you select. Run this as often as you like.\n\n"
                        "Same `client_id` as step 3, so what you approve here is "
                        "what step 3 writes.\n\n"
                        "`supported: false` means that agent cannot use the direct "
                        "HTTP form - read `reason`. For Claude Desktop, go to step 3 "
                        "with `\"transport\": \"stdio-bridge\"`."
                    ),
                    "descriptionFormat": "markdown",
                },
                "response": [
                    {
                        "name": "cursor",
                        "originalRequest": {
                            "method": "GET",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/client-config/cursor",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "client-config", "cursor",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "cursor",
                                    "display_name": "Cursor",
                                    "transport": "remote",
                                    "supported": True,
                                    "reason": "",
                                    "server_key": "my_coding_agent",
                                    "target_path": "C:\\Users\\you\\.cursor\\mcp.json",
                                    "notes": "Cursor reads ~/.cursor/mcp.json.",
                                    "config": {
                                        "mcpServers": {
                                            "my_coding_agent": {
                                                "url": "http://127.0.0.1:8787/gateway/f30a1e56f1e1/mcp",
                                                "headers": {
                                                    "X-API-Key": "mcr_9CryGi4UClk..."
                                                },
                                            }
                                        }
                                    },
                                },
                                indent=2,
                            ),
                        },
                    },
                    {
                        "name": "opencode (different shape)",
                        "originalRequest": {
                            "method": "GET",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/client-config/opencode",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "client-config", "opencode",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "opencode",
                                    "display_name": "OpenCode",
                                    "transport": "remote",
                                    "supported": True,
                                    "server_key": "my_coding_agent",
                                    "target_path": "C:\\Users\\you\\.config\\opencode\\opencode.jsonc",
                                    "config": {
                                        "mcp": {
                                            "my_coding_agent": {
                                                "type": "remote",
                                                "url": "http://127.0.0.1:8787/gateway/f30a1e56f1e1/mcp",
                                                "headers": {
                                                    "X-API-Key": "mcr_9CryGi4UClk..."
                                                },
                                                "enabled": True,
                                            }
                                        }
                                    },
                                },
                                indent=2,
                            ),
                        },
                    },
                    {
                        "name": "claude_desktop (unsupported for direct http)",
                        "originalRequest": {
                            "method": "GET",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/client-config/claude_desktop",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "client-config", "claude_desktop",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "claude_desktop",
                                    "display_name": "Claude Desktop",
                                    "transport": "remote",
                                    "supported": False,
                                    "reason": "Claude Desktop only speaks stdio; use the mcp-remote bridge form (see --transport stdio-bridge)",
                                    "server_key": "my_coding_agent",
                                    "target_path": None,
                                    "notes": "stdio-only. Needs a real path. Must be fully quit before editing.",
                                },
                                indent=2,
                            ),
                        },
                    },
                ],
            },
            # ---------------------------------------------------------- 3
            {
                "name": "3. WRITE config (changes a file)",
                "event": [
                    {
                        "listen": "test",
                        "script": {
                            "type": "text/javascript",
                            "exec": [
                                "const body = pm.response.json();",
                                "",
                                "if (body.dry_run === true) {",
                                "    console.log('DRY RUN - nothing written.');",
                                "    console.log('set \"dry_run\": false and send again to apply.');",
                                "    return;",
                                "}",
                                "",
                                "console.log('='.repeat(62));",
                                "console.log('WROTE ' + body.display_name.toUpperCase() + ' CONFIG');",
                                "console.log('='.repeat(62));",
                                "console.log('file   : ' + body.path);",
                                "if (body.created) {",
                                "    console.log('         (new file, nothing to back up)');",
                                "} else if (body.backup_path) {",
                                "    console.log('backup : ' + body.backup_path);",
                                "}",
                                "if (body.preserved_top_level_keys && body.preserved_top_level_keys.length) {",
                                "    console.log('kept   : ' +",
                                "        body.preserved_top_level_keys.join(', '));",
                                "}",
                                "if (body.jsonc_comments_dropped) {",
                                "    console.log('');",
                                "    console.log('note: file was JSONC, // comments were stripped.');",
                                "    console.log('      the backup holds the original.');",
                                "}",
                                "console.log('');",
                                "console.log('NEXT: fully quit and restart ' + body.display_name + '.');",
                            ],
                        },
                    }
                ],
                "request": {
                    "method": "POST",
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "url": {
                        "raw": "{{base_url}}/api/v1/gateways/{{active_gateway_id}}/install/{{client_id}}",
                        "host": ["{{base_url}}"],
                        "path": [
                            "api", "v1", "gateways", "{{active_gateway_id}}",
                            "install", "{{client_id}}",
                        ],
                    },
                    "body": {
                        "mode": "raw",
                        "raw": json.dumps(
                            {
                                "dry_run": False,
                                "transport": "remote",
                                "radar": False,
                                "target_path": None,
                                "base_url": None,
                            },
                            indent=2,
                        ),
                    },
                    "description": (
                        "**Step 3 - THE ONLY REQUEST THAT WRITES ANYTHING.**\n\n"
                        "Merges the config into the agent's own config file, backing "
                        "it up first.\n\n"
                        "### Safe because it\n\n"
                        "- **always backs up** an existing file to "
                        "`<path>.mcprelay-backup-<UTC timestamp>`\n"
                        "- **deep-merges** - unrelated keys and other MCP servers "
                        "survive\n"
                        "- **replaces** an entry with the same server key instead of "
                        "duplicating it\n"
                        "- **handles JSONC** - parses OpenCode's `opencode.jsonc`, "
                        "and reports `jsonc_comments_dropped` honestly\n"
                        "- **writes into the file that already exists** - OpenCode "
                        "reads both `opencode.jsonc` and `opencode.json`, and "
                        "mcprelay picks the present one rather than creating a "
                        "competing file\n"
                        "- **refuses** unparseable files with `400`, leaving them "
                        "untouched\n\n"
                        "### Body\n\n"
                        "| Field | Default | Purpose |\n|---|---|---|\n"
                        "| `dry_run` | `true` | **Set to `false` to write** |\n"
                        "| `transport` | `remote` | `stdio-bridge` for stdio-only hosts |\n"
                        "| `radar` | `false` | Use `/radar` instead of `/mcp` |\n"
                        "| `target_path` | agent default | Override the destination |\n"
                        "| `base_url` | bound host | Full MCP URL, for tunnels |\n\n"
                        "All nullable - `null` means \"use the default\".\n\n"
                        "### Suggested first send\n\n"
                        "Leave `\"dry_run\": true`, confirm the output matches step 2, "
                        "then flip it to `false`.\n\n"
                        "### Claude Desktop\n\n"
                        "Use `\"transport\": \"stdio-bridge\"` and supply "
                        "`target_path` - it has no default. Quit the app completely "
                        "first or it will overwrite your change on exit."
                    ),
                    "descriptionFormat": "markdown",
                },
                "response": [
                    {
                        "name": "written (with backup)",
                        "originalRequest": {
                            "method": "POST",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/install/cursor",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "install", "cursor",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "cursor",
                                    "display_name": "Cursor",
                                    "transport": "remote",
                                    "supported": True,
                                    "server_key": "my_coding_agent",
                                    "target_path": "C:\\Users\\you\\.cursor\\mcp.json",
                                    "written": True,
                                    "path": "C:\\Users\\you\\.cursor\\mcp.json",
                                    "backup_path": "C:\\Users\\you\\.cursor\\mcp.json.mcprelay-backup-20261016T111001Z",
                                    "created": False,
                                    "preserved_top_level_keys": ["$schema", "editor"],
                                    "jsonc_comments_dropped": False,
                                },
                                indent=2,
                            ),
                        },
                    },
                    {
                        "name": "dry run (nothing written)",
                        "originalRequest": {
                            "method": "POST",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/install/cursor",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "install", "cursor",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "cursor",
                                    "display_name": "Cursor",
                                    "transport": "remote",
                                    "supported": True,
                                    "server_key": "my_coding_agent",
                                    "target_path": "C:\\Users\\you\\.cursor\\mcp.json",
                                    "dry_run": True,
                                    "written": False,
                                },
                                indent=2,
                            ),
                        },
                    },
                    {
                        "name": "no path known for this agent (400)",
                        "originalRequest": {
                            "method": "POST",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/install/vscode",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "install", "vscode",
                                ],
                            },
                        },
                        "status": "Bad Request",
                        "code": 400,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "error": "VS Code (Continue / Cline) has no default config path - pass an explicit target_path (see profile notes: Uses a 'servers' key, not 'mcpServers'. Path depends on the extension: .continue/config.json, or a VS Code mcp.json.)"
                                },
                                indent=2,
                            ),
                        },
                    },
                    {
                        "name": "claude_desktop via stdio bridge",
                        "originalRequest": {
                            "method": "POST",
                            "url": {
                                "raw": "{{base_url}}/api/v1/gateways/f30a1e56f1e1/install/claude_desktop",
                                "host": ["{{base_url}}"],
                                "path": [
                                    "api", "v1", "gateways", "f30a1e56f1e1",
                                    "install", "claude_desktop",
                                ],
                            },
                        },
                        "status": "OK",
                        "code": 200,
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps(
                                {
                                    "client_id": "claude_desktop",
                                    "display_name": "Claude Desktop",
                                    "transport": "stdio-bridge",
                                    "supported": True,
                                    "server_key": "my_coding_agent",
                                    "target_path": None,
                                    "config": {
                                        "mcpServers": {
                                            "my_coding_agent": {
                                                "command": "npx",
                                                "args": [
                                                    "-y",
                                                    "mcp-remote",
                                                    "http://127.0.0.1:8787/gateway/f30a1e56f1e1/mcp",
                                                    "--header",
                                                    "X-API-Key: mcr_9CryGi4UClk...",
                                                ],
                                            }
                                        }
                                    },
                                    "dry_run": True,
                                    "written": False,
                                },
                                indent=2,
                            ),
                        },
                    },
                ],
            },
        ],
    }


def main() -> int:
    coll = json.load(open(COLLECTION, encoding="utf-8"))

    # Drop any previously generated variant so re-running is idempotent.
    before = len(coll["item"])
    coll["item"] = [
        item
        for item in coll["item"]
        if not str(item.get("name", "")).startswith("03")
    ]
    removed = before - len(coll["item"])

    # Renumber: 00/01/02 stay, 03 is appended in order.
    order = ["00", "01", "02"]
    kept = {n.split(" ", 1)[0]: item for n, item in
            ((i["name"], i) for i in coll["item"])}
    coll["item"] = [kept[k] for k in order if k in kept] + [folder()]

    with open(COLLECTION, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(coll, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"removed {removed} old folder-03 entr{'y' if removed == 1 else 'ies'}")
    print()
    print("collection now:")
    for f in coll["item"]:
        reqs = f.get("item", [])
        print(f"  {f['name']}  ({len(reqs)} requests)")
        for r in reqs:
            method = r["request"]["method"]
            url = (r["request"].get("url") or {}).get("raw", "?")
            print(f"      {method:<6} {r['name']:<34} {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
