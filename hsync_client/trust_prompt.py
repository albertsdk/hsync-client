# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""SSH-style terminal trust prompt for the bootnode and the sync server (ADR-0035/0036).

The client core is front-end agnostic: the CLI renders this prompt on a
terminal, the desktop confirms out of band with a pre-flight, and Android
confirms in-app. This module owns only the terminal prompt and the rule that
decides whether one may be shown at all, so the discovery/connect paths can hand
the client a prompt (or ``None`` to fail closed).

Consumers: ``hsync-client`` (``p2p_discovery.build_discovery_plan``, server
connect path).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from hsync_common.endpoint import format_authority
from hsync_common.protocol.tls_trust import TrustPolicy
from hsync_common.runtime import is_tty_interactive

__all__ = [
    "TrustPrompt",
    "build_trust_prompt",
    "server_terminal_trust_prompt",
    "terminal_trust_prompt",
]

# SSH-style trust prompt: ``(host, port, observed_fingerprint) -> accept?``.
TrustPrompt = Callable[[str, int, str], bool]


def _render_trust_prompt(subject: str, host: str, port: int, fingerprint: str) -> bool:
    """Render the SSH-style confirmation and return the operator's answer."""
    from rich.console import Console
    from rich.prompt import Confirm

    console = Console()
    console.print(
        f"The authenticity of {subject} [bold]'{format_authority(host, port)}'[/bold] can't be established.\n"
        f"SHA256 fingerprint is [bold]{fingerprint}[/bold]."
    )
    return Confirm.ask(
        "Are you sure you want to continue connecting?", default=False, console=console
    )


def terminal_trust_prompt(host: str, port: int, fingerprint: str) -> bool:
    """Ask the operator to accept an unknown bootnode fingerprint (SSH-style)."""
    return _render_trust_prompt("bootnode", host, port, fingerprint)


def server_terminal_trust_prompt(host: str, port: int, fingerprint: str) -> bool:
    """Ask the operator to accept an unknown sync-server fingerprint (SSH-style)."""
    return _render_trust_prompt("sync server", host, port, fingerprint)


def build_trust_prompt(
    config: Mapping[str, object],
    *,
    policy_key: str = "BOOTNODE_TRUST",
    prompt: TrustPrompt | None = None,
) -> TrustPrompt | None:
    """Return the terminal prompt to use, or ``None`` to fail closed.

    Only policy ``ask`` prompts, and only on a terminal: a non-interactive run
    never prompts (SSH ``BatchMode``), so the client fails closed. ``policy_key``
    selects the bootnode or server policy and ``prompt`` overrides the rendered
    prompt (the server passes :func:`server_terminal_trust_prompt`).
    """
    if config.get(policy_key) is not TrustPolicy.ask:
        return None
    if not is_tty_interactive():
        return None
    return prompt or terminal_trust_prompt
