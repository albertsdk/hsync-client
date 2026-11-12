# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Slave-side P2P peer-inventory publication.

The publisher owns the slave-side snapshot state and never reads from the
WebSocket; the transport's single receive loop demultiplexes acknowledgements
through :meth:`P2PInventoryPublisher.handle_ack`. The master-side SQLite index
lives in ``hsync_client.p2p_inventory``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from time import monotonic
from typing import Protocol

from hsync_common.hashing import is_sha256_hex
from hsync_common.models.file import File
from hsync_common.protocol.peer_download import (
    P2P_INVENTORY_MAX_ENTRIES,
    P2P_INVENTORY_MAX_FRAME_BYTES,
    P2P_WIRE_INT_MAX,
    P2PInventoryAckFrame,
    P2PInventoryDeleteEntry,
    P2PInventoryEntry,
    P2PInventoryFrame,
    P2PInventoryUpsertEntry,
    build_p2p_inventory_frame,
    validate_p2p_inventory_ack,
)
from hsync_common.protocol.transfer_progress import (
    P2PFileProgressFrame,
    build_p2p_file_progress,
)
from hsync_common.text import compact_json_bytes
from loguru import logger as log

from hsync_client.utils import normalize_bucket_relpath

P2P_INVENTORY_ACK_TIMEOUT_S = 15.0
# After an ACK timeout the receiver is backlogged, not gone: retry a fresh
# snapshot on the same connection after this backoff, doubling up to the max,
# so a master left waiting on this peer's inventory eventually gets it without
# the publisher ever tearing down the WSS channel it shares with the event flow.
P2P_INVENTORY_ACK_RETRY_MIN_S = 5.0
P2P_INVENTORY_ACK_RETRY_MAX_S = 60.0

_FRAME_SIZE_MARGIN = 512
_ACTIVE_PUBLISHER_LOCK = threading.Lock()
_active_publisher: P2PInventoryPublisher | None = None


def set_active_inventory_publisher(publisher: P2PInventoryPublisher | None) -> None:
    """Register the role-scoped publisher used by ignore-rule reloads."""
    global _active_publisher
    with _ACTIVE_PUBLISHER_LOCK:
        _active_publisher = publisher


def refresh_active_inventory() -> bool:
    """Rescan the local bucket after ignore rules change, when seeding."""
    with _ACTIVE_PUBLISHER_LOCK:
        publisher = _active_publisher
    if publisher is None:
        return False
    try:
        from hsync_client.sync import get_files_local

        publisher.replace_snapshot(get_files_local())
    except Exception:
        log.exception("Could not refresh P2P peer inventory after ignore-rule change")
        return False
    return True


class _PublisherWebSocket(Protocol):
    async def send(self, message: str) -> object: ...

    async def close(self, code: int = 1000, reason: str = "") -> object: ...


