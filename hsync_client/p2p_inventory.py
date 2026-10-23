# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""P2P peer inventory persistence and slave-side publication.

The master store is an intentionally volatile SQLite index: master activation
clears crash leftovers and connected publishers rebuild it from snapshots. The
publisher owns the corresponding slave-side state and never reads from the
WebSocket; the transport's single receive loop demultiplexes acknowledgements
through :meth:`P2PInventoryPublisher.handle_ack`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol

from hsync_common.hashing import is_sha256_hex
from hsync_common.models.event import is_valid_cid
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
    P2PSourceFile,
    build_p2p_inventory_ack,
    build_p2p_inventory_frame,
    is_valid_serve_slots,
    validate_p2p_inventory_ack,
    validate_p2p_inventory_frame,
)
from hsync_common.text import compact_json_bytes
from loguru import logger as log

from hsync_client.utils import normalize_bucket_relpath

P2P_INVENTORY_PER_CID_QUOTA = 2_000_000
P2P_INVENTORY_GLOBAL_QUOTA = 20_000_000
P2P_INVENTORY_MAX_SESSIONS = 64
P2P_INVENTORY_STAGING_TTL_S = 5 * 60.0
P2P_INVENTORY_ACK_TIMEOUT_S = 15.0
P2P_INVENTORY_SCHEMA_VERSION = 1


