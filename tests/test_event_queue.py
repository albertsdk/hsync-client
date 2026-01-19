"""
Tests for EventQueue.
"""

import asyncio
from time import time
from unittest.mock import AsyncMock

import pytest

from hsync_client.event_queue import (
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    MAX_RETRIES,
    EventQueue,
    QueuedEvent,
)


class TestQueuedEvent:
    """Tests for QueuedEvent dataclass."""

    def test_init_defaults(self):
        """Test default values."""
        event = QueuedEvent(data={"name": "added"})

        assert event.data == {"name": "added"}
        assert event.retry_count == 0
        assert event.next_retry_time == 0.0

    def test_get_backoff_delay_initial(self):
        """Test initial backoff delay."""
        event = QueuedEvent(data={})
        event.retry_count = 0

        delay = event.get_backoff_delay()
        assert delay == INITIAL_BACKOFF  # 1.0 second

    def test_get_backoff_delay_exponential(self):
        """Test exponential backoff growth."""
        event = QueuedEvent(data={})

        # First retry: 1.0s
        event.retry_count = 0
        assert event.get_backoff_delay() == 1.0

        # Second retry: 2.0s
        event.retry_count = 1
        assert event.get_backoff_delay() == 2.0

        # Third retry: 4.0s
        event.retry_count = 2
        assert event.get_backoff_delay() == 4.0

        # Fourth retry: 8.0s
        event.retry_count = 3
        assert event.get_backoff_delay() == 8.0

    def test_get_backoff_delay_max(self):
        """Test backoff is capped at MAX_BACKOFF."""
        event = QueuedEvent(data={})
        event.retry_count = 10  # Would be 1024s without cap

        delay = event.get_backoff_delay()
        assert delay == MAX_BACKOFF  # 60.0 seconds


