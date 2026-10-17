# mcprelay — a local MCP gateway

A config-driven gateway that aggregates several upstream MCP servers behind one
endpoint, namespaces their tools, enforces a per-gateway allow-list, and speaks
the modern stateless MCP `2026-07-28` protocol.

Modelled on the gateway concept at `stormmcp.ai`, minus everything that only
matters for a multi-tenant SaaS: no database, no admin UI, no OAuth broker.

```
mcp client ──X-API-Key──▶ /gateway/dev/mcp        (this project)
                                │
                                ├──▶ stdio subprocess   filesystem, memory, git
                                ├──▶ Streamable HTTP    github, notion
                                └──▶ legacy SSE         older servers
```

---

## Requirements

- Python 3.11+
- Node.js on `PATH` (the bundled example uses `npx` stdio servers)
- `uv`/`uvx` if you enable the optional `git` upstream

```powershell
pip install -r requirements.txt
```

> **Pin note.** `mcp>=2.0` is required. Version 1.x targets the legacy
> handshake-era protocol. If `pip install fastapi` fights you, note that the MCP
> SDK pulls in Starlette 1.x, which needs `fastapi>=0.140`.

---

## Quick start

```powershell
Copy-Item config.example.yaml config.local.yaml

# 1. check every upstream connects
python -m mcprelay doctor -c config.local.yaml

# 2. see what the gateway would expose
python -m mcprelay tools -c config.local.yaml -g dev

# 3. run it
python -m mcprelay serve -c config.local.yaml
```

Endpoints come up on `http://127.0.0.1:8787`:

| URL | What |
|---|---|
| `/gateway/dev/mcp` | the `dev` gateway's MCP endpoint |
| `/gateway/dev/radar` | same server, Radar-style entry |
| `/gateway/curated/mcp` | a second gateway with a tighter allow-list |
| `/healthz` | upstream states, tool counts, protocol versions |
| `/docs` | interactive control-plane reference |

---

## Create a gateway and hand it to an AI coding agent

This is the main flow. Server running in one terminal:

```powershell
python -m mcprelay serve -c config.local.yaml
```

### 1. Create the gateway

```powershell
python -m mcprelay create -c config.local.yaml -n "my coding agent" `
    -u filesystem -u memory -u sequential-thinking
```

```
  gateway created: 3d18f57f8416
  name      my coding agent
  endpoint  /gateway/3d18f57f8416/mcp
  upstreams filesystem, memory, sequential-thinking
  tools     24
  key       mcr_2ON1PGXEeknHUReFImCYCHjMgVrsMgl9z2eNmMz5ccQ

  This key is shown once. Store it now.
```

The gateway is live immediately — no restart. It persists to `var/gateways.json`
and is restored on the next start.

To restrict what it exposes:

```powershell
python -m mcprelay create -n "readonly agent" -u filesystem -u memory `
    --tool-filter '!memory.delete_*' --tool-filter '*'
```

### 2. Generate the client config

```powershell
# preview (prints JSON, writes nothing)
python -m mcprelay install -g 3d18f57f8416 --for cursor

# actually write it (backs up first)
python -m mcprelay install -g 3d18f57f8416 --for cursor --write
```

Supported `--for` values, each rendered in that agent's own format:

| `--for` | Config shape | Path |
|---|---|---|
| `opencode` | `{"mcp": {"<key>": {"type": "remote", …}}}` | `~/.config/opencode/opencode.json` |
| `cursor` | `{"mcpServers": {"<key>": {"url", "headers"}}}` | `~/.cursor/mcp.json` |
| `claude_code` | `{"mcpServers": {…}}` | `~/.claude/settings.json` |
| `claude_desktop` | stdio bridge only | no default path — see below |
| `windsurf` | `{"mcpServers": {…}}` | `~/.codeium/windsurf/mcp_config.json` |
| `vscode` | `{"servers": {"<key>": {"type": "http", …}}}` | extension-dependent |
| `generic` | `{"mcpServers": {…}}` | none — copy manually |

