# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Device-local trash for applied deletions (ADR-0056).

`hsync_client/sync.py::_apply_deletions` used to unlink every file a cycle
removed. ``TrashStore`` moves the bytes into a per-folder store under the
installation state dir instead, so a deletion stays recoverable, and a restore is
a plain write that the causal model resolves: the scanned version mints a clock
past the deletion marker (ADR-0003, delete-vs-recreate), so the engine uploads it
and every peer resurrects the path.

Two properties the implementation must keep:

* **No state-DB access.** Neither listing nor restoring touches the per-folder
  SQLite store, so both work while a sync runs and the single-writer discipline
  of that store is preserved. This is why the restore does not consult the
  tombstone: the entry's own ``sha256`` is the integrity check.
* **Reported degradation.** When the move fails the cycle still deletes and logs
  the lost recoverability: aborting the deletion would make the file reappear on
  every cycle (the peer still holds it).

Consumers: ``hsync_client/sync.py`` (write path), ``hsync_client/cli.py``
(``hsync trash list|restore|empty``), ``hsync-desktop`` and ``hsync-wd-os5``
(through the CLI's JSON output).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hsync_common.constants import (
    TRASH_ENTRY_FILENAME,
    TRASH_RESTORING_SUFFIX,
)
from hsync_common.defaults import (
    TRASH_MAX_SIZE_MB_DEFAULT,
    TRASH_QUOTA_FLOOR_MB_DEFAULT,
    TRASH_QUOTA_PERCENT_DEFAULT,
    TRASH_RETENTION_DAYS_DEFAULT,
)
from hsync_common.disk import BYTES_PER_MB, share_of_volume_bytes
from hsync_common.models.folder import read_folder_marker
from hsync_common.models.trash import (
    TRASH_ORIGIN_SYNC,
    TRASH_ORIGIN_USER,
    TrashEntry,
    entry_dir,
    entry_payload_path,
    is_safe_folder_id,
    parse_trash_entry,
    trash_folder_dir,
    trash_root,
    trash_root_inside_folder,
)
from hsync_common.path_validation import is_internal_name
from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import HSYNCKEEP_FILENAME, ActionLabel
from hsync_client.utils.fs_ops import clear_read_only, remove_with_retry

__all__ = [
    "EXPECTED_TRASH_SKIPS",
    "EmptyReport",
    "MoveResult",
    "PruneReport",
    "RestoreReport",
    "TrashItem",
    "TrashSettings",
    "TrashStore",
    "delete_local_file",
    "delete_local_tree",
    "empty_store",
    "prune_store",
    "store_for_folder",
    "trash_settings",
]

_SECONDS_PER_DAY = 86_400
# When the last retention sweep ran (process-wide; only the sync loop sweeps).
_last_prune_at = 0.0

# Refusals that are the normal shape of a folder rather than a lost recovery: a
# disabled store, a folder never synced, a path the store cannot own.
EXPECTED_TRASH_SKIPS = frozenset(
    {"disabled", "no-folder-id", "not-a-regular-file", "outside-folder"}
)
# Folder paths already reported for a root inside the shared tree (the realtime
# handlers build a store per deletion, so the error must not repeat per file).
_ROOT_INSIDE_FOLDER_REPORTED: set[str] = set()

# A recursive delete parks the files it removes, but only up to these bounds: a
# tree of a hundred thousand files would otherwise hash gigabytes on the delete
# path. What is left behind is reported, never dropped silently.
TREE_TRASH_MAX_FILES = 1000
TREE_TRASH_MAX_BYTES = 1024**3  # 1 GiB


@dataclass(frozen=True)
class TrashSettings:
    """Resolved trash configuration for one process (ADR-0056)."""

    enabled: bool = True
    root: Path = Path()
    retention_days: int = TRASH_RETENTION_DAYS_DEFAULT
    # Explicit byte budget; 0 means "derive it from the hosting filesystem".
    max_size: int = 0

    def budget(self) -> int:
        """Return the effective size budget in bytes.

        An explicit ``--trash-max-size``/``HSYNC_TRASH_MAX_SIZE`` wins. Otherwise
        the store may use a share of the filesystem that hosts it (the
        recycle-bin model), never less than the floor, so a small volume still
        holds a few files.
        """
        if self.max_size > 0:
            return self.max_size
        return share_of_volume_bytes(
            self.root,
            percent=TRASH_QUOTA_PERCENT_DEFAULT,
            floor_mb=TRASH_QUOTA_FLOOR_MB_DEFAULT,
        )


