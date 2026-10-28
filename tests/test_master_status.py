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
CID_B = "B" * 21
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


def _publish(store: P2PInventoryStore, cid: str, entries: list[dict]) -> None:
    """Open a session for *cid* and commit *entries* as its inventory snapshot."""
    token = store.begin_session(cid, serve_slots=2)
    frame = build_p2p_inventory_frame(gen=1, seq=0, mode="snapshot", more=False, entries=entries)
    assert store.apply_frame(cid, token, frame)["status"] == "ok"


def _store(tmp_path: Path, entries: list[dict] | None, *, session: bool = True):
    """A store with one publisher session holding *entries* (when given)."""
    store = P2PInventoryStore(tmp_path / "inventory.sqlite3", BUCKET)
    _OPEN_STORES.append(store)
    store.activate()
    if not session:
        return store
    if entries is None:
        store.begin_session(CID_A, serve_slots=2)  # connected, snapshot still staging
        return store
    _publish(store, CID_A, entries)
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
            ("synced", "/share/a.bin", 10, -1),
            ("to_upload", "/share/b.bin", 5, -1),
            ("to_download", "/share/remote-only.bin", 7, -1),
        ]
        assert remote_like == [
            {"filepath": "a.bin", "size": 10},
            {"filepath": "remote-only.bin", "size": 7},
        ]

    def test_a_row_carries_the_device_coverage(self):
        """A synced file states how many live devices actually hold it."""
        held = {("a.bin", DIGEST_A): 10, ("only-one.bin", DIGEST_B): 7}
        holders = {("a.bin", DIGEST_A): 3, ("only-one.bin", DIGEST_B): 1}

        states, _remote_like = classify(
            held,
            [_local("/share/a.bin", DIGEST_A, 10)],
            "/share",
            holders,
        )

        assert states == [
            ("synced", "/share/a.bin", 10, 3),
            ("to_download", "/share/only-one.bin", 7, 1),
        ]

    def test_same_path_different_content_is_an_upload(self):
        """A device holding another version of a local file does not sync it."""
        held = {("a.bin", DIGEST_B): 10}

        states, _remote_like = classify(held, [_local("/share/a.bin", DIGEST_A, 10)], "/share")

        assert states == [("to_upload", "/share/a.bin", 10, -1)]

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

        assert states == [("synced", "/share/real.bin", 3, -1)]
        assert remote_like == [{"filepath": "real.bin", "size": 3}]


