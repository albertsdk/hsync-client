# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The timestamp a host publishes, and what it owes to its own clock.

A version's ``corrected_mtime`` is recorded by every peer and competes again in
the tie-break once clocks recover, so a host that cannot vouch for its clock
publishes "unknown" (0) instead of a value derived from it (ADR-0055).
"""

from __future__ import annotations

import os

from hsync_common.clock import offset_store

from hsync_client.sync_state import _corrected_mtime_of


def test_untrusted_clock_advertises_unknown(tmp_path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    offset_store.clear()
    assert _corrected_mtime_of(str(path)) == 0


def test_step_revoked_clock_advertises_unknown(tmp_path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    os.utime(path, (1000, 1000))
    offset_store.clear()
    offset_store.record(50.0, rtt=0.04)
    assert _corrected_mtime_of(str(path)) == 1000 + 50

    offset_store._trusted = False  # what observe() does on a clock step
    assert _corrected_mtime_of(str(path)) == 0


def test_trusted_clock_advertises_the_corrected_mtime(tmp_path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    os.utime(path, (1000, 1000))
    offset_store.clear()
    offset_store.record(50.0, rtt=0.04)
    assert _corrected_mtime_of(str(path)) == 1050


def test_missing_file_is_unknown_even_with_a_trusted_clock(tmp_path) -> None:
    offset_store.clear()
    offset_store.record(10.0)
    assert _corrected_mtime_of(str(tmp_path / "absent")) == 0
