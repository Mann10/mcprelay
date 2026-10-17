"""API-key authentication for gateway endpoints.

Implemented as raw ASGI so it composes with the MCP transports' streaming
responses without buffering them.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Iterable

from starlette.responses import JSONResponse

log = logging.getLogger("mcprelay.auth")

API_KEY_HEADER = b"x-api-key"
BEARER_HEADER = b"authorization"


def _headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def extract_key(headers: dict[bytes, bytes]) -> str | None:
    raw = headers.get(API_KEY_HEADER)
    if raw:
        return raw.decode("latin-1").strip()
    auth = headers.get(BEARER_HEADER)
    if auth:
        text = auth.decode("latin-1").strip()
        if text.lower().startswith("bearer "):
            return text[7:].strip()
    return None


class ApiKeyAuthMiddleware:
    """Guards selected path prefixes with per-gateway API keys.

    ``rules`` is a sequence of ``(path_prefix, expected_key, gateway_id)``.
    Longer prefixes are matched first. A missing key and a wrong key produce
    the same response so the endpoint does not confirm which gateways exist.
    """

    def __init__(
        self,
        app: Any,
        rules: Iterable[tuple[str, str, str]],
        *,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.enabled = enabled
        # Keep the caller's list object rather than a sorted copy: runtime
        # gateways are appended after the app is built, and a snapshot taken
        # here would never learn about them - which would leave those endpoints
        # serving unauthenticated.
        self._rules_ref = rules

    @property
    def rules(self) -> list[tuple[str, str, str]]:
        """Matching rules, longest prefix first.

        Derived on each access so a rule appended after construction is always
        visible. The list is tiny (one entry per gateway), so recomputing per
        request costs nothing measurable.
        """
        return sorted(
            (r for r in self._rules_ref if r[1]),
            key=lambda rule: -len(rule[0]),
        )

    def add_rule(self, prefix: str, expected_key: str, gateway_id: str) -> None:
        """Register a gateway created after the app started.

        Appends to the shared list the app already holds a reference to, so the
        new prefix is protected immediately - no rebuild required.
        """
        self._rules_ref.append((prefix, expected_key, gateway_id))

    def remove_rule(self, prefix: str) -> None:
        self._rules_ref[:] = [r for r in self._rules_ref if r[0] != prefix]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.enabled and scope.get("type") == "http":
            path = scope.get("path", "")
            for prefix, expected, gateway_id in self.rules:
                if path == prefix or path.startswith(prefix + "/"):
                    presented = extract_key(_headers(scope))
                    if not presented or not hmac.compare_digest(presented, expected):
                        log.warning(
                            "401 %s %s gateway=%s key=%s",
                            scope.get("method"),
                            path,
                            gateway_id,
                            "missing" if not presented else "invalid",
                        )
                        response = JSONResponse(
                            {"error": "unauthorized", "gateway": gateway_id},
                            status_code=401,
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                        await response(scope, receive, send)
                        return
                    break
            else:
                # Path sits under /gateway/ but matched no registered prefix -
                # a runtime gateway whose auth rule has not landed, or a typo.
                # Fail closed rather than serving an unauthenticated endpoint.
                if path.startswith("/gateway/"):
                    log.warning(
                        "401 %s %s - no auth rule registered for this gateway path",
                        scope.get("method"),
                        path,
                    )
                    response = JSONResponse(
                        {"error": "unauthorized"}, status_code=401
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