class P2PInventoryPublisher:
    """Thread-safe observer plus ACK-driven WSS snapshot/delta publisher."""

    def __init__(
        self,
        bucket_root: str | Path,
        *,
        ack_timeout_s: float = P2P_INVENTORY_ACK_TIMEOUT_S,
    ) -> None:
        if ack_timeout_s <= 0:
            raise ValueError("ACK timeout must be positive")
        self._bucket_root = str(Path(bucket_root).resolve())
        self._ack_timeout_s = ack_timeout_s
        self._entries: dict[str, P2PInventoryUpsertEntry] = {}
        self._pending: dict[str, P2PInventoryEntry | None] = {}
        self._state_lock = threading.RLock()
        self._generation = 0
        self._resnapshot_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._ack_queue: asyncio.Queue[P2PInventoryAckFrame] | None = None
        self._task: asyncio.Task[None] | None = None
        self._websocket: _PublisherWebSocket | None = None
        self._disabled = False
        # Delivery-progress uplink: the latest sample per file plus a set of
        # files with a send in flight, so a stalled socket coalesces superseded
        # samples instead of piling one coroutine per tick.
        self._progress_pending: dict[str, P2PFileProgressFrame] = {}
        self._progress_inflight: set[str] = set()
        self._progress_lock = threading.Lock()

    def _normalize_path(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return normalize_bucket_relpath(value, self._bucket_root)

    def _normalize_entry(
        self, value: File | Mapping[str, object]
    ) -> P2PInventoryUpsertEntry | None:
        if isinstance(value, File):
            raw_path = value.filepath
            digest = value.sha256sum
            size = value.size
        else:
            raw_path = value.get("path") or value.get("filepath")
            digest = value.get("sha256") or value.get("sha256sum")
            size = value.get("size")
        path = self._normalize_path(raw_path)
        if (
            path is None
            or not isinstance(digest, str)
            or not is_sha256_hex(digest)
            or digest != digest.lower()
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= P2P_WIRE_INT_MAX
        ):
            return None

        local = Path(self._bucket_root) / path
        if local.is_symlink() or not local.is_file():
            return None
        return P2PInventoryUpsertEntry(path=path, sha256=digest, size=size)

    def _notify(self) -> None:
        loop = self._loop
        wake = self._wake
        if loop is not None and wake is not None and loop.is_running():
            loop.call_soon_threadsafe(wake.set)

    def replace_snapshot(self, entries: Iterable[File | Mapping[str, object]]) -> None:
        """Replace the scanned regular-file set while replaying racing mutations."""
        replacement: dict[str, P2PInventoryUpsertEntry] = {}
        for value in entries:
            entry = self._normalize_entry(value)
            if entry is not None:
                replacement[entry["path"]] = entry
        with self._state_lock:
            for path, mutation in self._pending.items():
                if mutation is None or "deleted" in mutation:
                    replacement.pop(path, None)
                else:
                    replacement[path] = mutation
            self._entries = replacement
            self._resnapshot_requested = True
        self._notify()

    def upsert(self, file: File | Mapping[str, object]) -> None:
        """Record one stable regular file, independent of upload success."""
        entry = self._normalize_entry(file)
        if entry is None:
            return
        with self._state_lock:
            self._entries[entry["path"]] = entry
            self._pending[entry["path"]] = entry
        self._notify()

    def remove_file(self, path: str) -> None:
        rel = self._normalize_path(path)
        if rel is None:
            return
        with self._state_lock:
            self._entries.pop(rel, None)
            self._pending[rel] = P2PInventoryDeleteEntry(path=rel, deleted=True)
        self._notify()

    def remove_prefix(self, directory: str) -> None:
        rel = self._normalize_path(directory)
        if rel is None:
            return
        prefix = rel.rstrip("/") + "/"
        with self._state_lock:
            paths = [path for path in self._entries if path == rel or path.startswith(prefix)]
            for path in paths:
                self._entries.pop(path, None)
                self._pending[path] = P2PInventoryDeleteEntry(path=path, deleted=True)
        if paths:
            self._notify()

    def rename_file(self, src: str, dst: str) -> None:
        src_rel = self._normalize_path(src)
        dst_rel = self._normalize_path(dst)
        if src_rel is None or dst_rel is None:
            return
        with self._state_lock:
            entry = self._entries.pop(src_rel, None)
            self._pending[src_rel] = P2PInventoryDeleteEntry(path=src_rel, deleted=True)
            if entry is not None:
                renamed = P2PInventoryUpsertEntry(
                    path=dst_rel, sha256=entry["sha256"], size=entry["size"]
                )
                self._entries[dst_rel] = renamed
                self._pending[dst_rel] = renamed
        self._notify()

    def rename_prefix(self, src_directory: str, dst_directory: str) -> None:
        src_rel = self._normalize_path(src_directory)
        dst_rel = self._normalize_path(dst_directory)
        if src_rel is None or dst_rel is None:
            return
        prefix = src_rel.rstrip("/") + "/"
        with self._state_lock:
            matches = [path for path in self._entries if path == src_rel or path.startswith(prefix)]
            for path in matches:
                entry = self._entries.pop(path)
                suffix = path.removeprefix(src_rel)
                renamed_path = f"{dst_rel}{suffix}"
                renamed = P2PInventoryUpsertEntry(
                    path=renamed_path,
                    sha256=entry["sha256"],
                    size=entry["size"],
                )
                self._pending[path] = P2PInventoryDeleteEntry(path=path, deleted=True)
                self._pending[renamed_path] = renamed
                self._entries[renamed_path] = renamed
        if matches:
            self._notify()

    def start(self, websocket: _PublisherWebSocket) -> asyncio.Task[None]:
        """Start publication on an established WSS connection."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("P2P inventory publisher is already connected")
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._ack_queue = asyncio.Queue(maxsize=1024)
        self._websocket = websocket
        self._disabled = False
        self._task = self._loop.create_task(self._publish_loop())
        return self._task

    async def stop(self) -> None:
        """Detach from WSS while retaining the authoritative local file set."""
        task = self._task
        self._task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._websocket = None
        self._ack_queue = None
        self._wake = None
        self._loop = None

    def report_received(self, rel_path: str, transferred: int, total: int) -> None:
        """Publish the bytes stored for a peer-served file (best-effort).

        Driven from the download thread that moves the bytes, so the send is
        marshalled onto the session's loop; a detached or closing session drops
        it. One drain coroutine per file keeps at most one frame in flight and
        coalesces superseded samples, so a stalled socket cannot pile up a
        coroutine per tick. The frame mirrors the master's ``file_progress``, so
        the serving side renders the receiver's scale instead of its own
        transport counter.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            frame = build_p2p_file_progress(file=rel_path, transferred=transferred, total=total)
        except ValueError:
            # An out-of-range sample (e.g. a body that outgrew its declared size)
            # must never break the download that carries it.
            return
        with self._progress_lock:
            self._progress_pending[rel_path] = frame
            if rel_path in self._progress_inflight:
                return  # the in-flight drain will pick up the latest sample
            self._progress_inflight.add(rel_path)
        try:
            asyncio.run_coroutine_threadsafe(self._drain_progress(rel_path), loop)
        except RuntimeError:
            # The loop is already shutting down: nothing left to send.
            with self._progress_lock:
                self._progress_inflight.discard(rel_path)

    async def _drain_progress(self, rel_path: str) -> None:
        """Send the newest pending sample for *rel_path* until none remains."""
        while True:
            with self._progress_lock:
                frame = self._progress_pending.pop(rel_path, None)
                if frame is None:
                    self._progress_inflight.discard(rel_path)
                    return
            websocket = self._websocket
            if websocket is None:
                # Session gone: drop the pending sample and clear the flag.
                with self._progress_lock:
                    self._progress_pending.pop(rel_path, None)
                    self._progress_inflight.discard(rel_path)
                return
            try:
                await websocket.send(self._serialize(frame))
            except Exception:
                # A delivery hint is never worth failing on: swallow so the
                # scheduled coroutine does not surface as an unretrieved error.
                continue

    def handle_ack(self, payload: object) -> bool:
        """Demultiplex one validated ACK from the transport's sole receiver."""
        ack = validate_p2p_inventory_ack(payload)
        queue = self._ack_queue
        if ack is None or queue is None:
            return False
        try:
            queue.put_nowait(ack)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(ack)
        return True

    def _capture_snapshot(self) -> tuple[int, list[P2PInventoryUpsertEntry]]:
        with self._state_lock:
            if self._generation >= P2P_WIRE_INT_MAX:
                raise RuntimeError("P2P inventory generation exhausted")
            self._generation += 1
            generation = self._generation
            entries = [self._entries[path] for path in sorted(self._entries)]
            self._pending.clear()
            self._resnapshot_requested = False
            return generation, entries

    def _take_mutations(self) -> list[P2PInventoryEntry]:
        with self._state_lock:
            mutations = [mutation for _, mutation in sorted(self._pending.items()) if mutation]
            self._pending.clear()
            return mutations

    def _needs_resnapshot(self) -> bool:
        with self._state_lock:
            return self._resnapshot_requested

    @staticmethod
    def _iter_chunks(entries: Iterable[P2PInventoryEntry]) -> Iterator[list[P2PInventoryEntry]]:
        """Yield frame-sized chunks lazily (at most one chunk built ahead).

        The previous implementation materialised every chunk plus a redundant
        ``list(entries)`` copy, holding O(N) references twice; streaming the
        chunks keeps the publisher's peak at two chunks.
        """
        current: list[P2PInventoryEntry] = []
        encoded_size = 0
        yielded = False
        limit = P2P_INVENTORY_MAX_FRAME_BYTES - _FRAME_SIZE_MARGIN
        for entry in entries:
            item_size = len(compact_json_bytes(entry)) + 1
            if current and (
                len(current) >= P2P_INVENTORY_MAX_ENTRIES or encoded_size + item_size > limit
            ):
                yielded = True
                yield current
                current = []
                encoded_size = 0
            current.append(entry)
            encoded_size += item_size
        if current:
            yielded = True
            yield current
        if not yielded:
            # Preserve the terminal empty-snapshot frame.
            yield []

    @staticmethod
    def _serialize(frame: P2PInventoryFrame) -> str:
        return compact_json_bytes(frame).decode("utf-8")

    async def _await_ack(self, frame: P2PInventoryFrame) -> P2PInventoryAckFrame | None:
        queue = self._ack_queue
        if queue is None:
            raise ConnectionError("P2P inventory publisher detached")
        deadline = monotonic() + self._ack_timeout_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            try:
                ack = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                return None
            if ack["gen"] != frame["gen"]:
                continue
            if ack["status"] != "ok" or ack["seq"] == frame["seq"]:
                return ack

    async def _send_with_ack(self, frame: P2PInventoryFrame) -> P2PInventoryAckFrame:
        websocket = self._websocket
        if websocket is None:
            raise ConnectionError("P2P inventory publisher detached")
        message = self._serialize(frame)
        for attempt in range(2):
            await websocket.send(message)
            ack = await self._await_ack(frame)
            if ack is not None:
                return ack
            if attempt == 0:
                log.warning(
                    "P2P inventory ACK timed out for gen={} seq={}; resending once",
                    frame["gen"],
                    frame["seq"],
                )
        # The acknowledgement never arrived. The master's WSS receiver is
        # backlogged (typical under a burst): the channel is congested, not
        # necessarily dead. Do NOT close the websocket here — closing it would
        # tear down the event channel this same connection carries and force a
        # full resync on both peers. The publish loop catches the timeout and
        # retries a fresh snapshot after a backoff instead.
        raise TimeoutError("P2P inventory acknowledgement timed out")

    async def _publish_snapshot(self) -> tuple[str, int, int]:
        generation, entries = self._capture_snapshot()
        chunks = self._iter_chunks(entries)
        current = next(chunks, None)  # at most two chunks alive (current + next)
        seq = 0
        terminal: P2PInventoryFrame | None = None
        while current is not None:
            nxt = next(chunks, None)
            frame = build_p2p_inventory_frame(
                gen=generation,
                seq=seq,
                mode="snapshot",
                more=nxt is not None,
                entries=current,
            )
            if nxt is not None:
                websocket = self._websocket
                if websocket is None:
                    raise ConnectionError("P2P inventory publisher detached")
                await websocket.send(self._serialize(frame))
            else:
                terminal = frame
            current = nxt
            seq += 1
        if terminal is None:  # pragma: no cover - empty input always yields one chunk
            raise RuntimeError("inventory snapshot did not produce a terminal frame")
        ack = await self._send_with_ack(terminal)
        if ack["status"] == "rejected":
            self._disabled = True
        return ack["status"], generation, terminal["seq"] + 1

    async def _publish_deltas(self, generation: int, next_seq: int) -> tuple[str, int]:
        mutations = self._take_mutations()
        if not mutations:
            return "idle", next_seq
        for chunk in self._iter_chunks(mutations):
            if not chunk:
                continue
            frame = build_p2p_inventory_frame(
                gen=generation,
                seq=next_seq,
                mode="delta",
                more=False,
                entries=chunk,
            )
            ack = await self._send_with_ack(frame)
            if ack["status"] != "ok":
                if ack["status"] == "rejected":
                    self._disabled = True
                return ack["status"], next_seq
            next_seq += 1
        return "ok", next_seq

    async def _publish_loop(self) -> None:
        backoff = P2P_INVENTORY_ACK_RETRY_MIN_S
        while True:
            try:
                await self._publish_loop_inner()
                return
            except TimeoutError:
                # The master stopped acknowledging (receiver backlogged under a
                # burst) but the transport is alive. Keep the channel — it also
                # carries the event flow — and retry a fresh snapshot after a
                # bounded backoff, so the master is not left without this peer's
                # inventory until a reconnect. A transport that truly died
                # raises ConnectionError on the next send and ends the loop.
                self._resnapshot_requested = True
                log.warning(
                    "P2P inventory ACK timed out; retrying a fresh snapshot in {:.0f}s",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, P2P_INVENTORY_ACK_RETRY_MAX_S)
            except ConnectionError:
                self._disabled = True
                log.warning("P2P inventory publisher stopped: transport lost")
                return

    async def _publish_loop_inner(self) -> None:
        while True:
            status, generation, next_seq = await self._publish_snapshot()
            if status == "rejected":
                return
            if status == "resync":
                continue
            while True:
                if self._needs_resnapshot():
                    break
                status, next_seq = await self._publish_deltas(generation, next_seq)
                if status == "rejected":
                    return
                if status == "resync":
                    break
                if status == "ok":
                    continue
                wake = self._wake
                if wake is None:
                    return
                wake.clear()
                if self._needs_resnapshot() or self._pending:
                    continue
                await wake.wait()
