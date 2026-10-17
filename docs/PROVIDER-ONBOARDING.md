# Gateway provider-onboarding runbook

How to add a hosted (remote) MCP provider to mcprelay, and how to verify it.
Derived from what actually happened while wiring this project up.

---

## The short version

1. Export the credential
2. Add an `upstreams:` entry with the full header value in one env var
3. `enabled: true`
4. `python -m mcprelay doctor -c config.local.yaml`
5. Only then reference it from a gateway

---

## Golden rules

**Put the whole header value in one variable.**

```yaml
headers:
  Authorization: "${GITHUB_AUTHORIZATION:}"    # correct
  Authorization: "Bearer ${GITHUB_TOKEN:}"     # avoid
```

The second form expands to the literal string `"Bearer "`. httpx rejects it with
`Illegal header value b'Bearer '` — an error that never mentions the missing
environment variable. mcprelay detects this case and drops the header with a
warning, but whole-value references have no failure mode to reason about.

**Ship disabled by default.** Set `enabled: false` and comment out the gateway
reference. A config that fails on first run because a credential is absent is a
bad first impression, and providers move their URLs often.

**`doctor` is the source of truth.** It prints per-upstream state, tool count,
negotiated protocol version and the real error. Trust it over guesses.

---

## Verification state of the bundled providers

| Upstream | Transport | Bundled state | Verified here? |
|---|---|---|---|
| `filesystem` | stdio | enabled | **yes** — 14 tools |
| `memory` | stdio | enabled | **yes** — 9 tools |
| `sequential-thinking` | stdio | enabled | **yes** — 1 tool |
| `git` | stdio (`uvx`) | disabled | **no** — needs `uv` + a repo |
| `github` | streamable-http | disabled | **no** — needs a token |
| `notion` | streamable-http | disabled | **no** — needs a token |

The three enabled ones are the reference servers: free, no account, no key.
They exercise every code path in the gateway (namespacing, routing, filtering,
error handling) before you add credentials to the picture.

> Hosted MCP endpoints change. Confirm each URL against the official MCP
> registry or the provider's own docs before enabling it.

---

## Credential types

Prefer long-lived tokens over OAuth for a local gateway. Brokered OAuth needs a
publicly reachable redirect URI, which `localhost` does not give you.

| Provider | Credential | Where to get it |
|---|---|---|
| GitHub | fine-grained PAT | Settings → Developer settings → Personal access tokens |
| Notion | internal integration token | Your integration's settings page |
| Linear | personal API key | Linear → Settings → Security |
| DuckDuckGo | none | — |

Scope the credential to what you actually expose. A gateway key that fronts a
filesystem server should not also carry a GitHub token with repo-write.

---

## One credential per upstream

Do not share a token across providers. mcprelay does not enforce this, so it
becomes your responsibility — a leak of one provider's token should not imply
access to another.

---

## Troubleshooting

Run doctor first, then read `last_error`:

```powershell
python -m mcprelay doctor -c config.local.yaml
```

| Symptom | Cause | Fix |
|---|---|---|
| `Illegal header value b'Bearer '` | `"Bearer ${VAR:}"` expanded to a bare scheme | move the whole value into the variable |
| `Illegal header value b''` | header value was entirely empty | same fix; check the var is exported in *this* shell |
| `Connection closed` | stdio command exited | check the command/package exists: `npx -y <pkg> --help` |
| `npm error 404` | package name wrong or unpublished | verify with `npm view <pkg> version` |
| `Server returned an error response` | remote rejected the request | usually auth or a stale URL; try the URL in a browser |
| `Unknown upstream` in doctor output | gateway references a name not under `upstreams:` | typo, or the entry is missing |

Note that a config referencing an unset variable **without** a default is a hard
error by design — that is a typo catching itself, not a bug.

---

## The npm 404 trap

Several packages under `@modelcontextprotocol/` have been renamed, versioned, or
unpublished. `@modelcontextprotocol/server-time` is **gone** (404, unpublished).
Check before wiring anything up:

```powershell
npm view @modelcontextprotocol/server-<name> version
```

Known-good as of writing: `server-filesystem@1.0.0`, `server-memory@1.0.0`,
`server-sequential-thinking@1.0.0`, `server-everything@1.0.0`.

---

## Confirming protocol era

`doctor` reports the negotiated version per upstream:

```
"protocol_version": "2025-11-25"
```

That is the legacy handshake era. mcprelay negotiates automatically and works
against both, but the version tells you whether an upstream has migrated to
`2026-07-28` — useful when a new provider behaves oddly.

---

## After adding a provider

