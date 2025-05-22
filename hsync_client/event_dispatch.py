# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Unified inbound event application for HSYNC Client.

Both Remote sync (events from the central server) and LAN slave mode
(events from the master) need to apply the same set of file system
operations — add/modify a file or directory, rename, delete, recreate a
symlink — in response to events received over a WebSocket. This module
factors that logic out so each transport only needs to provide a small
``EventDispatchContext`` describing its peculiarities (optional
echo-suppression callbacks, fallback policy when a rename
source is missing, symlink handling strategy).

All blocking I/O is dispatched to a worker thread via
``asyncio.to_thread`` so the WebSocket event loop stays responsive
while large transfers are in flight.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from os import remove, rename, unlink
from os import symlink as os_symlink
from pathlib import Path
from shutil import rmtree

from hsync_common.labels import resolve_label
from hsync_common.models.dir import Dir
from hsync_common.models.event import EventName
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import (
    is_safe_symlink_target,
    resolve_within,
    validate_remote_relpath,
    wire_relpaths,
)
from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME
from hsync_client.events import UiEventName, emit_event
from hsync_client.sync import update_dir, update_file
from hsync_client.transfer import download_file
from hsync_client.utils import path_lock, safe_rmdir_if_empty


def _remove_locked(filepath: str) -> bool:
    """Remove *filepath* under its per-path lock; return True if removed.

    Re-checks existence inside the lock so a concurrent transfer that just
    (re)wrote the same path is not torn by the delete.
    """
    with path_lock(filepath):
        if Path(filepath).exists():
            remove(filepath)
            return True
    return False


@dataclass(frozen=True)
class EventDispatchContext:
    """Per-transport behaviour for inbound event application.

    Attributes:
        mark_receiving: Optional callback invoked before a local write
            to suppress the self-induced watchdog event.
        unmark_receiving: Optional callback invoked after a local write
            to release the suppression.
        record_inbound_rename: Optional callback invoked after a local
            rename with the exact ``(src_path, dst_path)`` pair, so a
            later watchdog echo returning a 404 from the master can be
            recognised as benign rather than a real divergence.
        create_symlink_locally: If ``True`` a ``symlink`` event recreates
            the link via ``os.symlink``; if ``False`` the entry is
            fetched as a regular file.
        recover_on_missing_rename_src: If ``True`` and a rename's source
            does not exist locally, the destination is materialised
            (downloaded or resynced) instead of being silently dropped.
    """

    mark_receiving: Callable[[str], None] | None = None
    unmark_receiving: Callable[[str], None] | None = None
    record_inbound_rename: Callable[[str, str], None] | None = None
    create_symlink_locally: bool = False
    recover_on_missing_rename_src: bool = False


#: Prefix for every inbound-dispatch label: the event is always caused by a
#: remote peer (the master for a LAN slave, the central server in remote
#: mode), so the origin prefix is constant for this whole module.
INBOUND_PREFIX = "REMOTE "


def _tag(name: str) -> str:
    """Format an inbound-dispatch log tag (e.g. ``[REMOTE RENAME]``).

    Thin wrapper over :func:`hsync_common.labels.resolve_label`, which resolves
    the label through :class:`ActionLabel` so the colorization system in
    ``hsync_common.labels`` can match it. Shared with the watchdog handlers
    via ``BaseFileEventHandler._label`` to keep the two transports in sync.
    """
    return resolve_label(INBOUND_PREFIX, name)


def _confined_local(rel: object, event_name: object, ctx: EventDispatchContext) -> str | None:
    """Resolve a wire path inside ``LOCAL_BUCKET``, rejecting symlink escapes.

    The inbound gate validates paths lexically, but lexical safety cannot stop
    a symlink *inside* the bucket from redirecting the operation outside it
    (e.g. the bucket legitimately contains ``docs -> /home/user`` and a
    hostile peer sends ``deleted`` for ``docs/passwd``). Returns the confined
    absolute path, or ``None`` after logging/emitting the rejection.
    """
    bucket = CONFIG.get("LOCAL_BUCKET", "")
    local = resolve_within(bucket, rel) if isinstance(rel, str) and bucket else None
    if local is None:
        _reject_unsafe(event_name, rel if isinstance(rel, str) else None, ctx)
    return local


def _reject_unsafe(event_name: object, rel: str | None, ctx: EventDispatchContext) -> None:
    """Log and surface the rejection of an unsafe remote-supplied path."""
    log.warning(f"{_tag('SECURITY')} Rejected unsafe {event_name} path: {rel!r}")
    emit_event(
        UiEventName.security_rejected,
        op=str(event_name),
        path=rel or "",
        message="unsafe remote path rejected",
    )


def _mark(ctx: EventDispatchContext, path: str) -> None:
    if ctx.mark_receiving:
        ctx.mark_receiving(path)


def _unmark(ctx: EventDispatchContext, path: str) -> None:
    if ctx.unmark_receiving:
        ctx.unmark_receiving(path)


