# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Tests for the P2P slave readiness threshold (``[READY]`` log line).

The slave emits ``[READY]`` only when BOTH conditions hold:

* the local filesystem watcher is running, and
* the master handshake completed (``hello`` returned, or auth accepted).

A handshake that fails right after the watcher start (e.g. the master
requires a P2P key and none is configured) must never reach the threshold:
readiness is observable and must not be logged prematurely.
"""

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestP2PWebsocketClientReadiness:
    """The [READY] line marks "watcher active + event channel established"."""

    def _make_socket(self, greeting: str, *, yield_rounds: int = 0):
        """Build a minimal WebSocket double serving one greeting frame.

        After the handshake, the async iteration ends cleanly
        (StopAsyncIteration) so the client teardown path runs normally.
        ``yield_rounds`` hands control back to the loop that many times before
        ending, so a task the client started after the handshake gets to run.
        """

        class _Socket:
            def __init__(self):
                self.sent: list[str] = []

            async def recv(self) -> str:
                return greeting

            async def send(self, message: str) -> None:
                self.sent.append(message)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                for _ in range(yield_rounds):
                    await asyncio.sleep(0)
                raise StopAsyncIteration

        return _Socket()

    @contextlib.contextmanager
    def _client_patches(self, tmp_path, mock_log, socket, *, cycle=None):
        """Context manager applying the patch stack shared by the scenarios.

        ``cycle`` replaces the reconcile cycle the client runs after the
        handshake: the real one would scan the bucket and reach for the master.
        """
        import hsync_client.p2p_websocket as ws
        import hsync_client.sync as sync_module

        with (
            patch.dict(
                ws.CONFIG,
                {
                    "LOCAL_BUCKET": str(tmp_path),
                    "REMOTE_BUCKET": "test-bucket",
                    "RESYNC": 0,
                    "CID": "ValidSlaveCid1234567",
                    "P2P_KEY": None,
                },
                clear=False,
            ),
            patch.object(ws, "WatcherManager", MagicMock()),
            patch.object(ws, "P2PSlaveFileEventHandler", MagicMock()),
            patch.object(ws, "log", mock_log),
            patch.object(sync_module, "sync", cycle or AsyncMock()),
            patch("websockets.connect", return_value=socket),
        ):
            yield

    @pytest.mark.asyncio
    async def test_ready_logged_after_hello_handshake(self, tmp_path):
        """[READY] appears after watcher start + completed hello handshake."""
        import hsync_client.p2p_websocket as ws

        socket = self._make_socket(json.dumps({"type": "hello"}))
        mock_log = MagicMock()

        with self._client_patches(tmp_path, mock_log, socket):
            await ws.p2p_websocket_client("master.local", 9000)

        ready_msgs = [
            str(c.args[0])
            for c in mock_log.info.call_args_list
            if c.args and "[READY]" in str(c.args[0])
        ]
        assert len(ready_msgs) == 1, f"expected one [READY] line, got {ready_msgs!r}"
        assert f"watching '{tmp_path}'" in ready_msgs[0]

    @pytest.mark.asyncio
    async def test_no_ready_when_auth_required_but_no_key(self, tmp_path):
        """A refused auth handshake must never emit [READY]."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_P2P

        import hsync_client.p2p_websocket as ws

        greeting = json.dumps(
            {
                "type": "auth_required",
                "protocol_version": PROTOCOL_VERSION_P2P,
                "capabilities": [],
            }
        )
        socket = self._make_socket(greeting)
        mock_log = MagicMock()

        with self._client_patches(tmp_path, mock_log, socket):
            await ws.p2p_websocket_client("master.local", 9000)

        ready_msgs = [
            str(c.args[0])
            for c in mock_log.info.call_args_list
            if c.args and "[READY]" in str(c.args[0])
        ]
        assert ready_msgs == [], f"no [READY] expected on failed auth, got {ready_msgs!r}"

    @pytest.mark.asyncio
    async def test_status_connected_then_waiting(self, tmp_path):
        """The slave reports the link up (connected) and the drop (waiting)."""
        import hsync_client.p2p_websocket as ws

        socket = self._make_socket(json.dumps({"type": "hello"}))
        mock_log = MagicMock()
        statuses: list[str] = []

        with (
            self._client_patches(tmp_path, mock_log, socket),
            patch.object(ws, "set_link_status", statuses.append),
        ):
            await ws.p2p_websocket_client("master.local", 9000)

        assert statuses == ["connected", "waiting"]

    @pytest.mark.asyncio
    async def test_a_catch_up_cycle_runs_once_after_the_handshake(self, tmp_path):
        """The window the event stream cannot cover is reconciled per link.

        The initial sync lists the master before the socket is admitted, so a
        change made in between is in neither the listing nor the event stream;
        one reconcile cycle after ``[READY]`` closes that window.
        """
        import hsync_client.p2p_websocket as ws

        socket = self._make_socket(json.dumps({"type": "hello"}), yield_rounds=3)
        cycles: list[dict] = []

        async def _cycle(**kwargs):
            cycles.append(kwargs)

        with self._client_patches(tmp_path, MagicMock(), socket, cycle=_cycle):
            await ws.p2p_websocket_client("master.local", 9000)

        assert len(cycles) == 1
        assert cycles[0]["p2p_mode"] is True

    @pytest.mark.asyncio
    async def test_no_catch_up_when_the_handshake_is_refused(self, tmp_path):
        """A link that never became an event stream has no window to close."""
        from hsync_common.protocol.handshake import PROTOCOL_VERSION_P2P

        import hsync_client.p2p_websocket as ws

        greeting = json.dumps(
            {
                "type": "auth_required",
                "protocol_version": PROTOCOL_VERSION_P2P,
                "capabilities": [],
            }
        )
        socket = self._make_socket(greeting, yield_rounds=3)
        cycles: list[dict] = []

        async def _cycle(**kwargs):
            cycles.append(kwargs)

        with self._client_patches(tmp_path, MagicMock(), socket, cycle=_cycle):
            await ws.p2p_websocket_client("master.local", 9000)

        assert cycles == []
