# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Tests for the structured UI-event emitter (hsync_client.events).

These verify the JSON-Lines channel contract: events serialize to one valid,
schema-versioned line per emit, the emitter is a no-op when no fd is attached,
write failures never propagate, and the module-level singleton routes through
the installed emitter.
"""

import os

from hsync_common.protocol.events import (
    EVENTS_SCHEMA_VERSION,
    UiEventName,
    parse_event_line,
)

from hsync_client import events
from hsync_client.events import (
    EventEmitter,
    emit_event,
    emit_progress,
    emit_warning,
    get_emitter,
    get_link_status,
    init_emitter,
    set_link_status,
)


def _read_lines(read_fd: int) -> list[str]:
    """Read everything currently buffered on *read_fd* and split into lines."""
    chunks: list[bytes] = []
    while True:
        data = os.read(read_fd, 4096)
        if not data:
            break
        chunks.append(data)
    text = b"".join(chunks).decode("utf-8")
    return [line for line in text.splitlines() if line]


class TestEventEmitter:
    """Tests for the fd-bound EventEmitter."""

    def test_emit_writes_one_valid_jsonl_line(self):
        read_fd, write_fd = os.pipe()
        emitter = EventEmitter(write_fd)
        assert emitter.active

        emitter.emit(UiEventName.transfer, op="download", file="/tmp/a", size=12)
        emitter.close()  # flush + close write end so the reader sees EOF

        lines = _read_lines(read_fd)
        os.close(read_fd)

        assert len(lines) == 1
        parsed = parse_event_line(lines[0])
        assert parsed is not None
        assert parsed["schema_version"] == EVENTS_SCHEMA_VERSION
        assert parsed["event"] == "transfer"
        assert parsed["op"] == "download"
        assert parsed["file"] == "/tmp/a"
        assert parsed["size"] == 12
        assert "ts" in parsed

    def test_emit_drops_none_payload_values(self):
        read_fd, write_fd = os.pipe()
        emitter = EventEmitter(write_fd)
        emitter.emit(UiEventName.server, url="http://x", message=None)
        emitter.close()

        lines = _read_lines(read_fd)
        os.close(read_fd)

        parsed = parse_event_line(lines[0])
        assert parsed is not None
        assert parsed["url"] == "http://x"
        assert "message" not in parsed

    def test_emit_multiple_events_one_line_each(self):
        read_fd, write_fd = os.pipe()
        emitter = EventEmitter(write_fd)
        emitter.emit(UiEventName.preview_start)
        emitter.emit(UiEventName.file_status, state="synced", file="/f")
        emitter.emit(UiEventName.preview_end)
        emitter.close()

        lines = _read_lines(read_fd)
        os.close(read_fd)

        assert len(lines) == 3
        assert [parse_event_line(line)["event"] for line in lines] == [
            "preview_start",
            "file_status",
            "preview_end",
        ]

    def test_no_fd_is_inactive_noop(self):
        emitter = EventEmitter(None)
        assert not emitter.active
        # Must not raise even though there is nowhere to write.
        emitter.emit(UiEventName.error, message="boom")

    def test_emit_after_close_is_silent(self):
        read_fd, write_fd = os.pipe()
        emitter = EventEmitter(write_fd)
        emitter.close()
        os.close(read_fd)
        # Writing to a closed emitter must be swallowed, never raise.
        emitter.emit(UiEventName.status, status="connected")
        assert not emitter.active

    def test_broken_pipe_is_swallowed(self):
        read_fd, write_fd = os.pipe()
        emitter = EventEmitter(write_fd)
        os.close(read_fd)  # reader gone -> next flush hits EPIPE
        emitter.emit(UiEventName.status, status="waiting")
        emitter.close()


class TestModuleSingleton:
    """Tests for init_emitter / get_emitter / emit_event wiring."""

    def teardown_method(self):
        # Restore the no-channel default so tests stay isolated.
        init_emitter(None)

    def test_emit_event_routes_through_installed_emitter(self):
        read_fd, write_fd = os.pipe()
        init_emitter(write_fd)
        emit_event(UiEventName.bucket, bucket="docs")
        get_emitter().close()

        lines = _read_lines(read_fd)
        os.close(read_fd)

        parsed = parse_event_line(lines[0])
        assert parsed is not None
        assert parsed["event"] == "bucket"
        assert parsed["bucket"] == "docs"

    def test_emit_event_noop_when_no_channel(self):
        init_emitter(None)
        assert get_emitter() is not None
        assert not get_emitter().active
        emit_event(UiEventName.error, message="ignored")

    def test_init_emitter_replaces_prior(self):
        first = init_emitter(None)
        second = init_emitter(None)
        assert first is not second
        assert events.get_emitter() is second


class TestWarningChannel:
    """``emit_warning`` publishes a structured, deduplicated warning."""

    def teardown_method(self):
        init_emitter(None)
        events.reset_warning_dedup()

    def _capture(self) -> int:
        read_fd, write_fd = os.pipe()
        init_emitter(write_fd)
        return read_fd

    def _warnings(self, read_fd: int) -> list[dict]:
        get_emitter().close()
        parsed = [parse_event_line(line) for line in _read_lines(read_fd)]
        os.close(read_fd)
        return [event for event in parsed if event and event["event"] == "warning"]

    def test_a_warning_carries_its_source(self):
        read_fd = self._capture()

        emit_warning("Rendezvous node:443 unreachable", source="rendezvous", reason="unreachable")

        events_out = self._warnings(read_fd)
        assert len(events_out) == 1
        assert events_out[0]["message"] == "Rendezvous node:443 unreachable"
        assert events_out[0]["source"] == "rendezvous"
        assert events_out[0]["reason"] == "unreachable"

    def test_the_same_warning_is_reported_once_per_window(self):
        read_fd = self._capture()

        emit_warning("same", source="rendezvous")
        emit_warning("same", source="rendezvous")

        assert len(self._warnings(read_fd)) == 1

    def test_a_different_source_or_message_is_not_deduplicated(self):
        read_fd = self._capture()

        emit_warning("same", source="rendezvous")
        emit_warning("same", source="watcher")
        emit_warning("other", source="rendezvous")

        assert len(self._warnings(read_fd)) == 3

    def test_a_new_run_can_repeat_a_warning(self):
        read_fd = self._capture()
        emit_warning("same", source="watcher")
        assert len(self._warnings(read_fd)) == 1

        events.reset_warning_dedup()
        read_fd = self._capture()
        emit_warning("same", source="watcher")
        assert len(self._warnings(read_fd)) == 1


class TestProgressChannel:
    """``progress`` samples never block the emitter, even on a full pipe.

    They are samples, not state transitions: the consumer needs the terminal
    ``transfer`` event for the outcome, so dropping samples while a slow reader
    (the Desktop's UI thread) catches up is safe — and is the only way a
    serving master can avoid stalling its own data plane on the events pipe.
    """

    def teardown_method(self):
        init_emitter(None)

    @staticmethod
    def _capture() -> tuple[int, int]:
        read_fd, write_fd = os.pipe()
        init_emitter(write_fd)
        return read_fd, write_fd

    def test_progress_reaches_the_channel(self):
        read_fd, _write_fd = self._capture()

        emit_progress(UiEventName.progress, file="/b/f.bin", transferred=10, total=10, op="upload")
        get_emitter().close()

        parsed = [parse_event_line(line) for line in _read_lines(read_fd)]
        os.close(read_fd)

        assert parsed and parsed[0] is not None
        assert parsed[0]["event"] == "progress"
        assert parsed[0]["transferred"] == 10
        assert parsed[0]["op"] == "upload"

    def test_progress_does_not_block_on_a_full_pipe(self):
        import time

        read_fd, write_fd = self._capture()
        # Fill the pipe through a private non-blocking handle: the reader is not
        # draining, exactly like a stalled GUI event loop.
        fill_fd = os.dup(write_fd)
        os.set_blocking(fill_fd, False)
        try:
            while True:
                os.write(fill_fd, b"x" * 4096)
        except BlockingIOError:
            pass
        finally:
            os.close(fill_fd)

        started = time.monotonic()
        emit_progress(UiEventName.progress, file="/b/f.bin", transferred=1, total=2, op="upload")
        elapsed = time.monotonic() - started

        assert elapsed < 0.1, f"progress blocked for {elapsed:.3f}s on a full pipe"
        os.close(read_fd)

    def test_an_oversized_progress_line_is_dropped(self):
        """A line above PIPE_BUF would be torn: it is dropped, never split."""
        read_fd, _write_fd = self._capture()

        emit_progress(
            UiEventName.progress,
            file="/b/" + "x" * 5000,
            transferred=1,
            total=2,
            op="upload",
        )
        get_emitter().close()

        assert _read_lines(read_fd) == []
        os.close(read_fd)

    def test_structural_events_keep_the_lossless_path(self):
        read_fd, _write_fd = self._capture()

        emit_progress(UiEventName.progress, file="/b/f.bin", transferred=1, total=2, op="upload")
        emit_event(UiEventName.transfer, op="upload", file="/b/f.bin", size=2)
        get_emitter().close()

        parsed = [parse_event_line(line) for line in _read_lines(read_fd)]
        os.close(read_fd)

        assert [entry["event"] for entry in parsed if entry is not None] == [
            "progress",
            "transfer",
        ]

    def test_progress_is_a_noop_without_a_channel(self):
        init_emitter(None)

        emit_progress(UiEventName.progress, file="/b/f.bin", transferred=1, total=2, op="upload")


class TestLinkStatus:
    """``set_link_status`` publishes transitions, not repeated states."""

    def teardown_method(self):
        init_emitter(None)

    @staticmethod
    def _capture() -> int:
        read_fd, write_fd = os.pipe()
        init_emitter(write_fd)
        return read_fd

    @staticmethod
    def _statuses(read_fd: int) -> list[str]:
        get_emitter().close()  # flush
        lines = _read_lines(read_fd)
        os.close(read_fd)
        published = []
        for line in lines:
            parsed = parse_event_line(line)
            if parsed is not None and parsed["event"] == "status":
                published.append(parsed["status"])
        return published

    def test_publishes_every_transition(self):
        read_fd = self._capture()

        set_link_status("connected")
        set_link_status("waiting")
        set_link_status("connected")

        assert self._statuses(read_fd) == ["connected", "waiting", "connected"]
        assert get_link_status() == "connected"

    def test_drops_a_repeated_status(self):
        read_fd = self._capture()

        set_link_status("connected")
        set_link_status("connected")

        assert self._statuses(read_fd) == ["connected"]

    def test_a_new_channel_resets_the_published_status(self):
        read_fd = self._capture()
        set_link_status("connected")
        assert self._statuses(read_fd) == ["connected"]

        read_fd = self._capture()  # init_emitter installs a fresh channel
        assert get_link_status() is None
        set_link_status("connected")

        assert self._statuses(read_fd) == ["connected"]
