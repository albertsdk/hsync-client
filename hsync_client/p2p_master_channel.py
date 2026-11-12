# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""P2P link status, presence reporting and the master stdin channel.

The process-wide link state (``connected``/``waiting``), the master/slave presence
events and ``_MasterChannel`` (the master's stdin command loop) extracted from
``hsync_client.p2p_handler``.
"""

import threading

from hsync_client.master_status import MasterSyncStatus
from hsync_client.p2p_peers import PeerManager
from hsync_client.p2p_server import P2PServer
from hsync_client.p2p_websocket import P2PWebSocketServer
from hsync_client.presence import record_presence, slave_connected_count
from hsync_client.stdin_commands import StdinCommandHandler


def _p2p_status(connected_clients: int) -> str:
    """p2p link status: an active peer connection is required to be "connected".

    A serving master with no connected slave is ``waiting`` for a device, not a
    live link (so the status bar stays honest and no "connection lost" fires when
    nobody was ever connected).
    """
    return "connected" if connected_clients > 0 else "waiting"


def _emit_master_presence(count: int, peer_manager: "PeerManager") -> None:
    """Re-emit the local ``peers`` pair while the master serves.

    The rendezvous loop that normally owns the ``peers`` event is blocked inside
    ``_run_as_master``, so a client joining/leaving must refresh the master's own
    presence too (the mDNS TXT record is updated separately). ``connected``
    includes this node; ``max`` keeps ``2/2`` even when the mDNS peer entry lags
    the WebSocket join.
    """
    total = max(1 + len(peer_manager.get_alive_peers()), 1 + count)
    record_presence(min(1 + count, total), total)


class _MasterChannel:
    """Stdin commands + sync-status classification for the serving role.

    The serving role runs outside any asyncio loop (``_run_as_master`` yields
    with a plain sleep), so the slave-side readers in ``preview.py`` never see
    its commands: without this channel a front-end's ``PREVIEW``/``SYNC`` are
    silently dropped while the node is the master, leaving its file table stuck
    on the pre-connect state (all rows "Local") even though the status bar says
    "connected". ``MasterSyncStatus`` answers with a classification of the
    local share against the inventories the devices published. The channel also
    owns the served-share change hook, so a file a device uploads refreshes the
    classification instead of waiting for an unrelated inventory change.
    """

    def __init__(
        self,
        p2p_server: P2PServer,
        p2p_ws_server: P2PWebSocketServer,
        peer_manager: "PeerManager",
        log,
    ) -> None:
        self._p2p_server = p2p_server
        self._p2p_ws_server = p2p_ws_server
        self._peer_manager = peer_manager
        self._log = log
        self._status = MasterSyncStatus(
            lambda: self._p2p_server.inventory_store,
            log,
            connected_provider=lambda: self._p2p_ws_server.connected_clients,
        )
        self._reader: StdinCommandHandler | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the command reader and classify once (idempotent).

        Called on every rendezvous iteration while the node stays master, so a
        running channel must be left alone: the inventory callback — not the
        loop — drives the re-classification.
        """
        if self._thread is not None:
            return
        reader = StdinCommandHandler(self._log)
        # The answers are bracketed by the serving-side pass pair
        # (status_pass_start/status_pass_end): preview_start/preview_end belong to
        # the front-end's own preview cycles (see MasterSyncStatus.run_now).
        reader.register("PREVIEW", lambda **_kw: self._status.request())
        # A master is the source of the share: there is nothing to pull, but the
        # command must still acknowledge by refreshing the reported status.
        reader.register("SYNC", lambda **_kw: self._status.request())
        reader.register(
            "PEERS",
            lambda **_kw: _emit_master_presence(
                self._p2p_ws_server.connected_clients, self._peer_manager
            ),
        )
        self._reader = reader
        self._stop.clear()
        self._thread = threading.Thread(
            target=reader.run_in_thread,
            args=(self._stop,),
            name="hsync-stdin-master",
            daemon=True,
        )
        self._thread.start()
        self.refresh()

    def refresh(self) -> None:
        """(Re)wire the change hooks and ask for a classification."""
        store = self._p2p_server.inventory_store
        if store is not None:
            store.set_change_callback(self._status.request)
        self._p2p_ws_server.set_local_change_callback(self.local_change)
        self._status.request()

    def local_change(self) -> None:
        """The served share changed on disk: refresh the listing, re-classify.

        Runs from the watchdog thread on every create/modify/delete/rename of
        the share, the files a device uploads included. Without it the next
        pass compares the share with a listing up to
        ``master_status.LOCAL_CACHE_TTL_S`` old (and nothing else wakes the
        role when only its own folder changed), so a front-end keeps seeing the
        transfers a completed cycle already applied.
        """
        self._status.invalidate_local()
        self._status.request()

    def stop(self) -> None:
        """Stop reading commands and detach from the change hooks (idempotent)."""
        store = self._p2p_server.inventory_store
        if store is not None:
            store.set_change_callback(None)
        self._p2p_ws_server.set_local_change_callback(None)
        reader, self._reader = self._reader, None
        self._stop.set()
        if reader is not None:
            reader.stop_thread()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def close(self) -> None:
        """Full shutdown: stop the reader and the classification worker."""
        self.stop()
        self._status.stop()

    def invalidate_local(self) -> None:
        """The share changed on disk: do not reuse the cached listing."""
        self._status.invalidate_local()


def _emit_slave_presence(peer_manager: "PeerManager") -> None:
    """Re-emit the local ``peers`` pair when this slave links to the master.

    The rendezvous loop that normally owns the ``peers`` event is blocked inside
    the WebSocket run for the whole connection, so a slave joining a master must
    refresh its own presence too; otherwise it keeps the ``1/N`` pair it emitted
    before connecting (the master's mDNS TXT count lags the join). ``connected``
    includes this node and a live WSS link proves the master is reachable, so a
    two-node group reads ``2/2`` even before the master's TXT count converges.
    """
    master = peer_manager.get_master()
    if master is None:
        return
    total = 1 + len(peer_manager.get_alive_peers())
    connected = 1 + slave_connected_count(peer_manager, master)
    record_presence(min(connected, total), total)
