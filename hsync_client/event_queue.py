# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Event queue with retry and exponential backoff for HSYNC Client.

This module provides a queue-based event processing system that handles
transient failures gracefully with exponential backoff and retry logic.

Features:
- Async queue for pending events
- Automatic retry with exponential backoff (1s, 2s, 4s, ... up to 60s)
- Maximum retry count per event
- Strict FIFO ordering preserved across retries (head-of-line blocking):
  a failed event blocks newer events until it is re-sent or dropped, so a
  retried "added" can never overtake a later "deleted" (which would
  resurrect the file).
"""

from asyncio import (
    FIRST_COMPLETED,
    Event,
    Queue,
    Task,
    create_task,
    current_task,
    wait,
    wait_for,
)
from collections.abc import Iterable
from dataclasses import dataclass
from json import dumps
from logging import getLogger
from time import time
from typing import Any, Protocol


class WebSocketProtocol(Protocol):
    """Protocol for WebSocket-like objects that can send messages."""

    async def send(self, message: str) -> None:
        """Send a message through the WebSocket."""
        ...


# Logger instance for this module
log = getLogger("hsync")

# Configuration constants
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds
MAX_BACKOFF = 60.0  # seconds
BACKOFF_MULTIPLIER = 2.0


async def _await_cancelled(tasks: Iterable[Task]) -> None:
    """Cancel unfinished tasks and wait so they are not GC'd while pending.

    ``process()`` is often cancelled in the same tick as ``stop()``
    (``remote_events`` finally). Without this, the inner ``Queue.get()``
    waiter is orphaned and later logs ``Task was destroyed but it is pending!``.
    ``Task.uncancel`` lets the wait finish even when the outer task is already
    cancelling (Python 3.11+ sticky cancellation).
    """
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    to_wait = [task for task in tasks if not task.done()]
    if not to_wait:
        return
    me = current_task()
    if me is None:
        await wait(to_wait)
        return
    held = me.cancelling()
    for _ in range(held):
        me.uncancel()
    try:
        await wait(to_wait)
    finally:
        for _ in range(held):
            me.cancel()


@dataclass
class QueuedEvent:
    """
    An event in the processing queue with retry metadata.

    Attributes:
        data: The event payload to send.
        retry_count: Number of times this event has been retried.
        next_retry_time: Unix timestamp when retry is allowed.
    """

    data: dict[str, Any]
    retry_count: int = 0
    next_retry_time: float = 0.0

    def get_backoff_delay(self) -> float:
        """
        Calculate exponential backoff delay for next retry.

        Returns:
            Delay in seconds before next retry attempt.
        """
        delay = INITIAL_BACKOFF * (BACKOFF_MULTIPLIER**self.retry_count)
        return min(delay, MAX_BACKOFF)


class EventQueue:
    """
    Async event queue with retry and exponential backoff.

    Manages event processing with automatic retry for failed events.
    Uses exponential backoff to avoid overwhelming the server during
    connectivity issues.

    Events are processed in strict FIFO order. A send failure does not let
    later events overtake the failed one: the failed event is held in
    ``_pending_retry`` and re-attempted (after its backoff) before any newer
    event is pulled from the queue.

    Attributes:
        queue: Async queue of pending events.
        websocket: WebSocket connection for sending events.
    """

    def __init__(self, websocket: WebSocketProtocol):
        """
        Initialize the event queue.

        Args:
            websocket: WebSocket connection for sending events.
        """
        self.queue: Queue[QueuedEvent] = Queue()
        self.websocket = websocket
        self._running = False
        # A single failed event awaiting retry. While set, it blocks newer
        # events from being processed, guaranteeing in-order delivery.
        self._pending_retry: QueuedEvent | None = None
        # Set by stop(): wakes a loop that is idle-blocked on queue.get() or
        # sleeping until a retry deadline, so process() exits promptly.
        self._stop_event = Event()

    async def enqueue(self, event_data: dict[str, Any]) -> None:
        """
        Add an event to the queue.

        Args:
            event_data: The event payload to queue.
        """
        event = QueuedEvent(data=event_data)
        await self.queue.put(event)
        log.debug(f"Event queued: {event_data.get('name', 'unknown')}")

    async def _send_event(self, event: QueuedEvent) -> bool:
        """
        Attempt to send an event via WebSocket.

        Serialization is attempted first so that a permanently
        non-serializable payload is dropped immediately instead of being
        retried like a transient network failure (retries re-serialize the
        same broken payload and would fail identically every time, stalling
        the queue with head-of-line blocking).

        Args:
            event: The queued event to send.

        Returns:
            True if the event reached a terminal outcome (sent, or dropped
            because it can never be serialized), False if the send failed
            transiently and should be retried.
        """
        try:
            payload = dumps(event.data)
        except (TypeError, ValueError) as e:
            # Permanent failure: no retry can ever serialize this payload.
            # Drop it and keep the queue moving; the affected file will be
            # picked up again by the next full sync cycle.
            log.error(
                f"Event dropped (unserializable, permanent): {e} "
                f"({event.data.get('name', 'unknown')})"
            )
            return True
        try:
            await self.websocket.send(payload)
            log.debug(f"Event sent: {event.data.get('name', 'unknown')}")
            return True
        except Exception as e:
            log.warning(f"Failed to send event: {e}")
            return False

    async def _await_event(self) -> QueuedEvent | None:
        """Block for the next queued event, or return None on ``stop()``.

        The loop idles here instead of polling: ``enqueue()`` wakes it
        immediately (zero latency), and ``stop()`` wakes it through
        ``_stop_event`` so the task never hangs on an empty queue.

        Both waiters are always cancelled-and-awaited (including when
        ``process()`` itself is cancelled) so a ``Queue.get()`` task is never
        garbage-collected while pending.
        """
        get_task = create_task(self.queue.get())
        stop_task = create_task(self._stop_event.wait())
        try:
            done, _ = await wait((get_task, stop_task), return_when=FIRST_COMPLETED)
            if get_task in done and not get_task.cancelled():
                return get_task.result()
            return None
        finally:
            await _await_cancelled((get_task, stop_task))

    async def process(self) -> None:
        """
        Main event processing loop.

        Continuously processes events in strict FIFO order, holding a failed
        event for retry with exponential backoff before any newer event is
        sent (head-of-line blocking: a retried "added" can never overtake a
        later "deleted" on the same file).

        Idle performance: the loop blocks on ``queue.get()`` (woken the
        instant an event is enqueued) instead of the old fixed
        ``sleep(0.01)`` poll, which burned ~100 wakeups/second on an idle
        queue and added up to 10 ms of latency to every event dispatch.
        While a failed event awaits retry, the loop sleeps exactly until its
        backoff deadline; ``stop()`` interrupts both waits immediately.
        """
        self._running = True
        while self._running:
            event: QueuedEvent | None
            if self._pending_retry is not None:
                # A failed event is blocking the queue: sleep until its
                # backoff elapses (stop() wakes this early), then retry it
                # before anything newer.
                wait_sec = self._pending_retry.next_retry_time - time()
                if wait_sec > 0:
                    try:
                        await wait_for(self._stop_event.wait(), wait_sec)
                    except TimeoutError:
                        pass
                    continue
                event = self._pending_retry
                self._pending_retry = None
            else:
                # Idle point: block until an event arrives or stop() wakes us.
                event = await self._await_event()
                if event is None:  # stop() won the race on an empty queue
                    continue

            # Each event corresponds to exactly one queue.get(); task_done()
            # is therefore called once, on the terminal outcome (sent/dropped).
            if await self._send_event(event):
                self.queue.task_done()
            else:
                event.retry_count += 1
                if event.retry_count <= MAX_RETRIES:
                    backoff = event.get_backoff_delay()
                    event.next_retry_time = time() + backoff
                    # Hold the event: blocks the queue until it is re-sent.
                    self._pending_retry = event
                    log.warning(
                        f"Event failed, will retry in {backoff:.1f}s "
                        f"(attempt {event.retry_count}/{MAX_RETRIES})"
                    )
                else:
                    log.error(
                        f"Event dropped after {MAX_RETRIES} retries: "
                        f"{event.data.get('name', 'unknown')}"
                    )
                    self.queue.task_done()

    def stop(self) -> None:
        """Stop the event processing loop."""
        self._running = False
        self._stop_event.set()
        log.debug("Event queue stopped")
