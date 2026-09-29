# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""SSH-style terminal trust prompt for the bootnode (ADR-0035).

The client core is front-end agnostic: the CLI renders this prompt on a
terminal, the desktop confirms out of band with a pre-flight, and Android will
confirm in-app. This module owns only the terminal prompt and the rule that
decides whether one may be shown at all, so the discovery plan can hand the
bootnode client a prompt (or ``None`` to fail closed).

Consumers: ``hsync-client`` (``p2p_discovery.build_discovery_plan``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from hsync_common.protocol.bootnode_trust import TrustPolicy
from hsync_common.runtime import is_tty_interactive

__all__ = ["TrustPrompt", "build_trust_prompt", "terminal_trust_prompt"]

# SSH-style trust prompt: ``(host, port, observed_fingerprint) -> accept?``.
TrustPrompt = Callable[[str, int, str], bool]


def terminal_trust_prompt(host: str, port: int, fingerprint: str) -> bool:
    """Ask the operator to accept an unknown bootnode fingerprint (SSH-style)."""
    from rich.console import Console
    from rich.prompt import Confirm

    console = Console()
    console.print(
        f"The authenticity of bootnode [bold]'{host}:{port}'[/bold] can't be established.\n"
        f"SHA256 fingerprint is [bold]{fingerprint}[/bold]."
    )
    return Confirm.ask(
        "Are you sure you want to continue connecting?", default=False, console=console
    )


def build_trust_prompt(config: Mapping[str, object]) -> TrustPrompt | None:
    """Return the terminal prompt to use, or ``None`` to fail closed.

    Only policy ``ask`` prompts, and only on a terminal: a non-interactive run
    never prompts (SSH ``BatchMode``), so the client fails closed. ``--yes``
    auto-accepts, but again only when a terminal exists.
    """
    if config.get("BOOTNODE_TRUST") is not TrustPolicy.ask:
        return None
    if not is_tty_interactive():
        return None
    if config.get("AUTO_CONFIRM", False):
        return lambda host, port, fingerprint: True
    return terminal_trust_prompt