```powershell
python -m mcprelay doctor -c config.local.yaml                 # is it ready?
python -m mcprelay tools  -c config.local.yaml -g yourgateway  # what did it add?
python -m mcprelay call   -c config.local.yaml -g yourgateway -t <prefix>.<tool>
```

Then add it to a gateway in `config.example.yaml` and restart `serve`. The
control plane reflects live state without a restart:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/v1/upstreams | ConvertTo-Json -Depth 4
```

---

## Adding a brand-new upstream

No code change needed — config only:

```yaml
upstreams:
  acme:
    transport: streamable-http     # or: stdio, sse
    url: https://mcp.acme.example/rpc
    headers:
      Authorization: "${ACME_TOKEN:}"
    request_timeout_seconds: 60
```

The key `acme` becomes the tool prefix, so tools appear as `acme.search`,
`acme.create_ticket`, etc. It must match `[A-Za-z0-9_-]+` — a dot would be
ambiguous in the namespaced name.

SSE is supported for older servers:

```yaml
  legacy-server:
    transport: sse
    url: https://old.example/sse
```

---

## Output schemas: why the gateway drops them

An upstream that advertises `outputSchema` (SEP-2106) can break `tools/list`
for **every** tool on the gateway, not just its own. This is not hypothetical:
GitHub's hosted server triggered it.

### What happens

GitHub emits JSON Schema 2020-12 shapes:

```json
"outputSchema": { "type": ["object", "null"] }
"outputSchema": { "oneOf": [ {"type": "object"}, {"type": "null"} ] }
```

Those are valid 2020-12. The trouble is the **protocol revision**. Through
`2025-11-25` the SDK's surface model types `OutputSchema.type` as
`Literal['object']` - a union is rejected. And because the failure surfaces
while serializing the whole `ListToolsResult`, one odd schema costs the caller
all 60 tools instead of one.

The error is unhelpful on its own:

```
pydantic_core._pydantic_core.ValidationError: 31 validation errors for ListToolsResult
tools.15.outputSchema.type
  Input should be 'object' [type=literal_error, input_value=['object', 'null']]
```

### Why it is easy to misdiagnose

`mcp.types.Tool` types `output_schema` as `dict[str, Any] | None`, so it
accepts *anything*. Validating against it proves nothing. The strict model is
the per-version **surface** model in `mcp_types.methods.SERVER_RESULTS`, which
is what the SDK actually serializes against.

The gateway therefore validates every tool against the surface model for the
version the client negotiated, and degrades rather than failing:
`outputSchema` is dropped (and logged) when it does not fit, and only a tool
that is still invalid afterwards is omitted.

### Knobs

| Setting | Default | Effect |
| --- | --- | --- |
| `expose_output_schemas` (per gateway) | `false` | Do not proxy `outputSchema` at all |
| — | `true` | Proxy it; invalid schemas are dropped per-tool and logged |

Leave it off unless every client you serve negotiates `2026-07-28`. With it on
and a legacy client, the gateway drops the 31 offending schemas, keeps the 29
that are fine, and serves the full tool list.

### Telling the two failure modes apart

- `ValidationError ... for ListToolsResult` -> serialization; the tool list was
  built fine and could not be written out.
- `-32602 "Invalid request parameters"` -> the handler raised while building
  the result. Check the server log for the traceback; the wire error carries no
  detail.

### Reproducing

```powershell
python tools\probe_github_mcp.py         # bypass the gateway; is the upstream OK?
python tools\verify_github_tools.py <gateway-id>   # replay a legacy client's tools/list
```

---

## PATCH applies to the store, not to the live gateway

`PATCH /api/v1/gateways/{gid}` returns `200` and the control-plane view
(`GET /api/v1/gateways/{gid}/tools`) agrees the change landed - but the running
MCP endpoint keeps serving the **old** allow-list until the server restarts.

Cause: the mounted `GatewayConfig` is a snapshot built once at mount time.
`patch_gateway` calls `store.update_tools()` -> `_persist()` and returns. It
never rebuilds the live gateway the way `POST /api/v1/gateways` does.

This is a bad failure shape for a security control: it reports success, and the
only symptom is that nothing changed.

How to tell the three surfaces apart:

| Surface | Reflects |
| --- | --- |
| `var/gateways.json` | what you PATCHed |
| `GET /api/v1/gateways/{gid}/tools` | rebuilt from the store **per request** |
| live `tools/list` over MCP | whatever was mounted at startup - lags both |

Note `GET .../tools` only truncates descriptions when called with `?full=true`;
by default it passes `description_max_chars=0`, i.e. no truncation.

Workaround until it is fixed: restart mcprelay (not just the client) after any
PATCH.