async def apply_inbound_event(event_data: dict, ctx: EventDispatchContext) -> None:
    """Apply an event received from a remote peer (central server or LAN master).

    Performs CID-based echo suppression and upload-only filtering, then
    dispatches to the appropriate per-action helper. Unknown event names
    are silently ignored (caller-side validation is expected).
    """
    event_name = event_data.get("name")
    src = event_data.get("src")
    dst = event_data.get("dst")
    origin_cid = event_data.get("cid")

    own_cid = CONFIG.get("CID")
    if own_cid and origin_cid == own_cid:
        log.debug(f"{_tag('ECHO')} Ignoring own event: {event_name}")
        return

    if CONFIG.get("SYNC_DIRECTION") == "upload-only":
        log.debug("Upload-only mode: remote-origin event ignored.")
        return

    if not src:
        log.warning("Event missing 'src' field")
        return

    # Paths arriving from a remote peer are untrusted input: reject
    # traversal, absolute paths, backslashes and internal engine artifacts
    # before any local filesystem operation is derived from them. This is the
    # single choke point covering remote mode (hostile server) and LAN slave
    # mode (hostile master) alike.
    for payload in (p for p in (src, dst) if isinstance(p, dict)):
        rels = wire_relpaths(payload)
        if not rels or any(validate_remote_relpath(rel) is None for rel in rels):
            _reject_unsafe(event_name, rels[0] if rels else None, ctx)
            return

    if event_name in {EventName.added.value, EventName.modified.value}:
        await _apply_add_or_modify(src, ctx, event_name, origin_cid)
    elif event_name == EventName.renamed.value:
        await _apply_rename(src, dst, ctx, origin_cid)
    elif event_name == EventName.deleted.value:
        await _apply_delete(src, ctx, origin_cid)
    elif event_name == EventName.symlink.value:
        await _apply_symlink(src, ctx, origin_cid)


async def _apply_add_or_modify(
    src: dict, ctx: EventDispatchContext, event_name: object, origin_cid: object = None
) -> None:
    """Materialise an added/modified directory or file locally."""
    if "path" in src:
        target = _resolve_local_path(src, "path") if ctx.mark_receiving else None
        _mark(ctx, target) if target else None
        try:
            # Directories carry no content hash, so update_dir only ever
            # creates the directory or adjusts its permissions; log the
            # inbound create/modify explicitly so the slave's log shows the
            # directory event (the file branch is already covered by
            # download_file's [DOWNLOAD] line). The log is emitted only after
            # update_dir succeeds: a security-rejected path (symlink escape)
            # returns None and is logged by update_dir itself as [SECURITY],
            # so we must not pre-log it here as [CREATE]/[MODIFY] (misleading).
            result = await asyncio.to_thread(update_dir, src)
        finally:
            if target:
                _unmark(ctx, target)
        if result is not None:
            label = "CREATE" if event_name == EventName.added.value else "MODIFY"
            log.info(f'{_tag(label)} "{target or src.get("path")}"{_cid_suffix(origin_cid)}')
            emit_event(
                UiEventName.transfer,
                op="create" if label == "CREATE" else "modify",
                dir=target or "",
            )
    elif "filepath" in src:
        target = _resolve_local_path(src, "filepath") if ctx.mark_receiving else None
        _mark(ctx, target) if target else None
        try:
            await asyncio.to_thread(update_file, src)
        finally:
            if target:
                _unmark(ctx, target)


def _resolve_local_path(src: dict, key: str) -> str | None:
    """Return the LOCAL_BUCKET-prefixed path for ``src``, or ``None`` on failure."""
    bucket = CONFIG.get("LOCAL_BUCKET")
    if not bucket:
        return None
    if key == "path":
        return Dir.from_dict(src, bucket).path
    return File.from_dict(src, bucket, False).filepath


async def _apply_rename(
    src: dict, dst: dict | None, ctx: EventDispatchContext, origin_cid: object = None
) -> None:
    """Apply a rename event for either a directory or a file."""
    if not dst:
        log.warning("Rename event missing 'dst' field")
        return

    if "path" in src:
        src_local = _confined_local(src.get("path"), EventName.renamed.value, ctx)
        dst_local = _confined_local(dst.get("path"), EventName.renamed.value, ctx)
        if src_local is None or dst_local is None:
            return
        await _rename_path(
            src_local,
            dst_local,
            ctx,
            recover=lambda: update_dir(dst),
            origin_cid=origin_cid,
        )
    elif "filepath" in src:
        src_local = _confined_local(src.get("filepath"), EventName.renamed.value, ctx)
        dst_local = _confined_local(dst.get("filepath"), EventName.renamed.value, ctx)
        if src_local is None or dst_local is None:
            return
        dst_file = File.from_dict(dst, CONFIG["LOCAL_BUCKET"], False)
        await _rename_path(
            src_local,
            dst_local,
            ctx,
            recover=lambda: download_file(dst_file),
            origin_cid=origin_cid,
        )


def _cid_suffix(origin_cid: object) -> str:
    """Return a ``(CID: xxx)`` suffix for log lines, or ``""`` when unknown."""
    return f" (CID: {origin_cid})" if origin_cid else ""


