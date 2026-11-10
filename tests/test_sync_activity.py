# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The sync phase reports itself on the ``activity`` axis.

``status`` carries the link only. A cycle that runs publishes
``activity=syncing`` and returns to ``idle`` when the last cycle ends, so a
front-end reads the phase off its own axis and never loses a link transition to
it — a cycle running on a live link leaves the link ``connected``.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from hsync_common.protocol.events import parse_event_line

from hsync_client.events import get_emitter, init_emitter, set_link_status
from hsync_client.sync import log_initial_sync_start, sync


def _capture() -> int:
    read_fd, write_fd = os.pipe()
    init_emitter(write_fd)
    return read_fd


def _published(read_fd: int) -> list[tuple[str, str]]:
    """Return the ``(event, value)`` pairs published on *read_fd*, in order."""
    emitter = get_emitter()
    assert emitter is not None
    emitter.close()  # flush
    chunks = []
    while True:
        data = os.read(read_fd, 4096)
        if not data:
            break
        chunks.append(data)
    os.close(read_fd)
    out: list[tuple[str, str]] = []
    for line in b"".join(chunks).decode("utf-8").splitlines():
        event = parse_event_line(line)
        if event is None:
            continue
        name = str(event["event"])
        if name == "activity":
            out.append((name, str(event.get("activity", ""))))
        elif name == "status":
            out.append((name, str(event.get("status", ""))))
    return out


def test_the_initial_sync_marker_is_a_log_line_only():
    """The phase marker never publishes: the phase is the ``activity`` axis."""
    read_fd = _capture()

    log_initial_sync_start()

    assert _published(read_fd) == []


def test_a_cycle_on_a_live_link_keeps_the_link_and_brackets_the_phase(tmp_path):
    """A cycle brackets itself and leaves the connected link as the last status.

    Regression: the phase used to travel on ``status``, so this exact sequence
    ended with the front-end reading ``syncing`` and no ``connected`` to come
    back to (every following transition was dropped as a repeat).
    """
    cfg = {
        "LOCAL_BUCKET": str(tmp_path),
        "CID": "clientcid000000000000",
        "SYNC_DIRECTION": "bidirectional",
    }
    read_fd = _capture()
    set_link_status("connected")

    async def _run():
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.sync.get_files_remote", return_value=[]),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.push_tombstones_remote"),
            patch("hsync_client.sync.get_dirs_remote", return_value=[]),
        ):
            await sync()

    asyncio.run(_run())

    assert _published(read_fd) == [
        ("status", "connected"),
        ("activity", "syncing"),
        ("activity", "idle"),
    ]
