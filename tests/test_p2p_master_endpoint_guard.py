# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""``_MasterEndpointGuard`` — re-select the master when its endpoint changes.

The slave pins the master's data-plane URL for the whole session; a master that
moves networks would otherwise keep being dialed at its stale address. The guard
watches the shared peer state and reports the change so the session re-enters the
rendezvous loop.
"""

from __future__ import annotations

import asyncio

from hsync_client.p2p_handler import _MasterEndpointGuard

_CID = "B" * 21


class _Master:
    def __init__(self, cid: str, address: str, port: int, endpoints=()) -> None:
        self.cid = cid
        self.address = address
        self.port = port
        self.endpoints = endpoints


class _PeerManager:
    def __init__(self, master: _Master | None) -> None:
        self.master = master

    def get_master(self) -> _Master | None:
        return self.master


def _make_guard(peer_manager: _PeerManager, master: _Master) -> _MasterEndpointGuard:
    return _MasterEndpointGuard(
        peer_manager,
        master.cid,
        address=master.address,
        port=master.port,
        endpoints=master.endpoints,
    )


def _fires_within(guard: _MasterEndpointGuard, timeout: float) -> bool:
    async def main() -> bool:
        try:
            await asyncio.wait_for(guard.wait_async(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    return asyncio.run(main())


def test_fires_when_the_master_address_changes():
    master = _Master(_CID, "192.168.1.107", 44283, (("192.168.1.107", 44283),))
    manager = _PeerManager(master)
    guard = _make_guard(manager, master)
    guard.start()
    try:
        master.address = "10.145.201.139"
        master.endpoints = (("10.145.201.139", 44283),)
        assert _fires_within(guard, 3.0)
    finally:
        guard.close()


def test_fires_when_only_the_endpoint_list_changes():
    master = _Master(_CID, "192.168.1.107", 44283, (("192.168.1.107", 44283),))
    manager = _PeerManager(master)
    guard = _make_guard(manager, master)
    guard.start()
    try:
        master.endpoints = (("10.145.201.139", 44283), ("192.168.1.107", 44283))
        assert _fires_within(guard, 3.0)
    finally:
        guard.close()


def test_fires_when_the_master_disappears():
    master = _Master(_CID, "192.168.1.107", 44283, (("192.168.1.107", 44283),))
    manager = _PeerManager(master)
    guard = _make_guard(manager, master)
    guard.start()
    try:
        manager.master = None
        assert _fires_within(guard, 3.0)
    finally:
        guard.close()


def test_does_not_fire_without_a_change():
    master = _Master(_CID, "192.168.1.107", 44283, (("192.168.1.107", 44283),))
    manager = _PeerManager(master)
    guard = _make_guard(manager, master)
    guard.start()
    try:
        assert not _fires_within(guard, 1.5)
    finally:
        guard.close()


def test_close_releases_a_pending_waiter():
    master = _Master(_CID, "192.168.1.107", 44283, (("192.168.1.107", 44283),))
    manager = _PeerManager(master)
    guard = _make_guard(manager, master)
    guard.start()

    async def main() -> bool:
        async def close_soon() -> None:
            await asyncio.sleep(0.1)
            guard.close()

        closer = asyncio.create_task(close_soon())
        try:
            await asyncio.wait_for(guard.wait_async(), timeout=3.0)
            return True
        except TimeoutError:
            return False
        finally:
            await closer

    assert asyncio.run(main())
