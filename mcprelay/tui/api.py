"""Control-plane client + service discovery for the mcprelay TUI.

The TUI never owns the gateway process. It (a) finds a healthy control
plane, (b) spawns one if the machine has the config file for it and (c)
otherwise reports how to start it by hand. Same idea as opencode's
``--server`` / background-service split.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from typing import Any

import httpx


class RelayError(Exception):
    """User-presentable client failure."""


DEFAULT_PORT = 8787


def _candidate_base_urls(explicit: str | None) -> list[str]:
    """Where to look for a running gateway, in search order."""
    out: list[str] = []
    if explicit:
        out.append(explicit.rstrip("/"))
    for env in ("MCPRELAY_URL", "MCPRELAY_BASE"):
        if os.environ.get(env):
            out.append(os.environ[env].rstrip("/"))
    out.append(f"http://127.0.0.1:{DEFAULT_PORT}")
    # de-dup, keep order
    seen: set[str] = set()
    return [b for b in out if not (b in seen or seen.add(b))]


def _spawn_env() -> dict[str, str]:
    """Environment for the child gateway process (Windows detaching too)."""
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def spawn_serve(config: str) -> subprocess.Popen:
    """Start ``python -m mcprelay serve`` detached, without a console window."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "mcprelay", "serve", "-c", config],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=_spawn_env(),
    )