def trash_settings(config: dict | None = None) -> TrashSettings:
    """Build :class:`TrashSettings` from the run configuration.

    The env/CLI layer fills the ``TRASH_*`` keys (``hsync_client/config.py``);
    this is the single read site so the write path and the CLI cannot disagree.
    """
    active = CONFIG if config is None else config
    enabled = bool(active.get("TRASH_ENABLED", True))
    directory = str(active.get("TRASH_DIR") or "")
    retention = active.get("TRASH_RETENTION_DAYS", TRASH_RETENTION_DAYS_DEFAULT)
    max_size = active.get("TRASH_MAX_SIZE", TRASH_MAX_SIZE_MB_DEFAULT * BYTES_PER_MB)
    try:
        retention_days = max(int(retention), 0)
    except (TypeError, ValueError):
        retention_days = TRASH_RETENTION_DAYS_DEFAULT
    try:
        max_size_bytes = max(int(max_size), 0)
    except (TypeError, ValueError):
        max_size_bytes = 0
    return TrashSettings(
        enabled=enabled,
        root=trash_root(directory or None),
        retention_days=retention_days,
        max_size=max_size_bytes,
    )


def folder_id_of(folder: str | Path) -> str | None:
    """Return the ``.hsyncfolder`` UUID of *folder*, or None when it has none.

    Read-only on purpose: a folder that was never synced has no trash either, and
    the trash never needs the state DB to answer the question.
    """
    return read_folder_marker(folder)


def store_for_folder(folder: str | Path) -> TrashStore | None:
    """Return the trash store of *folder*, or None when it has none to extend.

    A folder without ``.hsyncfolder`` was never synced, so it has no trash. A
    store pointed inside the folder is refused too: the CLI rejects the flag, but
    the environment (or a relocated state dir) can still land it in the shared
    tree, where its bytes would be uploaded to every peer. That refusal is
    reported once per folder per process, because the realtime handlers ask for a
    store per deletion.

    Consumers: ``hsync_client/sync.py`` (the cycle), ``hsync_client/event_dispatch.py``
    and ``hsync_client/p2p_server.py`` (the realtime delete handlers), which is
    what makes a deletion keep its bytes whichever path applies it.
    """
    path = str(folder or "").strip()
    if not path:
        return None
    store = TrashStore(path)
    if store.folder_id is None:
        return None
    if trash_root_inside_folder(store.root, store.folder):
        if str(store.folder) not in _ROOT_INSIDE_FOLDER_REPORTED:
            _ROOT_INSIDE_FOLDER_REPORTED.add(str(store.folder))
            log.error(
                f'The trash store "{store.root}" is inside the synced folder: deleted '
                "files keep no copy. Move it with --trash-dir or HSYNC_TRASH_DIR."
            )
        return None
    return store


def delete_local_file(store: TrashStore | None, filepath: str | Path) -> bool:
    """Remove one path, parking the bytes in the device-local trash first.

    The removal always completes: when the store cannot take the file the lost
    recoverability is logged instead of aborting the deletion, because the peer
    still holds the file — a refused removal would simply re-appear on every
    cycle until the failure is fixed. Engine artifacts and the ``.hsynckeep``
    sentinel carry no user bytes, so they are unlinked without an entry.

    Consumers: ``hsync_client/sync.py`` (the cycle's deletions),
    ``hsync_client/event_dispatch.py`` and ``hsync_client/p2p_server.py`` (the
    realtime delete handlers).

    Returns:
        True when the path is gone.
    """
    path = Path(filepath)
    internal = path.name == HSYNCKEEP_FILENAME or is_internal_name(path.name)
    if store is not None and not internal:
        result = store.move(path)
        if result.trashed:
            return True
        if result.reason not in EXPECTED_TRASH_SKIPS:
            log.warning(f'{ActionLabel.DELETE} "{filepath}" has no trash copy: {result.reason}')
    if not path.exists() and not path.is_symlink():
        return False
    remove_with_retry(str(filepath))
    return True


