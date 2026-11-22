# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Local reconciliation pass of the serving role (p2p master).

The device that serves the share holds the reference copy, so it has no peer to
compare against and runs no sync cycle: the watcher is the only writer of the
invariants a cycle keeps true (empty-directory sentinels, deletion markers,
transfer artifacts), and an event the kernel dropped stays unrepaired. These
tests pin each invariant to the pass that replays it, plus the pass's lifecycle
on the role.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from hsync_common.constants import HSYNCKEEP_FILENAME
from hsync_common.models.file import File
from hsync_common.models.tombstone import tombstone_key

from hsync_client import master_maintenance, sync_state, tombstones

REPEAT_TIMEOUT_S = 5.0


def _cid(letter: str) -> str:
    """A valid 21-character device id."""
    return f"Peer{letter}1234567890ABCDEF"


def _config(tmp_path: Any, cid: str) -> dict[str, Any]:
    return {
        "LOCAL_BUCKET": str(tmp_path),
        "CID": cid,
        "SYNC_DIRECTION": "bidirectional",
    }


def _wait_for(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + REPEAT_TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _no_leftover_pass() -> Any:
    """A pass thread never outlives its test."""
    yield
    master_maintenance.stop_master_maintenance()


class TestLocalReconcileRepairsTheShare:
    """The pass repairs exactly what a missed watcher event leaves behind."""

    def test_a_deletion_the_watcher_missed_is_minted(self, tmp_path):
        """A merge base whose file is gone mints a marker that dominates it."""
        local = _cid("L")
        path = tmp_path / "a.txt"
        path.write_bytes(b"content")
        file_obj = File.get(str(path))
        assert file_obj is not None
        cfg = _config(tmp_path, local)
        with patch("hsync_client.state_backend.CONFIG", cfg):
            sync_state.record_base(str(path), file_obj.sha256sum or "", vclock={local: 1})
            sync_state.flush()
            sync_state._stores.clear()
            tombstones._stores.clear()
        path.unlink()  # no watcher running: the disk is the only witness

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            master_maintenance.local_reconcile()

            marker = tombstones.get_payload()[tombstone_key("a.txt")]
        assert marker["sha256"] == file_obj.sha256sum
        # The marker inherits the base clock and is ticked by this device, so it
        # dominates the version the peers hold.
        assert marker["vclock"] == {local: 2}
        # The base tracked a file that no longer exists: it is dropped, so the
        # next pass has nothing to re-mint and the store stays bounded.
        assert list(sync_state.iter_merge_bases()) == []

    def test_the_empty_directory_markers_follow_the_content(self, tmp_path):
        """A missing sentinel is created and a stale one is pruned."""
        empty = tmp_path / "empty"
        empty.mkdir()
        filled = tmp_path / "filled"
        filled.mkdir()
        (filled / "note.txt").write_bytes(b"x")
        (filled / HSYNCKEEP_FILENAME).write_bytes(b"")
        cfg = _config(tmp_path, _cid("L"))

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            master_maintenance.local_reconcile()

        assert (empty / HSYNCKEEP_FILENAME).exists()
        assert not (filled / HSYNCKEEP_FILENAME).exists()

    def test_a_leftover_transfer_artifact_is_removed(self, tmp_path):
        """An abandoned inbound ``.part`` is dropped instead of lingering."""
        stale = tmp_path / "movie.mkv.part"
        stale.write_bytes(b"half")
        cfg = _config(tmp_path, _cid("L"))

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            master_maintenance.local_reconcile()

        assert not stale.exists()

    def test_the_sweep_spares_an_artifact_a_writer_still_owns(self, tmp_path):
        """A live transfer's ``.part`` survives the pass until it is released."""
        from hsync_client.utils.paths import (
            register_inflight_partial,
            unregister_inflight_partial,
        )

        live = tmp_path / "live.mkv.part"
        live.write_bytes(b"half")
        cfg = _config(tmp_path, _cid("L"))

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            register_inflight_partial(str(live))
            try:
                master_maintenance.local_reconcile()
                assert live.exists()
            finally:
                unregister_inflight_partial(str(live))
            master_maintenance.local_reconcile()

        assert not live.exists()


class TestMaintenanceThreadLifecycle:
    """The pass runs with the serving stint and nowhere else."""

    def test_the_pass_repairs_at_once_and_then_on_the_interval(self):
        """The first pass is immediate: a stale marker has no other repairer."""
        calls: list[float] = []
        with patch(
            "hsync_client.master_maintenance.local_reconcile",
            side_effect=lambda: calls.append(time.monotonic()),
        ):
            master_maintenance.start_master_maintenance(interval=0.05)
            assert _wait_for(lambda: len(calls) >= 2)

    def test_stop_ends_the_pass(self):
        calls: list[float] = []
        with patch(
            "hsync_client.master_maintenance.local_reconcile",
            side_effect=lambda: calls.append(time.monotonic()),
        ):
            master_maintenance.start_master_maintenance(interval=0.05)
            assert _wait_for(lambda: bool(calls))
            master_maintenance.stop_master_maintenance()
            settled = len(calls)
            time.sleep(0.2)

        assert len(calls) == settled

    def test_starting_twice_keeps_one_pass(self):
        with patch("hsync_client.master_maintenance.local_reconcile"):
            master_maintenance.start_master_maintenance(interval=10)
            first = master_maintenance._thread
            master_maintenance.start_master_maintenance(interval=10)

            assert master_maintenance._thread is first

    def test_a_failing_pass_never_kills_the_role(self):
        """A raise is contained and the next interval still runs."""
        calls: list[int] = []

        def _fail() -> None:
            calls.append(1)
            raise RuntimeError("walk failed")

        with patch("hsync_client.master_maintenance.local_reconcile", side_effect=_fail):
            master_maintenance.start_master_maintenance(interval=0.02)
            assert _wait_for(lambda: len(calls) >= 3)

    def test_periodic_resync_off_disables_the_pass(self):
        with (
            patch("hsync_client.master_maintenance.CONFIG", {"RESYNC": 0}),
            patch("hsync_client.master_maintenance.local_reconcile") as reconcile,
        ):
            master_maintenance.start_master_maintenance()
            time.sleep(0.05)

            assert not reconcile.called
            assert master_maintenance._thread is None


class TestRoleWiring:
    """The pass belongs to the serving stint."""

    def test_the_role_teardown_stops_the_pass(self):
        from hsync_client.p2p_serving import _stop_p2p_serving

        with patch("hsync_client.p2p_serving.stop_master_maintenance") as stop:
            _stop_p2p_serving(None, None)

        stop.assert_called_once_with()