class TestCompareShare:
    """One read yields the union, the coverage and the per-device tallies."""

    def _statements(self, store: P2PInventoryStore) -> tuple[int, int, int]:
        count = 0

        def _trace(_statement: str) -> None:
            nonlocal count
            count += 1

        store._conn.set_trace_callback(_trace)
        try:
            comparison = store.compare_share([])
        finally:
            store._conn.set_trace_callback(None)
        return count, len(comparison.held), len(comparison.devices)

    def test_the_query_count_is_independent_of_the_share_size(self, tmp_path):
        small = [{"path": f"f{i}.bin", "sha256": DIGEST_A, "size": 1} for i in range(50)]
        large = [{"path": f"g{i}.bin", "sha256": DIGEST_B, "size": 1} for i in range(400)]

        small_queries, small_rows, small_devices = self._statements(
            _store(tmp_path / "small", small)
        )
        large_queries, large_rows, large_devices = self._statements(
            _store(tmp_path / "large", large)
        )

        assert (small_queries, small_rows, small_devices) == (2, 50, 1)
        assert (large_queries, large_rows, large_devices) == (2, 400, 1)

    def test_a_session_without_a_committed_snapshot_is_not_compared(self, tmp_path):
        """An opened session that published nothing is not a device view."""
        store = _store(tmp_path, None)

        comparison = store.compare_share([])

        assert store.live_session_cids() == (CID_A,)
        assert store.published_session_cids() == ()
        assert comparison.devices == ()

    def test_each_device_gets_its_own_backlog(self, tmp_path):
        store = _store(tmp_path, [{"path": "shared.bin", "sha256": DIGEST_A, "size": 4}])
        _publish(store, CID_B, [{"path": "only-b.bin", "sha256": DIGEST_B, "size": 2}])

        comparison = store.compare_share([("shared.bin", DIGEST_A), ("only-local.bin", DIGEST_B)])

        by_cid = {view.cid: view for view in comparison.devices}
        assert by_cid[CID_A].held == 1
        assert by_cid[CID_A].pending_here == 0
        assert by_cid[CID_A].pending_there == 1  # only-local.bin
        assert by_cid[CID_B].held == 1
        assert by_cid[CID_B].pending_here == 1  # only-b.bin
        assert by_cid[CID_B].pending_there == 2  # both local identities
        assert comparison.holders[("shared.bin", DIGEST_A)] == 1
        assert comparison.held[("shared.bin", DIGEST_A)] == 4


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
        # The serving side brackets its pass and never uses the preview markers.
        assert recorder.names() == ["status_pass_start", "sync_pending", "status_pass_end"]
        assert recorder.events[1][1]["reason"] == "no_device"

    def test_a_pass_is_bracketed_without_preview_markers(self, tmp_path, recorder, monkeypatch):
        """Inventory-driven passes must not consume the front-end's preview one-shot."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        status = self._status(store, [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)])

        status.run_now()

        status.stop()
        store.close()
        names = recorder.names()
        assert names == [
            "status_pass_start",
            "device_status",
            "file_status",
            "status_pass_end",
        ]
        assert "preview_start" not in names and "preview_end" not in names
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
        assert recorder.names() == ["status_pass_start", "sync_pending", "status_pass_end"]
        assert recorder.events[1][1]["reason"] == "awaiting_inventory"

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
        assert recorder.names() == ["status_pass_start", "sync_pending", "status_pass_end"]
        assert recorder.events[1][1]["reason"] == "awaiting_inventory"

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
        assert recorder.names() == [
            "status_pass_start",
            "device_status",
            "file_status",
            "file_status",
            "status_pass_end",
        ]
        assert recorder.states() == [
            ("synced", str(tmp_path / "a.bin")),
            ("to_upload", str(tmp_path / "b.bin")),
        ]
        # Every row states its coverage: how many of the live devices hold it.
        coverage = [
            (payload["holders"], payload["devices"])
            for name, payload in recorder.events
            if name == "file_status"
        ]
        assert coverage == [(1, 1), (0, 1)]

    def test_each_device_is_reported_separately(self, tmp_path, recorder, monkeypatch):
        """Two devices: one holds the file, the other still has to receive it."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        _publish(store, CID_B, [])
        status = self._status(
            store,
            [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)],
            connected_provider=lambda: 2,
        )

        status.run_now()

        status.stop()
        store.close()
        devices = {
            payload["peer_cid"]: payload
            for name, payload in recorder.events
            if name == "device_status"
        }
        assert devices[CID_A] == {
            "peer_cid": CID_A,
            "held": 1,
            "pending_here": 0,
            "pending_there": 0,
        }
        assert devices[CID_B] == {
            "peer_cid": CID_B,
            "held": 0,
            "pending_here": 0,
            "pending_there": 1,
        }
        coverage = [
            (payload["holders"], payload["devices"])
            for name, payload in recorder.events
            if name == "file_status"
        ]
        assert coverage == [(1, 2)]

    def test_a_device_without_a_published_inventory_blocks_the_verdict(
        self, tmp_path, recorder, monkeypatch
    ):
        """Two connected, one published: waiting with both counts, no verdict."""
        monkeypatch.setitem(CONFIG, "LOCAL_BUCKET", str(tmp_path))
        store = _store(tmp_path, [{"path": "a.bin", "sha256": DIGEST_A, "size": 4}])
        store.begin_session(CID_B, serve_slots=2)  # connected, snapshot still staging
        status = self._status(
            store,
            [_local(str(tmp_path / "a.bin"), DIGEST_A, 4)],
            connected_provider=lambda: 2,
        )

        status.run_now()

        status.stop()
        store.close()
        assert recorder.names() == ["status_pass_start", "sync_pending", "status_pass_end"]
        payload = recorder.events[1][1]
        assert payload["reason"] == "awaiting_inventory"
        assert (payload["devices"], payload["expected"]) == (1, 2)

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
