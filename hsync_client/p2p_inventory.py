# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""P2P peer inventory (master-side persistence and shared contracts).

The store is an intentionally volatile SQLite index: master activation clears
crash leftovers and connected publishers rebuild it from snapshots. The
slave-side publisher lives in ``hsync_client.p2p_inventory_publisher``; the
shared observer contract, holder/share views and the SQLite store live here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol

from hsync_common.ids import is_valid_cid
from hsync_common.models.file import File
from hsync_common.models.sentinel import is_sentinel_file
from hsync_common.protocol.peer_download import (
    P2P_SERVE_SLOTS_MAX,
    P2P_WIRE_INT_MAX,
    P2PInventoryAckFrame,
    P2PInventoryFrame,
    P2PSourceFile,
    build_p2p_inventory_ack,
    validate_p2p_inventory_frame,
)
from loguru import logger as log

P2P_INVENTORY_PER_CID_QUOTA = 2_000_000
P2P_INVENTORY_GLOBAL_QUOTA = 20_000_000
P2P_INVENTORY_MAX_SESSIONS = 64
P2P_INVENTORY_STAGING_TTL_S = 5 * 60.0
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

    def report_received(self, rel_path: str, transferred: int, total: int) -> None:
        """Publish the bytes stored for a peer-served file (delivery progress).

        The sender's own counter measures what it handed to its transport; only
        the receiver knows what it stored. A no-op observer (transports with no
        master WebSocket session) sends nothing.
        """
        ...


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

    def report_received(self, rel_path: str, transferred: int, total: int) -> None:
        return


NOOP_INVENTORY_OBSERVER: InventoryObserver = _NoopInventoryObserver()


@dataclass(frozen=True, slots=True)
class DeviceShareView:
    """One live device's backlog against the share it is compared with.

    ``pending_here`` counts the identities the device published and the share
    lacks; ``pending_there`` the share identities the device does not hold.
    """

    cid: str
    held: int
    pending_here: int
    pending_there: int


