# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Preview and stdin-command helpers shared across server and LAN modes.

Extracted from ``main_logic.py`` and ``lan_handler.py`` to break a circular
import between the two modules and eliminate duplicated code.
"""

import contextlib
from typing import Literal

from hsync_common.format import format_bytes
from hsync_common.models.file import clear_sha256_cache
from hsync_common.models.sync_action import SyncActionName
from hsync_common.protocol.pairing import lan_write_requires_key

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME, SYNC_DIRECTIONS, ActionLabel
from hsync_client.events import UiEventName, emit_event
from hsync_client.lan_inventory import InventoryObserver
from hsync_client.lan_sources import LanSourceResolver
from hsync_client.presence import reemit_presence
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.sync import (
    ensure_hsynckeep_files,
    get_files_local,
    get_files_remote,
    get_sync_actions,
    get_tombstones_remote,
    sync,
)
from hsync_client.utils import reset_bandwidth_throttle

_BANDWIDTH_KEY = Literal["MAX_BANDWIDTH_UPLOAD", "MAX_BANDWIDTH_DOWNLOAD"]


def _lan_write_blocked(direction: str, allow_unauthenticated_writes: bool, log, hint: str) -> bool:
    """Return True (and report) when a LAN write requires auth that is missing.

    The policy predicate lives in ``hsync_common.protocol.pairing.lan_write_requires_key``;
    this helper only applies the user-facing consequence (log + ``auth_required``
    event). Shared by :func:`_lan_write_allowed` (runtime-triggered) and
    ``main_logic._check_lan_write_policy`` (CLI-triggered), which differ only in
    the remediation wording (env vars vs CLI flags).
    """
    if not lan_write_requires_key(
        direction,
        allow_unauthenticated_writes=allow_unauthenticated_writes,
    ):
        return False
    log.error(f"{ActionLabel.AUTH_REQUIRED} LAN writes require authentication. {hint}")
    emit_event(
        UiEventName.auth_required,
        message="A shared LAN key or explicit unauthenticated-write opt-in is required",
    )
    return True


def _lan_write_allowed(direction: str, log) -> bool:
    """Enforce the no-key write policy for runtime-triggered LAN syncs."""
    if not CONFIG.get("LAN_MODE", False) or CONFIG.get("LAN_KEY"):
        return True
    return not _lan_write_blocked(
        direction,
        allow_unauthenticated_writes=CONFIG.get("LAN_ALLOW_UNAUTHENTICATED_WRITES", False),
        log=log,
        hint="Set HSYNC_LAN_KEY, use download-only, or explicitly allow unauthenticated writes.",
    )


def _apply_bandwidth(value: object, key: _BANDWIDTH_KEY, name: str, log) -> bool:
    """Parse *value* (``0``/``"0"``/int/str) into ``CONFIG[key]``.

    Returns False (after logging) when the value is not a valid bandwidth.
    """
    if value == 0 or value == "0":
        CONFIG[key] = None
    elif isinstance(value, int):
        CONFIG[key] = value
    elif isinstance(value, str):
        try:
            CONFIG[key] = int(value)
        except (ValueError, TypeError):
            log.warning(f"Invalid {name} bandwidth value: {value!r}")
            return False
    else:
        log.warning(f"Invalid {name} bandwidth value: {value!r}")
        return False
    return True


def _run_preview(log) -> None:
    """
    Run a preview (dry-run) showing pending sync actions.

    This is triggered by a ``PREVIEW`` command on stdin and shows what
    would be synced without actually performing any transfers.

    Emits structured log messages for each file with prefixes:
    - [SYNCED] - file is in sync (no action needed)
    - [TO DOWNLOAD] - file needs to be downloaded
    - [TO UPLOAD] - file needs to be uploaded
    - [TO DELETE] - file will be deleted
    """
    log.info("--- Preview (dry-run) ---")
    emit_event(UiEventName.preview_start)
    try:
        # Clear SHA-256 cache so every file is re-hashed with fresh content
        clear_sha256_cache()

        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        local_files = get_files_local()
        remote_files = get_files_remote()
        tombstones = get_tombstones_remote()
        lan_mode = CONFIG.get("LAN_MODE", False)
        sync_actions = get_sync_actions(
            remote_files,
            local_files,
            lan_mode=lan_mode,
            tombstones=tombstones,
            persist_state=False,
        )

        def _is_sentinel(action) -> bool:
            """Return True if the action concerns a .hsynckeep sentinel."""
            fp = action.file.filepath or ""
            return fp.endswith(HSYNCKEEP_FILENAME)

        downloads = [
            a for a in sync_actions if a.name == SyncActionName.download and not _is_sentinel(a)
        ]
        uploads = [
            a for a in sync_actions if a.name == SyncActionName.upload and not _is_sentinel(a)
        ]
        deletes = [
            a for a in sync_actions if a.name == SyncActionName.delete and not _is_sentinel(a)
        ]
        synced = [
            a for a in sync_actions if a.name == SyncActionName.nothing and not _is_sentinel(a)
        ]

        # Detect renames: DELETE + DOWNLOAD pairs with same sha256sum and size.
        # When a file is renamed on the master, the client sees a delete of
        # the old path and a download of the new path.  If both share the
        # same sha256 checksum and size the operation is really just a rename.
        renames: list[tuple] = []  # (delete_action, download_action)
        if deletes and downloads:
            delete_by_hash: dict[tuple, list] = {}
            for action in deletes:
                key = (action.file.sha256sum, action.file.size or 0)
                if key[0]:  # sha256sum must be available
                    delete_by_hash.setdefault(key, []).append(action)

            matched_download_idx: set[int] = set()
            matched_delete_ids: set[int] = set()
            for i, dl_action in enumerate(downloads):
                key = (dl_action.file.sha256sum, dl_action.file.size or 0)
                if key[0] and key in delete_by_hash:
                    for del_action in delete_by_hash[key]:
                        if id(del_action) not in matched_delete_ids:
                            renames.append((del_action, dl_action))
                            matched_download_idx.add(i)
                            matched_delete_ids.add(id(del_action))
                            break

            # Remove matched items so they are not emitted as separate events
            downloads = [a for i, a in enumerate(downloads) if i not in matched_download_idx]
            deletes = [a for a in deletes if id(a) not in matched_delete_ids]

        # Emit structured messages for each file status (text log + event channel)
        for actions, label, state in [
            (synced, ActionLabel.SYNCED, "synced"),
            (downloads, ActionLabel.TO_DOWNLOAD, "to_download"),
            (uploads, ActionLabel.TO_UPLOAD, "to_upload"),
            (deletes, ActionLabel.TO_DELETE, "to_delete"),
        ]:
            for action in actions:
                size = action.file.size or 0
                log.info(f'{label} "{action.file.filepath}" ({format_bytes(size)})')
                emit_event(
                    UiEventName.file_status, state=state, file=action.file.filepath, size=size
                )
        for del_action, dl_action in renames:
            size = dl_action.file.size or 0
            log.info(
                f'{ActionLabel.TO_RENAME} "{del_action.file.filepath}" -> '
                f'"{dl_action.file.filepath}" ({format_bytes(size)})'
            )
            emit_event(
                UiEventName.rename_status,
                src=del_action.file.filepath,
                dst=dl_action.file.filepath,
                size=size,
            )

        log.info(
            f"Preview: {len(synced)} synced, {len(downloads)} downloads, "
            f"{len(uploads)} uploads, {len(deletes)} deletes, "
            f"{len(renames)} renames"
        )
    except Exception as e:
        log.error(f"Preview failed: {e}")
        emit_event(UiEventName.error, message=f"Preview failed: {e}")
    log.info("--- End Preview ---")
    emit_event(UiEventName.preview_end)


async def _with_stdin_commands(
    log,
    coro,
    inventory_observer: InventoryObserver | None = None,
    source_resolver: LanSourceResolver | None = None,
) -> None:
    """Run *coro* with a background stdin command reader.

    The reader listens for JSON commands on stdin (one per line) and
    dispatches them to the appropriate handler.  ``PREVIEW`` triggers
    a dry-run preview and ``SYNC`` runs a full synchronisation.

    When *coro* finishes (or raises), the reader is cancelled.
    """
    import asyncio

    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))
    handler.register("PEERS", lambda **_kw: reemit_presence())

    async def _handle_sync(**_kw: object) -> None:
        lan = CONFIG.get("LAN_MODE", False)
        if not _lan_write_allowed(CONFIG.get("SYNC_DIRECTION", "bidirectional"), log):
            return
        await sync(
            lan_mode=lan,
            inventory_observer=inventory_observer,
            source_resolver=source_resolver,
        )

    handler.register("SYNC", _handle_sync)

    async def _handle_set_sync_direction(**kw: object) -> None:
        direction = str(kw.get("direction", "bidirectional"))
        if direction not in SYNC_DIRECTIONS:
            log.warning(f"Invalid sync direction: {direction!r}. Must be one of {SYNC_DIRECTIONS}.")
            return
        old = CONFIG.get("SYNC_DIRECTION", "bidirectional")
        if direction == old:
            log.debug(f"Sync direction already set to {direction!r}, no change.")
            return
        if not _lan_write_allowed(direction, log):
            return
        CONFIG["SYNC_DIRECTION"] = direction
        log.info(f"Sync direction changed: {old!r} → {direction!r}")
        # Trigger an immediate resync with the new direction
        lan = CONFIG.get("LAN_MODE", False)
        await sync(
            lan_mode=lan,
            inventory_observer=inventory_observer,
            source_resolver=source_resolver,
        )

    handler.register("SET_SYNC_DIRECTION", _handle_set_sync_direction)

    async def _handle_set_bandwidth(**kw: object) -> None:
        upload = kw.get("upload")
        download = kw.get("download")

        if upload is not None and not _apply_bandwidth(
            upload, "MAX_BANDWIDTH_UPLOAD", "upload", log
        ):
            return
        if download is not None and not _apply_bandwidth(
            download, "MAX_BANDWIDTH_DOWNLOAD", "download", log
        ):
            return

        upload_str = (
            format_bytes(CONFIG["MAX_BANDWIDTH_UPLOAD"]) + "/s"
            if CONFIG["MAX_BANDWIDTH_UPLOAD"]
            else "unlimited"
        )
        download_str = (
            format_bytes(CONFIG["MAX_BANDWIDTH_DOWNLOAD"]) + "/s"
            if CONFIG["MAX_BANDWIDTH_DOWNLOAD"]
            else "unlimited"
        )
        log.info(f"Bandwidth limits updated: upload={upload_str}, download={download_str}")
        # Reset throttle state to apply new limits
        reset_bandwidth_throttle()

    handler.register("SET_BANDWIDTH", _handle_set_bandwidth)

    stdin_task = asyncio.create_task(handler.run())

    try:
        await coro
    finally:
        stdin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stdin_task


async def _run_stdin_wait(
    log,
    inventory_observer: InventoryObserver | None = None,
    source_resolver: LanSourceResolver | None = None,
) -> None:
    """Block reading stdin commands until EOF or SYNC.

    Used in continuous dry-run mode (LAN or server) where there is no
    WebSocket connection yet.  When a ``SYNC`` command arrives the
    handler performs the full sync, then stops the reader so the caller
    can proceed to the next phase (e.g. WebSocket connection).
    """
    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))
    handler.register("PEERS", lambda **_kw: reemit_presence())

    async def _handle_sync(**_kw: object) -> None:
        if not _lan_write_allowed(CONFIG.get("SYNC_DIRECTION", "bidirectional"), log):
            return
        CONFIG["DRYRUN"] = False
        lan = CONFIG.get("LAN_MODE", False)
        await sync(
            lan_mode=lan,
            inventory_observer=inventory_observer,
            source_resolver=source_resolver,
        )
        handler.stop()

    handler.register("SYNC", _handle_sync)
    await handler.run()
