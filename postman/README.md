# mcprelay Postman collection

REST control-plane collection for the local MCP gateway, plus an environment
file and two scripts that keep it honest.

| File | What it is |
|---|---|
| `mcprelay.postman_collection.json` | The collection. v2.1 schema, 17 requests, 4 folders, 21 saved examples |
| `local.postman_environment.json` | Environment holding `base_url` |
| `fix_collection.py` | Repairs missing `url` blocks / undeclared variables, then validates |
| `smoke_test.py` | Issues every request against a live server and reports pass/fail |

---

## Import

**Postman desktop**

1. Open Postman → **Import**
2. Drag in `mcprelay.postman_collection.json`
3. Top-right environment picker → **Import** → `local.postman_environment.json`
4. Select the `localhost` environment (active by default)

**Postman CLI**

```bash
newman run mcprelay.postman_collection.json \
  --environment local.postman_environment.json \
  --reporters cli
```

---

## What this collection cannot do

It drives the **REST control plane only** — gateway CRUD and agent-config
install. The **MCP protocol** is not REST: an MCP client connects to

```
{{base_url}}/gateway/{{active_gateway_id}}/mcp
```

and speaks JSON-RPC with an `X-API-Key` header. Postman cannot speak that.
Use Cursor, Claude Code, OpenCode, VS Code, Windsurf, or
`python -m mcprelay call`.

---

## Suggested order

| # | Request | What it does |
|---|---|---|
| 1 | **How to use this collection** | Orientation, no side effects |
| 2 | **Health check** | Is it up, which upstreams are healthy |
| 3 | **List upstreams** | Every tool discovered per upstream |
| 4 | **List gateways** | Adopted automatically — no manual variable editing needed |
| 5 | **Create gateway** | ⚡ returns the API key **once** |
| 6 | **Get gateway** | Resolved tool list after allow-list filtering |
| 7 | **Gateway tools** | The exact set a client sees (`?full=true` for schemas) |
| 8 | **Tool choices** | Grouped per upstream, for choosing an allow-list |
| 9 | **Preview client config** | Shows the JSON, writes nothing |
| 10 | **Install (DRY RUN)** | ⚠️ defaults to safe; confirm before the next call |
| 11 | **Install (WRITE)** | 🔴 backs up, deep-merges, then writes |

---

## Variables

### Environment

| Key | Default |
|---|---|
| `base_url` | `http://127.0.0.1:8787` |

### Collection

| Key | Default | Set by |
|---|---|---|
| `gateway_id` | `dev` | you — a config-declared gateway |
| `runtime_gateway_id` | *(empty)* | **Create gateway**, or List gateways' test script |
| `active_gateway_id` | *(empty)* | collection pre-request script; prefers runtime over config |
| `gateway_api_key` | *(empty)* | **Create gateway** and **Rotate key** |
| `client_id` | `cursor` | you — `opencode`, `cursor`, `claude_code`, `claude_desktop`, `windsurf`, `vscode`, `generic` |
| `upstream_key` | `memory` | you — for reconnect |

You normally set nothing by hand: a prerequest script resolves
`active_gateway_id`, and **Create gateway** captures the id and key. Click
**Create gateway** first and everything downstream lines up.

---

## Test scripts worth knowing about

These run in the Postman console, so they print rather than just assert.

| Request | Prints |
|---|---|
| **Health check** | Which upstreams are not ready, with their error |
| **List upstreams** | Tool names per upstream |
| **List gateways** | Id, tool count, source; **asserts no `mcr_` key leaks anywhere** |
| **Create gateway** | The new id and key in a boxed banner |
| **Tool choices** | Every tool with an `[x]`/`[ ]` allowed marker — for picking a filter |
| **Preview client config** | The rendered JSON |
| **Install (WRITE)** | Destination, backup path, preserved keys, JSONC warning |
| **Rotate key** | The new key, plus a reminder that old configs now 401 |

Every request also asserts two global invariants: response under 5s, and the
response is **not an HTML error page**.

---

## Safety

- **Install defaults to `dry_run: true`.** You must set it to `false` to write.
- Writes **always back up** an existing file first, to
  `<path>.mcprelay-backup-<UTC timestamp>`.
- Writes **deep-merge**: unrelated keys and other MCP servers survive. Writing
  a server key that already exists **replaces** it rather than duplicating.
- JSONC files (OpenCode's `opencode.jsonc`) parse; `//` comments and trailing
  commas are handled, and the response reports `jsonc_comments_dropped: true`
  when real comments were lost. The backup holds them.
- **Unparseable files are refused** with `400` and left untouched.
- **Rotate key invalidates the old key immediately.** Reinstall every client
  config that used it, or those agents get `401`.

---

## Scripts

### `smoke_test.py` — verify the collection against a live server

```bash
python smoke_test.py                      # read-only
python smoke_test.py --destructive        # include create/patch/rotate/delete/write
python smoke_test.py --base http://127.0.0.1:9000
```

Resolves every `{{variable}}`, issues the request, and reports. Treats
`400`/`409` as expected where the request shape warrants it; only `404`, `405`
and `5xx` fail.

Under `--destructive` it **provisions its own runtime gateway** first, because
`rotate-key`, `patch` and `delete` only apply to runtime gateways — without one
they'd 404 against a config-declared gateway, which is correct but untestable.

### `fix_collection.py` — repair and validate

```bash
python fix_collection.py             # repair, then validate
python fix_collection.py --check-only
```

Adds `url` blocks to any request missing one, declares undeclared `{{variable}}`
references, and reports the resulting structure. Useful after hand-editing.

---

## Verified state

Both files parse as strict JSON, all `{{variable}}` references resolve, and all
17 requests were exercised against a running server:

```
17 passed, 0 failed   (--destructive)
17 passed, 10 skipped (read-only)
```

Regenerate or re-verify after changing the API:

```bash
python fix_collection.py --check-only
python smoke_test.py --destructive
```
