# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the p2p data relay (client side).

Covers the two pieces that decide *whether* traffic is relayed — the
``_select_master_endpoint`` fallback in ``p2p_handler`` and the loopback bridge in
``rdv_relay`` — without a live node: the end-to-end path is exercised by
``tests/test_bb_p2p_rendezvous_relay.py``.
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
    p2p_handler._relay_hint_masters.clear()
    _PEERS.relay.clear()
    yield
    p2p_handler._close_relay_dialer()
    p2p_handler._relay_retry_at = 0.0
    p2p_handler._relay_hint_masters.clear()
    _PEERS.relay.clear()


from hsync_client.constants import RELAY_SESSION_WAIT_S
from hsync_client.p2p_handler import _close_relay_dialer, _relay_master_endpoint
from hsync_client.p2p_rendezvous import RendezvousPlanState
from hsync_client.rdv_client import RendezvousProtocolError, frame_line
from hsync_client.rdv_relay import (
    FailoverRelayDialer,
    GroupRelayAcceptor,
    RelayDialer,
    bridge,
    refusal_code,
)


class _Peers:
    """Minimal peer manager: records the relay-tunnel liveness marks."""

    def __init__(self) -> None:
        self.relay: dict[str, bool] = {}

    def mark_relay_tunnel(self, cid: str, up: bool) -> None:
        self.relay[cid] = up


# One recorder for the whole module: the adopted tunnel's target must be marked
# while it is open (``PeerManager.mark_relay_tunnel``); the autouse fixture below
# clears it between tests.
_PEERS = _Peers()


class _Master:
    cid = "A1b2C3d4E5f6G7h8I9j0K"
    address = "203.0.113.7"
    port = 8443


class _Dialer:
    def __init__(
        self,
        endpoint: tuple[str, int] | None,
        *,
        target_cid: str = _Master.cid,
        alive: bool | None = None,
        session_up: bool | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._target_cid = target_cid
        self.opened = False
        self.closed = False
        self.waited: list[float] = []
        # A tunnel that paired is alive (its endpoint is bound) and serving until
        # it is closed; a fake may override either to describe a tunnel whose QUIC
        # session died or one that is repairing it. A refusal names the code the
        # node answered with, which is what makes a tunnel *not* repairing.
        self.alive = endpoint is not None if alive is None else alive
        self.session_up = endpoint is not None if session_up is None else session_up
        self.refusal: str | None = None

    @property
    def repairing(self) -> bool:
        return self.alive and not self.session_up and self.refusal is None

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        self.opened = True
        return self._endpoint

    def close(self) -> None:
        self.closed = True
        self.alive = False
        self.session_up = False

    def wait_session(self, timeout: float = 5.0) -> tuple[str, int] | None:
        self.waited.append(timeout)
        return self._endpoint if self.session_up else None

    @property
    def endpoint(self) -> tuple[str, int] | None:
        return self._endpoint

    @property
    def target_cid(self) -> str:
        return self._target_cid


class _RefusingDialer(_Dialer):
    """A dialer that fails to pair and reports the node's refusal code."""

    def __init__(self, refusal: str | None) -> None:
        super().__init__(None)
        self.refusal = refusal


def _capture_warnings(monkeypatch) -> list[dict]:
    """Capture the structured warnings the handler publishes, in order."""
    warnings: list[dict] = []
    monkeypatch.setattr(
        p2p_handler,
        "emit_warning",
        lambda message, **kwargs: warnings.append({"message": message, **kwargs}),
    )
    return warnings


class _Plan:
    def __init__(self, dialer: _Dialer | None) -> None:
        self._dialer = dialer
        self.asked: list[str] = []

    def relay_dialer(self, target_cid: str) -> _Dialer | None:
        self.asked.append(target_cid)
        return self._dialer


def test_a_lan_only_plan_never_relays() -> None:
    """The mDNS plan has no rendezvous point, so the fallback stays a no-op."""
    plan = object.__new__(RendezvousPlanState)
    assert plan.relay_dialer("whatever") is None
    assert plan.relay_acceptor(8443) is None
    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None


def test_the_tunnel_is_opened_only_after_the_direct_candidates_failed() -> None:
    """The fallback returns the loopback endpoint and keeps the dialer alive."""
    dialer = _Dialer(("127.0.0.1", 41234))
    plan = _Plan(dialer)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) == ("127.0.0.1", 41234)
        assert plan.asked == [_Master.cid]
        assert dialer.opened is True
        assert p2p_handler._active_relay_dialer is dialer
    finally:
        _close_relay_dialer()
    assert dialer.closed is True


