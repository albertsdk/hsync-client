# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the throttled ``progress`` events emitted during transfers."""

from __future__ import annotations

import pytest

from hsync_client import progress, transfer
from hsync_client.events import UiEventName


class TestProgressReporter:
    """``ProgressReporter`` coalesces updates and always flushes the final one."""

    @pytest.fixture
    def captured(self, monkeypatch):
        calls: list[tuple[str, dict]] = []
        clock = [0.0]
        monkeypatch.setattr(progress, "emit_event", lambda name, **kw: calls.append((name, kw)))
        monkeypatch.setattr(progress, "monotonic", lambda: clock[0])
        return calls, clock

    def test_coalesces_updates_within_the_interval(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 100)

        clock[0] = 10.0
        rep.update(10)  # first update after the initial window → emitted
        clock[0] = 10.2
        rep.update(20)  # inside the interval and not finished → coalesced
        clock[0] = 10.3
        rep.update(100)  # final update → always emitted

        assert [kw["transferred"] for _name, kw in calls] == [10, 100]
        assert all(name == UiEventName.progress for name, _kw in calls)
        assert all(kw["total"] == 100 for _name, kw in calls)

    def test_emits_again_after_the_interval(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 1000)

        clock[0] = 1.0
        rep.update(100)
        clock[0] = 2.0
        rep.update(200)  # interval elapsed → emitted even though not finished

        assert [kw["transferred"] for _name, kw in calls] == [100, 200]

    def test_unknown_total_is_still_throttled(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 0)

        clock[0] = 5.0
        rep.update(10)
        clock[0] = 5.2
        rep.update(20)  # within the interval, total unknown → coalesced
        clock[0] = 5.6
        rep.update(30)  # interval elapsed → emitted

        assert [kw["transferred"] for _name, kw in calls] == [10, 30]


class TestStreamingBodyProgress:
    """The multipart upload body reports cumulative sent bytes."""

    def test_reports_cumulative_bytes(self, tmp_path):
        payload = tmp_path / "f.bin"
        size = transfer._UPLOAD_CHUNK_SIZE * 2 + 5
        payload.write_bytes(b"x" * size)

        seen: list[int] = []
        body, _content_type = transfer._build_multipart_streaming(
            str(payload), {}, boundary="BOUNDARY", on_progress=seen.append
        )
        b"".join(body)  # drain the streaming body

        assert seen[-1] == size
        assert seen == sorted(seen)

    def test_no_callback_is_optional(self, tmp_path):
        payload = tmp_path / "f.bin"
        payload.write_bytes(b"x" * 10)

        body, _content_type = transfer._build_multipart_streaming(
            str(payload), {}, boundary="BOUNDARY"
        )

        assert b"".join(body)  # drains without a progress callback
