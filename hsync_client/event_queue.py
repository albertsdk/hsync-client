# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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

from asyncio import Queue, sleep
from dataclasses import dataclass, field
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


@dataclass
class QueuedEvent:
    """
    An event in the processing queue with retry metadata.

    Attributes:
        data: The event payload to send.
        retry_count: Number of times this event has been retried.
        next_retry_time: Unix timestamp when retry is allowed.
        created_at: Unix timestamp when event was first queued.
    """

    data: dict[str, Any]
    retry_count: int = 0
    next_retry_time: float = 0.0
    created_at: float = field(default_factory=time)

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

        Args:
            event: The queued event to send.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            await self.websocket.send(dumps(event.data))
            log.debug(f"Event sent: {event.data.get('name', 'unknown')}")
            return True
        except Exception as e:
            log.warning(f"Failed to send event: {e}")
            return False

    async def _next_event(self) -> QueuedEvent | None:
        """
        Return the next event to process, preserving strict FIFO order.

        A failed event held in ``_pending_retry`` takes precedence and blocks
        newer events until its backoff has elapsed and it is re-attempted
        (head-of-line blocking). This guarantees events are delivered in the
        order they occurred, so a retried "added" can never overtake a later
        "deleted" on the same file.

        Returns:
            The next event to send, or None if nothing is ready yet.
        """
        if self._pending_retry is not None:
            if time() >= self._pending_retry.next_retry_time:
                event = self._pending_retry
                self._pending_retry = None
                return event
            # Still waiting on the failed event: do not pull newer events.
            return None

        if not self.queue.empty():
            return await self.queue.get()

        return None

    async def process(self) -> None:
        """
        Main event processing loop.

        Continuously processes events in strict FIFO order, holding a failed
        event for retry with exponential backoff before any newer event is
        sent.
        """
        self._running = True
        while self._running:
            event = await self._next_event()
            if event is None:
                # Nothing ready (empty queue or pending retry still waiting).
                await sleep(0.01)
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

            # Small delay to prevent busy loop
            await sleep(0.01)

    def stop(self) -> None:
        """Stop the event processing loop."""
        self._running = False
        log.debug("Event queue stopped")
