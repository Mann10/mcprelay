"""Tool-name namespacing and collision detection.

MCP tool names are flat strings, so aggregating N upstreams means N servers can
each expose ``create_issue``. We namespace as ``<upstream>.<tool>``; dots are
legal in MCP tool names, so no escaping is needed.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

SEPARATOR = "."

# Conservative: avoids whitespace, control chars, and separators that would make
# the name ambiguous to parse back apart.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class NamingError(Exception):
    """Raised when a tool name cannot be namespaced safely."""


def validate_upstream_key(key: str) -> str:
    """Validate an upstream key so ``a.b`` can never split into two segments."""
    if not _SAFE_SEGMENT.match(key):
        raise NamingError(
            f"upstream key {key!r} is invalid: use only letters, digits, '_' and '-' "
            f"(it becomes the tool-name prefix)"
        )
    return key


def qualify(upstream_key: str, tool_name: str) -> str:
    """Return the namespaced name a client will see."""
    return f"{upstream_key}{SEPARATOR}{tool_name}"


def split(qualified_name: str) -> tuple[str, str]:
    """Inverse of :func:`qualify`.

    Splits on the *first* separator so an upstream whose tool name itself
    contains a dot still round-trips.
    """
    head, sep, tail = qualified_name.partition(SEPARATOR)
    if not sep or not head or not tail:
        raise NamingError(
            f"tool name {qualified_name!r} is not namespaced; expected '<upstream>.<tool>'"
        )
    return head, tail


@dataclass(frozen=True)
class MatchOutcome:
    included: bool
    pattern: str
    matched: bool


def match_filter(qualified_name: str, patterns: tuple[str, ...]) -> MatchOutcome:
    """First matching pattern wins.

    A leading ``!`` negates, so patterns read like a gitignore block::

        ["!memory.delete_*", "*"]     # everything except memory deletions
        ["*"]                          # everything
        ["github.*", "notion.search"]  # only these two groups

    If no pattern matches the tool is DENIED. There is no implicit allow-all:
    ``["*"]`` has to be written out, so adding a new upstream never silently
    exposes everything it happens to carry.
    """
    for pattern in patterns:
        negated = pattern.startswith("!")
        candidate = pattern[1:] if negated else pattern
        if fnmatch.fnmatchcase(qualified_name, candidate):
            return MatchOutcome(included=not negated, pattern=pattern, matched=True)
    return MatchOutcome(included=False, pattern="", matched=False)


def truncate(text: str | None, limit: int) -> str | None:
    """Shorten a description, on a word boundary where possible."""
    if not text:
        return text
    if limit <= 0 or len(text) <= limit:
        return text
    clipped = text[:limit]
    space = clipped.rfind(" ")
    if space > limit // 2:
        clipped = clipped[:space]
    return clipped.rstrip(" ,;:-") + " ..."