def test_a_live_tunnel_to_the_same_master_is_reused(monkeypatch) -> None:
    """Re-opening the tunnel would kill every pooled connection on it.

    The slave session is re-established on every reconnect: a fresh loopback port
    each time left the HTTP client (and the WebSocket) dialling a dead endpoint,
    which the peer saw as an SSL EOF.
    """
    live = _Dialer(("127.0.0.1", 41234))
    plan = _Plan(_Dialer(("127.0.0.1", 55555)))
    monkeypatch.setattr(p2p_handler, "_active_relay_dialer", live)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) == ("127.0.0.1", 41234)
    finally:
        _close_relay_dialer()
    assert plan.asked == [], "a live tunnel was replaced"
    assert live.closed is True  # only the teardown closed it


def test_a_repairing_tunnel_is_waited_for_not_replaced(monkeypatch) -> None:
    """A tunnel repairing its session keeps its port; the caller waits for it.

    The endpoint is bound once and the HTTP client and the WebSocket hold pooled
    connections on it, so binding a new one for a transient drop would strand
    them (the peer sees an SSL EOF). The cycle reports no endpoint instead, and
    the tunnel's own repair loop brings the session back.
    """
    repairing = _Dialer(("127.0.0.1", 41234), session_up=False)
    plan = _Plan(_Dialer(("127.0.0.1", 55555)))
    monkeypatch.setattr(p2p_handler, "_active_relay_dialer", repairing)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    finally:
        _close_relay_dialer()
    assert plan.asked == [], "the repairing tunnel was replaced"
    assert repairing.waited == [RELAY_SESSION_WAIT_S]


def test_a_refused_tunnel_is_not_waited_for(monkeypatch) -> None:
    """A refusal has nothing to wait for: the cycle must not stall on it.

    The rendezvous loop re-enters this path every wake, so waiting on a tunnel
    whose pairing the node just refused would hold the loop (which also serves as
    master) for the whole wait, on every cycle.
    """
    refused = _Dialer(("127.0.0.1", 41234), session_up=False)
    refused.refusal = "relay_unavailable"
    plan = _Plan(_Dialer(("127.0.0.1", 55555)))
    monkeypatch.setattr(p2p_handler, "_active_relay_dialer", refused)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    finally:
        _close_relay_dialer()
    assert refused.waited == [], "the cycle waited on a refused tunnel"
    assert plan.asked == [], "the refused tunnel was replaced"


def test_a_dead_tunnel_is_replaced(monkeypatch) -> None:
    """A tunnel whose session died is re-paired (its endpoint no longer serves)."""
    dead = _Dialer(("127.0.0.1", 41234), alive=False)
    plan = _Plan(_Dialer(("127.0.0.1", 55555)))
    monkeypatch.setattr(p2p_handler, "_active_relay_dialer", dead)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) == ("127.0.0.1", 55555)
    finally:
        _close_relay_dialer()
    assert dead.closed is True
    assert plan.asked == [_Master.cid]


def test_a_tunnel_to_another_master_is_replaced(monkeypatch) -> None:
    """A tunnel only ever reaches one peer: a new master needs its own."""
    other = _Dialer(("127.0.0.1", 41234), target_cid="Zz9Yy8Xx7Ww6Vv5Uu4Tt3")
    plan = _Plan(_Dialer(("127.0.0.1", 55555)))
    monkeypatch.setattr(p2p_handler, "_active_relay_dialer", other)
    try:
        assert _relay_master_endpoint(plan, _Master(), log, _PEERS) == ("127.0.0.1", 55555)
    finally:
        _close_relay_dialer()
    assert other.closed is True
    assert plan.asked == [_Master.cid]


def test_a_refused_session_keeps_the_retry_loop() -> None:
    """A node that cannot pair returns ``None``: no tunnel, no state."""
    dialer = _Dialer(None)
    plan = _Plan(dialer)
    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    assert dialer.opened is True
    assert p2p_handler._active_relay_dialer is None


def test_a_refused_relay_is_not_retried_on_every_tick() -> None:
    """A node without the relay must not be re-dialled once per rendezvous tick."""
    dialer = _Dialer(None)
    plan = _Plan(dialer)
    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    assert plan.asked == [_Master.cid]


