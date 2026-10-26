# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the serving-role sync classification (``master_status``)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from hsync_common.protocol.peer_download import build_p2p_inventory_frame

from hsync_client.config import CONFIG
from hsync_client.master_status import MasterSyncStatus, classify
from hsync_client.p2p_inventory import P2PInventoryStore

CID_A = "A" * 21
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
BUCKET = "share"
LOG = logging.getLogger("test-master-status")

# Every store built by ``_store``; the autouse fixture below closes them.
_OPEN_STORES: list[P2PInventoryStore] = []


@pytest.fixture(autouse=True)
def _close_inventory_stores() -> Generator[None]:
    """Close the stores built by ``_store`` so no handle outlives the test.

    ``P2PInventoryStore`` opens a SQLite connection on construction; leaking it
    surfaces as ``ResourceWarning: unclosed database`` in whichever later test
    happens to trigger a GC pass.
    """
    yield
    for store in _OPEN_STORES:
        store.close()
    _OPEN_STORES.clear()


def _local(path: str, digest: str, size: int) -> dict:
    return {"filepath": path, "sha256sum": digest, "size": size}


def _store(tmp_path: Path, entries: list[dict] | None, *, session: bool = True):
    """A store with one publisher session holding *entries* (when given)."""
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", BUCKET)
    _OPEN_STORES.append(store)
    store.activate()
    if not session:
        return store
    token = store.begin_session(CID_A, serve_slots=2)
    if entries is not None:
        frame = build_p2p_inventory_frame(
            gen=1, seq=0, mode="snapshot", more=False, entries=entries
        )
        assert store.apply_frame(CID_A, token, frame)["status"] == "ok"
    return store


class TestClassify:
    """The pure classification of a local listing against published identities."""

    def test_synced_upload_and_download(self):
        held = {("a.bin", DIGEST_A): 10, ("remote-only.bin", DIGEST_B): 7}

        states, remote_like = classify(
            held,
            [_local("/share/a.bin", DIGEST_A, 10), _local("/share/b.bin", DIGEST_A, 5)],
            "/share",
        )

        assert states == [
            ("synced", "/share/a.bin", 10),
            ("to_upload", "/share/b.bin", 5),
            ("to_download", "/share/remote-only.bin", 7),
        ]
        assert remote_like == [
            {"filepath": "a.bin", "size": 10},
            {"filepath": "remote-only.bin", "size": 7},
        ]

    def test_same_path_different_content_is_an_upload(self):
        """A device holding another version of a local file does not sync it."""
        held = {("a.bin", DIGEST_B): 10}

        states, _remote_like = classify(held, [_local("/share/a.bin", DIGEST_A, 10)], "/share")

        assert states == [("to_upload", "/share/a.bin", 10)]

    def test_symlinks_and_sentinels_are_skipped(self):
        held = {("real.bin", DIGEST_A): 3, (".hsynckeep", DIGEST_A): 0}

        states, remote_like = classify(
            held,
            [
                {"filepath": "/share/link", "type": "symlink", "sha256sum": DIGEST_A, "size": 0},
                _local("/share/sub/.hsynckeep", DIGEST_A, 0),
                _local("/share/real.bin", DIGEST_A, 3),
            ],
            "/share",
        )

        assert states == [("synced", "/share/real.bin", 3)]
        assert remote_like == [{"filepath": "real.bin", "size": 3}]


class TestHeldIdentities:
    """The bulk read is one query, independent of the share size."""

    def _statements(self, store: P2PInventoryStore, entries: list[dict]) -> tuple[int, int]:
        count = 0

        def _trace(_statement: str) -> None:
            nonlocal count
            count += 1

        store._conn.set_trace_callback(_trace)
        try:
            held = store.held_identities()
        finally:
            store._conn.set_trace_callback(None)
        return count, len(held)

    def test_one_query_for_a_small_and_a_large_inventory(self, tmp_path):
        small = [{"path": f"f{i}.bin", "sha256": DIGEST_A, "size": 1} for i in range(50)]
        large = [{"path": f"g{i}.bin", "sha256": DIGEST_B, "size": 1} for i in range(400)]

        small_queries, small_rows = self._statements(_store(tmp_path / "small", small), small)
        large_queries, large_rows = self._statements(_store(tmp_path / "large", large), large)

        assert (small_queries, small_rows) == (1, 50)
        assert (large_queries, large_rows) == (1, 400)


