# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests: the Remote receive loop never waits for a transfer.

A single large download must not delay an event that arrives after it, and
distinct inbound downloads must not be serialized by the receive loop.
"""

import contextlib
import time
from json import dumps
from unittest.mock import AsyncMock, patch

from hsync_client import event_dispatch as dispatch
from hsync_client.remote_websocket import _process_remote_events


class _StopLoop(Exception):
    """Ends the receive loop once the scripted frames are consumed."""


def _added(filepath: str) -> dict:
    return {
        "name": "added",
        "cid": "other_client",
        "bucket": "bucket",
        "src": {"filepath": filepath, "sha256sum": "a" * 64, "size": 10, "mtime": 1},
    }


def _websocket(*frames: dict) -> AsyncMock:
    websocket = AsyncMock()
    websocket.recv = AsyncMock(side_effect=[*(dumps(frame) for frame in frames), _StopLoop("stop")])
    return websocket


async def test_delete_is_applied_while_a_download_is_in_flight(tmp_path):
    """A delete issued right after a large `added` must not queue behind it."""
    victim = tmp_path / "other.txt"
    victim.write_text("bye")

    order: list[str] = []
    real_remove = dispatch._remove_locked

    def slow_download(_src: dict, **_kwargs) -> None:
        time.sleep(0.3)
        order.append("download")

    def recording_remove(path: str) -> bool:
        removed = real_remove(path)
        order.append("delete")
        return removed

    config = {"CID": "my_client", "LOCAL_BUCKET": str(tmp_path)}
    websocket = _websocket(
        _added("big.bin"),
        {
            "name": "deleted",
            "cid": "other_client",
            "bucket": "bucket",
            "src": {"filepath": "other.txt", "sha256sum": "b" * 64},
        },
    )

    with (
        patch("hsync_client.remote_websocket.CONFIG", config),
        patch("hsync_client.event_dispatch.CONFIG", config),
        patch("hsync_client.event_dispatch.update_file", side_effect=slow_download),
        patch("hsync_client.event_dispatch._remove_locked", side_effect=recording_remove),
    ):
        with contextlib.suppress(_StopLoop):
            await _process_remote_events(websocket, "bucket")

    assert order == ["delete", "download"], f"the delete waited for the download: {order}"
    assert not victim.exists()


async def test_distinct_inbound_downloads_overlap(tmp_path):
    """Two downloads of distinct paths run at the same time."""
    active = {"now": 0, "peak": 0}

    def slow_download(_src: dict, **_kwargs) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.2)
        active["now"] -= 1

    config = {"CID": "my_client", "LOCAL_BUCKET": str(tmp_path)}

    with (
        patch("hsync_client.remote_websocket.CONFIG", config),
        patch("hsync_client.event_dispatch.CONFIG", config),
        patch("hsync_client.event_dispatch.update_file", side_effect=slow_download),
    ):
        with contextlib.suppress(_StopLoop):
            await _process_remote_events(_websocket(_added("a.bin"), _added("b.bin")), "bucket")

    assert active["peak"] > 1, "inbound downloads were serialized"