def delete_local_tree(store: TrashStore | None, path: str | Path) -> tuple[int, int]:
    """Park the files of the tree at *path*, returning ``(kept, left_behind)``.

    The caller still removes what remains (the directories): parking first is what
    makes a recursive deletion — a peer removing a folder, the master applying one
    — as recoverable as a single file. Symlinks carry no restorable payload and are
    left to the removal, and the two module caps bound the work this may do on the
    delete path.

    Consumers: ``hsync_client/event_dispatch.py`` and
    ``hsync_client/p2p_server.py`` (the recursive delete handlers).
    """
    root = Path(path)
    if store is None or not root.is_dir():
        return 0, 0
    kept = 0
    left = 0
    budget = TREE_TRASH_MAX_BYTES
    # ``os.walk`` lists each directory before descending, so moving the files out
    # of the current one does not disturb the traversal.
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink() or kept >= TREE_TRASH_MAX_FILES or budget <= 0:
                left += 1
                continue
            try:
                budget -= candidate.stat().st_size
            except OSError:
                left += 1
                continue
            if store.move(candidate).trashed:
                kept += 1
            else:
                left += 1
    if left:
        log.warning(
            f'{ActionLabel.DELETE} "{root}": {left} file(s) of the tree kept no copy '
            f"(bound: {TREE_TRASH_MAX_FILES} files / {TREE_TRASH_MAX_BYTES} B)."
        )
    return kept, left


@dataclass(frozen=True)
class TrashItem:
    """One entry as a front-end lists it."""

    entry_id: str
    entry: TrashEntry
    directory: Path
    payload: Path


@dataclass(frozen=True)
class MoveResult:
    """Outcome of moving one deleted file into the store."""

    trashed: bool = False
    entry_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RestoreReport:
    """Outcome of one restore attempt.

    ``status`` is the machine-readable verdict a front-end branches on:
    ``restored``, ``no-entry``, ``destination-exists``, ``download-only``,
    ``payload-missing`` or ``payload-corrupt``. ``replaced`` says the path
    already existed and its copy was parked as its own entry first, so the
    forced restore lost nothing.
    """

    folder: str
    relpath: str
    status: str
    entry_id: str = ""
    size: int = 0
    destination: str = ""
    detail: str = ""
    replaced: bool = False

    @property
    def restored(self) -> bool:
        return self.status == "restored"


@dataclass(frozen=True)
class EmptyReport:
    """Outcome of emptying the store (or one folder's part of it)."""

    removed: int = 0
    freed: int = 0


@dataclass(frozen=True)
class PruneReport:
    """Outcome of one retention/budget sweep.

    ``discarded`` counts entries whose record or payload was unusable — a crash
    between the record and the move, a hand-edited file, a truncated payload —
    which the sweep removes instead of leaving a restorable-looking entry the
    restore can only refuse.
    """

    expired: int = 0
    evicted: int = 0
    discarded: int = 0
    freed: int = 0
    kept: int = 0


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace(source: Path, destination: Path) -> None:
    """``os.replace`` that survives the Windows read-only attribute.

    Windows refuses to rename a read-only file (one copied from read-only media,
    or marked by an application). Clearing the bit and retrying once is what
    ``clear_read_only`` exists for; on POSIX it is a no-op, so the retry never
    runs and the first attempt is the only one.
    """
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        clear_read_only(str(source))
        os.replace(source, destination)


