# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — outbound event-queue micro-benchmark (async, in-memory socket stub).

Every watcher/realtime event is serialized and pushed through
:class:`~hsync_client.event_queue.EventQueue`. This benchmark measures the two
hot steps of that path without a real WebSocket:

* ``get_backoff_delay`` — the pure retry backoff the queue computes when a send
  fails and the event is held (head-of-line blocking);
* ``enqueue`` — the async append of a queued event;
* ``_send_event(success)`` — JSON serialization plus the awaited ``send`` on a
  counting stub, i.e. the per-event cost the queue pays on the success path.

The async measurements run on one loop (``asyncio.run``) so the loop setup cost
is outside the samples; the stub ``send`` asserts the payload type so a broken
serialization path cannot be reported as fast.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_event_queue.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    measure_async,
    print_table,
)

from hsync_client.event_queue import EventQueue, QueuedEvent  # noqa: E402


class _StubWS:
    """Counting WebSocket stand-in: asserts the payload shape, never touches the wire."""

    def __init__(self) -> None:
        self.sent = 0

    async def send(self, message: str) -> None:
        assert isinstance(message, str)
        self.sent += 1


async def _async_main(count: int) -> list[BenchResult]:
    ws = _StubWS()
    queue = EventQueue(ws)
    payload = {"name": "modified", "src": "dir/file.txt", "dst": "dir/file.txt"}

    async def op_enqueue() -> None:
        await queue.enqueue(payload)

    event = QueuedEvent(data=payload)

    async def op_send_event() -> None:
        before = ws.sent
        assert await queue._send_event(event) is True
        assert ws.sent == before + 1

    return [
        await measure_async("enqueue", op_enqueue, count=count),
        await measure_async("send_event(success)", op_send_event, count=count),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    # Sync backoff computation (pure): cycle retry counts 0..6.
    retries = [0, 1, 2, 3, 4, 5, 6]
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert [QueuedEvent(data={}, retry_count=r).get_backoff_delay() for r in retries] == expected
    state = {"i": 0}

    def op_backoff() -> None:
        i = state["i"] % len(retries)
        state["i"] += 1
        event = QueuedEvent(data={}, retry_count=retries[i])
        assert event.get_backoff_delay() == expected[i]

    results: list[BenchResult] = [
        measure("get_backoff_delay(mixed)", op_backoff, count=count),
        *asyncio.run(_async_main(count)),
    ]

    print(f"event-queue micro-benchmark (ops={count}, in-memory socket stub)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
