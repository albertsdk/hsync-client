# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the P2P startup clock-offset probe (ADR-0023)."""

import threading
import time
from unittest.mock import patch

import pytest
from hsync_common.clock import offset_store

from hsync_client import timesync


class TestRendezvousClockOffset:
    """The ack echo is a last-resort time reference (ADR-0055)."""

    @staticmethod
    def _now_z(offset_seconds: float = 0.0) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(time.time() + offset_seconds, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def test_establishes_trust_for_a_degraded_host(self):
        from datetime import UTC, datetime

        offset_store.clear()
        now = datetime.now(UTC).replace(microsecond=0)
        recorded = timesync.record_rendezvous_clock_offset(
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            sent_at=now.timestamp(),
            received_at=now.timestamp() + 0.2,
        )
        assert recorded is True
        assert offset_store.is_trusted() is True
        assert abs(offset_store.get()) < 2.0
        # Half the round-trip plus the one-second quantization of the echo.
        assert offset_store.uncertainty() == pytest.approx(0.6, abs=0.05)

    def test_never_refines_a_better_source(self):
        offset_store.clear()
        offset_store.record(0.25, rtt=0.02)
        assert (
            timesync.record_rendezvous_clock_offset(
                self._now_z(), sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.get() == 0.25

    def test_implausible_node_clock_is_refused(self):
        offset_store.clear()
        assert (
            timesync.record_rendezvous_clock_offset(
                "2000-01-01T00:00:00Z", sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.is_trusted() is False

    @pytest.mark.parametrize("value", [None, 12.0, "2026-01-01 00:00:00", "garbage"])
    def test_unusable_timestamp_is_ignored(self, value):
        offset_store.clear()
        assert (
            timesync.record_rendezvous_clock_offset(
                value, sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.is_trusted() is False


class TestStartNtpClockOffsetProbe:
    """Tests for start_ntp_clock_offset_probe (background SNTP probe)."""

    def test_probe_runs_on_background_daemon_thread(self):
        """The probe runs on a named daemon thread, not the caller's."""
        main_thread = threading.current_thread()
        started = threading.Event()

        def fake_measure(logger=None) -> tuple[float, float, str | None]:
            started.set()
            return 0.0, 0.0, None

        with patch.object(timesync, "_measure_ntp_offset", side_effect=fake_measure):
            thread = timesync.start_ntp_clock_offset_probe()
            try:
                assert thread.daemon is True
                assert thread.name == "hsync-ntp-probe"
                assert thread is not main_thread
                assert started.wait(2.0), "probe thread never started"
            finally:
                thread.join(2.0)
                assert not thread.is_alive()

    def test_start_returns_immediately_even_if_probe_blocks(self):
        """Startup is never blocked by a slow/unreachable NTP probe."""
        release = threading.Event()
        entered = threading.Event()

        def blocking_measure(logger=None) -> tuple[float, float, str | None]:
            entered.set()
            release.wait(5.0)
            return 0.0, 0.0, None

        with patch.object(timesync, "_measure_ntp_offset", side_effect=blocking_measure):
            thread = timesync.start_ntp_clock_offset_probe()
            try:
                assert entered.wait(2.0), "probe thread never entered the blocking measure"
                assert thread.is_alive()
            finally:
                release.set()
                thread.join(2.0)
                assert not thread.is_alive()

    def test_stale_probe_does_not_overwrite_newer_result(self):
        """A slower older worker must not replace a newer probe's offset."""
        first_entered = threading.Event()
        first_release = threading.Event()
        calls = 0

        def fake_measure(logger=None) -> tuple[float, float, str | None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                first_release.wait(5.0)
                return 99.0, 0.0, "stale"
            return 1.0, 0.0, "fresh"

        with patch.object(timesync, "_measure_ntp_offset", side_effect=fake_measure):
            stale = timesync.start_ntp_clock_offset_probe()
            assert first_entered.wait(2.0)
            fresh = timesync.start_ntp_clock_offset_probe()
            try:
                fresh.join(2.0)
                assert not fresh.is_alive()
                assert offset_store.get() == 1.0
            finally:
                first_release.set()
                stale.join(2.0)
                assert not stale.is_alive()
            assert offset_store.get() == 1.0


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _patch_authority(
    monkeypatch,
    *,
    master: bool = False,
    response=None,
    payload=None,
    raises=None,
) -> None:
    """Patch the authority probe's network/TLS layer for one test.

    ``master`` sets (or clears) the p2p master fingerprint so the probe takes the
    master trust path; ``payload`` builds a trusted 200 body, while ``response``/
    ``raises`` inject a raw response or a transport error.
    """
    from hsync_client.config import CONFIG

    if master:
        monkeypatch.setitem(CONFIG, "MASTER_CERT_FINGERPRINT", "f" * 64)
    else:
        monkeypatch.delitem(CONFIG, "MASTER_CERT_FINGERPRINT", raising=False)
    monkeypatch.setattr(timesync, "get_ssl_verify", lambda: True)
    if raises is not None:

        def _boom(*args, **kwargs):
            raise raises

        monkeypatch.setattr("hsync_client.utils.http_get", _boom)
        return
    if payload is not None:
        response = _FakeResponse(200, payload)
    monkeypatch.setattr("hsync_client.utils.http_get", lambda url, **kwargs: response)


def _past_the_floor() -> float:
    """A monotonic stamp beyond the probe retry floor.

    Anchored to ``time.monotonic()`` on purpose: it returns the host *uptime*, so
    a literal ``0.0`` still reads as "just probed" on a freshly booted machine
    (the GitHub runner case).
    """
    return time.monotonic() - 2 * timesync._PROBE_RETRY_SECONDS


class TestAuthorityClockOffset:
    """The authority (server/master) probe and its ``trusted`` contract."""

    def test_the_server_is_trusted_without_a_flag(self, monkeypatch):
        offset_store.clear()
        _patch_authority(monkeypatch, response=_FakeResponse(200, {"timestamp": time.time()}))
        assert timesync.probe_authority_clock_offset(server_url="https://srv") is True
        assert offset_store.is_trusted() is True

    def test_a_master_must_vouch_for_its_own_clock(self, monkeypatch):
        offset_store.clear()
        _patch_authority(
            monkeypatch,
            master=True,
            response=_FakeResponse(200, {"timestamp": time.time(), "trusted": False}),
        )
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is False

        offset_store.clear()
        _patch_authority(
            monkeypatch,
            master=True,
            response=_FakeResponse(200, {"timestamp": time.time(), "trusted": True}),
        )
        assert timesync.probe_authority_clock_offset(server_url="https://master") is True
        assert offset_store.is_trusted() is True

    def test_a_master_without_the_flag_is_untrusted(self, monkeypatch):
        offset_store.clear()
        _patch_authority(
            monkeypatch,
            master=True,
            response=_FakeResponse(200, {"timestamp": time.time()}),
        )
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is False

    def test_a_malformed_timestamp_degrades(self, monkeypatch):
        offset_store.clear()
        _patch_authority(monkeypatch, response=_FakeResponse(200, {"timestamp": "nope"}))
        assert timesync.probe_authority_clock_offset(server_url="https://srv") is False
        assert offset_store.is_trusted() is False

    def test_an_implausible_offset_is_refused(self, monkeypatch):
        offset_store.clear()
        _patch_authority(
            monkeypatch,
            response=_FakeResponse(200, {"timestamp": time.time() + 99_999}),
        )
        assert timesync.probe_authority_clock_offset(server_url="https://srv") is False
        assert offset_store.is_trusted() is False

    def test_the_probe_sends_the_configured_credential(self, monkeypatch):
        """Regression: a p2p master's ``/api/v1/time`` is an authenticated GET, so
        the probe must carry the bearer it already holds (an empty header set was
        answered 401, so the coarse rendezvous offset was never refined)."""
        from hsync_client.config import CONFIG

        offset_store.clear()
        monkeypatch.setitem(CONFIG, "MASTER_CERT_FINGERPRINT", "f" * 64)
        monkeypatch.setitem(CONFIG, "REQUEST_HEADERS", {"Authorization": "Bearer secret"})
        monkeypatch.setattr(timesync, "get_ssl_verify", lambda: True)
        seen: dict = {}

        def _capture(url, **kwargs):
            seen.update(kwargs)
            return _FakeResponse(200, {"timestamp": time.time(), "trusted": True})

        monkeypatch.setattr("hsync_client.utils.http_get", _capture)

        assert timesync.probe_authority_clock_offset(server_url="https://master") is True
        assert seen["headers"]["Authorization"] == "Bearer secret"


class TestNtpOptIn:
    def test_ntp_is_off_without_the_env(self, monkeypatch):
        monkeypatch.delenv("HSYNC_NTP_HOSTS", raising=False)
        assert timesync.ntp_configured() is False

    def test_ntp_is_on_when_the_env_is_set(self, monkeypatch):
        monkeypatch.setenv("HSYNC_NTP_HOSTS", "ntp.local")
        assert timesync.ntp_configured() is True

    def test_the_master_tick_does_not_probe_without_ntp(self, monkeypatch):
        monkeypatch.delenv("HSYNC_NTP_HOSTS", raising=False)
        offset_store.clear()
        calls = []
        monkeypatch.setattr(
            timesync, "start_ntp_clock_offset_probe", lambda *a, **k: calls.append(1)
        )
        timesync.maybe_refresh_clock_offset()
        assert calls == []


class TestRefreshAuthorityClockOffset:
    def test_noop_while_trusted(self, monkeypatch):
        offset_store.clear()
        offset_store.record(0.1, rtt=0.02)
        calls = []
        monkeypatch.setattr(
            timesync,
            "probe_authority_clock_offset",
            lambda *a, **k: calls.append(1) or True,
        )
        timesync.refresh_authority_clock_offset()
        assert calls == []

    def test_probes_when_untrusted(self, monkeypatch):
        offset_store.clear()
        calls = []
        monkeypatch.setattr(
            timesync,
            "probe_authority_clock_offset",
            lambda *a, **k: calls.append(1) or True,
        )
        monkeypatch.setattr(timesync, "_last_authority_probe_monotonic", _past_the_floor())
        timesync.refresh_authority_clock_offset()
        assert calls == [1]

    def test_the_first_probe_of_a_process_is_never_throttled(self, monkeypatch):
        # ``time.monotonic()`` is the host uptime, so the "never probed" sentinel
        # must sit a whole floor below uptime zero; ``0.0`` throttled a machine
        # booted seconds ago.
        assert timesync._NEVER_PROBED + timesync._PROBE_RETRY_SECONDS <= 0.0
        offset_store.clear()
        calls = []
        monkeypatch.setattr(
            timesync,
            "probe_authority_clock_offset",
            lambda *a, **k: calls.append(1) or True,
        )
        monkeypatch.setattr(timesync, "_last_authority_probe_monotonic", timesync._NEVER_PROBED)
        timesync.refresh_authority_clock_offset()
        assert calls == [1]

    def test_is_throttled_after_a_recent_probe(self, monkeypatch):
        offset_store.clear()
        calls = []
        monkeypatch.setattr(
            timesync,
            "probe_authority_clock_offset",
            lambda *a, **k: calls.append(1) or True,
        )
        monkeypatch.setattr(timesync, "_last_authority_probe_monotonic", time.monotonic())
        timesync.refresh_authority_clock_offset()
        assert calls == []


class TestMasterClobberGuard:
    """An unanchored p2p master must not replace a better trusted source."""

    def test_an_untrusted_master_keeps_the_trusted_offset(self, monkeypatch):
        _patch_authority(
            monkeypatch,
            master=True,
            response=_FakeResponse(200, {"timestamp": time.time(), "trusted": False}),
        )
        offset_store.clear()
        offset_store.record(0.42, rtt=0.01)
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is True
        assert offset_store.get() == pytest.approx(0.42)

    def test_a_failed_master_probe_keeps_the_trusted_offset(self, monkeypatch):
        _patch_authority(monkeypatch, master=True, raises=OSError("down"))
        offset_store.clear()
        offset_store.record(0.42, rtt=0.01)
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is True
        assert offset_store.get() == pytest.approx(0.42)

    def test_an_untrusted_master_degrades_an_untrusted_host(self, monkeypatch):
        _patch_authority(
            monkeypatch,
            master=True,
            response=_FakeResponse(200, {"timestamp": time.time(), "trusted": False}),
        )
        offset_store.clear()
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is False


class TestRefreshNtpFallback:
    def test_refresh_tries_ntp_even_when_the_authority_is_throttled(self, monkeypatch):
        offset_store.clear()
        monkeypatch.setenv("HSYNC_NTP_HOSTS", "ntp.local")
        calls: list[str] = []
        monkeypatch.setattr(
            timesync,
            "probe_authority_clock_offset",
            lambda *a, **k: calls.append("authority") or False,
        )
        monkeypatch.setattr(
            timesync, "start_ntp_clock_offset_probe", lambda *a, **k: calls.append("ntp")
        )
        monkeypatch.setattr(timesync, "_last_authority_probe_monotonic", time.monotonic())
        monkeypatch.setattr(timesync, "_last_probe_monotonic", _past_the_floor())
        timesync.refresh_authority_clock_offset()
        assert "authority" not in calls
        assert "ntp" in calls


class TestMasterSourceHandling:
    def test_a_trusted_master_records_its_source(self, monkeypatch):
        _patch_authority(
            monkeypatch,
            master=True,
            payload={"timestamp": time.time(), "trusted": True},
        )
        offset_store.clear()
        assert timesync.probe_authority_clock_offset(server_url="https://master") is True
        assert offset_store.source() == "master"

    def test_an_untrusted_master_revokes_a_master_derived_offset(self, monkeypatch):
        _patch_authority(
            monkeypatch,
            master=True,
            payload={"timestamp": time.time(), "trusted": False},
        )
        offset_store.clear()
        offset_store.record(0.5, rtt=0.01, source="master")
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is False

    def test_an_untrusted_master_keeps_an_ntp_offset(self, monkeypatch):
        _patch_authority(
            monkeypatch,
            master=True,
            payload={"timestamp": time.time(), "trusted": False},
        )
        offset_store.clear()
        offset_store.record(0.5, rtt=0.01, source="ntp")
        assert timesync.probe_authority_clock_offset(server_url="https://master") is False
        assert offset_store.is_trusted() is True
        assert offset_store.source() == "ntp"


class TestAuthorityProbeConcurrency:
    def test_a_request_during_an_inflight_probe_is_coalesced(self, monkeypatch):
        calls: list[int | None] = []
        entered = threading.Event()
        release = threading.Event()

        def _fake(logger=None, *, server_url=None, generation=None):
            calls.append(generation)
            if len(calls) == 1:
                entered.set()
                release.wait(5.0)
            return True

        monkeypatch.setattr(timesync, "probe_authority_clock_offset", _fake)
        monkeypatch.setattr(timesync, "_authority_probe_inflight", False)
        monkeypatch.setattr(timesync, "_authority_probe_pending", False)
        monkeypatch.setattr(timesync, "_authority_probe_generation", 0)

        first = timesync.start_authority_clock_offset_probe()
        assert first is not None
        assert entered.wait(2.0)
        assert timesync.start_authority_clock_offset_probe() is None
        release.set()
        first.join(5.0)
        assert not first.is_alive()
        assert len(calls) == 2
        assert calls[1] > calls[0]

    def test_a_stale_generation_is_discarded(self, monkeypatch):
        _patch_authority(monkeypatch, response=_FakeResponse(200, {"timestamp": time.time()}))
        monkeypatch.setattr(timesync, "_authority_probe_generation", 5)
        offset_store.clear()
        assert timesync.probe_authority_clock_offset(server_url="https://srv", generation=4) is True
        assert offset_store.get() is None


class TestNtpTargetsOptIn:
    """A malformed explicit source never falls back to the public defaults."""

    def test_a_malformed_explicit_source_yields_no_public_default(self, monkeypatch):
        monkeypatch.setenv("HSYNC_NTP_HOSTS", "bad:notaport")
        assert timesync._ntp_targets() == ()

    def test_a_valid_explicit_source_is_parsed(self, monkeypatch):
        monkeypatch.setenv("HSYNC_NTP_HOSTS", "ntp.local:8123")
        assert timesync._ntp_targets() == (("ntp.local", 8123),)

    def test_no_public_probe_for_a_malformed_source(self, monkeypatch):
        monkeypatch.setenv("HSYNC_NTP_HOSTS", "bad:notaport")

        def _no_targets(targets):
            assert targets == ()
            return None

        monkeypatch.setattr(timesync, "measure_clock_offset_ntp_multi", _no_targets)
        offset_store.clear()
        offset, rtt, host = timesync._measure_ntp_offset(timesync.log)
        assert offset == 0.0
        assert rtt is None
        assert host is None
        assert offset_store.is_trusted() is False