def _canonical_snapshot_digest(frame: P2PInventoryFrame) -> str:
    """Hash a validated terminal snapshot frame in canonical JSON form."""
    encoded = json.dumps(
        frame,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


class InventorySessionLimitError(RuntimeError):
    """Raised when a new publisher would exceed the master session cap."""


@dataclass(frozen=True, slots=True)
class InventoryHolder:
    """Immutable exact-content holder row returned to REST lookup threads."""

    cid: str
    path: str
    sha256: str
    size: int


class InventoryObserver(Protocol):
    """Explicit mutation surface maintained by a role-scoped publisher."""

    def replace_snapshot(self, entries: Iterable[File | Mapping[str, object]]) -> None: ...

    def upsert(self, file: File | Mapping[str, object]) -> None: ...

    def remove_file(self, path: str) -> None: ...

    def remove_prefix(self, directory: str) -> None: ...

    def rename_file(self, src: str, dst: str) -> None: ...

    def rename_prefix(self, src_directory: str, dst_directory: str) -> None: ...


class _NoopInventoryObserver:
    """Null object used by transports that do not publish P2P inventory."""

    def replace_snapshot(self, entries: Iterable[File | Mapping[str, object]]) -> None:
        return

    def upsert(self, file: File | Mapping[str, object]) -> None:
        return

    def remove_file(self, path: str) -> None:
        return

    def remove_prefix(self, directory: str) -> None:
        return

    def rename_file(self, src: str, dst: str) -> None:
        return

    def rename_prefix(self, src_directory: str, dst_directory: str) -> None:
        return


NOOP_INVENTORY_OBSERVER: InventoryObserver = _NoopInventoryObserver()


def default_p2p_inventory_path(bucket: str) -> Path:
    """Return the per-bucket master inventory database path."""
    from hsync_client.identity import default_device_dir

    namespace = hashlib.sha256(bucket.encode("utf-8")).hexdigest()
    return default_device_dir() / "lan-index" / f"{namespace}.sqlite3"


class P2PInventoryStore:
    """Thread-safe SQLite inventory index with session-token ownership."""

    def __init__(
        self,
        database_path: str | Path,
        bucket: str,
        *,
        clock: Callable[[], float] = monotonic,
        per_cid_quota: int = P2P_INVENTORY_PER_CID_QUOTA,
        global_quota: int = P2P_INVENTORY_GLOBAL_QUOTA,
        max_sessions: int = P2P_INVENTORY_MAX_SESSIONS,
        staging_ttl_s: float = P2P_INVENTORY_STAGING_TTL_S,
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        if min(per_cid_quota, global_quota, max_sessions) <= 0 or staging_ttl_s <= 0:
            raise ValueError("inventory quotas and staging TTL must be positive")

        self._path = Path(database_path)
        self._bucket = bucket
        self._clock = clock
        self._per_cid_quota = per_cid_quota
        self._global_quota = global_quota
        self._max_sessions = max_sessions
        self._staging_ttl_s = staging_ttl_s
        self._lock = threading.RLock()
        self._closed = False
        self._session_addresses: dict[str, tuple[str, str]] = {}

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._path.parent.chmod(0o700)
        self._conn = sqlite3.connect(
            str(self._path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._configure()
            self._create_schema()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @classmethod
    def for_bucket(cls, bucket: str, **kwargs: Any) -> P2PInventoryStore:
        """Open the canonical database for *bucket*. Call ``activate`` next."""
        return cls(default_p2p_inventory_path(bucket), bucket, **kwargs)

    def _configure(self) -> None:
        self._conn.execute("PRAGMA busy_timeout = 5000")
        mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise sqlite3.OperationalError("P2P inventory database could not enable WAL mode")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def _create_schema(self) -> None:
        current_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version != P2P_INVENTORY_SCHEMA_VERSION:
            # The routing index is role-local and rebuilt from peer snapshots;
            # recreating it is safer than reading a different table shape.
            self._conn.executescript(
                """
                DROP TABLE IF EXISTS inventory_staging;
                DROP TABLE IF EXISTS inventory_holders;
                DROP TABLE IF EXISTS inventory_sessions;
                """
            )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory_sessions (
                cid TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                serve_slots INTEGER NOT NULL,
                phase_gen INTEGER,
                expected_seq INTEGER NOT NULL DEFAULT 0,
                last_applied INTEGER NOT NULL DEFAULT -1,
                snapshot_ready INTEGER NOT NULL DEFAULT 0,
                committed_gen INTEGER,
                committed_seq INTEGER,
                committed_digest TEXT,
                staging_started REAL,
                disabled INTEGER NOT NULL DEFAULT 0,
                connected_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_staging (
                token TEXT NOT NULL,
                cid TEXT NOT NULL,
                gen INTEGER NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (token, gen, path),
                FOREIGN KEY (token) REFERENCES inventory_sessions(token) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS inventory_holders (
                bucket TEXT NOT NULL,
                cid TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY (bucket, cid, path)
            );

            CREATE INDEX IF NOT EXISTS inventory_holders_content
                ON inventory_holders(bucket, path, sha256);
            CREATE INDEX IF NOT EXISTS inventory_holders_cid
                ON inventory_holders(cid);
            CREATE INDEX IF NOT EXISTS inventory_staging_cid
                ON inventory_staging(cid);
            """
        )
        self._conn.execute(f"PRAGMA user_version = {P2P_INVENTORY_SCHEMA_VERSION}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("P2P inventory store is closed")

    @contextmanager
    def _transaction(self):
        self._ensure_open()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def activate(self) -> None:
        """Clear crash leftovers before this node publishes master readiness."""
        with self._lock, self._transaction():
            self._session_addresses.clear()
            self._conn.execute("DELETE FROM inventory_staging")
            self._conn.execute("DELETE FROM inventory_sessions")
            self._conn.execute("DELETE FROM inventory_holders")

    def close(self) -> None:
        """Close the database after the HTTP and WSS surfaces have stopped."""
        with self._lock:
            if self._closed:
                return
            self._session_addresses.clear()
            self._conn.close()
            self._closed = True

    def begin_session(
        self,
        cid: str,
        *,
        serve_slots: int,
        token: str | None = None,
        remote_address: str | None = None,
    ) -> str:
        """Replace *cid*'s publisher owner and return its opaque session token.

        ``serve_slots`` is the capacity the publisher advertised on the
        authenticated handshake. It is stored with the session because the
        master echoes it to requesters in a source candidate (ADR-0030), so a
        candidate can never exist without a live capacity advertisement.
        """
        if not is_valid_cid(cid):
            raise ValueError("invalid publisher CID")
        if not is_valid_serve_slots(serve_slots):
            raise ValueError("invalid serve_slots")
        owner_token = token or secrets.token_urlsafe(32)
        if not owner_token:
            raise ValueError("session token is required")

        with self._lock, self._transaction():
            self._cleanup_expired_staging_locked()
            current = self._conn.execute(
                "SELECT token FROM inventory_sessions WHERE cid = ?", (cid,)
            ).fetchone()
            if current is None:
                count = int(
                    self._conn.execute("SELECT COUNT(*) FROM inventory_sessions").fetchone()[0]
                )
                if count >= self._max_sessions:
                    raise InventorySessionLimitError(
                        "P2P inventory publisher session quota reached"
                    )
                self._conn.execute(
                    """
                    INSERT INTO inventory_sessions(cid, token, serve_slots, connected_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cid, owner_token, serve_slots, self._clock()),
                )
            else:
                old_token = str(current["token"])
                self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (old_token,))
                self._conn.execute(
                    """
                    UPDATE inventory_sessions
                    SET token = ?, serve_slots = ?, phase_gen = NULL, expected_seq = 0,
                        last_applied = -1, snapshot_ready = 0,
                        committed_gen = NULL, committed_seq = NULL,
                        committed_digest = NULL, staging_started = NULL,
                        disabled = 0, connected_at = ?
                    WHERE cid = ?
                    """,
                    (owner_token, serve_slots, self._clock(), cid),
                )
            if remote_address is not None:
                self._session_addresses[cid] = (owner_token, remote_address)
            else:
                self._session_addresses.pop(cid, None)
        return owner_token

    def end_session(self, cid: str, token: str) -> bool:
        """Purge *cid* only when *token* still owns its current connection."""
        with self._lock, self._transaction():
            row = self._conn.execute(
                "SELECT token FROM inventory_sessions WHERE cid = ?", (cid,)
            ).fetchone()
            if row is None or str(row["token"]) != token:
                self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
                return False
            self._session_addresses.pop(cid, None)
            self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
            self._conn.execute("DELETE FROM inventory_holders WHERE cid = ?", (cid,))
            self._conn.execute("DELETE FROM inventory_sessions WHERE cid = ?", (cid,))
            return True

    def session_address(self, cid: str) -> str | None:
        """Return the remote address bound to the current WSS owner."""
        with self._lock:
            owner = self._session_addresses.get(cid)
            return owner[1] if owner is not None else None

    def session_serve_slots(self, cid: str) -> int | None:
        """Return the capacity the current WSS owner advertised, or ``None``.

        ``None`` means the CID has no live publisher session, so it cannot be
        returned as a source candidate (ADR-0030).
        """
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT serve_slots FROM inventory_sessions WHERE cid = ? AND disabled = 0",
                (cid,),
            ).fetchone()
        return int(row["serve_slots"]) if row is not None else None

    def has_session(self, cid: str) -> bool:
        """Return whether *cid* owns an admitted, non-rejected publisher session."""
        with self._lock:
            self._ensure_open()
            return (
                self._conn.execute(
                    "SELECT 1 FROM inventory_sessions WHERE cid = ? AND disabled = 0", (cid,)
                ).fetchone()
                is not None
            )

    def _cleanup_expired_staging_locked(self) -> int:
        cutoff = self._clock() - self._staging_ttl_s
        rows = self._conn.execute(
            """
            SELECT token FROM inventory_sessions
            WHERE staging_started IS NOT NULL AND staging_started < ?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            token = str(row["token"])
            self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
            self._conn.execute(
                """
                UPDATE inventory_sessions
                SET phase_gen = NULL, expected_seq = 0, last_applied = -1,
                    snapshot_ready = 0, staging_started = NULL
                WHERE token = ?
                """,
                (token,),
            )
        return len(rows)

    @staticmethod
    def _wire_counter(value: object) -> int:
        return (
            int(value)
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= P2P_WIRE_INT_MAX
            else 0
        )

    def reject_session(
        self,
        cid: str,
        token: str,
        *,
        gen: int = 0,
        seq: int = 0,
        reason: str = "invalid",
    ) -> P2PInventoryAckFrame:
        """Disable one current publisher while preserving its committed rows."""
        try:
            with self._lock, self._transaction():
                return self._reject_locked(cid, token, gen=gen, seq=seq, reason=reason)
        except sqlite3.Error:
            log.exception("Failed to persist P2P inventory publisher rejection")
            return build_p2p_inventory_ack(
                gen=self._wire_counter(gen),
                seq=self._wire_counter(seq),
                status="rejected",
                reason="storage",
            )

    def _reject_locked(
        self, cid: str, token: str, *, gen: int, seq: int, reason: str
    ) -> P2PInventoryAckFrame:
        row = self._session_locked(cid, token)
        if row is None:
            return build_p2p_inventory_ack(
                gen=self._wire_counter(gen),
                seq=self._wire_counter(seq),
                status="rejected",
                reason="old_owner",
            )
        self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
        self._conn.execute(
            """
            UPDATE inventory_sessions
            SET disabled = 1, phase_gen = NULL, expected_seq = 0,
                last_applied = -1, snapshot_ready = 0, staging_started = NULL
            WHERE cid = ? AND token = ?
            """,
            (cid, token),
        )
        return build_p2p_inventory_ack(
            gen=self._wire_counter(gen),
            seq=self._wire_counter(seq),
            status="rejected",
            reason=reason,
        )

    def _session_locked(self, cid: str, token: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM inventory_sessions WHERE cid = ? AND token = ?", (cid, token)
        ).fetchone()

    def apply_frame(self, cid: str, token: str, frame: object) -> P2PInventoryAckFrame:
        """Validate and atomically apply one exact snapshot/delta frame."""
        clean = validate_p2p_inventory_frame(frame)
        if clean is None:
            raw = frame if isinstance(frame, dict) else {}
            return self.reject_session(
                cid,
                token,
                gen=self._wire_counter(raw.get("gen")),
                seq=self._wire_counter(raw.get("seq")),
                reason="invalid",
            )

        try:
            with self._lock, self._transaction():
                self._cleanup_expired_staging_locked()
                row = self._session_locked(cid, token)
                if row is None:
                    return build_p2p_inventory_ack(
                        gen=clean["gen"],
                        seq=clean["seq"],
                        status="rejected",
                        reason="old_owner",
                    )
                if bool(row["disabled"]):
                    return build_p2p_inventory_ack(
                        gen=clean["gen"],
                        seq=clean["seq"],
                        status="rejected",
                        reason="session_disabled",
                    )
                if clean["mode"] == "snapshot":
                    return self._apply_snapshot_locked(cid, token, row, clean)
                return self._apply_delta_locked(cid, token, row, clean)
        except sqlite3.Error:
            log.exception("P2P inventory write failed for publisher {}", cid)
            return self.reject_session(
                cid,
                token,
                gen=clean["gen"],
                seq=clean["seq"],
                reason="storage",
            )

    def _resync_locked(
        self, cid: str, token: str, *, gen: int, expected: int
    ) -> P2PInventoryAckFrame:
        self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
        self._conn.execute(
            """
            UPDATE inventory_sessions
            SET phase_gen = NULL, expected_seq = 0, last_applied = -1,
                snapshot_ready = 0, staging_started = NULL
            WHERE cid = ? AND token = ?
            """,
            (cid, token),
        )
        return build_p2p_inventory_ack(
            gen=gen,
            seq=max(0, min(expected, P2P_WIRE_INT_MAX)),
            status="resync",
        )

    def _counts_locked(self, cid: str, token: str) -> tuple[int, int, int]:
        committed = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM inventory_holders WHERE cid = ?", (cid,)
            ).fetchone()[0]
        )
        staged = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM inventory_staging WHERE token = ?", (token,)
            ).fetchone()[0]
        )
        global_count = int(
            self._conn.execute("SELECT COUNT(*) FROM inventory_holders").fetchone()[0]
        ) + int(self._conn.execute("SELECT COUNT(*) FROM inventory_staging").fetchone()[0])
        return committed, staged, global_count

    def _quota_allows_locked(self, cid: str, token: str, added: int) -> bool:
        committed, staged, global_count = self._counts_locked(cid, token)
        return (
            committed + staged + added <= self._per_cid_quota
            and global_count + added <= self._global_quota
        )

    def _apply_snapshot_locked(
        self,
        cid: str,
        token: str,
        row: sqlite3.Row,
        frame: P2PInventoryFrame,
    ) -> P2PInventoryAckFrame:
        gen = frame["gen"]
        seq = frame["seq"]
        committed_gen = row["committed_gen"]
        committed_seq = row["committed_seq"]
        committed_digest = row["committed_digest"]
        terminal_digest = None if frame["more"] else _canonical_snapshot_digest(frame)

        if (
            committed_gen is not None
            and gen == int(committed_gen)
            and committed_seq is not None
            and seq == int(committed_seq)
            and not frame["more"]
        ):
            if committed_digest is not None and str(committed_digest) == terminal_digest:
                return build_p2p_inventory_ack(gen=gen, seq=seq, status="ok")
            return self._resync_locked(cid, token, gen=gen, expected=0)
        if committed_gen is not None and gen <= int(committed_gen):
            return self._resync_locked(cid, token, gen=gen, expected=0)
        if frame["more"] and seq == P2P_WIRE_INT_MAX:
            return self._reject_locked(cid, token, gen=gen, seq=seq, reason="invalid")

        phase_gen = row["phase_gen"]
        expected = int(row["expected_seq"])
        if phase_gen is None or gen > int(phase_gen):
            if seq != 0:
                return self._resync_locked(cid, token, gen=gen, expected=0)
            self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
            self._conn.execute(
                """
                UPDATE inventory_sessions
                SET phase_gen = ?, expected_seq = 0, last_applied = -1,
                    snapshot_ready = 0, staging_started = ?
                WHERE cid = ? AND token = ?
                """,
                (gen, self._clock(), cid, token),
            )
            expected = 0
        elif gen < int(phase_gen) or seq != expected:
            wanted = expected if gen == int(phase_gen) else 0
            return self._resync_locked(cid, token, gen=gen, expected=wanted)

        entries = frame["entries"]
        paths = [entry["path"] for entry in entries]
        if paths:
            placeholders = ",".join("?" for _ in paths)
            duplicate = self._conn.execute(
                f"""
                SELECT 1 FROM inventory_staging
                WHERE token = ? AND path IN ({placeholders})
                LIMIT 1
                """,
                (token, *paths),
            ).fetchone()
            if duplicate is not None:
                return self._reject_locked(cid, token, gen=gen, seq=seq, reason="invalid")
        if not self._quota_allows_locked(cid, token, len(entries)):
            return self._reject_locked(cid, token, gen=gen, seq=seq, reason="quota")

        try:
            self._conn.executemany(
                """
                INSERT INTO inventory_staging(token, cid, gen, path, sha256, size, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        token,
                        cid,
                        gen,
                        entry["path"],
                        entry["sha256"],
                        entry["size"],
                        self._clock(),
                    )
                    for entry in entries
                ],
            )
        except sqlite3.IntegrityError:
            return self._reject_locked(cid, token, gen=gen, seq=seq, reason="invalid")

        if frame["more"]:
            self._conn.execute(
                "UPDATE inventory_sessions SET expected_seq = ? WHERE cid = ? AND token = ?",
                (seq + 1, cid, token),
            )
            return build_p2p_inventory_ack(gen=gen, seq=seq, status="ok")

        self._conn.execute("DELETE FROM inventory_holders WHERE cid = ?", (cid,))
        self._conn.execute(
            """
            INSERT INTO inventory_holders(bucket, cid, path, sha256, size)
            SELECT ?, cid, path, sha256, size
            FROM inventory_staging WHERE token = ? AND gen = ?
            """,
            (self._bucket, token, gen),
        )
        self._conn.execute("DELETE FROM inventory_staging WHERE token = ?", (token,))
        self._conn.execute(
            """
            UPDATE inventory_sessions
            SET phase_gen = ?, expected_seq = ?, last_applied = ?,
                snapshot_ready = 1, committed_gen = ?, committed_seq = ?,
                committed_digest = ?, staging_started = NULL
            WHERE cid = ? AND token = ?
            """,
            (gen, min(seq + 1, P2P_WIRE_INT_MAX), seq, gen, seq, terminal_digest, cid, token),
        )
        return build_p2p_inventory_ack(gen=gen, seq=seq, status="ok")

    def _apply_delta_locked(
        self,
        cid: str,
        token: str,
        row: sqlite3.Row,
        frame: P2PInventoryFrame,
    ) -> P2PInventoryAckFrame:
        gen = frame["gen"]
        seq = frame["seq"]
        phase_gen = row["phase_gen"]
        last_applied = int(row["last_applied"])
        if phase_gen is None or gen != int(phase_gen) or not bool(row["snapshot_ready"]):
            return self._resync_locked(cid, token, gen=gen, expected=0)
        if seq <= last_applied:
            return build_p2p_inventory_ack(gen=gen, seq=seq, status="ok")
        expected = last_applied + 1
        if expected > P2P_WIRE_INT_MAX or seq != expected:
            return self._resync_locked(
                cid, token, gen=gen, expected=min(expected, P2P_WIRE_INT_MAX)
            )

        paths = [entry["path"] for entry in frame["entries"]]
        placeholders = ",".join("?" for _ in paths)
        existing = {
            str(item[0])
            for item in self._conn.execute(
                f"""
                SELECT path FROM inventory_holders
                WHERE bucket = ? AND cid = ? AND path IN ({placeholders})
                """,
                (self._bucket, cid, *paths),
            ).fetchall()
        }
        upserts = [entry for entry in frame["entries"] if "sha256" in entry]
        deletes = [entry for entry in frame["entries"] if "deleted" in entry]
        added = sum(1 for entry in upserts if entry["path"] not in existing)
        removed = sum(1 for entry in deletes if entry["path"] in existing)
        committed, staged, global_count = self._counts_locked(cid, token)
        if (
            committed + staged + added - removed > self._per_cid_quota
            or global_count + added - removed > self._global_quota
        ):
            return self._reject_locked(cid, token, gen=gen, seq=seq, reason="quota")

        for entry in frame["entries"]:
            if "sha256" in entry:
                self._conn.execute(
                    """
                    INSERT INTO inventory_holders(bucket, cid, path, sha256, size)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(bucket, cid, path) DO UPDATE SET
                        sha256 = excluded.sha256, size = excluded.size
                    """,
                    (self._bucket, cid, entry["path"], entry["sha256"], entry["size"]),
                )
            else:
                self._conn.execute(
                    "DELETE FROM inventory_holders WHERE bucket = ? AND cid = ? AND path = ?",
                    (self._bucket, cid, entry["path"]),
                )
        self._conn.execute(
            """
            UPDATE inventory_sessions
            SET expected_seq = ?, last_applied = ?
            WHERE cid = ? AND token = ?
            """,
            (min(seq + 1, P2P_WIRE_INT_MAX), seq, cid, token),
        )
        return build_p2p_inventory_ack(gen=gen, seq=seq, status="ok")

    def holders_for(
        self, files: Iterable[P2PSourceFile]
    ) -> Mapping[tuple[str, str], tuple[InventoryHolder, ...]]:
        """Return immutable current-owner holder snapshots for exact identities."""
        result: dict[tuple[str, str], tuple[InventoryHolder, ...]] = {}
        with self._lock:
            self._ensure_open()
            for file in files:
                path = file["path"]
                digest = file["sha256"]
                rows = self._conn.execute(
                    """
                    SELECT h.cid, h.path, h.sha256, h.size
                    FROM inventory_holders AS h
                    JOIN inventory_sessions AS s ON s.cid = h.cid
                    WHERE h.bucket = ? AND h.path = ? AND h.sha256 = ?
                        AND s.disabled = 0
                    ORDER BY h.cid
                    """,
                    (self._bucket, path, digest),
                ).fetchall()
                result[(path, digest)] = tuple(
                    InventoryHolder(
                        cid=str(row["cid"]),
                        path=str(row["path"]),
                        sha256=str(row["sha256"]),
                        size=int(row["size"]),
                    )
                    for row in rows
                )
        return MappingProxyType(result)

    def row_counts(self) -> tuple[int, int, int]:
        """Return ``(sessions, staged, committed)`` for diagnostics/tests."""
        with self._lock:
            self._ensure_open()
            return tuple(
                int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "inventory_sessions",
                    "inventory_staging",
                    "inventory_holders",
                )
            )  # type: ignore[return-value]


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
        # full resync on both peers. The publish loop treats the timeout as
        # "stop publishing on this connection" instead; the event flow keeps
        # working and the next connection re-publishes a fresh snapshot.
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
        try:
            await self._publish_loop_inner()
        except (TimeoutError, ConnectionError):
            # The master stopped acknowledging (receiver backlogged under a
            # burst) or the transport died. Publishing is disabled for this
            # connection; the websocket stays open so the event channel it
            # carries keeps flowing, and the next connection's ``start()``
            # resets the publisher and re-snapshots. The master's session
            # expires via its staging TTL in the meantime.
            self._disabled = True
            log.warning("P2P inventory publisher stopped: master not acknowledging")

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
