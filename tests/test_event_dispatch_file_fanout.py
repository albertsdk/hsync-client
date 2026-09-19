# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the bounded per-path file-download fan-out in ``event_dispatch``.

The LAN slave's WSS receive loop used to ``await`` every inbound file event's
download inline, serializing the whole burst at ~60 files/s. With a
``file_download_semaphore`` on the context, distinct paths download
concurrently while events for the SAME path stay strictly ordered; without
one (remote-mode ctx) the legacy inline behavior is preserved.
"""

import asyncio
import time
from unittest.mock import patch

from hsync_client.event_dispatch import (
    EventDispatchContext,
    apply_inbound_event,
    drain_file_downloads,
)


def _file_event(filepath: str) -> dict:
    return {
        "name": "added",
        "cid": "BBBBBBBBBBBBBBBBBBBBB",
        "src": {"filepath": filepath, "sha256sum": "a" * 64, "size": 1, "mtime": 1},
    }


class TestFileDownloadFanOut:
    @staticmethod
    def _ctx(slots: int | None) -> EventDispatchContext:
        return EventDispatchContext(
            file_download_semaphore=asyncio.Semaphore(slots) if slots else None,
        )

    async def test_distinct_paths_download_concurrently(self, tmp_path):
        """Four slow downloads on distinct paths must overlap, not serialize."""
        config = {"LOCAL_BUCKET": str(tmp_path), "CID": "AAAAAAAAAAAAAAAAAAAAA"}
        ctx = self._ctx(slots=4)
        active = {"now": 0, "max": 0}

        def slow_update(_src, **_kwargs):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            time.sleep(0.2)
            active["now"] -= 1

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file", side_effect=slow_update),
        ):
            t0 = time.perf_counter()
            for index in range(4):
                await apply_inbound_event(_file_event(f"f{index}.txt"), ctx)
            await drain_file_downloads(ctx)
            elapsed = time.perf_counter() - t0

        assert active["max"] > 1, "distinct-path downloads did not overlap"
        # 4 × 0.2 s serial would be ≥ 0.8 s; overlapped must be well below.
        assert elapsed < 0.7, f"fan-out did not parallelize: {elapsed:.2f}s"

    async def test_same_path_events_apply_in_order(self, tmp_path):
        """Two events for one path must never interleave (create→modify order)."""
        config = {"LOCAL_BUCKET": str(tmp_path), "CID": "AAAAAAAAAAAAAAAAAAAAA"}
        ctx = self._ctx(slots=2)
        calls: list[tuple[int, int]] = []
        active = {"now": 0}

        def recording_update(_src, **_kwargs):
            active["now"] += 1
            calls.append((active["now"], len(calls)))
            time.sleep(0.1)
            active["now"] -= 1

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file", side_effect=recording_update),
        ):
            await apply_inbound_event(_file_event("same.txt"), ctx)
            await apply_inbound_event(_file_event("same.txt"), ctx)
            await drain_file_downloads(ctx)

        assert len(calls) == 2
        assert all(concurrency == 1 for concurrency, _order in calls), (
            f"same-path downloads interleaved: {calls}"
        )

    async def test_no_semaphore_keeps_inline_behavior(self, tmp_path):
        """A ctx without a semaphore (remote mode) still awaits inline."""
        config = {"LOCAL_BUCKET": str(tmp_path), "CID": "AAAAAAAAAAAAAAAAAAAAA"}
        ctx = self._ctx(slots=None)
        finished = {"done": False}

        def quick_update(_src, **_kwargs):
            finished["done"] = True

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file", side_effect=quick_update),
        ):
            await apply_inbound_event(_file_event("inline.txt"), ctx)

        assert finished["done"] is True, "inline path must complete before returning"
        assert not ctx.file_download_tasks, "inline path must not schedule tasks"

    async def test_mark_unmark_brackets_the_write(self, tmp_path):
        """Echo suppression must cover the whole download, even when chained."""
        config = {"LOCAL_BUCKET": str(tmp_path), "CID": "AAAAAAAAAAAAAAAAAAAAA"}
        marked: list[str] = []
        unmarked: list[str] = []

        ctx = EventDispatchContext(
            file_download_semaphore=asyncio.Semaphore(2),
            mark_receiving=lambda path: marked.append(path.rsplit("/", 1)[-1]),
            unmark_receiving=lambda path: unmarked.append(path.rsplit("/", 1)[-1]),
        )

        def marking_update(_src, **_kwargs):
            assert marked.count("x.txt") > unmarked.count("x.txt"), (
                "the write ran without its receiving mark"
            )

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file", side_effect=marking_update),
        ):
            await apply_inbound_event(_file_event("x.txt"), ctx)
            await apply_inbound_event(_file_event("x.txt"), ctx)
            await drain_file_downloads(ctx)

        assert marked == ["x.txt", "x.txt"] and unmarked == ["x.txt", "x.txt"]
