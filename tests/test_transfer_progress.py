# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the throttled ``progress`` events emitted during transfers."""

from __future__ import annotations

import pytest
from hsync_common.models.file import File

from hsync_client import progress, transfer
from hsync_client.events import UiEventName
from hsync_client.p2p_inventory import NOOP_INVENTORY_OBSERVER
from hsync_client.p2p_sources import P2PPeerSource


class _RecordingObserver:
    """Minimal ``InventoryObserver`` capturing the delivery reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def report_received(self, rel_path: str, transferred: int, total: int) -> None:
        self.calls.append((rel_path, transferred, total))


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


class TestServeRegistry:
    """The master applies a peer echo only to the serve that peer requested.

    The registry is keyed by ``(rel, cid)`` so a seeder-served peer's frame, a
    concurrent serve of the same file by another peer, or a stale frame cannot
    drive the row; the per-key refcount keeps two concurrent serves of the same
    file from one finishing the other early.
    """

    @pytest.fixture(autouse=True)
    def _clean_registries(self):
        progress._serving.clear()
        progress._delivery_paths.clear()
        yield
        progress._serving.clear()
        progress._delivery_paths.clear()

    def test_an_idle_path_is_not_serving(self):
        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-a") is False

    def test_a_served_path_marks_both_forms(self):
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")

        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-a") is True
        assert progress.delivery_progress_active("/share/sub/f.bin") is True
        assert progress.delivery_progress_active("sub/f.bin") is True

    def test_another_peers_frame_does_not_mark(self):
        """A concurrent serve by another peer must not be driven by this CID."""
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")

        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-b") is False
        assert progress.delivery_progress_active("/share/sub/f.bin") is False

    def test_an_unserved_path_is_not_marked(self):
        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-a") is False
        assert progress.delivery_progress_active("sub/f.bin") is False

    def test_the_last_serve_clears_the_delivery_marks(self):
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")
        progress.mark_delivery_if_serving("sub/f.bin", "peer-a")

        progress.stop_serving("sub/f.bin", "peer-a")

        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-a") is False
        assert progress.delivery_progress_active("/share/sub/f.bin") is False
        assert progress.delivery_progress_active("sub/f.bin") is False

    def test_a_second_serve_of_the_same_key_keeps_the_marks(self):
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")
        progress.mark_delivery_if_serving("sub/f.bin", "peer-a")

        progress.stop_serving("sub/f.bin", "peer-a")

        # Still in flight: the delivery mark must survive the first serve's end.
        assert progress.mark_delivery_if_serving("sub/f.bin", "peer-a") is True
        assert progress.delivery_progress_active("/share/sub/f.bin") is True

        progress.stop_serving("sub/f.bin", "peer-a")

        assert progress.delivery_progress_active("/share/sub/f.bin") is False

    def test_a_second_peer_serving_the_same_file_keeps_the_marks(self):
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-b")
        progress.mark_delivery_if_serving("sub/f.bin", "peer-a")

        progress.stop_serving("sub/f.bin", "peer-a")

        # The other peer's serve still owns the shared, path-keyed mark.
        assert progress.delivery_progress_active("/share/sub/f.bin") is True

        progress.stop_serving("sub/f.bin", "peer-b")

        assert progress.delivery_progress_active("/share/sub/f.bin") is False

    def test_forget_is_deferred_while_a_serve_is_active(self):
        """The terminal event fires before ``stop_serving``: it must not re-arm
        the wire reporter of a still-active serve."""
        progress.note_serving("sub/f.bin", "/share/sub/f.bin", "peer-a")
        progress.mark_delivery_if_serving("sub/f.bin", "peer-a")

        progress.forget_delivery_progress("/share/sub/f.bin")

        assert progress.delivery_progress_active("/share/sub/f.bin") is True

        progress.stop_serving("sub/f.bin", "peer-a")
        assert progress.delivery_progress_active("/share/sub/f.bin") is False

    def test_stopping_an_unknown_serve_is_a_noop(self):
        progress.stop_serving("sub/f.bin", "peer-a")

        assert progress.delivery_progress_active("sub/f.bin") is False


class TestDownloadDeliveryEcho:
    """A peer-served download forwards the stored bytes to the serving master."""

    def _file(self, filepath: str, size: int) -> File:
        return File(
            filepath=filepath, sha256sum="x", size=size, mtime=0, ctime=0, permissions="644"
        )

    def test_the_echo_reports_the_stored_bytes(self, monkeypatch):
        monkeypatch.setattr(transfer, "CONFIG", {"LOCAL_BUCKET": "/share"})
        observer = _RecordingObserver()

        echo = transfer._download_delivery_echo(
            observer, self._file("sub/f.bin", 100), "/share/sub/f.bin", 100, None
        )

        assert echo is not None
        echo(40)
        assert observer.calls == [("sub/f.bin", 40, 100)]

    def test_a_seeder_served_download_has_no_echo(self, monkeypatch):
        """Only a canonical-master download reports to the master."""
        monkeypatch.setattr(transfer, "CONFIG", {"LOCAL_BUCKET": "/share"})
        source = P2PPeerSource(
            cid="peer-a",
            download_url="https://10.0.0.2:1234/api/v1/buckets/b/files/download",
            cert_fingerprint="fp",
            compression=False,
            serve_slots=1,
        )

        echo = transfer._download_delivery_echo(
            _RecordingObserver(), self._file("sub/f.bin", 100), "/share/sub/f.bin", 100, source
        )

        assert echo is None

    def test_a_sentinel_has_no_echo(self, monkeypatch):
        monkeypatch.setattr(transfer, "CONFIG", {"LOCAL_BUCKET": "/share"})

        echo = transfer._download_delivery_echo(
            _RecordingObserver(),
            self._file("docs/.hsynckeep", 0),
            "/share/docs/.hsynckeep",
            0,
            None,
        )

        assert echo is None

    def test_the_noop_observer_drops_the_sample(self, monkeypatch):
        """A transport with no master session reports nothing, without raising."""
        monkeypatch.setattr(transfer, "CONFIG", {"LOCAL_BUCKET": "/share"})

        echo = transfer._download_delivery_echo(
            NOOP_INVENTORY_OBSERVER, self._file("sub/f.bin", 100), "/share/sub/f.bin", 100, None
        )

        assert echo is not None
        echo(40)  # must not raise


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


class TestSentinelTransfers:
    """The engine never transfers the internal ``.hsynckeep`` sentinel.

    Each peer maintains its own sentinels; the directory is propagated by the
    directory event, so no download/upload terminal event ever names a sentinel.
    The emitters are the single seam that enforces it.
    """

    def test_progress_reporter_is_none_for_a_sentinel(self):
        assert progress.progress_reporter("docs/empty/.hsynckeep", 0, "upload") is None

    def test_completed_transfer_emits_nothing_for_a_sentinel(self, monkeypatch):
        events: list[tuple] = []
        monkeypatch.setattr(progress, "emit_event", lambda name, **kw: events.append((name, kw)))

        progress.emit_completed_transfer("upload", "docs/empty/.hsynckeep", 0)

        assert events == []

    def test_failed_transfer_emits_nothing_for_a_sentinel(self, monkeypatch):
        events: list[tuple] = []
        monkeypatch.setattr(progress, "emit_event", lambda name, **kw: events.append((name, kw)))

        progress.emit_failed_transfer("upload", "docs/empty/.hsynckeep", "boom")

        assert events == []
