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
``EventDispatchContext`` describing its peculiarities (log prefix,
optional echo-suppression callbacks, fallback policy when a rename
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

from hsync_common.models.dir import Dir
from hsync_common.models.event import EventName
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
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
        log_prefix: String prepended to log tags, e.g. ``""`` for Remote
            (``[RENAME    ]``) or ``"LAN "`` for LAN slave
            (``[LAN RENAME]``).
        mark_receiving: Optional callback invoked before a local write
            to suppress the self-induced watchdog event.
        unmark_receiving: Optional callback invoked after a local write
            to release the suppression.
        create_symlink_locally: If ``True`` a ``symlink`` event recreates
            the link via ``os.symlink``; if ``False`` the entry is
            fetched as a regular file.
        recover_on_missing_rename_src: If ``True`` and a rename's source
            does not exist locally, the destination is materialised
            (downloaded or resynced) instead of being silently dropped.
    """

    log_prefix: str = ""
    mark_receiving: Callable[[str], None] | None = None
    unmark_receiving: Callable[[str], None] | None = None
    create_symlink_locally: bool = False
    recover_on_missing_rename_src: bool = False


def _tag(prefix: str, name: str) -> str:
    """Format a 10-character log tag (e.g. ``[RENAME    ]`` or ``[LAN RENAME]``)."""
    return f"[{prefix}{name:<{10 - len(prefix)}}]"


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

    own_cid = CONFIG.get("CID")
    if own_cid and event_data.get("cid") == own_cid:
        log.debug(f"{_tag(ctx.log_prefix, 'ECHO')} Ignoring own event: {event_name}")
        return

    if CONFIG.get("SYNC_DIRECTION") == "upload-only":
        log.debug(f"Upload-only mode: {ctx.log_prefix.strip() or 'remote'} event ignored.")
        return

    if not src:
        log.warning("Event missing 'src' field")
        return

    if event_name in {EventName.added.value, EventName.modified.value}:
        await _apply_add_or_modify(src, ctx)
    elif event_name == EventName.renamed.value:
        await _apply_rename(src, dst, ctx)
    elif event_name == EventName.deleted.value:
        await _apply_delete(src, ctx)
    elif event_name == EventName.symlink.value:
        await _apply_symlink(src, ctx)


async def _apply_add_or_modify(src: dict, ctx: EventDispatchContext) -> None:
    """Materialise an added/modified directory or file locally."""
    if "path" in src:
        target = _resolve_local_path(src, "path") if ctx.mark_receiving else None
        _mark(ctx, target) if target else None
        try:
            await asyncio.to_thread(update_dir, src)
        finally:
            if target:
                _unmark(ctx, target)
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


async def _apply_rename(src: dict, dst: dict | None, ctx: EventDispatchContext) -> None:
    """Apply a rename event for either a directory or a file."""
    if not dst:
        log.warning("Rename event missing 'dst' field")
        return

    if "path" in src:
        src_dir = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
        dst_dir = Dir.from_dict(dst, CONFIG["LOCAL_BUCKET"])
        await _rename_path(
            src_dir.path,
            dst_dir.path,
            ctx,
            recover=lambda: update_dir(dst),
        )
    elif "filepath" in src:
        src_file = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        dst_file = File.from_dict(dst, CONFIG["LOCAL_BUCKET"], False)
        await _rename_path(
            src_file.filepath,
            dst_file.filepath,
            ctx,
            recover=lambda: download_file(dst_file),
        )


async def _rename_path(
    src_path: str | None,
    dst_path: str | None,
    ctx: EventDispatchContext,
    recover: Callable[[], None],
) -> None:
    """Rename ``src_path`` to ``dst_path``, with optional recovery on missing source."""
    if not (src_path and dst_path):
        return
    _mark(ctx, src_path)
    _mark(ctx, dst_path)
    try:
        if Path(src_path).exists():
            log.info(f'{_tag(ctx.log_prefix, "RENAME")} "{src_path}" -> "{dst_path}"')
            emit_event(UiEventName.rename, src=src_path, dst=dst_path)
            parent = str(Path(dst_path).parent)
            if parent and not Path(parent).exists():
                Path(parent).mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(rename, src_path, dst_path)
        elif ctx.recover_on_missing_rename_src:
            log.warning(f'Cannot rename: "{src_path}" -> "{dst_path}"')
            log.warning(f'Not found: "{src_path}"')
            await asyncio.to_thread(recover)
    finally:
        _unmark(ctx, src_path)
        _unmark(ctx, dst_path)


async def _apply_delete(src: dict, ctx: EventDispatchContext) -> None:
    """Apply a delete event for either a directory or a file."""
    if CONFIG.get("IGNORE_DELETE", False):
        log.debug(f"{_tag(ctx.log_prefix, 'IGN DEL')} Deletion ignored (ignore-delete mode)")
        return

    if "path" in src:
        dir_obj = Dir.from_dict(src, CONFIG["LOCAL_BUCKET"])
        if not dir_obj.path:
            return
        _mark(ctx, dir_obj.path)
        try:
            if Path(dir_obj.path).exists():
                log.info(f'{_tag(ctx.log_prefix, "DELETE")} "{dir_obj.path}"')
                emit_event(UiEventName.transfer, op="delete", file=dir_obj.path)
                await asyncio.to_thread(rmtree, dir_obj.path, False)
            else:
                log.warning(f'Cannot delete: "{dir_obj.path}"')
                log.warning(f'Not found: "{dir_obj.path}"')
        finally:
            _unmark(ctx, dir_obj.path)
    elif "filepath" in src:
        file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        if not file_obj.filepath:
            return
        _mark(ctx, file_obj.filepath)
        try:
            if Path(file_obj.filepath).exists():
                log.info(f'{_tag(ctx.log_prefix, "DELETE")} "{file_obj.filepath}"')
                emit_event(UiEventName.transfer, op="delete", file=file_obj.filepath)
                await asyncio.to_thread(_remove_locked, file_obj.filepath)
                if Path(file_obj.filepath).name == HSYNCKEEP_FILENAME:
                    parent = str(Path(file_obj.filepath).parent)
                    if safe_rmdir_if_empty(parent):
                        log.debug(f'Removed empty directory "{parent}"')
            else:
                log.warning(f'Cannot delete: "{file_obj.filepath}"')
                log.warning(f'Not found: "{file_obj.filepath}"')
        finally:
            _unmark(ctx, file_obj.filepath)


async def _apply_symlink(src: dict, ctx: EventDispatchContext) -> None:
    """Apply a symlink event: either recreate the link locally or fetch as a file."""
    if ctx.create_symlink_locally:
        if src.get("type") != "symlink":
            return
        link = Symlink.from_dict(src)
        link.filepath = f"{CONFIG['LOCAL_BUCKET']}/{link.filepath.lstrip('/')}"
        target = link.target
        if not (link.filepath and target):
            return
        log.info(
            f'{_tag(ctx.log_prefix, "SYMLINK")} Creating symlink: "{link.filepath}" -> "{target}"'
        )
        parent = str(Path(link.filepath).parent)
        if not Path(parent).exists():
            Path(parent).mkdir(parents=True, exist_ok=True)
        if Path(link.filepath).is_symlink() or Path(link.filepath).exists():
            unlink(link.filepath)
        await asyncio.to_thread(os_symlink, target, link.filepath)
    elif "filepath" in src:
        file_obj = File.from_dict(src, CONFIG["LOCAL_BUCKET"], False)
        if file_obj.filepath:
            _mark(ctx, file_obj.filepath)
            try:
                await asyncio.to_thread(update_file, src)
            finally:
                _unmark(ctx, file_obj.filepath)
