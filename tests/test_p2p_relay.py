# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the p2p data relay (client side).

Covers the two pieces that decide *whether* traffic is relayed — the
``_select_master_endpoint`` fallback in ``p2p_handler`` and the loopback bridge in
``boot_relay`` — without a live node: the end-to-end path is exercised by
``tests/test_bb_p2p_bootnode_relay.py``.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from loguru import logger as log

import hsync_client.p2p_handler as p2p_handler


@pytest.fixture(autouse=True)
def _reset_relay_state() -> None:
    """Every test starts with no tunnel, no listener and no backoff deadline."""
    p2p_handler._close_relay_dialer()
    p2p_handler._relay_retry_at = 0.0
    yield
    p2p_handler._close_relay_dialer()
    p2p_handler._relay_retry_at = 0.0


from hsync_client.boot_client import BootnodeProtocolError, frame_line
from hsync_client.boot_relay import (
    FailoverRelayDialer,
    GroupRelayAcceptor,
    bridge,
    refusal_code,
)
from hsync_client.p2p_discovery import DiscoveryPlanState
from hsync_client.p2p_handler import _close_relay_dialer, _relay_master_endpoint


class _Master:
    cid = "A1b2C3d4E5f6G7h8I9j0K"
    address = "203.0.113.7"
    port = 8443


class _Dialer:
    def __init__(self, endpoint: tuple[str, int] | None) -> None:
        self._endpoint = endpoint
        self.opened = False
        self.closed = False

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        self.opened = True
        return self._endpoint

    def close(self) -> None:
        self.closed = True


class _Plan:
    def __init__(self, dialer: _Dialer | None) -> None:
        self._dialer = dialer
        self.asked: list[str] = []

    def relay_dialer(self, target_cid: str) -> _Dialer | None:
        self.asked.append(target_cid)
        return self._dialer


def test_a_lan_only_plan_never_relays() -> None:
    """The mDNS plan has no rendezvous point, so the fallback stays a no-op."""
    plan = object.__new__(DiscoveryPlanState)
    assert plan.relay_dialer("whatever") is None
    assert plan.relay_acceptor(8443) is None
    assert _relay_master_endpoint(plan, _Master(), log) is None


def test_the_tunnel_is_opened_only_after_the_direct_candidates_failed() -> None:
    """The fallback returns the loopback endpoint and keeps the dialer alive."""
    dialer = _Dialer(("127.0.0.1", 41234))
    plan = _Plan(dialer)
    try:
        assert _relay_master_endpoint(plan, _Master(), log) == ("127.0.0.1", 41234)
        assert plan.asked == [_Master.cid]
        assert dialer.opened is True
        assert p2p_handler._active_relay_dialer is dialer
    finally:
        _close_relay_dialer()
    assert dialer.closed is True


def test_a_refused_session_keeps_the_retry_loop() -> None:
    """A node that cannot pair returns ``None``: no tunnel, no state."""
    dialer = _Dialer(None)
    plan = _Plan(dialer)
    assert _relay_master_endpoint(plan, _Master(), log) is None
    assert dialer.opened is True
    assert p2p_handler._active_relay_dialer is None


def test_a_refused_relay_is_not_retried_on_every_tick() -> None:
    """A node without the relay must not be re-dialled once per discovery tick."""
    dialer = _Dialer(None)
    plan = _Plan(dialer)
    assert _relay_master_endpoint(plan, _Master(), log) is None
    assert _relay_master_endpoint(plan, _Master(), log) is None
    assert plan.asked == [_Master.cid]


def test_a_previous_tunnel_is_closed_before_a_new_attempt() -> None:
    """A dialer from an earlier session never outlives the next attempt."""
    first = _Dialer(("127.0.0.1", 40000))
    second = _Dialer(("127.0.0.1", 40001))
    try:
        _relay_master_endpoint(_Plan(first), _Master(), log)
        assert _relay_master_endpoint(_Plan(second), _Master(), log) == ("127.0.0.1", 40001)
        assert first.closed is True
        assert second.closed is False
    finally:
        _close_relay_dialer()


def test_a_relay_error_line_reports_its_uniform_code() -> None:
    """The refusal code is read from the validated frame, never guessed."""
    assert (
        refusal_code(
            json.dumps(
                {
                    "protocol_version": "boot/1",
                    "type": "relay_error",
                    "error": "relay_unavailable",
                }
            ).encode()
        )
        == "relay_unavailable"
    )
    assert refusal_code(b"not-json") == "relay_unavailable"
    assert refusal_code(b"") == "relay_unavailable"