class _Recorder:
    """Collect the emitted events (monkeypatched over ``emit_event``)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event, **payload) -> None:
        self.events.append((str(getattr(event, "value", event)), payload))

    def names(self) -> list[str]:
        return [name for name, _payload in self.events]

    def states(self) -> list[tuple[str, str]]:
        return [
            (payload["state"], payload["file"])
            for name, payload in self.events
            if name == "file_status"
        ]


@pytest.fixture()
def recorder(monkeypatch) -> _Recorder:
    from hsync_client import master_status

    rec = _Recorder()
    monkeypatch.setattr(master_status, "emit_event", rec)
    monkeypatch.setattr(master_status, "emit_folder_size", lambda *_a, **_k: None)
    return rec


class TestMasterSyncStatus:
    """The pass reports what the front-end needs, and nothing it cannot know."""

    @staticmethod
    def _status(store, local_files: list[dict], **kwargs) -> MasterSyncStatus:
        kwargs.setdefault("local_cache_ttl_s", 60.0)
        return MasterSyncStatus(
            lambda: store,
            LOG,
            local_provider=lambda: list(local_files),
            min_interval_s=0.0,
            **kwargs,
        )

    def test_no_session_reports_waiting_for_device(self, tmp_path, recorder, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, None, session=False)
        status = self._status(store, [])

        status.run_now()

        status.stop()
        store.close()
        # No preview markers: those belong to the window's own preview cycles.
        assert recorder.names() == ["sync_pending"]
        assert recorder.events[0][1]["reason"] == "no_device"

    def test_an_automatic_pass_emits_no_preview_markers(self, tmp_path, recorder, monkeypatch):
        """Inventory-driven passes must not consume the front-end's preview one-shot."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        status = self._status(store, [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)])

        status.run_now()

        status.stop()
        store.close()
        assert recorder.names() == ["file_status"]
        assert recorder.states() == [("synced", str(tmp_path / "a.bin"))]

    def test_a_connected_device_without_a_session_reports_awaiting_inventory(
        self, tmp_path, recorder, monkeypatch
    ):
        """A connected device that opened no inventory session is a waiting state."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, None, session=False)
        status = self._status(store, [], connected_provider=lambda: 1)

        status.run_now()

        status.stop()
        store.close()
        assert recorder.names() == ["sync_pending"]
        assert recorder.events[0][1]["reason"] == "awaiting_inventory"

    def test_a_connected_device_without_an_inventory_reports_waiting(
        self, tmp_path, recorder, monkeypatch
    ):
        """A device that has not published yet is "unknown", not "out of sync"."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, None)
        status = self._status(store, [_local(str(tmp_path / "a.bin"), DIGEST_A, 1)])

        status.run_now()

        status.stop()
        store.close()
        assert recorder.names() == ["sync_pending"]
        assert recorder.events[0][1]["reason"] == "awaiting_inventory"

    def test_it_classifies_against_the_published_inventory(self, tmp_path, recorder, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        local = [
            _local(str(tmp_path / "a.bin"), DIGEST_A, 4),
            _local(str(tmp_path / "b.bin"), DIGEST_B, 2),
        ]
        status = self._status(store, local)

        status.run_now()

        status.stop()
        store.close()
        assert recorder.names() == ["file_status", "file_status"]
        assert recorder.states() == [
            ("synced", str(tmp_path / "a.bin")),
            ("to_upload", str(tmp_path / "b.bin")),
        ]

    def test_the_local_listing_is_reused_between_passes(self, tmp_path, recorder, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        calls: list[int] = []

        def _local_provider() -> list[dict]:
            calls.append(1)
            return [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)]

        status = MasterSyncStatus(
            lambda: store, LOG, local_provider=_local_provider, min_interval_s=0.0
        )

        status.run_now()
        status.run_now()

        status.stop()
        store.close()
        assert len(calls) == 1

    def test_request_runs_a_pass_in_the_background(self, tmp_path, recorder, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        status = self._status(store, [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)])
        done = threading.Event()
        real_run = status.run_now

        def _run() -> None:
            real_run()
            done.set()

        monkeypatch.setattr(status, "run_now", _run)

        status.request()

        assert done.wait(timeout=5.0) is True
        status.stop()
        store.close()
        assert recorder.states() == [("synced", str(tmp_path / "a.bin"))]

    def test_stop_ends_the_worker(self, tmp_path, recorder, monkeypatch):
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, None, session=False)
        status = self._status(store, [])

        status.request()
        time.sleep(0.05)
        status.stop()
        before = len(recorder.events)
        status.request()  # a request after stop must stay inert
        time.sleep(0.05)

        store.close()
        assert len(recorder.events) == before
        assert not any(
            t.name == "hsync-master-status" and t.is_alive() for t in threading.enumerate()
        )
