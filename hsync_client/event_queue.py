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
- Priority for failed events on retry
"""

from asyncio import Queue, sleep
from dataclasses import dataclass, field
from json import dumps
from logging import getLogger
from time import time
from typing import Any, Callable, Optional, Protocol


class WebSocketProtocol(Protocol):
    """Protocol for WebSocket-like objects that can send messages."""

    async def send(self, message: str) -> None:
        """Send a message through the WebSocket."""
        ...


# Logger instance for this module
LOG = getLogger("hsync")

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
        delay = INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** self.retry_count)
        return min(delay, MAX_BACKOFF)


class EventQueue:
    """
    Async event queue with retry and exponential backoff.

    Manages event processing with automatic retry for failed events.
    Uses exponential backoff to avoid overwhelming the server during
    connectivity issues.

    Attributes:
        queue: Async queue of pending events.
        failed_events: List of events that failed and need retry.
        websocket: WebSocket connection for sending events.
    """

    def __init__(self, websocket: WebSocketProtocol):
        """
        Initialize the event queue.

        Args:
            websocket: WebSocket connection for sending events.
        """
        self.queue: Queue[QueuedEvent] = Queue()
        self.failed_events: list[QueuedEvent] = []
        self.websocket = websocket
        self._running = False

    async def enqueue(self, event_data: dict[str, Any]) -> None:
        """
        Add an event to the queue.

        Args:
            event_data: The event payload to queue.
        """
        event = QueuedEvent(data=event_data)
        await self.queue.put(event)
        LOG.debug(f"Event queued: {event_data.get('name', 'unknown')}")

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
            LOG.debug(f"Event sent: {event.data.get('name', 'unknown')}")
            return True
        except Exception as e:
            LOG.warning(f"Failed to send event: {e}")
            return False

    async def _retry_failed_events(self) -> None:
        """
        Process failed events that are ready for retry.

        Moves events back to the main queue when their
        backoff delay has elapsed.
        """
        current_time = time()
        ready_events = []
        still_waiting = []

        for event in self.failed_events:
            if current_time >= event.next_retry_time:
                ready_events.append(event)
            else:
                still_waiting.append(event)

        self.failed_events = still_waiting

        for event in ready_events:
            LOG.debug(f"Retrying event (attempt {event.retry_count + 1})")
            await self.queue.put(event)

    async def process(self) -> None:
        """
        Main event processing loop.

        Continuously processes events from the queue, handling
        failures with exponential backoff retry.
        """
        self._running = True
        while self._running:
            # First check for events ready to retry
            await self._retry_failed_events()

            # Process next event from queue
            if not self.queue.empty():
                event = await self.queue.get()

                if await self._send_event(event):
                    self.queue.task_done()
                else:
                    # Handle failure with retry
                    event.retry_count += 1
                    if event.retry_count <= MAX_RETRIES:
                        backoff = event.get_backoff_delay()
                        event.next_retry_time = time() + backoff
                        self.failed_events.append(event)
                        LOG.warning(
                            f"Event failed, will retry in {backoff:.1f}s "
                            f"(attempt {event.retry_count}/{MAX_RETRIES})"
                        )
                    else:
                        LOG.error(
                            f"Event dropped after {MAX_RETRIES} retries: "
                            f"{event.data.get('name', 'unknown')}"
                        )
                    self.queue.task_done()

            # Small delay to prevent busy loop
            await sleep(0.01)

    def stop(self) -> None:
        """Stop the event processing loop."""
        self._running = False
        LOG.debug("Event queue stopped")