def _move_payload(source: Path, destination: Path) -> None:
    """Move *source* to *destination*, surviving a cross-device layout.

    ``os.replace`` is atomic and keeps the bytes when both ends share a
    filesystem; a trash root on another volume (``EXDEV``) falls back to a
    written-through copy plus unlink, so a crash can leave a ``.part`` the prune
    sweep discards instead of a truncated entry.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    partial = destination.with_name(destination.name + ".part")
    with source.open("rb") as reader, partial.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    _replace(partial, destination)
    clear_read_only(str(source))
    source.unlink()


class TrashStore:
    """One folder's view of the device-local trash (ADR-0056).

    The store is instantiated per folder, but the **budget** is enforced across
    the whole trash root: N shares must not each fill the same volume.
    """

    def __init__(
        self,
        folder: str | Path,
        *,
        settings: TrashSettings | None = None,
        cid: str = "",
        bucket: str = "",
    ) -> None:
        self.folder = Path(folder)
        self.settings = settings if settings is not None else trash_settings()
        self.cid = cid or str(CONFIG.get("CID") or "")
        # A front-end reads the share name from the entry, so an engine-created
        # entry records this run's bucket instead of an empty field.
        self.bucket = bucket or str(CONFIG.get("REMOTE_BUCKET") or "")
        self.folder_id = folder_id_of(self.folder)

    # ── layout ────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self.settings.root

    def _folder_dir(self) -> Path | None:
        if self.folder_id is None:
            return None
        return trash_folder_dir(self.root, self.folder_id)

    def entries(self) -> list[TrashItem]:
        """Return this folder's entries, newest first.

        A directory without a readable ``entry.json`` (a crash between the
        record and the move, or a hand-edited file) is not an entry: the sweep
        removes it rather than guessing.
        """
        folder_dir = self._folder_dir()
        if folder_dir is None or not folder_dir.is_dir():
            return []
        items: list[TrashItem] = []
        for directory in sorted(folder_dir.iterdir()):
            if not directory.is_dir() or directory.name.endswith(TRASH_RESTORING_SUFFIX):
                continue
            entry = _read_entry(directory)
            if entry is None:
                continue
            relpath = str(entry["relpath"])
            try:
                payload = entry_payload_path(directory, relpath)
            except ValueError:
                continue
            items.append(
                TrashItem(
                    entry_id=directory.name,
                    entry=entry,
                    directory=directory,
                    payload=payload,
                )
            )
        items.sort(key=lambda item: (item.entry["deleted_at"], item.entry_id), reverse=True)
        return items

    # ── write path ────────────────────────────────────────────────────────

    def move(self, abs_path: str | Path, *, origin: str = TRASH_ORIGIN_SYNC) -> MoveResult:
        """Move one file that is about to be deleted into the store.

        Returns a :class:`MoveResult` instead of raising: the caller must finish
        the deletion either way (an unrecoverable removal is reported, not
        turned into a sync loop).
        """
        if not self.settings.enabled:
            return MoveResult(reason="disabled")
        folder_dir = self._folder_dir()
        if folder_dir is None:
            return MoveResult(reason="no-folder-id")
        source = Path(abs_path)
        try:
            relative = source.relative_to(self.folder).as_posix()
        except ValueError:
            return MoveResult(reason="outside-folder")
        if source.is_symlink() or not source.is_file():
            # A link's "content" is a target outside the store; moving it would
            # preserve nothing the user can restore.
            return MoveResult(reason="not-a-regular-file")
        try:
            info = source.stat()
            digest = _sha256_of(source)
        except OSError as exc:
            return MoveResult(reason=f"unreadable: {exc}")

        entry_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
        directory: Path | None = None
        try:
            directory = entry_dir(self.root, str(self.folder_id), entry_id)
            record = TrashEntry(
                relpath=relative,
                sha256=digest,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode),
                deleted_at=int(time.time()),
                origin=origin,
                cid=self.cid or None,
                bucket=self.bucket,
            )
            directory.mkdir(parents=True, exist_ok=True)
            # Record first: a crash leaves an entry without a payload (swept),
            # never a moved payload without a record.
            _write_entry(directory, record)
            _move_payload(source, entry_payload_path(directory, relative))
        except (OSError, ValueError) as exc:
            if directory is not None:
                _discard(directory)
            return MoveResult(reason=str(exc))
        return MoveResult(trashed=True, entry_id=entry_id)

    # ── restore ───────────────────────────────────────────────────────────

    def restore(
        self, relpath: str, *, direction: str = "bidirectional", force: bool = False
    ) -> RestoreReport:
        """Write a trashed file back into the folder.

        The restore is a plain local write on purpose: the engine treats a
        re-created path as a new version that supersedes the deletion marker
        (ADR-0003), so it uploads it and every peer resurrects the path. No
        state-DB write and no lock, which is what lets it run during a sync.

        With *force*, an existing destination is not a dead end: the file there
        is parked as its own entry first (origin ``user``), so the path ends up
        with the restored bytes and the copy it replaced stays restorable. A
        directory is never replaced — nothing of it could be parked under one
        file entry — so that refusal stands.
        """
        canonical = str(self.folder)
        if not self.settings.enabled:
            return RestoreReport(folder=canonical, relpath=relpath, status="disabled")
        if direction == "download-only":
            # A download-only run mirrors the peer and deletes local extras, so
            # the restored file would be removed again on the next cycle.
            return RestoreReport(folder=canonical, relpath=relpath, status="download-only")
        item = next((item for item in self.entries() if item.entry["relpath"] == relpath), None)
        if item is None:
            return RestoreReport(folder=canonical, relpath=relpath, status="no-entry")
        # Integrity first: a corrupt entry must leave the folder untouched, which
        # matters most under --force (nothing is parked before the bytes check).
        if not item.payload.is_file():
            return RestoreReport(
                folder=canonical,
                relpath=relpath,
                status="payload-missing",
                entry_id=item.entry_id,
            )
        try:
            size = item.payload.stat().st_size
            intact = size == item.entry["size"] and _sha256_of(item.payload) == item.entry["sha256"]
        except OSError as exc:
            return RestoreReport(
                folder=canonical,
                relpath=relpath,
                status="failed",
                entry_id=item.entry_id,
                detail=str(exc),
            )
        if not intact:
            return RestoreReport(
                folder=canonical,
                relpath=relpath,
                status="payload-corrupt",
                entry_id=item.entry_id,
            )
        destination = self.folder / relpath
        replaced = False
        if destination.exists() or destination.is_symlink():
            if not force:
                return RestoreReport(
                    folder=canonical,
                    relpath=relpath,
                    status="destination-exists",
                    entry_id=item.entry_id,
                )
            parked = self.move(destination, origin=TRASH_ORIGIN_USER)
            if not parked.trashed:
                return RestoreReport(
                    folder=canonical,
                    relpath=relpath,
                    status="destination-exists",
                    entry_id=item.entry_id,
                    detail=parked.reason,
                )
            replaced = True
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _restore_payload(item.payload, destination)
            _apply_metadata(destination, item.entry)
        except OSError as exc:
            return RestoreReport(
                folder=canonical,
                relpath=relpath,
                status="failed",
                entry_id=item.entry_id,
                detail=str(exc),
            )
        _discard(item.directory)
        return RestoreReport(
            folder=canonical,
            relpath=relpath,
            status="restored",
            entry_id=item.entry_id,
            size=item.entry["size"],
            destination=str(destination),
            replaced=replaced,
        )

    # ── maintenance ───────────────────────────────────────────────────────

    def empty(self, *, older_than_days: int | None = None) -> EmptyReport:
        """Delete this folder's entries (all of them, or those older than an age)."""
        folder_dir = self._folder_dir()
        folders = [folder_dir] if folder_dir is not None and folder_dir.is_dir() else []
        return _discard_entries(folders, _age_cutoff(older_than_days))

    def prune(self, *, now: int | None = None) -> PruneReport:
        """Enforce the retention window and the size budget over the whole root.

        The budget is shared by every folder (one volume, one budget), so the
        sweep never runs per share. The rule itself lives in :func:`prune_store`
        — the ``hsync trash prune`` command applies it without a store, since a
        one-shot or stopped client never reaches the hourly gate.
        """
        return prune_store(self.settings, now=now)

    def prune_if_due(self, *, interval_seconds: int = 3600) -> PruneReport | None:
        """Run :meth:`prune` at most once per interval; ``None`` when not due.

        The sync loop calls this every cycle, so a long-running daemon applies
        the retention window without a separate scheduler; the gate keeps the
        sweep off the hot path.
        """
        global _last_prune_at
        moment = time.time()
        if _last_prune_at and moment - _last_prune_at < interval_seconds:
            return None
        _last_prune_at = moment
        return self.prune()


