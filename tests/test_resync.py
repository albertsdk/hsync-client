# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for resync function in websocket_handler module."""

import contextlib
from unittest.mock import patch

import pytest


class TestResync:
    """Tests for resync function."""

    @pytest.mark.asyncio
    async def test_resync_calls_sync_periodically(self):
        """resync calls sync after configured delay."""
        from hsync_client.websocket_handler import resync

        config = {"RESYNC": 0.01}  # Very short delay for testing

        call_count = 0

        async def mock_sync():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise Exception("Stop after 2 calls")

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
        ):
            with contextlib.suppress(Exception):
                await resync()
            assert call_count >= 2