Useful flags:

| Flag | Effect |
|---|---|
| `--write` | write the file (backs up any existing one) |
| `--transport stdio-bridge` | bridge via `npx -y mcp-remote`, for stdio-only hosts |
| `--radar` | point at `/radar` instead of `/mcp` |
| `--target-path PATH` | override the destination |

For hosts with no known path, pass one explicitly:

```powershell
python -m mcprelay install -g 3d18f57f8416 --for claude_desktop `
    --transport stdio-bridge --write `
    --target-path "$env:APPDATA\Claude\claude_desktop_config.json"
```

Quit Claude Desktop fully before writing its config — a running instance can
overwrite it on exit.

### 3. Restart the client

Then confirm the tools arrived:

```powershell
python -m mcprelay ls -c config.local.yaml
python -m mcprelay call -c config.local.yaml -g 3d18f57f8416 -t memory.read_graph
```

### Over HTTP

The same thing with plain REST, if you would rather script it:

```powershell
# create
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8787/api/v1/gateways `
     -ContentType application/json `
     -Body (@{ name="my agent"; upstreams=@("filesystem","memory") } | ConvertTo-Json)
$r.endpoint; $r.api_key

# preview a client config
Invoke-RestMethod "http://127.0.0.1:8787/api/v1/gateways/$($r.id)/client-config/cursor"

# write it
Invoke-RestMethod -Method Post `
     -Uri "http://127.0.0.1:8787/api/v1/gateways/$($r.id)/install/cursor" `
     -ContentType application/json -Body (@{ write=$true } | ConvertTo-Json)
```

### Managing gateways

```powershell
python -m mcprelay ls -c config.local.yaml                  # list
Invoke-RestMethod -Method Post "http://127.0.0.1:8787/api/v1/gateways/<id>/rotate-key"
Invoke-RestMethod -Method Delete "http://127.0.0.1:8787/api/v1/gateways/<id>"
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/gateways` | create (returns the key once) |
| `GET /api/v1/gateways` | list, merged config + runtime |
| `GET /api/v1/gateways/{id}` | detail, incl. resolved tool list |
| `PATCH /api/v1/gateways/{id}` | change allow-list / description budget |
| `POST /api/v1/gateways/{id}/rotate-key` | issue a new key, invalidate the old |
| `DELETE /api/v1/gateways/{id}` | remove a runtime gateway |
| `GET /api/v1/gateways/{id}/tools` | exact merged tool view |
| `GET /api/v1/gateways/{id}/client-config/{client}` | preview a client's config |
| `POST /api/v1/gateways/{id}/install/{client}` | write it (`dry_run` to preview) |
| `GET /api/v1/clients` | supported clients and their formats |

Keys are shown **once**, at creation, and never appear in any listing or in
`/healthz`. If you lose one, rotate.

> Local-only by default. Binding to a LAN interface puts gateways on the
> network behind a single static key each — don't, unless you mean to.

---

## Connecting a client by hand

Every MCP client that speaks Streamable HTTP works:

```jsonc
{
  "mcpServers": {
    "mcprelay-dev": {
      "url": "http://127.0.0.1:8787/gateway/dev/mcp",
      "headers": { "X-API-Key": "dev-local-key" }
    }
  }
}
```

For a stdio-only client, bridge through `mcp-remote`:

```jsonc
{
  "mcpServers": {
    "mcprelay-dev": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8787/gateway/dev/mcp",
               "--header", "X-API-Key: dev-local-key"]
    }
  }
}
```

`Authorization: Bearer <key>` is accepted as an alternative to `X-API-Key`.

---

## Configuration

Full annotated example in [`config.example.yaml`](config.example.yaml).

### Upstreams

```yaml
upstreams:
  memory:
    transport: stdio                  # stdio | streamable-http | sse
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]

  github:
    transport: streamable-http
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "${GITHUB_AUTHORIZATION:}"
    enabled: false                    # flip on once you export the credential
```

