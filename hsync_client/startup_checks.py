# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Pre-startup safety and policy checks for ``hsync``.

Interactive/non-interactive deletion-risk and keyless-P2P confirmation, plus the
container-networking warning: pure gates evaluated before the sync engine starts,
so they stay separate from ``hsync_client.main_logic`` orchestration.
"""

from __future__ import annotations

import sys
from typing import Any

from hsync_common.listing import folder_has_user_content
from hsync_common.runtime import detect_container, is_tty_interactive

from hsync_client.config import CONFIG, deletions_are_kept
from hsync_client.logger import flush_logs


def _check_deletion_risk(
    local_path: str,
    p2p_mode: bool,
    prefer_master: bool,
    sync_direction: str,
    allow_deletions: bool,
    logger: Any,
) -> None:
    """Check if sync settings could delete local files and warn the user.

    If the local directory is non-empty and the sync mode could cause
    file deletions, a warning is printed and – unless *allow_deletions* is
    True – the user is prompted for confirmation.  When *allow_deletions* is
    True the warning is still logged but no interactive prompt is shown.

    Raises SystemExit if the user declines to continue.
    """
    # upload-only never deletes local files
    if sync_direction == "upload-only":
        return

    # `keep` never removes a local file either, so there is no risk to confirm.
    # Without this the non-interactive path would abort a backup setup that
    # cannot pass --allow-deletions (containers, systemd units, cron, pipes).
    if deletions_are_kept(CONFIG):
        return

    # prefer_master in P2P mode means this peer serves files to slaves — the
    # deletion-risk guard concerns serving, not authority (no peer is
    # authoritative under the causal model).
    if p2p_mode and prefer_master:
        return

    # Only download-only mode can wipe local-only files at the initial sync.
    # Bidirectional (server or P2P) uploads local-only files instead of
    # deleting them; remote delete events are expected normal behaviour.
    if sync_direction != "download-only":
        return

    # A directory is considered "empty" for deletion-risk purposes when it
    # contains no user data (hidden/temp/backup files and the .hsynckeep
    # sentinel are system artefacts); the shared helper owns that rule so the
    # client and desktop cannot drift.
    if not folder_has_user_content(local_path):
        return

    reason = (
        "download-only mode: remote deletions will be applied locally "
        "and local-only files may be removed"
    )

    warnings = [
        f"The shared directory '{local_path}' is not empty and the current "
        f"sync settings could DELETE local files.",
        f"Reason: {reason}",
    ]
    for line in warnings:
        logger.warning(line)

    if allow_deletions:
        return

    # No operator can answer the prompt without an interactive terminal
    # (containers, systemd units, cron, pipes): fail fast with a clear
    # message instead of blocking on input() or silently declining via EOF.
    if not is_tty_interactive():
        error = (
            "Deletion-risk confirmation required but no interactive terminal is "
            "available. Re-run with --allow-deletions to accept the risk and proceed."
        )
        logger.error(error)
        # The loguru sinks are asynchronous (enqueue=True): a message written
        # right before SystemExit can be lost. Echo the full diagnostic through
        # the synchronous CLI error channel too, so the operator always sees why
        # the run aborted.
        print("Error: " + " ".join([*warnings, error]), file=sys.stderr)
        # This fatal exit happens before the run's own ``finally`` (which drains
        # the sinks), so it must drain here: without it the warning/error records
        # sit in the queue while the process unwinds, and an in-process caller
        # (the CLI driven through Click's test isolation) loses them once its
        # capture stream is gone.
        flush_logs()
        raise SystemExit(1)

    try:
        response = input("Do you want to continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response not in ("y", "yes"):
        logger.info("Sync cancelled by user.")
        flush_logs()
        raise SystemExit(0)


def _check_p2p_write_policy(
    p2p_mode: bool,
    p2p_key: str | None,
    sync_direction: str,
    logger: Any,
    allow_unauthenticated_writes: bool = False,
) -> None:
    """Require authorization for every write-capable P2P sync.

    A self-signed P2P transport hides traffic but does not prove the master's
    identity, so a client that uploads data must authenticate with a shared secret
    (``HSYNC_P2P_PASSPHRASE`` or a raw ``HSYNC_P2P_KEY``) unless the dangerous
    explicit opt-in is enabled. The policy lives in
    :func:`hsync_common.protocol.pairing.p2p_write_requires_key`; here we apply it.

    Raises SystemExit when the policy is violated.
    """
    if not p2p_mode:
        return

    if p2p_key:
        return

    from hsync_client.preview import _p2p_write_blocked

    if _p2p_write_blocked(
        sync_direction,
        allow_unauthenticated_writes=allow_unauthenticated_writes,
        log=logger,
        hint=(
            "Set a shared password (HSYNC_P2P_PASSPHRASE) or raw key "
            "(HSYNC_P2P_KEY), use --sync-direction download-only, or explicitly "
            "accept the risk with --allow-unauthenticated-writes."
        ),
    ):
        raise SystemExit(1)


def _check_p2p_keyless_policy(
    p2p_mode: bool,
    p2p_key: str | None,
    allow_unauthenticated_p2p: bool,
    allow_unauthenticated_writes: bool,
    logger: Any,
) -> None:
    """Confirm keyless P2P operation before any connection is made.

    Without a shared key the transport hides traffic but authenticates nobody:
    reads are open and a peer's identity cannot be verified. This gate prompts
    (or fails closed) unless the operator explicitly acknowledged the posture
    with ``--allow-unauthenticated-p2p``; the writes opt-in implies the same
    acknowledgement.
    """
    if not p2p_mode:
        return
    if p2p_key:
        return
    if allow_unauthenticated_p2p or allow_unauthenticated_writes:
        return

    warnings = [
        "P2P mode is running without a shared secret (HSYNC_P2P_PASSPHRASE/HSYNC_P2P_KEY).",
        "Consequence: peers are not authenticated and reads are open to anyone",
        "who can reach this node.",
    ]
    for line in warnings:
        logger.warning(line)

    # No operator can answer the prompt without an interactive terminal.
    if not is_tty_interactive():
        error = (
            "Keyless P2P confirmation required but no interactive terminal is "
            "available. Set a shared password (HSYNC_P2P_PASSPHRASE) or raw key "
            "(HSYNC_P2P_KEY), or re-run with --allow-unauthenticated-p2p "
            "(env HSYNC_P2P_ALLOW_UNAUTHENTICATED) to accept the risk."
        )
        logger.error(error)
        print("Error: " + " ".join([*warnings, error]), file=sys.stderr)
        raise SystemExit(1)

    try:
        response = input("Run P2P without a shared key? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response not in ("y", "yes"):
        logger.info("Sync cancelled by user.")
        raise SystemExit(0)


def _warn_if_p2p_in_container(logger: Any) -> None:
    """Warn that mDNS peer discovery usually fails under container networking.

    P2P discovery relies on multicast, which does not cross the NAT boundary
    of the default bridge network used by container runtimes; P2P mode inside
    a container therefore requires host networking to find peers.
    """
    info = detect_container()
    if not info.is_container:
        return
    logger.warning(
        f"P2P mode inside a {info.runtime or 'unknown'} container: mDNS peer "
        "rendezvous cannot cross container bridge/NAT networking. Use host "
        "networking (e.g. 'docker run --network host') or peers will not be found."
    )
