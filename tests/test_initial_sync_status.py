# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The initial sync must report a distinct "syncing" status.

The realtime channel connects only after the first full reconcile, so without
this event the desktop status bar kept saying "Connecting…" for the whole
(possibly minutes-long) initial sync.
"""

from unittest.mock import patch

from hsync_client.events import UiEventName
from hsync_client.sync import log_initial_sync_start


def test_initial_sync_reports_the_syncing_status():
    with patch("hsync_client.sync.emit_event") as mock_emit:
        log_initial_sync_start()

    mock_emit.assert_called_once_with(UiEventName.status, status="syncing")
