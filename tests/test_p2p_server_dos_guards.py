"""Regression tests for the P2P listener DoS guards (M2).

The root cause was the per-I/O idle timeout resetting on any byte: a peer
trickling one byte per <300 s held a worker slot forever. The throughput floor
in ``_AiohttpTransport._check_progress`` is the fix; these tests pin it without a
network.
"""

from __future__ import annotations

import time

import pytest

from hsync_client.p2p_server import (
    P2P_REQUEST_GRACE_S,
    P2P_REQUEST_MIN_RATE_BPS,
    _AiohttpTransport,
)


class _FakeUrl:
    raw_path_qs = "/api/v1/health"


class _FakeVersion:
    major = 1
    minor = 1


class _FakeRequest:
    method = "GET"
    rel_url = _FakeUrl()
    version = _FakeVersion()
    headers: dict[str, str] = {}
    remote = "192.0.2.10"


def _transport() -> _AiohttpTransport:
    return _AiohttpTransport(_FakeRequest(), None, None)  # type: ignore[arg-type]


def test_trickle_below_the_floor_is_aborted() -> None:
    transport = _transport()
    transport._first_move_at = time.monotonic() - (P2P_REQUEST_GRACE_S + 30.0)
    transport._moved = 1  # one byte, then stalled
    with pytest.raises(TimeoutError):
        transport._check_progress()


def test_throttled_upload_at_the_cli_floor_is_not_killed() -> None:
    transport = _transport()
    elapsed = P2P_REQUEST_GRACE_S + 300.0
    transport._first_move_at = time.monotonic() - elapsed
    transport._moved = int(1024 * elapsed)  # 1 KB/s, the CLI floor
    transport._check_progress()  # must not raise


def test_long_compute_before_the_first_byte_is_not_aborted() -> None:
    """A handler that hashes a large file for minutes before its first write
    must not be aborted just because no byte moved yet."""
    transport = _transport()
    transport._check_progress()  # _first_move_at is None → no raise, ever


def test_floor_is_far_below_the_cli_floor() -> None:
    assert P2P_REQUEST_MIN_RATE_BPS < 1024


def test_inflight_cap_does_not_throttle_a_legitimate_peer() -> None:
    """The per-IP in-flight cap must stay above the client's own maximum
    transfer concurrency plus room for metadata requests."""
    from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_MAX
    from hsync_client.p2p_server import P2P_MAX_INFLIGHT_PER_IP

    assert P2P_MAX_INFLIGHT_PER_IP > MAX_CONCURRENT_TRANSFERS_MAX


def test_per_ip_cap_is_shared_fairly_across_active_peers() -> None:
    from hsync_client.p2p_server import (
        P2P_MAX_INFLIGHT_PER_IP,
        P2P_MIN_INFLIGHT_PER_IP,
        P2P_SERVER_MAX_THREADS,
        _per_ip_inflight_cap,
    )

    assert _per_ip_inflight_cap(1) == P2P_MAX_INFLIGHT_PER_IP
    assert _per_ip_inflight_cap(2) == P2P_MAX_INFLIGHT_PER_IP
    # Many peers each get at least the floor, never more than the ceiling.
    assert _per_ip_inflight_cap(P2P_SERVER_MAX_THREADS) == P2P_MIN_INFLIGHT_PER_IP
    assert _per_ip_inflight_cap(10_000) == P2P_MIN_INFLIGHT_PER_IP
    for active in range(1, P2P_SERVER_MAX_THREADS * 2):
        cap = _per_ip_inflight_cap(active)
        assert P2P_MIN_INFLIGHT_PER_IP <= cap <= P2P_MAX_INFLIGHT_PER_IP


def test_tls_handshake_timeout_is_bounded() -> None:
    from aiohttp import web

    from hsync_client.p2p_server import P2P_TLS_HANDSHAKE_TIMEOUT_S, _P2PSite

    assert 0 < P2P_TLS_HANDSHAKE_TIMEOUT_S < 60  # below the asyncio default
    assert issubclass(_P2PSite, web.SockSite)


class _AdmissionRequest:
    """Just the ``query``/``headers`` mappings ``_admission_key`` reads."""

    def __init__(self, query: dict[str, str], headers: dict[str, str] | None = None) -> None:
        self.query = query
        self.headers = headers or {}


_CID = "Afgx6i95EChYuFuUMe4ay"


def test_a_relayed_peer_is_bucketed_by_its_cid() -> None:
    """Relayed peers share the loopback address: key them by identity.

    Otherwise every relayed slave lands in one rate/in-flight bucket and a busy
    one starves the others.
    """
    from hsync_client.p2p_server import _admission_key

    assert _admission_key("127.0.0.1", _AdmissionRequest({"cid": _CID})) == f"relay:{_CID}"
    assert _admission_key("::1", _AdmissionRequest({"cid": _CID})) == f"relay:{_CID}"


def test_the_client_id_header_keys_a_loopback_request_without_a_cid_param() -> None:
    """Requests whose URL carries no ``cid`` still separate by the header."""
    from hsync_client.p2p_server import _admission_key

    request = _AdmissionRequest({}, {"X-Client-Id": _CID})
    assert _admission_key("127.0.0.1", request) == f"relay:{_CID}"


def test_the_query_cid_wins_over_the_header() -> None:
    from hsync_client.p2p_server import _admission_key

    other = "Bfgx6i95EChYuFuUMe4ay"
    request = _AdmissionRequest({"cid": _CID}, {"X-Client-Id": other})
    assert _admission_key("127.0.0.1", request) == f"relay:{_CID}"


def test_a_direct_peer_keeps_its_address_as_the_bucket() -> None:
    from hsync_client.p2p_server import _admission_key

    assert _admission_key("192.168.1.50", _AdmissionRequest({"cid": _CID})) == "192.168.1.50"


def test_a_loopback_request_without_a_valid_cid_falls_back_to_the_address() -> None:
    from hsync_client.p2p_server import _admission_key

    assert _admission_key("127.0.0.1", _AdmissionRequest({})) == "127.0.0.1"
    assert _admission_key("127.0.0.1", _AdmissionRequest({"cid": "not-a-cid"})) == "127.0.0.1"
