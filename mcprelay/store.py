"""Runtime gateway CRUD backed by a JSON state file.

YAML in the config file remains the source of truth for *upstreams* and for
declared gateways. This store adds the ability to create, inspect and delete
gateways while the server runs - the thing you need in order to actually stand
a gateway up and hand it to a client.

Credentials are generated here and nowhere else, so key issuance has one code
path to audit.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("mcprelay.store")


class StoreError(Exception):
    """Raised for invalid CRUD requests."""


@dataclass
class ManagedGateway:
    """A gateway created at runtime."""

    id: str
    name: str
    upstream_keys: list[str]
    api_key: str
    description: str = ""
    tool_filter: list[str] = field(default_factory=lambda: ["*"])
    description_max_chars: int = 400
    tools_ttl_seconds: int = 300
    radar_enabled: bool = True
    expose_output_schemas: bool = False
    created_at: str = ""
    created_by: str = "control-plane"

    @property
    def path(self) -> str:
        return f"/gateway/{self.id}/mcp"

    @property
    def radar_path(self) -> str:
        return f"/gateway/{self.id}/radar"

    def to_public(self, *, include_key: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data.pop("api_key", None)
        data["upstreams"] = self.upstream_keys
        if include_key:
            data["api_key"] = self.api_key
        return data


def generate_api_key() -> str:
    """A 256-bit URL-safe key.

    Generated with ``secrets`` and never derived from the gateway name, so a key
    cannot be guessed from public config and two gateways cannot collide.
    """
    return "mcr_" + secrets.token_urlsafe(32)


class GatewayStore:
    """Thread-safe JSON-file gateway store.

    Writes are atomic (temp file + replace) so a crash mid-write cannot leave a
    truncated state file, and a backup is kept on every mutation.
    """

    def __init__(self, path: str, allowed_upstreams: set[str]) -> None:
        self.path = path
        self.allowed_upstreams = allowed_upstreams
        self._lock = threading.RLock()
        self._gateways: dict[str, ManagedGateway] = {}
        self._loaded = False

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except json.JSONDecodeError as exc:
                    log.error(
                        "gateway state file %s is corrupt (%s); starting empty. "
                        "Move it aside to recover.",
                        self.path,
                        exc,
                    )
                    payload = {}
                for raw in payload.get("gateways", []):
                    try:
                        gw = ManagedGateway(**raw)
                    except TypeError as exc:
                        log.error("skipping malformed gateway record: %s", exc)
                        continue
                    self._gateways[gw.id] = gw
                log.info(
                    "loaded %d runtime gateway(s) from %s", len(self._gateways), self.path
                )
            self._loaded = True

    def _persist(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)

        if os.path.exists(self.path):
            backup = f"{self.path}.bak"
            with open(self.path, "r", encoding="utf-8") as src:
                content = src.read()
            with open(backup, "w", encoding="utf-8") as dst:
                dst.write(content)

        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "gateways": [asdict(gw) for gw in self._gateways.values()],
                },
                handle,
                indent=2,
            )
            handle.write("\n")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ CRUD

    def list(self) -> list[ManagedGateway]:
        with self._lock:
            return sorted(self._gateways.values(), key=lambda g: g.created_at)

    def get(self, gateway_id: str) -> ManagedGateway | None:
        with self._lock:
            return self._gateways.get(gateway_id)

    def require(self, gateway_id: str) -> ManagedGateway:
        gw = self.get(gateway_id)
        if gw is None:
            raise StoreError(f"unknown gateway {gateway_id!r}")
        return gw

    def create(
        self,
        *,
        name: str,
        upstreams: list[str],
        description: str = "",
        tool_filter: list[str] | None = None,
        description_max_chars: int = 400,
        tools_ttl_seconds: int = 300,
        radar_enabled: bool = True,
        expose_output_schemas: bool = False,
        created_by: str = "control-plane",
    ) -> ManagedGateway:
        with self._lock:
            if not self._loaded:
                self.load()

            name = (name or "").strip()
            if not name:
                raise StoreError("gateway name is required")
            if not upstreams:
                raise StoreError("select at least one upstream")

            unknown = [u for u in upstreams if u not in self.allowed_upstreams]
            if unknown:
                raise StoreError(
                    f"unknown upstream(s): {', '.join(sorted(unknown))}. "
                    f"Available: {', '.join(sorted(self.allowed_upstreams))}"
                )

            gid = uuid.uuid4().hex[:12]
            while gid in self._gateways:          # pragma: no cover
                gid = uuid.uuid4().hex[:12]

            gw = ManagedGateway(
                id=gid,
                name=name,
                upstream_keys=list(upstreams),
                api_key=generate_api_key(),
                description=description or f"mcprelay gateway {name}",
                tool_filter=list(tool_filter or ["*"]),
                description_max_chars=int(description_max_chars),
                tools_ttl_seconds=int(tools_ttl_seconds),
                radar_enabled=bool(radar_enabled),
                expose_output_schemas=bool(expose_output_schemas),
                created_at=datetime.now(timezone.utc).isoformat(),
                created_by=created_by,
            )
            self._gateways[gid] = gw
            self._persist()
            log.info("created gateway %s (%s) with upstreams %s", gid, name, upstreams)
            return gw

    def delete(self, gateway_id: str) -> ManagedGateway:
        with self._lock:
            gw = self.require(gateway_id)
            del self._gateways[gateway_id]
            self._persist()
            log.info("deleted gateway %s", gateway_id)
            return gw

    def rotate_key(self, gateway_id: str) -> ManagedGateway:
        """Issue a fresh key. The old one stops working immediately."""
        with self._lock:
            gw = self.require(gateway_id)
            gw.api_key = generate_api_key()
            self._persist()
            log.info("rotated key for gateway %s", gateway_id)
            return gw

    def update_tools(
        self,
        gateway_id: str,
        *,
        tool_filter: list[str] | None = None,
        description_max_chars: int | None = None,
    ) -> ManagedGateway:
        """Change the allow-list / description budget without recreating."""
        with self._lock:
            gw = self.require(gateway_id)
            if tool_filter is not None:
                if not tool_filter:
                    raise StoreError("tool_filter cannot be empty")
                gw.tool_filter = list(tool_filter)
            if description_max_chars is not None:
                gw.description_max_chars = int(description_max_chars)
            self._persist()
            return gw
