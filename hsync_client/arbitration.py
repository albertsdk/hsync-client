# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Causal upload arbitration for the P2P master.

Mirrors the central server's gate using the client's own sync-state store
(``state DB``) and the master's CID. The P2P master is a transport coordinator,
not an authority: every slave upload is arbitrated causally against the stored
version for the path (ADR-0022), exactly as the central server does.

The decision is the shared reference implementation in
:mod:`hsync_common.models.arbitration`; this module supplies only the
client-side store/CID binding.
"""

from __future__ import annotations

from hsync_common.models.arbitration import (
    ArbitrationOutcome,
    ArbitrationResult,
    conflicted_copy_name,
    decide_arbitration,
    plan_self_record,
)
from hsync_common.models.version import new_version_id

from hsync_client.sync_state import get_sync_state_store

__all__ = [
    "ArbitrationOutcome",
    "ArbitrationResult",
    "arbitrate_upload",
    "conflicted_copy_name",
]


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
    advance = plan_self_record(stored, disk_hash, master_cid)
    if advance is not None:
        store.record(
            relpath,
            base_hash=advance.base_hash,
            vclock=advance.vclock,
            version_id=new_version_id(),
            origin_cid=master_cid,
        )
        stored = store.get(relpath)

    return decide_arbitration(
        stored=stored,
        incoming_hash=incoming_hash,
        incoming_vclock_raw=incoming_vclock_raw,
        incoming_version_id=incoming_version_id,
        incoming_origin_cid=incoming_origin_cid,
        incoming_corrected_mtime=incoming_corrected_mtime,
    )
