# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Concise consequences of each security bypass, shared by the CLI gates.

Every ``--allow-*`` option (and the TOFU ``accept-new`` trust policy) relaxes a
security guarantee. When one is explicitly adopted the operator must see a short
warning about the resulting exposure, on stderr and in the service log. The
wording here is the single source for the CLI; the desktop and Android front-ends
keep their own localized copies with the same meaning.
"""

from __future__ import annotations

from logging import Logger
from typing import Any

from hsync_common.protocol.tls_trust import TrustPolicy

__all__ = ["BYPASS_WARNINGS", "log_active_bypasses"]

BYPASS_WARNINGS: dict[str, str] = {
    "allow_deletions": (
        "Deletion-risk confirmation bypassed: local files can be deleted to mirror the source."
    ),
    "allow_unauthenticated_writes": (
        "Unauthenticated writes allowed: anyone who can reach this node may modify "
        "or delete bucket files."
    ),
    "allow_unauthenticated_p2p": (
        "P2P runs without a shared key: peers are not authenticated and reads are "
        "open to the network."
    ),
    "accept_new": (
        "First-seen peer certificate trusted without verification (accept-new): "
        "on-path attacks are not detected on first contact."
    ),
}


def log_active_bypasses(
    logger: Logger,
    *,
    allow_deletions: bool = False,
    allow_unauthenticated_writes: bool = False,
    allow_unauthenticated_p2p: bool = False,
    bootnode_trust: Any = None,
    server_trust: Any = None,
) -> None:
    """Log one concise warning per actively-adopted security bypass."""
    if allow_deletions:
        logger.warning(BYPASS_WARNINGS["allow_deletions"])
    if allow_unauthenticated_writes:
        logger.warning(BYPASS_WARNINGS["allow_unauthenticated_writes"])
    if allow_unauthenticated_p2p:
        logger.warning(BYPASS_WARNINGS["allow_unauthenticated_p2p"])
    if bootnode_trust is TrustPolicy.accept_new or server_trust is TrustPolicy.accept_new:
        logger.warning(BYPASS_WARNINGS["accept_new"])
