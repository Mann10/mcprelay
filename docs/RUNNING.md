# Running mcprelay

## The command

```powershell
cd C:\personal\Reverse_engineer\gateway
python -m mcprelay serve -c config.local.yaml
```

Run it **in a VSCode terminal and leave it there**. `Ctrl+C` stops it cleanly.

This was verified, not assumed: the process exits, **no traceback** appears, and
all six stdio MCP child processes (`node`/`npx`) are reaped by the shutdown
path. Output ends like this:

```
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO    mcprelay.app   shutting down upstreams
INFO:     Application shutdown complete.
INFO:     Finished server process [52508]
```

---

## If it says "already running"

That is **not** a Ctrl+C problem. It means a *previous* instance is still alive
and holding port 8787. Find and stop it:

```powershell
# 1. who has the port
Get-NetTCPConnection -LocalPort 8787 -State Listen |
  Select-Object -ExpandProperty OwningProcess

# 2. stop it
Stop-Process -Id <pid> -Force

# 3. confirm the port is free
Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
```

Step 3 should print nothing.

Or in one line:

```powershell
Stop-Process -Id (Get-NetTCPConnection -LocalPort 8787 -State Listen).OwningProcess -Force
```

---

## If Ctrl+C seems to hang

Press it once and wait a few seconds — shutdown closes every upstream
connection, and each stdio server is a subprocess. If it is genuinely stuck,
kill the process tree:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*mcprelay*" } |
  ForEach-Object { taskkill /F /T /PID $_.ProcessId }
```

`/T` kills child processes too, so the MCP subprocesses go with it.

---

## If stray MCP processes are left behind

`taskkill /F /T` on the *python* process does not always reap grandchildren.
Check for leftovers:

```powershell
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match "modelcontextprotocol" } |
  Select-Object ProcessId, ParentProcessId, CommandLine
```

To clear them:

```powershell
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match "modelcontextprotocol" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**Check first, though.** If a server is running, those processes are its
children and are *supposed* to exist. A healthy run shows three chains:

```
python (mcprelay)
  └─ cmd.exe → node npx → cmd.exe → node @modelcontextprotocol/server-memory
  └─ cmd.exe → node npx → cmd.exe → node @modelcontextprotocol/server-filesystem
  └─ cmd.exe → node npx → cmd.exe → node @modelcontextprotocol/server-sequential-thinking
```

They are only stray once the python process is gone.

---

## Running a second instance

```powershell
python -m mcprelay serve -c config.local.yaml --port 8788
```

Each instance spawns its own upstream subprocesses, so two at once means six
more node processes.

---

## Daily workflow

```powershell
# terminal 1 - leave running, Ctrl+C to stop
python -m mcprelay serve -c config.local.yaml

# terminal 2 - as needed
python -m mcprelay doctor  -c config.local.yaml    # upstream health
python -m mcprelay ls      -c config.local.yaml    # gateways
python -m mcprelay tools   -c config.local.yaml -g dev
python -m mcprelay create  -c config.local.yaml -n "my agent" -u filesystem -u memory
python -m mcprelay install -c config.local.yaml -g <id> --for opencode --write
```

---

## Where things are written

| What | Path |
|---|---|
| Config | `config.local.yaml` |
| Runtime gateways | `var/gateways.json` (+ `.bak`) |
| Server logs | `var/server.out.log`, `var/server.err.log` |

`var/` is disposable — deleting it drops runtime gateways but leaves the
YAML-declared `dev` and `curated` alone.
