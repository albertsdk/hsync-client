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
        monkeypatch.setattr(progress, "emit_progress", lambda name, **kw: calls.append((name, kw)))
        monkeypatch.setattr(progress, "monotonic", lambda: clock[0])
        return calls, clock

    def test_coalesces_updates_within_the_interval(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 100, "download")

        clock[0] = 10.0
        rep.update(10)  # first update after the initial window → emitted
        clock[0] = 10.2
        rep.update(20)  # inside the interval and not finished → coalesced
        clock[0] = 10.3
        rep.update(100)  # final update → always emitted

        assert [kw["transferred"] for _name, kw in calls] == [10, 100]
        assert all(name == UiEventName.progress for name, _kw in calls)
        assert all(kw["total"] == 100 for _name, kw in calls)
        assert all(kw["op"] == "download" for _name, kw in calls)

    def test_emits_again_after_the_interval(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 1000, "upload")

        clock[0] = 1.0
        rep.update(100)
        clock[0] = 2.0
        rep.update(200)  # interval elapsed → emitted even though not finished

        assert [kw["transferred"] for _name, kw in calls] == [100, 200]
        assert all(kw["op"] == "upload" for _name, kw in calls)

    def test_unknown_total_is_still_throttled(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 0, "download")

        clock[0] = 5.0
        rep.update(10)
        clock[0] = 5.2
        rep.update(20)  # within the interval, total unknown → coalesced
        clock[0] = 5.6
        rep.update(30)  # interval elapsed → emitted

        assert [kw["transferred"] for _name, kw in calls] == [10, 30]


class TestDeliveryProgress:
    """The receiver owns the truth: its samples supersede the sender's counter.

    A sender counts the bytes it handed to its transport, the receiver the bytes
    it stored — two different totals, so once the master reports on a path
    (``file_progress``) the local counter must stop feeding that row.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        progress._delivery_paths.clear()
        yield
        progress._delivery_paths.clear()

    @pytest.fixture
    def captured(self, monkeypatch):
        calls: list[tuple[str, dict]] = []
        clock = [0.0]
        monkeypatch.setattr(progress, "emit_progress", lambda name, **kw: calls.append((name, kw)))
        monkeypatch.setattr(progress, "monotonic", lambda: clock[0])
        return calls, clock

    def test_the_reporter_echoes_the_sample_to_the_receiver(self, captured):
        calls, clock = captured
        echoed: list[int] = []
        rep = progress.ProgressReporter("/bucket/f.bin", 100, "download", echo=echoed.append)

        clock[0] = 10.0
        rep.update(40)

        assert echoed == [40]
        # The master's own view (it is storing the file) keeps reporting.
        assert [kw["transferred"] for _n, kw in calls] == [40]

    def test_a_reported_upload_stops_the_local_counter(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 100, "upload")

        clock[0] = 10.0
        rep.update(40)
        progress.note_delivery_progress("/bucket/f.bin")
        clock[0] = 10.5
        rep.update(100)  # the receiver reports this path now

        assert [kw["transferred"] for _n, kw in calls] == [40]

    def test_a_reported_download_keeps_reporting(self, captured):
        calls, clock = captured
        rep = progress.ProgressReporter("/bucket/f.bin", 100, "download")

        clock[0] = 10.0
        rep.update(40)
        progress.note_delivery_progress("/bucket/f.bin")
        clock[0] = 10.5
        rep.update(100)

        assert [kw["transferred"] for _n, kw in calls] == [40, 100]

    def test_a_wire_separated_mark_matches_a_native_path(self, captured):
        """The receiver addresses a file with "/" separators, Windows does not."""
        calls, clock = captured
        rep = progress.ProgressReporter("C:\\share\\sub\\f.bin", 100, "upload")
        progress.note_delivery_progress("C:/share/sub/f.bin")

        clock[0] = 10.0
        rep.update(40)

        assert calls == []

    def test_a_delivery_only_reporter_stays_silent(self, captured):
        """The receiver reports this transfer, so the local scale is not shown."""
        calls, clock = captured
        echoed: list[int] = []
        rep = progress.ProgressReporter(
            "/bucket/f.bin", 100, "upload", echo=echoed.append, delivery_only=True
        )

        clock[0] = 10.0
        rep.update(40)

        assert calls == []
        assert echoed == [40]

    def test_the_terminal_event_forgets_the_delivery_mark(self, captured):
        calls, clock = captured
        progress.note_delivery_progress("/bucket/f.bin")

        progress.emit_completed_transfer("upload", "/bucket/f.bin", 100)

        assert progress.delivery_progress_active("/bucket/f.bin") is False

    def test_a_failed_transfer_forgets_the_delivery_mark(self, captured):
        progress.note_delivery_progress("/bucket/f.bin")

        progress.emit_failed_transfer("upload", "/bucket/f.bin", "boom")

        assert progress.delivery_progress_active("/bucket/f.bin") is False


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