async def _rename_path(
    src_path: str | None,
    dst_path: str | None,
    ctx: EventDispatchContext,
    recover: Callable[[], None],
    origin_cid: object = None,
) -> None:
    """Rename ``src_path`` to ``dst_path``, with optional recovery on missing source."""
    if not (src_path and dst_path):
        return
    _mark(ctx, src_path)
    _mark(ctx, dst_path)
    try:
        if Path(src_path).exists():
            log.info(f'{_tag("RENAME")} "{src_path}" -> "{dst_path}"{_cid_suffix(origin_cid)}')
            emit_event(UiEventName.rename, src=src_path, dst=dst_path)
            parent = str(Path(dst_path).parent)
            if parent and not Path(parent).exists():
                Path(parent).mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(rename, src_path, dst_path)
            if ctx.record_inbound_rename:
                ctx.record_inbound_rename(src_path, dst_path)
        elif ctx.recover_on_missing_rename_src:
            log.warning(f'Cannot rename: "{src_path}" -> "{dst_path}"')
            log.warning(f'Not found: "{src_path}"')
            await asyncio.to_thread(recover)
    finally:
        _unmark(ctx, src_path)
        _unmark(ctx, dst_path)


async def _apply_delete(src: dict, ctx: EventDispatchContext, origin_cid: object = None) -> None:
    """Apply a delete event for either a directory or a file."""
    if CONFIG.get("IGNORE_DELETE", False):
        log.debug(f"{_tag('IGNORE DEL')} Deletion ignored (ignore-delete mode)")
        return

    if "path" in src:
        local = _confined_local(src.get("path"), EventName.deleted.value, ctx)
        if local is None:
            return
        _mark(ctx, local)
        try:
            if Path(local).exists():
                log.info(f'{_tag("DELETE")} "{local}"{_cid_suffix(origin_cid)}')
                emit_event(UiEventName.transfer, op="delete", file=local)
                await asyncio.to_thread(rmtree, local, False)
            else:
                # The path is already absent locally: this is a benign echo
                # (the originator deleted it, or a peer's delete converged
                # after a local deletion, or the path never existed here).
                # Mirrors the master's "File already deleted" success branch
                # (lan_server.py:_handle_delete) and the rename 404-echo
                # degradation (_rename_on_master): a WARNING here is noise
                # because there is nothing to act on; the periodic resync
                # catches genuine divergences.
                log.debug(f'{_tag("DELETE")} Already absent (benign echo): "{local}"')
        finally:
            _unmark(ctx, local)
    elif "filepath" in src:
        local = _confined_local(src.get("filepath"), EventName.deleted.value, ctx)
        if local is None:
            return
        _mark(ctx, local)
        try:
            if Path(local).exists():
                log.info(f'{_tag("DELETE")} "{local}"{_cid_suffix(origin_cid)}')
                emit_event(UiEventName.transfer, op="delete", file=local)
                await asyncio.to_thread(_remove_locked, local)
                if Path(local).name == HSYNCKEEP_FILENAME:
                    parent = str(Path(local).parent)
                    if safe_rmdir_if_empty(parent):
                        log.debug(f'Removed empty directory "{parent}"')
            else:
                # The path is already absent locally: this is a benign echo
                # (the originator deleted it, or a peer's delete converged
                # after a local deletion, or the path never existed here).
                # Mirrors the master's "File already deleted" success branch
                # (lan_server.py:_handle_delete) and the rename 404-echo
                # degradation (_rename_on_master): a WARNING here is noise
                # because there is nothing to act on; the periodic resync
                # catches genuine divergences.
                log.debug(f'{_tag("DELETE")} Already absent (benign echo): "{local}"')
        finally:
            _unmark(ctx, local)


async def _apply_symlink(src: dict, ctx: EventDispatchContext, origin_cid: object = None) -> None:
    """Apply a symlink event: either recreate the link locally or fetch as a file."""
    if ctx.create_symlink_locally:
        if src.get("type") != "symlink":
            return
        link = Symlink.from_dict(src)
        # The link location must be confined (a symlink *inside* the bucket
        # could otherwise redirect the create/unlink outside it); the target
        # must also stay inside the bucket, otherwise a hostile peer could
        # plant a symlink to arbitrary local files (e.g. /etc/passwd).
        local = _confined_local(link.filepath, EventName.symlink.value, ctx)
        if local is None:
            return
        target = link.target
        if not target:
            return
        if not is_safe_symlink_target(local, target, CONFIG["LOCAL_BUCKET"]):
            _reject_unsafe(EventName.symlink.value, target, ctx)
            return
        log.info(
            f'{_tag("SYMLINK")} Creating symlink: "{local}" -> "{target}"{_cid_suffix(origin_cid)}'
        )
        parent = str(Path(local).parent)
        if not Path(parent).exists():
            Path(parent).mkdir(parents=True, exist_ok=True)
        if Path(local).is_symlink() or Path(local).exists():
            unlink(local)
        await asyncio.to_thread(os_symlink, target, local)
    elif "filepath" in src:
        file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        if file_obj.filepath:
            _mark(ctx, file_obj.filepath)
            try:
                await asyncio.to_thread(update_file, src)
            finally:
                _unmark(ctx, file_obj.filepath)