def empty_store(settings: TrashSettings, *, older_than_days: int | None = None) -> EmptyReport:
    """Delete every entry of a whole trash root (or those older than an age).

    Consumers: the ``hsync trash empty --all`` command, which empties every share
    of one device in a single call. ``TrashStore.empty`` stays per folder.
    """
    return _discard_entries(_trash_folders(settings.root), _age_cutoff(older_than_days))


def prune_store(settings: TrashSettings, *, now: int | None = None) -> PruneReport:
    """Enforce the retention window and the size budget over a whole trash root.

    The budget is shared by every folder (one volume, one budget), so the sweep
    never runs per share. An entry whose record or payload is unusable is
    discarded rather than kept: a crash between the record and the move must not
    leave a restorable-looking entry that every restore can only refuse.

    Consumers: ``hsync_client/trash.py::TrashStore.prune`` (the cycle's sweep) and
    the ``hsync trash prune`` command (an operator run, or a one-shot client that
    never reaches the hourly gate).
    """
    moment = int(time.time()) if now is None else now
    cutoff = moment - settings.retention_days * _SECONDS_PER_DAY
    expired = 0
    discarded = 0
    freed = 0
    kept: list[tuple[TrashEntry, Path]] = []
    for folder_dir in _trash_folders(settings.root):
        for directory in folder_dir.iterdir():
            if not directory.is_dir() or directory.name.endswith(TRASH_RESTORING_SUFFIX):
                continue
            entry = _read_entry(directory)
            if entry is None or not _payload_intact(directory, entry):
                discarded += 1
                _discard(directory)
                continue
            if settings.retention_days > 0 and entry["deleted_at"] < cutoff:
                expired += 1
                freed += entry["size"]
                _discard(directory)
                continue
            kept.append((entry, directory))
    budget = settings.budget()
    evicted = 0
    total = sum(entry["size"] for entry, _directory in kept)
    if budget > 0 and total > budget:
        # Oldest first, and deterministic within the same second: the entry id
        # starts with its creation timestamp.
        for entry, directory in sorted(
            kept, key=lambda pair: (pair[0]["deleted_at"], pair[1].name)
        ):
            if total <= budget:
                break
            total -= entry["size"]
            freed += entry["size"]
            evicted += 1
            _discard(directory)
    return PruneReport(
        expired=expired,
        evicted=evicted,
        discarded=discarded,
        freed=freed,
        kept=len(kept) - evicted,
    )


