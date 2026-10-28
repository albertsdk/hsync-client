# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Preview and stdin-command helpers shared across server and P2P modes.

Extracted from ``main_logic.py`` and ``p2p_handler.py`` to break a circular
import between the two modules and eliminate duplicated code.
"""

import contextlib
from collections.abc import Callable
from typing import Literal

from hsync_common.format import format_bytes
from hsync_common.models.file import clear_sha256_cache
from hsync_common.models.sync_action import SyncActionName
from hsync_common.protocol.pairing import p2p_write_requires_key

from hsync_client.config import CONFIG, deletions_are_kept
from hsync_client.constants import HSYNCKEEP_FILENAME, SYNC_DIRECTIONS, ActionLabel
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import AuthenticationError, NetworkError, ServerError
from hsync_client.p2p_inventory import InventoryObserver
from hsync_client.p2p_sources import P2PSourceResolver
from hsync_client.presence import reemit_presence
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.sync import (
    _mass_deletion_blocked,
    emit_folder_size,
    ensure_hsynckeep_files,
    get_files_local,
    get_files_remote,
    get_sync_actions,
    get_tombstones_remote,
    log_initial_sync_start,
    split_rename_pairs,
    sync,
)
from hsync_client.utils import reset_bandwidth_throttle

_BANDWIDTH_KEY = Literal["MAX_BANDWIDTH_UPLOAD", "MAX_BANDWIDTH_DOWNLOAD"]


def _p2p_write_blocked(direction: str, allow_unauthenticated_writes: bool, log, hint: str) -> bool:
    """Return True (and report) when a P2P write requires auth that is missing.

    The policy predicate lives in ``hsync_common.protocol.pairing.p2p_write_requires_key``;
    this helper only applies the user-facing consequence (log + ``auth_required``
    event). Shared by :func:`_p2p_write_allowed` (runtime-triggered) and
    ``main_logic._check_p2p_write_policy`` (CLI-triggered), which differ only in
    the remediation wording (env vars vs CLI flags).
    """
    if not p2p_write_requires_key(
        direction,
        allow_unauthenticated_writes=allow_unauthenticated_writes,
    ):
        return False
    log.error(f"{ActionLabel.AUTH_REQUIRED} P2P writes require authentication. {hint}")
    emit_event(
        UiEventName.auth_required,
        message="A shared P2P key or explicit unauthenticated-write opt-in is required",
    )
    return True


def _p2p_write_allowed(direction: str, log) -> bool:
    """Enforce the no-key write policy for runtime-triggered P2P syncs."""
    if not CONFIG.get("P2P_MODE", False) or CONFIG.get("P2P_KEY"):
        return True
    return not _p2p_write_blocked(
        direction,
        allow_unauthenticated_writes=CONFIG.get("P2P_ALLOW_UNAUTHENTICATED_WRITES", False),
        log=log,
        hint=(
            "Set a shared password (HSYNC_P2P_PASSPHRASE) or raw key "
            "(HSYNC_P2P_KEY), use download-only, or explicitly allow "
            "unauthenticated writes."
        ),
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


async def _handle_set_bandwidth(log, **kw: object) -> None:
    """Apply runtime upload/download limits from a ``SET_BANDWIDTH`` command."""
    upload = kw.get("upload")
    download = kw.get("download")

    if upload is not None and not _apply_bandwidth(upload, "MAX_BANDWIDTH_UPLOAD", "upload", log):
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
    # Reset throttle state to apply new limits.
    reset_bandwidth_throttle()


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
        emit_folder_size(local_files, remote_files)
        tombstones = get_tombstones_remote()
        p2p_mode = CONFIG.get("P2P_MODE", False)
        sync_actions = get_sync_actions(
            remote_files,
            local_files,
            p2p_mode=p2p_mode,
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
        delete_remotes = [
            a
            for a in sync_actions
            if a.name == SyncActionName.delete_remote and not _is_sentinel(a)
        ]
        synced = [
            a for a in sync_actions if a.name == SyncActionName.nothing and not _is_sentinel(a)
        ]

        # Detect renames in both directions through the helper the run itself
        # uses, so the plan a confirmation shows and the plan that executes
        # cannot disagree: a move arrives either as delete + download (renamed
        # elsewhere) or as delete_remote + upload (renamed here while offline),
        # and both halves share the content hash and size.
        renames: list[tuple] = []
        for new_actions, old_actions in ((downloads, deletes), (uploads, delete_remotes)):
            pairs, remaining_new, remaining_old = split_rename_pairs(
                [action.file for action in new_actions],
                [action.file for action in old_actions],
            )
            renames.extend(pairs)
            keep_new = {id(file) for file in remaining_new}
            keep_old = {id(file) for file in remaining_old}
            new_actions[:] = [a for a in new_actions if id(a.file) in keep_new]
            old_actions[:] = [a for a in old_actions if id(a.file) in keep_old]

        # A rename still arrives under `keep` (it is a move, the content
        # survives), but a plain deletion never does: the preview must not
        # promise a removal the run is configured to skip. The count is reported
        # rather than dropped: "nothing to delete" and "deletions kept" are two
        # different answers for the confirmation a front-end shows.
        if deletes and deletions_are_kept(CONFIG):
            emit_event(
                UiEventName.preview_skipped,
                reason="deletion_policy",
                count=len(deletes),
                size=sum(action.file.size or 0 for action in deletes),
            )
            deletes = []
        elif deletes:
            # The guard pauses only *unconfirmed* deletions (a tombstone proves
            # the removal is intentional), exactly like sync(): the preview
            # reports the pause the run would apply instead of letting the
            # confirmation promise a wipe that will be blocked.
            unconfirmed = [action for action in deletes if not action.tombstone_confirmed]
            blocked, _reason = _mass_deletion_blocked(
                len(unconfirmed), len(local_files), len(remote_files)
            )
            if blocked:
                emit_event(
                    UiEventName.preview_warning,
                    reason="mass_delete_guard",
                    deletes=len(unconfirmed),
                    local=len(local_files),
                )

        # Emit structured messages for each file status (text log + event channel)
        for actions, label, state in [
            (synced, ActionLabel.SYNCED, "synced"),
            (downloads, ActionLabel.TO_DOWNLOAD, "to_download"),
            (uploads, ActionLabel.TO_UPLOAD, "to_upload"),
            (deletes + delete_remotes, ActionLabel.TO_DELETE, "to_delete"),
        ]:
            for action in actions:
                size = action.file.size or 0
                log.info(f'{label} "{action.file.filepath}" ({format_bytes(size)})')
                emit_event(
                    UiEventName.file_status, state=state, file=action.file.filepath, size=size
                )
        for old_file, new_file in renames:
            size = new_file.size or 0
            log.info(
                f'{ActionLabel.TO_RENAME} "{old_file.filepath}" -> '
                f'"{new_file.filepath}" ({format_bytes(size)})'
            )
            emit_event(
                UiEventName.rename_status,
                src=old_file.filepath,
                dst=new_file.filepath,
                size=size,
            )

        log.info(
            f"Preview: {len(synced)} synced, {len(downloads)} downloads, "
            f"{len(uploads)} uploads, {len(deletes) + len(delete_remotes)} deletes, "
            f"{len(renames)} renames"
        )
    except Exception as e:
        log.error(f"Preview failed: {e}")
        if isinstance(e, AuthenticationError):
            # An authentication failure is not a generic preview error: surface
            # it as ``auth_error`` so the GUI can prompt for the shared password.
            emit_event(UiEventName.auth_error, message=f"Preview failed: {e}")
        else:
            # A connection-level failure (not a server 5xx, not auth) is tagged
            # so the GUI can say "could not reach the server" instead of a
            # generic error.
            reason = (
                "connection"
                if isinstance(e, NetworkError) and not isinstance(e, ServerError)
                else ""
            )
            emit_event(UiEventName.error, message=f"Preview failed: {e}", reason=reason)
    log.info("--- End Preview ---")
    emit_event(UiEventName.preview_end)


async def _with_stdin_commands(
    log,
    coro,
    inventory_observer: InventoryObserver | None = None,
    source_resolver: P2PSourceResolver | None = None,
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
        lan = CONFIG.get("P2P_MODE", False)
        if not _p2p_write_allowed(CONFIG.get("SYNC_DIRECTION", "bidirectional"), log):
            return
        await sync(
            p2p_mode=lan,
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
        if not _p2p_write_allowed(direction, log):
            return
        CONFIG["SYNC_DIRECTION"] = direction
        log.info(f"Sync direction changed: {old!r} → {direction!r}")
        # Trigger an immediate resync with the new direction
        lan = CONFIG.get("P2P_MODE", False)
        await sync(
            p2p_mode=lan,
            inventory_observer=inventory_observer,
            source_resolver=source_resolver,
        )

    handler.register("SET_SYNC_DIRECTION", _handle_set_sync_direction)

    async def _on_set_bandwidth(**kw: object) -> None:
        await _handle_set_bandwidth(log, **kw)

    handler.register("SET_BANDWIDTH", _on_set_bandwidth)

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
    source_resolver: P2PSourceResolver | None = None,
    on_sync_start: Callable[[], None] | None = None,
    on_sync_error: Callable[[], None] | None = None,
) -> None:
    """Block reading stdin commands until EOF or SYNC.

    Used in continuous dry-run mode (P2P or server) where there is no
    WebSocket connection yet.  When a ``SYNC`` command arrives the
    handler performs the full sync, then stops the reader so the caller
    can proceed to the next phase (e.g. WebSocket connection).

    ``on_sync_start`` / ``on_sync_error`` let the P2P caller credit the master
    for presence (and clear it on failure) on this preview-confirm transition,
    which is the real initial sync of the desktop flow.
    """
    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))
    handler.register("PEERS", lambda **_kw: reemit_presence())

    async def _handle_sync(**_kw: object) -> None:
        if not _p2p_write_allowed(CONFIG.get("SYNC_DIRECTION", "bidirectional"), log):
            return
        CONFIG["DRYRUN"] = False
        # The preview-confirm flow's first real sync: mark the phase (status
        # bar) and reconcile the directories like the direct initial sync.
        log_initial_sync_start()
        if on_sync_start is not None:
            on_sync_start()
        lan = CONFIG.get("P2P_MODE", False)
        try:
            await sync(
                p2p_mode=lan,
                inventory_observer=inventory_observer,
                source_resolver=source_resolver,
                reconcile_dirs=True,
            )
        except Exception:
            if on_sync_error is not None:
                on_sync_error()
            raise
        handler.stop()

    handler.register("SYNC", _handle_sync)

    async def _on_set_bandwidth(**kw: object) -> None:
        await _handle_set_bandwidth(log, **kw)

    handler.register("SET_BANDWIDTH", _on_set_bandwidth)
    await handler.run()
