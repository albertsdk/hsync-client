"""
Tests for EventQueue.
"""

import asyncio
from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hsync_client.event_queue import (
    EventQueue,
    QueuedEvent,
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    BACKOFF_MULTIPLIER,
    MAX_RETRIES,
)


class TestQueuedEvent:
    """Tests for QueuedEvent dataclass."""

    def test_init_defaults(self):
        """Test default values."""
        event = QueuedEvent(data={"name": "added"})
        
        assert event.data == {"name": "added"}
        assert event.retry_count == 0
        assert event.next_retry_time == 0.0
        assert event.created_at > 0

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
        assert len(queue.failed_events) == 0
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
    async def test_retry_failed_events(self, mock_websocket):
        """Test retry of failed events when backoff expires."""
        queue = EventQueue(mock_websocket)
        
        # Add event with expired backoff
        expired_event = QueuedEvent(data={"name": "test"})
        expired_event.retry_count = 1
        expired_event.next_retry_time = time() - 10  # In the past
        queue.failed_events.append(expired_event)
        
        # Add event with future backoff
        future_event = QueuedEvent(data={"name": "future"})
        future_event.retry_count = 1
        future_event.next_retry_time = time() + 100  # In the future
        queue.failed_events.append(future_event)
        
        await queue._retry_failed_events()
        
        # Expired event should be moved to queue
        assert not queue.queue.empty()
        # Future event should still be in failed_events
        assert len(queue.failed_events) == 1
        assert queue.failed_events[0].data["name"] == "future"

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
        
        await asyncio.gather(
            queue.process(),
            run_and_stop()
        )
        
        mock_websocket.send.assert_called()