The key becomes the tool-name prefix, so it must match `[A-Za-z0-9_-]+`. A dot
would be ambiguous once names are `prefix.tool`.

### Secrets

`${VAR}`, `${env:VAR}`, `${VAR:default}`. A **missing variable with no default
is a hard error** — a typo can never masquerade as an empty credential.

Put the **whole** header value in one variable:

```yaml
Authorization: "${GITHUB_AUTHORIZATION:}"    # good - drops cleanly when unset
Authorization: "Bearer ${GITHUB_TOKEN:}"     # avoids "Bearer " with no secret
```

A bare scheme with no secret is dropped with a warning, so that upstream just
degrades instead of failing with a transport error that never mentions the
missing variable.

### Allow-lists

Evaluated **first-match-wins**, with `!` for negation. No match ⇒ **denied**
(there is no implicit allow-all).

```yaml
tool_filter:
  include:
    - "!memory.delete_*"     # deny these
    - "*"                   # allow the rest
```

Put negations **first** — a trailing `!github.*` after `*` is unreachable.

### Gateways

```yaml
gateways:
  curated:
    api_key: "${MCPRELAY_CURATED_KEY:curated-local-key}"
    upstreams: [filesystem, memory, sequential-thinking]
    tools_ttl_seconds: 120        # advertised as ttlMs (SEP-2549)
    cache_scope: private          # 'public' | 'private'
    tool_filter:
      include: ["!memory.delete_*", "*"]
      description_max_chars: 160  # bound tools/list size
```

Each gateway gets its own MCP server and its own key. Keys must be unique.

---

## CLI

```powershell
python -m mcprelay doctor   -c config.local.yaml     # JSON health, exit 1 if any upstream down
python -m mcprelay tools    -c config.local.yaml -g dev
python -m mcprelay call     -c config.local.yaml -g dev -t memory.read_graph -a query=storm
python -m mcprelay serve    -c config.local.yaml [--host] [--port] [--no-auth]
```

`call` is useful for testing without an MCP client. Values are JSON-coerced, so
`-a limit=5`, `-a flag=true`, `-a obj={"a":1}` all work.

---

## Control plane

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | per-upstream state, tool counts, negotiated protocol versions |
| GET | `/api/v1/upstreams` | health plus every discovered tool per upstream |
| POST | `/api/v1/upstreams/{key}/reconnect` | reconnect one upstream without restarting |
| GET | `/api/v1/gateways/{id}/tool-choices` | per-upstream tool list, for building a selection UI |