def test_a_node_without_the_relay_surfaces_an_actionable_hint(monkeypatch) -> None:
    """``relay_unavailable`` becomes a warning that names the node-side fix.

    Without it the operator only sees the generic "No reachable endpoint" line
    and cannot tell that the rendezvous needs ``--relay`` for a CGNAT pair.
    """
    warnings = _capture_warnings(monkeypatch)
    dialer = _RefusingDialer("relay_unavailable")
    plan = _Plan(dialer)

    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None

    assert [(w["source"], w["reason"]) for w in warnings] == [("relay", "relay_unavailable")]
    assert "--relay" in warnings[0]["message"]
    assert _Master.cid in p2p_handler._relay_hint_masters


def test_the_actionable_hint_is_emitted_once_per_master(monkeypatch) -> None:
    """The rendezvous loop retries, so the hint must not repeat every attempt."""
    warnings = _capture_warnings(monkeypatch)
    plan = _Plan(_RefusingDialer("relay_unavailable"))

    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None
    p2p_handler._relay_retry_at = 0.0  # next attempt as the loop would
    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) is None

    assert len(warnings) == 1


def test_a_non_relay_refusal_never_claims_the_relay_is_disabled(monkeypatch) -> None:
    """A rate limit or protocol error must not produce the ``--relay`` advice."""
    warnings = _capture_warnings(monkeypatch)

    assert (
        _relay_master_endpoint(_Plan(_RefusingDialer("rate_limited")), _Master(), log, _PEERS)
        is None
    )
    p2p_handler._relay_retry_at = 0.0
    assert _relay_master_endpoint(_Plan(_Dialer(None)), _Master(), log, _PEERS) is None

    assert warnings == []
    assert p2p_handler._relay_hint_masters == set()


def test_a_relay_dialer_reports_no_refusal_before_an_attempt() -> None:
    """The refusal is empty until the node answers, so no false hint is raised."""
    dialer = RelayDialer(object(), cid=_Master.cid, bucket="b", target_cid="peer")
    assert dialer.refusal is None


def test_the_failover_dialer_reports_the_attempt_refusal() -> None:
    """The combined dialer surfaces why no server paired the session."""
    dialer = FailoverRelayDialer([_RefusingDialer("relay_unavailable")])
    assert dialer.open(timeout=1.0) is None
    assert dialer.refusal == "relay_unavailable"


class _FakeWriter:
    """Minimal QUIC stream writer: records frames, never blocks on drain."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _FakeReader:
    """Replays the given lines, then reports EOF (``b""``)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeQuic:
    def __init__(self, reader: _FakeReader, writer: _FakeWriter) -> None:
        self._stream = (reader, writer)

    async def create_stream(self):
        return self._stream