def test_frame_line_validates_before_sending() -> None:
    """A malformed relay frame fails locally instead of being dropped remotely."""
    line = frame_line(
        {"protocol_version": "boot/1", "type": "relay_ping", "cid": _Master.cid, "bucket": "b"}
    )
    assert line.endswith(b"\n")
    with pytest.raises(BootnodeProtocolError):
        frame_line({"protocol_version": "boot/1", "type": "relay_ping", "cid": "short"})


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class _Writer:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def test_the_bridge_copies_bytes_and_closes_both_sides() -> None:
    """The loopback side and the relayed stream are pumped verbatim."""
    left_reader = _Reader([b"from-local", b""])
    right_reader = _Reader([b"from-peer", b""])
    left_writer = _Writer()
    right_writer = _Writer()

    asyncio.run(bridge(left_reader, left_writer, right_reader, right_writer, idle_timeout=1.0))

    assert bytes(right_writer.buffer) == b"from-local"
    assert bytes(left_writer.buffer) == b"from-peer"
    assert left_writer.closed is True
    assert right_writer.closed is True


class _BudgetDialer:
    """A relay dialer double that needs a minimum slice of the budget."""

    def __init__(self, *, spend: float, endpoint: tuple[str, int] | None) -> None:
        self._spend = spend
        self._endpoint = endpoint
        self.calls: list[float] = []
        self.closed = 0

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        self.calls.append(timeout)
        time.sleep(min(self._spend, max(0.0, timeout)))
        # A real dial cannot finish inside a budget shorter than it needs.
        return self._endpoint if timeout >= self._spend else None

    def close(self) -> None:
        self.closed += 1


class _BudgetAcceptor:
    """A relay acceptor double that needs a minimum slice of the budget."""

    def __init__(self, *, spend: float, accepted: bool) -> None:
        self._spend = spend
        self._accepted = accepted
        self.calls: list[float] = []
        self.stopped = 0

    @property
    def active(self) -> bool:
        return self._accepted

    def start(self, timeout: float = 10.0) -> bool:
        self.calls.append(timeout)
        time.sleep(min(self._spend, max(0.0, timeout)))
        return self._accepted if timeout >= self._spend else False

    def stop(self) -> None:
        self.stopped += 1


def test_the_dialer_budget_covers_every_server_not_each_one() -> None:
    """Two dead servers must not double the caller's relay timeout."""
    first = _BudgetDialer(spend=0.06, endpoint=None)
    second = _BudgetDialer(spend=0.06, endpoint=("127.0.0.1", 5000))

    started = time.monotonic()
    assert FailoverRelayDialer([first, second]).open(timeout=0.08) is None
    elapsed = time.monotonic() - started

    assert elapsed < 0.25, elapsed
    assert second.calls == [] or second.calls[0] < 0.08


def test_the_dialer_still_pairs_when_a_later_server_has_budget() -> None:
    first = _BudgetDialer(spend=0.0, endpoint=None)
    second = _BudgetDialer(spend=0.0, endpoint=("127.0.0.1", 5000))

    dialer = FailoverRelayDialer([first, second])
    assert dialer.open(timeout=1.0) == ("127.0.0.1", 5000)
    dialer.close()
    assert first.closed == 1 and second.closed == 1


def test_the_acceptor_budget_covers_every_server_not_each_one() -> None:
    """Registering on two servers must stay inside one deadline."""
    first = _BudgetAcceptor(spend=0.06, accepted=False)
    second = _BudgetAcceptor(spend=0.06, accepted=True)

    started = time.monotonic()
    assert GroupRelayAcceptor([first, second]).start(timeout=0.08) is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.25, elapsed


def test_the_acceptor_reports_success_when_any_server_registers() -> None:
    first = _BudgetAcceptor(spend=0.0, accepted=False)
    second = _BudgetAcceptor(spend=0.0, accepted=True)

    acceptor = GroupRelayAcceptor([first, second])
    assert acceptor.start(timeout=1.0) is True
    assert acceptor.active is True
    acceptor.stop()
    assert first.stopped == 1 and second.stopped == 1
