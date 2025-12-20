# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the shared presence sink (hsync_client.presence)."""

from unittest.mock import patch

from hsync_common.protocol.events import UiEventName

from hsync_client.presence import record_presence, reemit_presence, reset_presence


def _peers_calls(mock_emit):
    """Return (connected, total) pairs from ``peers`` emissions."""
    return [
        (c.kwargs.get("connected"), c.kwargs.get("total"))
        for c in mock_emit.call_args_list
        if c.args and c.args[0] == UiEventName.peers
    ]


class TestPresenceSink:
    def test_emits_only_on_change(self):
        reset_presence()
        with patch("hsync_client.presence.emit_event") as mock_emit:
            assert record_presence(2, 3) is True
            assert record_presence(2, 3) is False
            assert record_presence(3, 3) is True
        assert _peers_calls(mock_emit) == [(2, 3), (3, 3)]

    def test_reemit_presence_before_record_is_silent(self):
        reset_presence()
        with patch("hsync_client.presence.emit_event") as mock_emit:
            assert reemit_presence() is False
        assert _peers_calls(mock_emit) == []

    def test_reemit_presence_replays_last_pair(self):
        reset_presence()
        with patch("hsync_client.presence.emit_event") as mock_emit:
            record_presence(1, 4)
            assert reemit_presence() is True
        assert _peers_calls(mock_emit) == [(1, 4), (1, 4)]
