# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the bounded per-path scheduling of inbound mutation events.

The queue keeps a WebSocket receive loop responsive: an event is scheduled and
the loop returns to ``recv`` immediately, distinct paths apply concurrently, and
mutations on the same path stay strictly ordered.
"""

import asyncio
import time
from unittest.mock import patch

from hsync_client.event_dispatch import EventDispatchContext, event_path_keys
from hsync_client.inbound_queue import InboundEventQueue

_APPLY = "hsync_client.inbound_queue.apply_inbound_event"


def _file_event(filepath: str, name: str = "added") -> dict:
    return {
        "name": name,
        "cid": "BBBBBBBBBBBBBBBBBBBBB",
        "src": {"filepath": filepath, "sha256sum": "a" * 64, "size": 1, "mtime": 1},
    }


def _rename_event(src: str, dst: str) -> dict:
    return {
        "name": "renamed",
        "cid": "BBBBBBBBBBBBBBBBBBBBB",
        "src": {"filepath": src},
        "dst": {"filepath": dst},
    }


def test_event_path_keys_cover_every_mutated_path():
    """Every path an event mutates is a key; a rename contributes two."""
    assert event_path_keys(_file_event("a.txt")) == ("a.txt",)
    assert event_path_keys(_rename_event("a.txt", "b.txt")) == ("a.txt", "b.txt")
    assert event_path_keys({"name": "added", "src": {}}) == ()


async def test_distinct_paths_apply_concurrently():
    """Four slow mutations on distinct paths must overlap, not serialize."""
    active = {"now": 0, "peak": 0}

    async def slow(_event: dict, _ctx: EventDispatchContext) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.2)
        active["now"] -= 1

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=4)
    with patch(_APPLY, side_effect=slow):
        started = time.perf_counter()
        for index in range(4):
            await queue.submit(_file_event(f"f{index}.txt"))
        await queue.drain()
        elapsed = time.perf_counter() - started

    assert active["peak"] > 1, "distinct-path mutations did not overlap"
    # 4 × 0.2 s serial would be ≥ 0.8 s; overlapped must be well below.
    assert elapsed < 0.7, f"mutations did not parallelize: {elapsed:.2f}s"


async def test_same_path_mutations_stay_ordered():
    """Two events for one path never interleave and keep the stream order."""
    order: list[str] = []
    active = {"now": 0, "peak": 0}

    async def recording(event: dict, _ctx: EventDispatchContext) -> None:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.1)
        order.append(event["name"])
        active["now"] -= 1

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=4)
    with patch(_APPLY, side_effect=recording):
        await queue.submit(_file_event("same.txt"))
        await queue.submit(_file_event("same.txt", name="modified"))
        await queue.drain()

    assert order == ["added", "modified"]
    assert active["peak"] == 1, "two mutations of the same path overlapped"


async def test_rename_waits_for_the_download_of_its_source():
    """A rename is chained after the pending mutation of its source path.

    Applying it out of order would find the source missing and re-download the
    destination, after which the pending download re-creates the source.
    """
    order: list[str] = []

    async def apply(event: dict, _ctx: EventDispatchContext) -> None:
        if event["name"] == "renamed":
            order.append("rename")
            return
        await asyncio.sleep(0.2)
        order.append("download")

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=4)
    with patch(_APPLY, side_effect=apply):
        await queue.submit(_file_event("big.bin"))
        await queue.submit(_rename_event("big.bin", "big2.bin"))
        await queue.drain()

    assert order == ["download", "rename"]


async def test_drain_awaits_every_outstanding_mutation():
    """Teardown waits for the queued mutations and leaves nothing behind."""
    applied: list[str] = []

    async def apply(event: dict, _ctx: EventDispatchContext) -> None:
        await asyncio.sleep(0.05)
        applied.append(event["src"]["filepath"])

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=2)
    with patch(_APPLY, side_effect=apply):
        for index in range(4):
            await queue.submit(_file_event(f"f{index}.txt"))
        assert queue.outstanding == 4
        await queue.drain()

    assert sorted(applied) == [f"f{index}.txt" for index in range(4)]
    assert queue.outstanding == 0


async def test_submit_waits_when_the_queue_is_full():
    """A saturated queue pushes back instead of buffering without bound."""
    release = asyncio.Event()

    async def blocked(_event: dict, _ctx: EventDispatchContext) -> None:
        await release.wait()

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=1, max_pending=2)
    with patch(_APPLY, side_effect=blocked):
        await queue.submit(_file_event("a.txt"))
        await queue.submit(_file_event("b.txt"))
        third = asyncio.create_task(queue.submit(_file_event("c.txt")))
        await asyncio.sleep(0.05)
        assert not third.done(), "submit did not apply backpressure when full"
        release.set()
        await third
        await queue.drain()


async def test_a_failing_event_does_not_stop_the_stream():
    """An event that raises is isolated; the rest of the stream still applies."""
    applied: list[str] = []

    async def apply(event: dict, _ctx: EventDispatchContext) -> None:
        if event["src"]["filepath"] == "boom.txt":
            raise RuntimeError("boom")
        applied.append(event["src"]["filepath"])

    queue = InboundEventQueue(EventDispatchContext(), max_concurrent=1)
    with patch(_APPLY, side_effect=apply):
        await queue.submit(_file_event("boom.txt"))
        await queue.submit(_file_event("ok.txt"))
        await queue.drain()

    assert applied == ["ok.txt"]
    assert queue.outstanding == 0
