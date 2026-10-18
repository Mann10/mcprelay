"""mcprelay TUI - the interactive client for a running (or auto-spawned) gateway.

The TUI is deliberately a *client*: the gateway server keeps running in
another process, the TUI attaches to its control plane over HTTP. That
mirrors opencode's split (terminal UI, background service) and means
gateways and keep-alives survive a TUI exit.
"""

from .app import run

__all__ = ["run"]