@dataclass(frozen=True, slots=True)
class ShareComparison:
    """The published inventories, read in one pass for a serving classification.

    ``held`` is the union of every live device's identities with its size,
    ``holders`` how many live devices hold each identity (per-file coverage) and
    ``devices`` the per-device backlog. One consistent snapshot: a device joining
    or republishing mid-read cannot split the union from the tallies.
    """

    devices: tuple[DeviceShareView, ...]
    held: dict[tuple[str, str], int]
    holders: dict[tuple[str, str], int]


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
        # Serving-role hook: the master re-classifies its share when a device
        # joins, leaves or republishes its inventory (see master_status).
        self._change_callback: Callable[[], None] | None = None

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

        ``serve_slots`` is the block-serving capacity the publisher advertised;
        ``0`` means it shares only its read-only inventory and never serves
        blocks. The value is stored with the session: the master echoes a
        non-zero capacity to requesters in a source candidate (ADR-0030), while
        an inventory-only session is never returned as a source.
        """
        if not is_valid_cid(cid):
            raise ValueError("invalid publisher CID")
        if (
            not isinstance(serve_slots, int)
            or isinstance(serve_slots, bool)
            or not 0 <= serve_slots <= P2P_SERVE_SLOTS_MAX
        ):
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
        self._notify_changed()
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
        self._notify_changed()
        return True

    def session_address(self, cid: str) -> str | None:
        """Return the remote address bound to the current WSS owner."""
        with self._lock:
            owner = self._session_addresses.get(cid)
            return owner[1] if owner is not None else None

    def session_serve_slots(self, cid: str) -> int | None:
        """Return the block-serving capacity the current owner advertised.

        ``None`` means either no live publisher session, or an inventory-only
        session (capacity ``0``), so the CID cannot be a source candidate
        (ADR-0030).
        """
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT serve_slots FROM inventory_sessions WHERE cid = ? AND disabled = 0",
                (cid,),
            ).fetchone()
        if row is None:
            return None
        slots = int(row["serve_slots"])
        return slots if slots > 0 else None

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
                    ack = self._apply_snapshot_locked(cid, token, row, clean)
                else:
                    ack = self._apply_delta_locked(cid, token, row, clean)
        except sqlite3.Error:
            log.exception("P2P inventory write failed for publisher {}", cid)
            return self.reject_session(
                cid,
                token,
                gen=clean["gen"],
                seq=clean["seq"],
                reason="storage",
            )
        if ack.get("status") == "ok":
            self._notify_changed()
        return ack

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

    def live_session_cids(self) -> tuple[str, ...]:
        """CIDs with a live inventory session, sorted.

        A disabled session keeps its rows but is no longer a device the share can
        be compared with (a reconnect retires the previous token), so it never
        counts. A session that opened but has not committed its snapshot is still
        live: :meth:`published_session_cids` tells the two apart. Consumers:
        ``hsync_client.master_status`` (the completeness gate against the
        connected clients).
        """
        return self._session_cids(published_only=False)

    def published_session_cids(self) -> tuple[str, ...]:
        """CIDs whose inventory snapshot is committed, sorted.

        The devices a serving classification can stand on: an opened session
        whose snapshot is still staging says nothing about what the device holds.
        Consumers: ``hsync_client.master_status``.
        """
        return self._session_cids(published_only=True)

    def _session_cids(self, *, published_only: bool) -> tuple[str, ...]:
        """Sorted CIDs of the live sessions, optionally the published only."""
        sql = (
            "SELECT cid FROM inventory_sessions"
            " WHERE disabled = 0 AND committed_gen IS NOT NULL ORDER BY cid"
            if published_only
            else "SELECT cid FROM inventory_sessions WHERE disabled = 0 ORDER BY cid"
        )
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(sql).fetchall()
        return tuple(str(row["cid"]) for row in rows)

    def compare_share(self, local_identities: Iterable[tuple[str, str]]) -> ShareComparison:
        """Read every live device's inventory against the local identities.

        One query for the whole store, folded in memory into the union, the
        per-identity holder count and the per-device tallies. The per-file
        :meth:`holders_for` runs a JOIN per identity (~185 us/file measured,
        ~18 s per 100 000 files) and is unusable for a whole-share comparison;
        this bulk read costs ~0.1 s at the same scale. ``holders`` costs one
        small int per distinct identity, the only structure that grows with the
        share.

        The union and ``holders`` keep the empty-directory sentinel (the
        empty-dir verdict reads it); the per-device tallies drop it, because it
        is internal and never a user backlog entry.

        Consumers: ``hsync_client.master_status`` (the serving device reports a
        per-device verdict instead of a union that hides which device lags).
        """
        local = set(local_identities)
        with self._lock:
            self._ensure_open()
            cids = [
                str(row["cid"])
                for row in self._conn.execute(
                    """
                    SELECT cid FROM inventory_sessions
                    WHERE disabled = 0 AND committed_gen IS NOT NULL
                    ORDER BY cid
                    """
                ).fetchall()
            ]
            # held, pending_here, local-held per device.
            tallies: dict[str, list[int]] = {cid: [0, 0, 0] for cid in cids}
            held: dict[tuple[str, str], int] = {}
            holders: dict[tuple[str, str], int] = {}
            rows = self._conn.execute(
                """
                SELECT h.cid, h.path, h.sha256, h.size
                FROM inventory_holders AS h
                JOIN inventory_sessions AS s ON s.cid = h.cid
                WHERE h.bucket = ? AND s.disabled = 0
                """,
                (self._bucket,),
            )
            for row in rows:
                cid = str(row["cid"])
                tally = tallies.get(cid)
                if tally is None:  # session retired while the cursor was open
                    continue
                identity = (str(row["path"]), str(row["sha256"]))
                held.setdefault(identity, int(row["size"]))
                holders[identity] = holders.get(identity, 0) + 1
                # The empty-directory sentinel is internal (never user content):
                # it stays out of the per-device backlog, so a device that
                # already holds every user file reports no pending transfer.
                if is_sentinel_file(identity[0]):
                    continue
                tally[0] += 1
                if identity in local:
                    tally[2] += 1
                else:
                    tally[1] += 1
        total_local = len(local)
        devices = tuple(
            DeviceShareView(
                cid=cid,
                held=int(held_count),
                pending_here=int(pending_here),
                pending_there=total_local - int(local_held),
            )
            for cid, (held_count, pending_here, local_held) in tallies.items()
        )
        return ShareComparison(devices=devices, held=held, holders=holders)

    def set_change_callback(self, callback: Callable[[], None] | None) -> None:
        """Notify *callback* whenever the committed inventory set changes.

        Used by the serving role to re-classify its share against the devices
        (``hsync_client.master_status``) without polling. The callback runs on
        the caller's thread (the WebSocket handler), so it must only schedule
        work.
        """
        with self._lock:
            self._change_callback = callback

    def _notify_changed(self) -> None:
        callback = self._change_callback
        if callback is None:
            return
        try:
            callback()
        except Exception:  # never let a UI hook break an inventory frame
            pass

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
