# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression: a plain ``touch`` must dispatch a metadata-only change.

On Linux a ``utime`` fires only ``IN_ATTRIB`` (no close), so the base handler's
``_process_permission_only`` path must detect an mtime-only change with unchanged
content and call ``_on_metadata_changed``. Without it the **master** (and the
server-mode client) never propagated a touch, while the slave did — the
asymmetry where only the client notified the master.
"""

from __future__ import annotations

import os

from hsync_common.models.file import File

from hsync_client.file_event_handler import BaseFileEventHandler


class _CapturingHandler(BaseFileEventHandler):
    """Base handler recording which transport hook fired."""

    def __init__(self) -> None:
        super().__init__(watcher=None)
        self.metadata: list[str] = []
        self.permissions: list[str] = []

    def _on_metadata_changed(self, file, src_path: str) -> None:  # type: ignore[override]
        self.metadata.append(src_path)

    def _on_permissions_changed(self, file, src_path: str) -> None:  # type: ignore[override]
        self.permissions.append(src_path)


def _record_content(handler: _CapturingHandler, path) -> None:
    """Simulate the prior successful content broadcast for *path*."""
    stat = path.stat()
    sha = File.get(str(path)).sha256sum
    handler._record_broadcast(str(path), handler._stat_key(stat), sha)


class TestTouchDispatch:
    def test_touch_with_identical_content_dispatches_metadata(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"hello")
        handler = _CapturingHandler()
        _record_content(handler, path)

        # First observation: only records the baselines.
        handler._process_permission_only(str(path))
        assert handler.metadata == []
        assert handler.permissions == []

        # Plain touch: same bytes, newer mtime.
        newer = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(newer, newer))
        handler._process_permission_only(str(path))

        assert handler.metadata == [str(path)]
        assert handler.permissions == []

    def test_write_is_not_mistaken_for_a_touch(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"hello")
        handler = _CapturingHandler()
        _record_content(handler, path)
        handler._process_permission_only(str(path))

        # A write whose IN_MODIFY arrives before the close: content differs from
        # the last propagated hash, so it must not be broadcast as a touch
        # (on_closed handles the real change).
        path.write_bytes(b"HELLO, WORLD")
        newer = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(newer, newer))
        handler._process_permission_only(str(path))

        assert handler.metadata == []

    def test_first_observation_never_dispatches(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"hello")
        handler = _CapturingHandler()

        handler._process_permission_only(str(path))

        assert handler.metadata == []
        assert handler.permissions == []
