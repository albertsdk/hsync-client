# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared SSH-style trust messages for the durable trust stores.

Three transports pin a certificate fingerprint and refuse a mismatch as a possible
man-in-the-middle: the bootnode (``bootnode_known_hosts.json``, keyed ``host:port``),
the sync server (``server_known_hosts.json``, keyed ``host:port``) and the P2P
master/seeders (``p2p_known_peers.json``, keyed ``cid|bucket``). This module owns the
operator-facing prose for those moments — the "permanently added" announcement and
the SSH-style recovery guidance with the exact ``hsync trust …`` commands — so the
paths stay consistent and a recovery command is never duplicated inline.

Consumers: ``hsync-client`` (``boot_client.py``, ``main_logic.py``,
``p2p_handler.py``, ``p2p_sources.py``), tests.
"""

from __future__ import annotations

from pathlib import Path

from hsync_common.labels import ActionLabel

__all__ = [
    "known_hosts_added_message",
    "known_peer_added_message",
    "known_server_added_message",
    "mismatch_guidance",
    "peer_mismatch_guidance",
    "peer_pairing_needed_guidance",
    "peer_source_changed_line",
    "server_mismatch_guidance",
    "server_pairing_needed_guidance",
]


def _permanently_added_line(identity: str, store_kind: str, store_path: Path) -> str:
    """The shared "permanently added" sentence (OpenSSH ``known_hosts`` style).

    *identity* is ``host:port`` or ``cid|bucket``; *store_kind* is the human label
    of the store (``known hosts`` / ``known servers`` / ``known peers``).
    """
    return (
        f"Permanently added '{identity}' (SHA256) to the list of known {store_kind} ({store_path})."
    )


def _presented_fingerprint_lines(subject: str, observed: str) -> list[str]:
    """The shared "presented fingerprint" section (two lines)."""
    return [
        f"The {subject} presented this SHA256 fingerprint:",
        f"  sha256:{observed}",
    ]


def known_hosts_added_message(host: str, port: int, store_path: Path) -> str:
    """SSH-style confirmation that a bootnode fingerprint was just pinned, and where.

    Mirrors OpenSSH's ``Warning: Permanently added '…' (…) to the list of known
    hosts`` and names the effective store path, which OpenSSH leaves implicit.

    Consumers: ``hsync-client`` (bootnode connect path: prompt acceptance and TOFU).
    """
    return _permanently_added_line(f"{host}:{port}", "hosts", store_path)


def known_server_added_message(host: str, port: int, store_path: Path) -> str:
    """SSH-style confirmation that a sync-server fingerprint was just pinned, and where.

    Consumers: ``hsync-client`` (server connect path: prompt acceptance and TOFU).
    """
    return _permanently_added_line(f"{host}:{port}", "servers", store_path)


def known_peer_added_message(cid: str, bucket: str, store_path: Path) -> str:
    """SSH-style confirmation that a P2P peer fingerprint was just pinned, and where.

    Consumers: ``hsync-client`` (P2P master connect path, TOFU).
    """
    return _permanently_added_line(f"{cid}|{bucket}", "peers", store_path)


def _host_mismatch_guidance(
    kind: str,
    host: str,
    port: int,
    observed: str,
    *,
    stored: str | None,
    store_path: Path,
    explicit_pins: bool,
    flag_name: str,
    env_var: str,
) -> str:
    """SSH-style recovery block for a refused ``host:port`` pin (bootnode/server).

    Three variants: a pin that changed (possible man-in-the-middle), a host that is
    not pinned under ``strict``, and a fingerprint that matches none of the explicit
    ``--<flag_name>`` values. Each names the presented fingerprint, the store that
    holds the accepted pin and the exact ``hsync trust <kind>`` command that
    unblocks the situation.
    """
    label = kind.capitalize()
    if stored is not None:
        headline = (
            "the certificate fingerprint does not match the accepted pin "
            "(possible man-in-the-middle)."
        )
    elif explicit_pins:
        headline = (
            f"the certificate fingerprint does not match any fingerprint passed with --{flag_name}."
        )
    else:
        headline = f"the {kind} is not pinned and the trust policy is 'strict'."
    lines = [
        f"{label} '{host}:{port}' trust failed: {headline}",
        *_presented_fingerprint_lines(kind, observed),
    ]
    if stored is not None:
        lines += [
            f"The accepted pin is stored in {store_path}.",
            "If you can verify the new fingerprint out of band, re-pin it with:",
            f"  hsync trust {kind} add {host} sha256:{observed} --port {port}",
            "To forget the pin and be prompted again at the next connection "
            "(like 'ssh-keygen -R'):",
            f"  hsync trust {kind} remove {host} --port {port}",
        ]
    elif explicit_pins:
        lines += [
            f"Read the {kind}'s current fingerprint out of band with:",
            f"  hsync trust {kind} fingerprint {host} --port {port}",
            f"then update the pinned value (--{flag_name} or {env_var}).",
        ]
    else:
        lines += [
            "Verify it out of band and pin it with:",
            f"  hsync trust {kind} add {host} sha256:{observed} --port {port}",
            f"Alternatively, start the client with --{kind}-tofu (trust the first "
            f"sighting) or --{flag_name} <fp>.",
        ]
    return "\n".join(lines)


def _host_pairing_needed_guidance(kind: str, host: str, port: int, observed: str) -> str:
    """SSH-style recovery block for an unpinned ``host:port`` requiring confirmation."""
    lines = [
        f"{ActionLabel.PAIRING_NEEDED} {kind} '{host}:{port}' is not pinned and "
        "requires explicit trust confirmation.",
        *_presented_fingerprint_lines(kind, observed),
        "Verify it out of band and pin it with:",
        f"  hsync trust {kind} add {host} sha256:{observed} --port {port}",
        "Until then the connection is refused.",
    ]
    return "\n".join(lines)


def mismatch_guidance(
    host: str,
    port: int,
    observed: str,
    *,
    stored: str | None,
    store_path: Path,
    explicit_pins: bool,
) -> str:
    """SSH-style recovery block for a refused bootnode fingerprint (English).

    Consumers: ``hsync-client`` (``BootnodeClient._mismatch``), tests.
    """
    return _host_mismatch_guidance(
        "bootnode",
        host,
        port,
        observed,
        stored=stored,
        store_path=store_path,
        explicit_pins=explicit_pins,
        flag_name="bootnode-fingerprint",
        env_var="HSYNC_BOOTNODE_FINGERPRINT",
    )


def server_mismatch_guidance(
    host: str,
    port: int,
    observed: str,
    *,
    stored: str | None,
    store_path: Path,
    explicit_pins: bool,
) -> str:
    """SSH-style recovery block for a refused sync-server fingerprint (English).

    Consumers: ``hsync-client`` (server connect path), tests.
    """
    return _host_mismatch_guidance(
        "server",
        host,
        port,
        observed,
        stored=stored,
        store_path=store_path,
        explicit_pins=explicit_pins,
        flag_name="server-fingerprint",
        env_var="HSYNC_SERVER_FINGERPRINT",
    )


def server_pairing_needed_guidance(host: str, port: int, observed: str) -> str:
    """SSH-style recovery block for an unpinned sync server requiring confirmation.

    Consumers: ``hsync-client`` (server connect path), tests.
    """
    return _host_pairing_needed_guidance("server", host, port, observed)


def peer_mismatch_guidance(
    cid: str,
    bucket: str,
    address: str,
    observed: str,
    store_path: Path,
) -> str:
    """SSH-style recovery block for a changed P2P peer pin (possible MITM).

    Consumers: ``hsync-client`` (``p2p_handler._evaluate_master_pin``), tests.
    """
    lines = [
        f"{ActionLabel.FINGERPRINT_CHANGED} peer '{cid}|{bucket}' (at {address}) trust failed: "
        "the certificate fingerprint does not match the accepted pin (possible man-in-the-middle).",
        *_presented_fingerprint_lines("peer", observed),
        f"The accepted pin is stored in {store_path}.",
        "If you can verify the new fingerprint out of band, re-pin it with:",
        f"  hsync trust peer add {cid} sha256:{observed} --bucket {bucket}",
        "To forget the pin and be prompted again at the next connection (like 'ssh-keygen -R'):",
        f"  hsync trust peer remove {cid} --bucket {bucket}",
    ]
    return "\n".join(lines)


def peer_pairing_needed_guidance(
    cid: str,
    bucket: str,
    address: str,
    observed: str,
) -> str:
    """SSH-style recovery block for a P2P peer under strict pairing.

    Consumers: ``hsync-client`` (``p2p_handler._evaluate_master_pin``), tests.
    """
    lines = [
        f"{ActionLabel.PAIRING_NEEDED} peer '{cid}|{bucket}' (at {address}) is not pinned and strict pairing is enabled.",
        *_presented_fingerprint_lines("peer", observed),
        "Verify it out of band and pin it with:",
        f"  hsync trust peer add {cid} sha256:{observed} --bucket {bucket}",
        "Until then the connection is refused.",
    ]
    return "\n".join(lines)


def peer_source_changed_line(cid: str, bucket: str) -> str:
    """One-line refusal for a peer source whose certificate no longer matches.

    The per-session CID disabling already dedupes, so this single line is enough.

    Consumers: ``hsync-client`` (``p2p_sources.P2PSourceResolver.authorize``).
    """
    return (
        f"{ActionLabel.FINGERPRINT_CHANGED} peer source {cid} presented a certificate that does not "
        "match the pinned one (possible man-in-the-middle). Forget the pin with "
        f"'hsync trust peer remove {cid} --bucket {bucket}' and reconnect."
    )