class TestEventQueue:
    """Tests for EventQueue class."""

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        ws = AsyncMock()
        ws.send = AsyncMock()
        return ws

    def test_init(self, mock_websocket):
        """Test queue initialization."""
        queue = EventQueue(mock_websocket)

        assert queue.websocket == mock_websocket
        assert queue.queue.empty()
        assert queue._pending_retry is None
        assert queue._running is False

    @pytest.mark.asyncio
    async def test_enqueue(self, mock_websocket):
        """Test adding event to queue."""
        queue = EventQueue(mock_websocket)

        await queue.enqueue({"name": "added", "src": "file.txt"})

        assert not queue.queue.empty()
        event = await queue.queue.get()
        assert event.data["name"] == "added"

    @pytest.mark.asyncio
    async def test_send_event_success(self, mock_websocket):
        """Test successful event sending."""
        queue = EventQueue(mock_websocket)
        event = QueuedEvent(data={"name": "modified"})

        result = await queue._send_event(event)

        assert result is True
        mock_websocket.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_event_failure(self, mock_websocket):
        """Test event sending failure."""
        mock_websocket.send.side_effect = Exception("Connection lost")
        queue = EventQueue(mock_websocket)
        event = QueuedEvent(data={"name": "modified"})

        result = await queue._send_event(event)

        assert result is False

    @pytest.mark.asyncio
    async def test_send_event_unserializable_is_dropped_without_send(self, mock_websocket):
        """A permanently non-serializable event is dropped immediately and the
        websocket is never contacted.

        Regression for the retry storm: bytes (e.g. watchdog raw data) cannot
        be JSON-serialized; retrying would fail identically every time and
        stall the queue with head-of-line blocking. The queue must treat it
        as a terminal outcome (True) instead of a transient failure (False).
        """
        queue = EventQueue(mock_websocket)
        event = QueuedEvent(data={"name": "added", "src": b"binary\x00payload"})

        result = await queue._send_event(event)

        # Terminal outcome: no retry, no pending hold.
        assert result is True
        # The websocket was never asked to send the broken payload.
        mock_websocket.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_does_not_hold_unserializable_event(self, mock_websocket):
        """The process loop drains a non-serializable event without holding it
        for retry: the next event must be delivered afterwards."""
        queue = EventQueue(mock_websocket)
        await queue.enqueue({"name": "poison", "src": b"binary"})
        await queue.enqueue({"name": "healthy"})

        async def run_and_stop():
            await asyncio.sleep(0.1)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop())

        # Poison event was dropped, not held for retry.
        assert queue._pending_retry is None
        # The healthy event behind it was still delivered.
        mock_websocket.send.assert_called()
        sent_payloads = [call.args[0] for call in mock_websocket.send.call_args_list]
        assert any('"healthy"' in payload for payload in sent_payloads)
        assert all("binary" not in payload for payload in sent_payloads)

    @pytest.mark.asyncio
    async def test_pending_retry_blocks_newer_events(self, mock_websocket):
        """A failed event held for retry blocks newer events until ready."""
        queue = EventQueue(mock_websocket)

        # Two events: the first is held as a pending retry, the second is newer.
        await queue.enqueue({"name": "pending"})
        await queue.enqueue({"name": "newer"})

        # Pull the first event and hold it as pending with a not-yet-elapsed
        # backoff (mirrors a failed send in process()).
        pending = await queue.queue.get()
        pending.retry_count = 1
        pending.next_retry_time = time() + 100  # not ready yet
        queue._pending_retry = pending

        # While the pending event is not ready, process() must not send the
        # newer event: the pending event blocks the queue (head-of-line).
        async def run_and_stop():
            await asyncio.sleep(0.05)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop())

        mock_websocket.send.assert_not_called()
        assert queue._pending_retry is pending

        # Once the backoff elapses, the pending event is sent before the newer one.
        pending.next_retry_time = 0
        queue._running = True

        async def run_and_stop_2():
            await asyncio.sleep(0.05)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop_2())

        sent = [c.args[0] for c in mock_websocket.send.call_args_list]
        assert '"pending"' in sent[0]
        assert '"newer"' in sent[1]

    def test_stop(self, mock_websocket):
        """Test stopping the queue."""
        queue = EventQueue(mock_websocket)
        queue._running = True

        queue.stop()

        assert queue._running is False

    @pytest.mark.asyncio
    async def test_process_sends_event(self, mock_websocket):
        """Test that process loop sends queued events."""
        queue = EventQueue(mock_websocket)

        await queue.enqueue({"name": "added"})

        # Run process briefly then stop
        async def run_and_stop():
            await asyncio.sleep(0.05)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop())

        mock_websocket.send.assert_called()

    @pytest.mark.asyncio
    async def test_failed_event_is_held_for_retry(self, mock_websocket):
        """A failed send holds the event for retry instead of dropping it."""
        mock_websocket.send = AsyncMock(side_effect=Exception("fail"))
        queue = EventQueue(mock_websocket)

        await queue.enqueue({"name": "added"})

        # Run process briefly (less than the 1s backoff, so it stays pending).
        async def run_and_stop():
            await asyncio.sleep(0.05)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop())

        # Event failed once and is now awaiting retry, not silently dropped.
        assert queue._pending_retry is not None
        assert queue._pending_retry.data["name"] == "added"
        assert queue._pending_retry.retry_count == 1

    @pytest.mark.asyncio
    async def test_process_drops_after_max_retries(self, mock_websocket):
        """Test that events are dropped after MAX_RETRIES."""
        mock_websocket.send = AsyncMock(side_effect=Exception("always fail"))
        queue = EventQueue(mock_websocket)

        # Model the real flow: a held event always originates from a
        # queue.get(), so the queue has one unfinished task to balance the
        # final task_done() on drop.
        await queue.enqueue({"name": "doomed"})
        event = await queue.queue.get()
        event.retry_count = MAX_RETRIES
        event.next_retry_time = 0  # Ready to retry immediately
        queue._pending_retry = event

        # Run process briefly
        async def run_and_stop():
            await asyncio.sleep(0.1)
            queue.stop()

        await asyncio.gather(queue.process(), run_and_stop())

        # The doomed event is dropped and no longer held.
        assert queue._pending_retry is None