async def _probe(http: httpx.AsyncClient, base: str, timeout: float) -> dict[str, Any]:
    """GET /healthz asking 'is there a mcprelay here at all?'."""
    response = await http.get(f"{base}/healthz", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    # Only accept what is actually ours: a bare /healthz of some other
    # service would produce confusing garbage everywhere downstream.
    if "gateways" not in payload or "upstreams" not in payload:
        raise RelayError(f"{base} answered /healthz but is not mcprelay")
    return payload


async def discover(
    http: httpx.AsyncClient,
    explicit: str | None,
    config: str,
    spawn: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Find (or start) the gateway control plane and return (base, health)."""
    config_path = os.path.abspath(config)
    errors: list[str] = []

    for base in _candidate_base_urls(explicit):
        try:
            return base, await _probe(http, base, timeout=1.5)
        except Exception as exc:  # noqa: BLE001 - any probe failure is just a miss
            errors.append(f"{base}: {exc}")

    if not spawn or not os.path.isfile(config_path):
        raise RelayError(
            "no mcprelay gateway found at "
            + ", ".join(_candidate_base_urls(explicit))
            + (f" - and no config file at {config_path} to spawn one from"
               if not os.path.isfile(config_path)
               else " - pass --no-spawn to attach-only")
        )

    # Config file present: boot our own service and wait for it. First
    # upstream connect happens during startup, real readiness can take a
    # while with slow npx-based stdio servers.
    spawn_serve(config_path)
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        for base in _candidate_base_urls(explicit):
            try:
                return base, await _probe(http, base, timeout=1.0)
            except Exception:  # noqa: BLE001 - still warming up
                continue
    raise RelayError(
        "spawned a gateway but /healthz never came up within 45s; "
        f"check: python -m mcprelay doctor -c {config}"
    )


class RelayClient:
    """Small typed wrapper over the mcprelay control plane."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=20.0)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ---------------------------------------------------------------- health

    async def health(self) -> dict[str, Any]:
        return (await self.http.get("/healthz")).raise_for_status().json()

    async def root(self) -> dict[str, Any]:
        return (await self.http.get("/")).raise_for_status().json()

    # ------------------------------------------------------------- upstreams

    async def upstreams(self) -> dict[str, Any]:
        return (await self.http.get("/api/v1/upstreams")).raise_for_status().json()

    async def reconnect_upstream(self, key: str) -> dict[str, Any]:
        response = await self.http.post(f"/api/v1/upstreams/{key}/reconnect")
        if response.status_code == 404:
            raise RelayError(f"unknown upstream {key!r}")
        if response.status_code == 409:
            raise RelayError(f"upstream {key!r} is disabled in config")
        return response.raise_for_status().json()

    async def add_upstream(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create + connect + persist one runtime upstream."""
        response = await self.http.post("/api/v1/upstreams", json=payload)
        if response.status_code == 400:
            raise RelayError(response.json().get("error", "upstream rejected"))
        return response.raise_for_status().json()

    async def delete_upstream(self, key: str) -> dict[str, Any]:
        response = await self.http.delete(f"/api/v1/upstreams/{key}")
        if response.status_code == 404:
            raise RelayError(f"not a runtime upstream: {key!r}")
        if response.status_code == 409:
            raise RelayError("upstreams declared in the config file must be "
                             "removed in the YAML")
        return response.raise_for_status().json()

    # -------------------------------------------------------------- gateways

    async def gateways(self) -> list[dict[str, Any]]:
        return (
            (await self.http.get("/api/v1/gateways")).raise_for_status().json()
        ).get("gateways", [])

    async def gateway(self, gid: str, include_key: bool = False) -> dict[str, Any]:
        response = await self.http.get(
            f"/api/v1/gateways/{gid}",
            params={"include_key": "true" if include_key else "false"},
        )
        if response.status_code == 404:
            raise RelayError(f"unknown gateway {gid!r}")
        return response.raise_for_status().json()

    async def create_gateway(
        self,
        *,
        name: str,
        upstreams: list[str],
        description: str = "",
        tool_filter: list[str] | None = None,
    ) -> dict[str, Any]:
        response = await self.http.post(
            "/api/v1/gateways",
            json={
                "name": name,
                "upstreams": upstreams,
                "description": description or (f"mcprelay gateway {name}"),
                "tool_filter": tool_filter or ["*"],
            },
        )
        if response.status_code == 400:
            raise RelayError(response.json().get("error", "gateway rejected"))
        return response.raise_for_status().json()

    async def patch_gateway(
        self, gid: str, tool_filter: list[str] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if tool_filter is not None:
            body["tool_filter"] = tool_filter
        response = await self.http.patch(f"/api/v1/gateways/{gid}", json=body)
        if response.status_code == 400:
            raise RelayError(response.json().get("error", "update rejected"))
        if response.status_code == 409:
            raise RelayError(response.json().get("error", "gateway is read-only"))
        if response.status_code == 404:
            raise RelayError(f"unknown gateway {gid!r}")
        return response.raise_for_status().json()

    async def rotate_key(self, gid: str) -> dict[str, Any]:
        response = await self.http.post(f"/api/v1/gateways/{gid}/rotate-key")
        if response.status_code == 404:
            raise RelayError("only runtime-created gateways can rotate keys")
        return response.raise_for_status().json()

    async def delete_gateway(self, gid: str) -> dict[str, Any]:
        response = await self.http.delete(f"/api/v1/gateways/{gid}")
        if response.status_code == 409:
            raise RelayError("declared-in-config gateways must be removed in the YAML")
        if response.status_code == 404:
            raise RelayError(f"unknown gateway {gid!r}")
        return response.raise_for_status().json()

    # ---------------------------------------------------------- tool catalog

    async def gateway_tools(self, gid: str) -> dict[str, Any]:
        response = await self.http.get(f"/api/v1/gateways/{gid}/tools", params={"full": "true"})
        if response.status_code == 404:
            raise RelayError(f"unknown gateway {gid!r}")
        return response.raise_for_status().json()

    async def tool_choices(self, gid: str) -> dict[str, Any]:
        response = await self.http.get(f"/api/v1/gateways/{gid}/tool-choices")
        if response.status_code == 404:
            raise RelayError(f"unknown gateway {gid!r}")
        return response.raise_for_status().json()

    # ------------------------------------------------- client configuration

    async def clients(self) -> list[dict[str, Any]]:
        return (
            (await self.http.get("/api/v1/clients")).raise_for_status().json()
        ).get("clients", [])

    async def client_config(self, gid: str, client_id: str) -> dict[str, Any]:
        response = await self.http.get(f"/api/v1/gateways/{gid}/client-config/{client_id}")
        if response.status_code == 404:
            raise RelayError(f"unknown client {client_id!r}")
        return response.raise_for_status().json()

    async def install_client(
        self,
        gid: str,
        client_id: str,
        *,
        radar: bool = False,
        write: bool = False,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"radar": radar, "dry_run": not write}
        if target_path:
            payload["target_path"] = target_path
        response = await self.http.post(
            f"/api/v1/gateways/{gid}/install/{client_id}", json=payload
        )
        if response.status_code == 404:
            raise RelayError(f"unknown client {client_id!r}")
        data = response.raise_for_status().json()
        if not data.get("supported", True):
            raise RelayError(data.get("reason", "client not supported"))
        return data