def _age_cutoff(older_than_days: int | None) -> int | None:
    """Return the ``deleted_at`` floor for an age filter, or ``None`` for all."""
    if older_than_days is None:
        return None
    return int(time.time()) - older_than_days * _SECONDS_PER_DAY


def _discard_entries(folder_dirs: Iterable[Path], cutoff: int | None) -> EmptyReport:
    """Delete the entries of *folder_dirs*, skipping those at or after *cutoff*."""
    removed = 0
    freed = 0
    for folder_dir in folder_dirs:
        for directory in folder_dir.iterdir():
            if not directory.is_dir() or directory.name.endswith(TRASH_RESTORING_SUFFIX):
                continue
            entry = _read_entry(directory)
            if entry is None:
                _discard(directory)
                continue
            if cutoff is not None and entry["deleted_at"] >= cutoff:
                continue
            removed += 1
            freed += entry["size"]
            _discard(directory)
    return EmptyReport(removed=removed, freed=freed)


def _payload_intact(directory: Path, entry: TrashEntry) -> bool:
    """True when the payload is still there with the recorded size.

    The size — not the hash — is the check the sweep can afford; the full hash
    comparison stays in ``TrashStore.restore``, where the bytes are about to be
    written back.
    """
    try:
        payload = entry_payload_path(directory, entry["relpath"])
        return payload.is_file() and payload.stat().st_size == entry["size"]
    except (OSError, ValueError):
        return False


def _read_entry(directory: Path) -> TrashEntry | None:
    """Return the validated record of *directory*, or None when unusable."""
    try:
        payload = json.loads((directory / TRASH_ENTRY_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_trash_entry(payload)


def _write_entry(directory: Path, entry: TrashEntry) -> None:
    """Write ``entry.json`` atomically (tmp + replace)."""
    target = directory / TRASH_ENTRY_FILENAME
    partial = target.with_name(target.name + ".tmp")
    partial.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, target)


def _discard(directory: Path) -> None:
    """Remove one entry directory, ignoring a race with the sweep."""
    shutil.rmtree(directory, ignore_errors=True)


def _trash_folders(root: Path) -> list[Path]:
    """Return the per-folder trash directories under *root*."""
    if not root.is_dir():
        return []
    return [
        directory
        for directory in root.iterdir()
        if directory.is_dir() and is_safe_folder_id(directory.name)
    ]


def _restore_payload(source: Path, destination: Path) -> None:
    """Move a payload back, atomically and invisible to the watcher.

    A cross-device restore copies into ``<name>.hsync-restore.tmp`` — a name the
    default ignore patterns already cover — and renames it, so a sync cycle never
    sees a half-written file.
    """
    try:
        _replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    partial = destination.with_name(destination.name + ".hsync-restore.tmp")
    with source.open("rb") as reader, partial.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    _replace(partial, destination)
    clear_read_only(str(source))
    source.unlink()


def _apply_metadata(destination: Path, entry: TrashEntry) -> None:
    """Best-effort restore of the recorded mtime and permission bits.

    The bytes are already in place; a filesystem that refuses one of the two
    (FAT without POSIX modes, an immutable attribute) must not undo the restore.
    """
    if entry["mtime_ns"] > 0:
        try:
            os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        except OSError:
            pass
    if entry["mode"] and os.name == "posix":
        try:
            os.chmod(destination, entry["mode"])
        except OSError:
            pass
