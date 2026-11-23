# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the metadata-only (touch) arbitration helper.

A content-identical remote change is adopted causally when its vector clock
dominates. With no measured clock every replica advertises ``corrected_mtime =
0`` (ADR-0054) and a bare ``touch`` mints no new version, so both sides carry the
same vclock; the newer advertised *raw* mtime then decides. That choice is
deterministic and symmetric (both replicas pick the same winner from the same
pair of claims), so a touch converges with no ping-pong.
"""

from __future__ import annotations

import os

from hsync_client.sync import (
    _apply_local_raw_mtime,
    _metadata_only_decision,
    _raw_local_mtime,
    _remote_metadata_wins,
)


def _touch(tmp_path, mtime: int) -> str:
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return str(path)


class TestRemoteMetadataWins:
    def test_remote_dominates_is_adopted(self):
        assert (
            _remote_metadata_wins(
                abs_path="/nonexistent",  # short-circuits before the stat
                remote_raw_mtime=0,
                local_vclock={"a": 1},
                remote_vclock={"a": 1, "b": 1},
            )
            is True
        )

    def test_local_dominates_is_not_adopted(self):
        assert (
            _remote_metadata_wins(
                abs_path="/nonexistent",
                remote_raw_mtime=0,
                local_vclock={"a": 1, "b": 1},
                remote_vclock={"a": 1},
            )
            is False
        )

    def test_equal_clock_newer_remote_raw_mtime_wins(self, tmp_path):
        local = _touch(tmp_path, 1_600_000_000)
        assert (
            _remote_metadata_wins(
                abs_path=local,
                remote_raw_mtime=1_600_000_500,
                local_vclock={"a": 1},
                remote_vclock={"a": 1},
            )
            is True
        )

    def test_equal_clock_older_remote_raw_mtime_loses(self, tmp_path):
        local = _touch(tmp_path, 1_600_000_500)
        assert (
            _remote_metadata_wins(
                abs_path=local,
                remote_raw_mtime=1_600_000_000,
                local_vclock={"a": 1},
                remote_vclock={"a": 1},
            )
            is False
        )

    def test_equal_clock_equal_raw_mtime_is_noop(self, tmp_path):
        local = _touch(tmp_path, 1_600_000_000)
        assert (
            _remote_metadata_wins(
                abs_path=local,
                remote_raw_mtime=1_600_000_000,
                local_vclock={"a": 1},
                remote_vclock={"a": 1},
            )
            is False
        )

    def test_missing_local_path_is_not_adopted(self, tmp_path):
        assert (
            _remote_metadata_wins(
                abs_path=str(tmp_path / "nope"),
                remote_raw_mtime=1_600_000_500,
                local_vclock={"a": 1},
                remote_vclock={"a": 1},
            )
            is False
        )


class TestRawLocalMtime:
    def test_reads_on_disk_mtime(self, tmp_path):
        assert _raw_local_mtime(_touch(tmp_path, 1_700_000_000)) == 1_700_000_000

    def test_missing_path_is_none(self, tmp_path):
        assert _raw_local_mtime(str(tmp_path / "nope")) is None


class TestMetadataOnlyDecision:
    """The shared reconcile/realtime winner is one deterministic comparator."""

    def test_no_divergence_is_none(self, tmp_path):
        path = _touch(tmp_path, 1_600_000_000)
        assert _metadata_only_decision(
            direction="bidirectional",
            adopt_on_tie=False,
            local_vclock={"a": 1},
            remote_vclock={"a": 2},
            local_corrected=1_600_000_000,
            remote_corrected=1_600_000_000,
            abs_path=path,
            remote_raw_mtime=1_600_000_000,
        ) == (None, False)

    def test_download_only_adopts_even_an_older_raw_mtime(self, tmp_path):
        path = _touch(tmp_path, 1_600_000_500)
        assert _metadata_only_decision(
            direction="download-only",
            adopt_on_tie=False,
            local_vclock={"a": 1},
            remote_vclock={"a": 1},
            local_corrected=0,
            remote_corrected=0,
            abs_path=path,
            remote_raw_mtime=1_600_000_000,
        ) == (True, False)

    def test_equal_clock_newer_raw_wins_bidirectional(self, tmp_path):
        path = _touch(tmp_path, 1_600_000_000)
        assert _metadata_only_decision(
            direction="bidirectional",
            adopt_on_tie=False,
            local_vclock={"a": 1},
            remote_vclock={"a": 1},
            local_corrected=0,
            remote_corrected=0,
            abs_path=path,
            remote_raw_mtime=1_600_000_500,
        ) == (True, False)

    def test_trusted_clock_tie_adopts_remote_for_the_event_path(self, tmp_path):
        # update_file (adopt_on_tie=True): the peer just announced the change,
        # so an unresolved equal-clock divergence adopts its (even older) value.
        path = _touch(tmp_path, 1_600_000_900)
        assert _metadata_only_decision(
            direction="bidirectional",
            adopt_on_tie=True,
            local_vclock={"a": 1},
            remote_vclock={"a": 1},
            local_corrected=1_600_000_900,
            remote_corrected=1_600_000_000,
            abs_path=path,
            remote_raw_mtime=0,
        ) == (True, True)

    def test_trusted_clock_tie_keeps_local_for_the_reconcile(self, tmp_path):
        # Reconcile (adopt_on_tie=False): this replica is the local writer, so an
        # unresolved equal-clock divergence keeps and pushes the local value.
        path = _touch(tmp_path, 1_600_000_900)
        assert _metadata_only_decision(
            direction="bidirectional",
            adopt_on_tie=False,
            local_vclock={"a": 1},
            remote_vclock={"a": 1},
            local_corrected=1_600_000_900,
            remote_corrected=1_600_000_000,
            abs_path=path,
            remote_raw_mtime=0,
        ) == (False, True)

    def test_huge_raw_mtime_is_rejected_not_adopted_clamped(self, tmp_path):
        path = _touch(tmp_path, 1_600_000_000)
        # An over-bound advertised mtime must be rejected, never adopted
        # clamped: a clamped value would differ from the value the peer keeps
        # re-advertising and re-diverge every cycle.
        assert _metadata_only_decision(
            direction="bidirectional",
            adopt_on_tie=False,
            local_vclock={"a": 1},
            remote_vclock={"a": 1},
            local_corrected=0,
            remote_corrected=0,
            abs_path=path,
            remote_raw_mtime=10**12,
        ) == (None, False)

    def test_apply_local_raw_mtime_clamps_and_never_raises(self, tmp_path):
        path = tmp_path / "f.bin"
        path.write_bytes(b"x")
        _apply_local_raw_mtime(str(path), 10**12)
        assert int(os.stat(path).st_mtime) < 10**12
