# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Causal upload arbitration for the P2P master.

Mirrors :mod:`hsync_server.arbitration` using the client's own sync-state
store (``syncstate.db``) and the master's CID. The P2P master is a transport
coordinator, not an authority: every slave upload is arbitrated causally
against the stored version for the path (ADR-0022), exactly as the central
server does. The pure decision logic lives in ``hsync-common`` and is shared
with the server; this module supplies only the client-side store/cid binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hsync_common.models.version import (
    SyncDecision,
    merge_vclocks,
    new_version_id,
    parse_version_info,
    resolve_three_way,
    tick_vclock,
)

from hsync_client.sync_state import get_sync_state_store


class ArbitrationOutcome(StrEnum):
    """Outcome of arbitrating an incoming upload against the stored version."""

    accepted = "accepted"
    accepted_conflict_won = "accepted_conflict_won"
    superseded = "superseded"
    conflict_lost = "conflict_lost"


@dataclass(frozen=True)
class ArbitrationResult:
    """Result of causal arbitration for an upload.

    ``record_vclock`` is the clock the master should record on accept
    (``merge_vclocks(incoming_vclock, stored_vclock)`` per the convergence
    invariant D11).
    ``record_corrected_mtime`` is the corrected mtime (ADR-0023) the master
    records alongside: the incoming value on accept, the stored value on
    reject, 0 when unknown.
    ``loser_cid`` is the author of the losing content for conflict-copy naming.
    """

    outcome: ArbitrationOutcome
    record_vclock: dict[str, int]
    record_corrected_mtime: int = 0
    loser_cid: str | None = None


def arbitrate_upload(
    abs_path: str,
    incoming_hash: str,
    incoming_vclock_raw: object,
    incoming_version_id: str,
    incoming_origin_cid: str | None,
    disk_hash: str | None,
    incoming_corrected_mtime: int = 0,
) -> ArbitrationResult:
    """Arbitrate an incoming slave upload against the stored version.

    Args:
        abs_path: Absolute path of the file in the master's bucket.
        incoming_hash: SHA-256 of the incoming content.
        incoming_vclock_raw: Raw parsed ``vclock`` payload (validated here).
        incoming_version_id: Version id from the uploader.
        incoming_origin_cid: Origin cid from the uploader (may be None).
        disk_hash: SHA-256 of the current file on disk (None if no file).
        incoming_corrected_mtime: ADR-0023 corrected mtime advertised by the
            uploader (0 ⇒ unknown ⇒ the hash fallback applies).

    Returns:
        The :class:`ArbitrationResult`; the caller writes the file / copy /
        response based on ``outcome`` and records ``record_vclock`` on accept.
    """
    from hsync_common.models.tombstone import relpath_in_bucket

    from hsync_client.state_backend import bucket_dir, local_cid

    relpath = relpath_in_bucket(abs_path, bucket_dir())
    store = get_sync_state_store()
    stored = store.get(relpath) if relpath else None

    # Self-record out-of-band edits: if the disk content differs from the
    # stored base, the master's view is stale — advance it with the master cid
    # before comparing (anti-aliasing invariant, ADR-0022 §3.4.7).
    master_cid = local_cid()
    if stored is not None and disk_hash is not None and stored["base_hash"] != disk_hash:
        if master_cid:
            advanced_clock = tick_vclock(stored["vclock"], master_cid)
            store.record(
                relpath,
                base_hash=disk_hash,
                vclock=advanced_clock,
                version_id=new_version_id(),
                origin_cid=master_cid,
            )
            stored = store.get(relpath)

    stored_hash = stored["base_hash"] if stored else None
    stored_vclock = stored["vclock"] if stored else None

    # Parse the incoming wire vclock tolerantly (malformed ⇒ empty clock).
    incoming_info = parse_version_info(
        {
            "vclock": incoming_vclock_raw,
            "version_id": incoming_version_id,
            "origin_cid": incoming_origin_cid,
            "ts": 0,
        }
    )
    incoming_vclock = incoming_info["vclock"] if incoming_info else {}
    effective_origin = incoming_info["origin_cid"] if incoming_info else incoming_origin_cid

    stored_corrected_mtime = int(stored.get("corrected_mtime", 0) or 0) if stored else 0
    decision = resolve_three_way(
        incoming_hash,
        incoming_vclock,
        stored_hash,
        stored_vclock,
        None,
        local_corrected_mtime=incoming_corrected_mtime,
        remote_corrected_mtime=stored_corrected_mtime,
    )

    if decision is SyncDecision.in_sync:
        return ArbitrationResult(
            outcome=ArbitrationOutcome.accepted,
            record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
            record_corrected_mtime=incoming_corrected_mtime or stored_corrected_mtime,
        )
    if decision is SyncDecision.upload:
        return ArbitrationResult(
            outcome=ArbitrationOutcome.accepted,
            record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
            record_corrected_mtime=incoming_corrected_mtime,
        )
    if decision is SyncDecision.download:
        return ArbitrationResult(
            outcome=ArbitrationOutcome.superseded,
            record_vclock=stored_vclock or {},
            record_corrected_mtime=stored_corrected_mtime,
            loser_cid=None,
        )
    # conflict_upload: incoming wins tie-break ⇒ accept, preserve stored loser.
    if decision is SyncDecision.conflict_upload:
        loser_cid = stored["origin_cid"] if stored and stored["origin_cid"] else effective_origin
        return ArbitrationResult(
            outcome=ArbitrationOutcome.accepted_conflict_won,
            record_vclock=merge_vclocks(incoming_vclock, stored_vclock),
            record_corrected_mtime=incoming_corrected_mtime,
            loser_cid=loser_cid,
        )
    # conflict_download
    return ArbitrationResult(
        outcome=ArbitrationOutcome.conflict_lost,
        record_vclock=stored_vclock or {},
        record_corrected_mtime=stored_corrected_mtime,
        loser_cid=effective_origin,
    )


def conflicted_copy_name(file_location: str, cid: str | None, ts: str) -> str:
    """Build the canonical conflicted-copy path for a losing content.

    ``<stem>_conflicted_copy_<ts>_<cid>.<ext>`` (ADR-0021).
    """
    safe_cid = "".join(c for c in (cid or "unknown") if c.isalnum() or c in "-_.")[:32] or "unknown"
    stem = Path(file_location).stem
    suffix = Path(file_location).suffix
    parent = str(Path(file_location).parent)
    return f"{parent}/{stem}_conflicted_copy_{ts}_{safe_cid}{suffix}"