class _QueuedReader:
    """Async reader backed by a queue; ``b""`` signals EOF."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def read(self, size: int = -1) -> bytes:
        return await self._queue.get()


class _SinkWriter:
    """Async writer that records close and never blocks on drain."""

    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> int:
        return len(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def test_a_unidirectional_session_survives_its_idle_clock() -> None:
    """The bridge idle watchdog counts *session* silence, not one direction.

    An upload streams bytes client→master while the reply leg stays quiet
    until the request ends. Activity in either direction keeps the whole
    session (and the silent leg) alive; only a session with no bytes in
    either direction idles out.
    """

    async def scenario() -> None:
        left_q: asyncio.Queue = asyncio.Queue()
        right_q: asyncio.Queue = asyncio.Queue()
        left_writer = _SinkWriter()
        right_writer = _SinkWriter()
        task = asyncio.create_task(
            bridge(
                _QueuedReader(left_q),
                left_writer,
                _QueuedReader(right_q),
                right_writer,
                idle_timeout=0.05,
            )
        )
        # Feed only the left→right leg for longer than the idle timeout.
        for _ in range(8):
            await left_q.put(b"x" * 1024)
            await asyncio.sleep(0.025)
        assert not task.done(), "the tunnel died while one leg was active"
        assert not left_writer.closed and not right_writer.closed
        # Closing one side still tears the session down.
        await left_q.put(b"")
        await asyncio.wait_for(task, 1.0)

    asyncio.run(scenario())


def test_a_fully_idle_session_is_torn_down() -> None:
    """A session with no bytes in either direction still idles out."""

    async def scenario() -> None:
        task = asyncio.create_task(
            bridge(
                _QueuedReader(asyncio.Queue()),
                _SinkWriter(),
                _QueuedReader(asyncio.Queue()),
                _SinkWriter(),
                idle_timeout=0.05,
            )
        )
        await asyncio.wait_for(task, 1.0)

    asyncio.run(scenario())


async def test_the_relay_acceptor_stops_when_its_control_stream_dies(monkeypatch) -> None:
    """A dropped control stream must end the acceptor, not leave a zombie.

    When the node drops the listener (a QUIC reset, an idle sweep) the incoming
    loop and the keepalive end, but ``_run`` used to keep waiting on ``stop``:
    the thread stayed alive with ``_registered`` still True, so ``active``
    reported a live listener that did not exist and the master could never
    re-register it — every relay-only peer stayed stranded. The acceptor must
    observe the dead stream and clear itself so the re-assertion can retry.
    """
    import contextlib

    from hsync_client.rdv_relay import RelayAcceptor

    listening = (
        json.dumps({"protocol_version": "rdv/1", "type": "relay_listening", "ttl": 60}) + "\n"
    ).encode()
    reader = _FakeReader([listening])  # ack, then immediate EOF: the node dropped us

    @contextlib.asynccontextmanager
    async def _fake_quic_session(client):
        yield _FakeQuic(reader, _FakeWriter())

    monkeypatch.setattr("hsync_client.rdv_relay._quic_session", _fake_quic_session)
    client = type("_Client", (), {"timeout": 5.0})()

    acceptor = RelayAcceptor(client, cid="A1b2C3d4E5f6G7h8I9j0K", bucket="b", local_port=9000)
    # ``_thread_main`` normally sets this before ``_run``; we drive ``_run``
    # directly, so provide the stop event it waits on.
    acceptor._stop = asyncio.Event()

    await asyncio.wait_for(acceptor._run(), timeout=5.0)

    assert acceptor._registered is False
    assert acceptor.active is False


def test_a_live_tunnel_outlives_a_new_plan_for_the_same_master() -> None:
    """The endpoint stays put while the tunnel is alive (no stale pool).

    A rendezvous plan is rebuilt on every cycle, but a tunnel still open to the
    same master keeps serving: replacing it would invalidate every connection the
    HTTP client and the WebSocket hold on its loopback port.
    """
    first = _Dialer(("127.0.0.1", 40000))
    second = _Dialer(("127.0.0.1", 40001))
    try:
        _relay_master_endpoint(_Plan(first), _Master(), log, _PEERS)
        assert _relay_master_endpoint(_Plan(second), _Master(), log, _PEERS) == ("127.0.0.1", 40000)
        assert first.closed is False
        assert second.opened is False
    finally:
        _close_relay_dialer()
    assert first.closed is True


def test_a_dropped_session_is_re_paired_on_the_same_endpoint(monkeypatch) -> None:
    """A session that dies is repaired underneath the same loopback port.

    The endpoint is what the HTTP client and the WebSocket hold pooled
    connections on, so re-binding it after a drop would strand them (the peer
    sees an SSL EOF): the tunnel re-pairs while the port stays put, and only a
    refused attempt arms the retry backoff.
    """
    import contextlib

    import hsync_client.rdv_relay as relay_mod
    from hsync_client.rdv_relay import RelayDialer

    ok = (
        json.dumps({"protocol_version": "rdv/1", "type": "relay_ok", "relay_id": "0" * 32}) + "\n"
    ).encode()

    class _ControlWriter(_FakeWriter):
        """Control stream whose drains start failing on demand."""

        def __init__(self) -> None:
            super().__init__()
            self.dead = False

        async def drain(self) -> None:
            if self.dead:
                raise ConnectionError("the session is gone")

    writers: list[_ControlWriter] = []
    sessions = 0

    @contextlib.asynccontextmanager
    async def _fake_quic_session(client):
        nonlocal sessions
        sessions += 1
        writer = _ControlWriter()
        writers.append(writer)
        yield _FakeQuic(_FakeReader([ok]), writer)

    monkeypatch.setattr(relay_mod, "_quic_session", _fake_quic_session)
    monkeypatch.setattr(relay_mod, "RELAY_KEEPALIVE_INTERVAL", 0.0)

    async def scenario() -> tuple[tuple[str, int] | None, tuple[str, int] | None, bool]:
        client = type("_Client", (), {"timeout": 5.0})()
        dialer = RelayDialer(
            client, cid=_Master.cid, bucket="b", target_cid="Zz9Yy8Xx7Ww6Vv5Uu4Tt3"
        )
        dialer._stop = asyncio.Event()
        task = asyncio.create_task(dialer._run())
        try:
            for _ in range(400):
                await asyncio.sleep(0.005)
                if dialer.session_up and dialer.endpoint is not None:
                    break
            before = dialer.endpoint
            writers[0].dead = True  # the QUIC session dies under the tunnel
            for _ in range(400):
                await asyncio.sleep(0.005)
                if sessions >= 2 and dialer.session_up:
                    break
            return before, dialer.endpoint, dialer.session_up
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    before, after, session_up = asyncio.run(scenario())

    assert sessions >= 2, "the dropped session was not re-paired"
    assert before is not None and before == after, "the endpoint moved on a repair"
    assert session_up is True


def test_an_adopted_tunnel_marks_its_target_alive() -> None:
    """An open tunnel is liveness evidence for the peer it reaches, and only then.

    The node pairs a relay session only for a target that has a listener, so an
    open tunnel proves the peer is registered and serving: it must hold the peer
    even while the discovery samples have stopped reporting it.
    """
    dialer = _Dialer(("127.0.0.1", 41234))
    plan = _Plan(dialer)

    assert _relay_master_endpoint(plan, _Master(), log, _PEERS) == ("127.0.0.1", 41234)
    assert _PEERS.relay == {_Master.cid: True}

    _close_relay_dialer()

    assert _PEERS.relay == {_Master.cid: False}


def test_a_relay_error_line_reports_its_uniform_code() -> None:
    """The refusal code is read from the validated frame, never guessed."""
    assert (
        refusal_code(
            json.dumps(
                {
                    "protocol_version": "rdv/1",
                    "type": "relay_error",
                    "error": "relay_unavailable",
                }
            ).encode()
        )
        == "relay_unavailable"
    )
    assert refusal_code(b"not-json") == "relay_unavailable"
    assert refusal_code(b"") == "relay_unavailable"


def test_the_dialer_keepalive_keeps_the_tunnel_alive(monkeypatch) -> None:
    """The dialer must ping the node so its 30 s QUIC idle does not kill the
    tunnel between data-plane sessions (the acceptor already does this; without
    it a relayed endpoint starts answering ``Connection refused`` mid-sync).

    ``RELAY_KEEPALIVE_INTERVAL`` (20 s) is shorter than the node's QUIC idle
    (30 s), so only a keepalive that actually sends frames closes the gap. This
    test drives the loop directly with a fake writer and a zero interval.
    """
    import hsync_client.rdv_relay as relay_mod

    dialer = RelayDialer(object(), cid=_Master.cid, bucket="b", target_cid="peer")
    writer = _FakeWriter()
    monkeypatch.setattr(relay_mod, "RELAY_KEEPALIVE_INTERVAL", 0.0)

    async def drive() -> None:
        task = asyncio.create_task(dialer._keepalive(writer))
        # Let the loop write at least one ping, then stop it.
        for _ in range(50):
            await asyncio.sleep(0.001)
            if writer.chunks:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert writer.chunks, "the dialer keepalive never wrote a frame"
    frame = json.loads(writer.chunks[0].decode())
    assert frame["type"] == "relay_ping"
    assert frame["cid"] == _Master.cid
    assert frame["bucket"] == "b"


def test_frame_line_validates_before_sending() -> None:
    """A malformed relay frame fails locally instead of being dropped remotely."""
    line = frame_line(
        {"protocol_version": "rdv/1", "type": "relay_ping", "cid": _Master.cid, "bucket": "b"}
    )
    assert line.endswith(b"\n")
    with pytest.raises(RendezvousProtocolError):
        frame_line({"protocol_version": "rdv/1", "type": "relay_ping", "cid": "short"})


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


def test_a_refusal_carries_the_wait_the_node_asked_for() -> None:
    """``rate_limited`` names the wait, and it is the only signal that the
    ``relay_connect`` budget is spent: the tunnel must honour it."""
    from hsync_client.rdv_relay import refusal_retry_after

    raw = json.dumps(
        {
            "protocol_version": "rdv/1",
            "type": "relay_error",
            "error": "rate_limited",
            "retry_after": 30,
        }
    ).encode()

    assert refusal_retry_after(raw) == 30
    assert refusal_code(raw) == "rate_limited"
    assert refusal_retry_after(b"not-json") is None
    assert refusal_retry_after(b"") is None


def test_a_failover_dialer_needs_at_least_one_server() -> None:
    """An empty set cannot pair anything: reject it instead of failing later."""
    with pytest.raises(ValueError):
        FailoverRelayDialer([])