Gateway lifecycle and client-config endpoints are tabulated in the
[create-a-gateway section](#create-a-gateway-and-hand-it-to-an-ai-coding-agent).

The control plane is unauthenticated and bound to loopback.

---

## Tests

```powershell
python -m pytest tests\test_units.py tests\test_store_units.py -q   # 102 unit tests

python -m mcprelay serve -c config.local.yaml                       # terminal 1
python tests\e2e_test.py                                           # terminal 2
python tests\e2e_gateway_test.py                                   # terminal 2
```

**102 unit tests** (no network): namespacing, allow-list precedence, config
validation, error flattening, key generation, store persistence and corruption
recovery, JSONC handling, and non-destructive config merging.

**25 end-to-end checks** — a real MCP client over HTTP: auth (missing, wrong,
bearer), namespacing, cross-upstream routing, allow-list enforcement on **both**
`tools/list` and `tools/call`, clean errors, the Radar search tool, `/radar`.

**62 end-to-end checks** — the create-then-connect flow: CRUD, key shown once
and never leaked, key rotation invalidating the old key, a runtime gateway
actually serving MCP, survival across restart, allow-list patching, generated
configs for seven clients, and config writes that back up and preserve.

---

## Design notes

**Why one long-lived connection per upstream.** The `2026-07-28` spec removed
protocol sessions, so a modern upstream would tolerate connect-per-request. But
stdio upstreams are subprocesses, most servers in the wild are still legacy-era,
and the SDK amortises discovery over a held session. The cost is that a crashed
upstream needs explicit recovery — hence `POST /api/v1/upstreams/{key}/reconnect`.

**Why the allow-list is enforced twice.** Filtering `tools/list` without also
rejecting in `tools/call` leaves the call path open. `Registry.call` is the
enforcement point and raises `PermissionError`.

**Why the low-level `Server` instead of FastMCP.** A gateway's tool set is
dynamic — it depends on config and on which upstreams are healthy — so there is
nothing meaningful to bind to a decorator. `on_list_tools` / `on_call_tool`
constructor hooks are the right shape. (In mcp 2.x, FastMCP was renamed
`MCPServer`.)

**`stateless_http=True`.** New sessions are not created per request. This is what
the modern spec enables and it is why the gateway keeps no session store.

**`session_manager.run()` is entered by our lifespan.** `streamable_http_app()`
builds the session manager but does not start it; skipping this yields
`RuntimeError: Task group is not initialized` on the first request.

**Isolation over resilience-by-refusal.** One bad upstream must never stop the
others. Connections are gathered with `return_exceptions=True` and each failure
is recorded on that upstream alone. The gateway serves whatever is healthy and
reports the rest via `/healthz`.

**Runtime gateways are mounted, not rebuilt.** Creating one appends a Starlette
mount and an auth rule while the server is running, and enters that gateway's
session manager in the existing lifespan stack. Keys are generated in
`store.py` only, returned once at creation, and never appear in a listing.

**Auth fails closed.** A path under `/gateway/` that matches no registered rule
is rejected rather than served. Combined with rotating the auth rule in place on
key change, a revoked key stops working immediately and a new one works
immediately.

---

## Layout

```
gateway/
├── mcprelay/
│   ├── config.py       config model, ${VAR} expansion, validation
│   ├── naming.py       namespacing, allow-list matching, truncation
│   ├── upstream.py     one upstream = one held client connection
│   ├── registry.py     aggregation, per-gateway view, dispatch
│   ├── mcp_server.py   the MCP surface + Radar search meta-tool
│   ├── store.py        runtime gateway CRUD, JSON state, key generation
│   ├── agentconfig.py  per-agent config rendering + safe writing
│   ├── auth.py         constant-time X-API-Key middleware
│   ├── app.py          FastAPI wiring, control plane, lifespan
│   └── __main__.py     CLI
├── tests/
│   ├── test_units.py       47 unit tests (core logic)
│   ├── test_store_units.py 55 unit tests (store + client configs)
│   ├── e2e_test.py         25 e2e checks (MCP protocol)
│   └── e2e_gateway_test.py 62 e2e checks (CRUD + client install)
├── tools/              SDK introspection + isolation scripts
├── docs/
│   └── PROVIDER-ONBOARDING.md
├── config.example.yaml
└── requirements.txt
```

---

## Limitations

- **No per-caller tool filtering.** Allow-lists are per *gateway*, from config.
  One key = one tool set. `cache_scope: public` is therefore currently safe but
  would become a cross-tenant leak the moment you add per-caller filtering.
- **No upstream OAuth.** Use long-lived API keys. Brokered OAuth needs a public
  redirect URI, which localhost does not give you.
- **No resource/prompt proxying.** Tools only. `resources/*` and `prompts/*` are
  not aggregated; namespacing and access control would need rethinking first.
- **Runtime gateways persist to a JSON file, not a database.** Fine for a
  handful. Concurrent writers to the same file are not coordinated beyond a
  process-local lock, and there is no multi-user story.
- **Deleting a gateway needs a restart to fully unmount.** The record and its
  auth rule are removed immediately; the Starlette mount and its session manager
  linger until the process restarts.
- **One failure domain per upstream, not per tool.** A crashed upstream drops all
  its tools until reconnect.
- **No rate limiting, quota or audit log** on the gateway's own endpoint.
- **Client configs are generated, not verified.** Each profile's shape was
  written from that agent's documented schema; only the ones you install get
  proven. A format can drift.
