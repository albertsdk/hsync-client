# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
from hsync_client.events import EventEmitter, emit_event, get_emitter, init_emitter


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
