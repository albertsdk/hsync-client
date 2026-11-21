# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The serving master's link status is the data plane, not only the socket.

A p2p slave syncs over HTTPS before it opens its control WebSocket, and it tears
that socket down on every reconnect. Keying the master's status on the socket
alone therefore reported "waiting" (a lost link, in the front-end) for the whole
sync; the data-plane stamp below is what keeps the link honest in both
directions.
"""

import hsync_client.p2p_link as link


def test_a_dataplane_request_publishes_the_link_as_connected(monkeypatch):
    published: list[str] = []
    monkeypatch.setattr(link, "set_link_status", published.append)

    link.note_master_datapath_activity(now=100.0)

    assert link.master_link_status(0, now=110.0) == "connected"
    assert published == ["connected"]


def test_the_link_falls_back_to_waiting_after_the_hold(monkeypatch):
    monkeypatch.setattr(link, "set_link_status", lambda status: None)

    link.note_master_datapath_activity(now=100.0)

    assert link.master_link_status(0, now=100.0 + link.MASTER_LINK_HOLD_SECONDS) == "connected"
    assert link.master_link_status(0, now=100.0 + link.MASTER_LINK_HOLD_SECONDS + 0.1) == "waiting"


def test_a_connected_socket_is_a_link_without_any_activity():
    link.reset_master_datapath_activity()

    assert link.master_link_status(1, now=1_000.0) == "connected"


def test_a_master_nobody_talks_to_is_waiting():
    link.reset_master_datapath_activity()

    assert link.master_link_status(0, now=1_000.0) == "waiting"
    assert link.peer_activity_fresh(now=1_000.0) is False
