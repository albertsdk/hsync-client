# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Regression tests for the watcher-start hardening (inotify limits).

On a machine whose kernel inotify limits are exhausted (e.g. Errno 24 when
``fs.inotify.max_user_instances`` is full), watchdog raises a bare ``OSError``
that previously surfaced as an opaque transport failure: the LAN slave logged
a generic ``WebSocket connection ended: OSError: [Errno 24] inotify instance
limit reached`` and looped at the discovery cadence (~10 s) without any
remediation hint. The hardening under test:

1. ``utils.inotify_exhaustion_hint`` classifies the two inotify failures;
2. ``utils.start_observer_guarded`` logs a parsable ``[WATCHER ERROR]`` line
   with the remediation hint and raises ``WatcherStartError``;
3. every retry loop (LAN master, LAN slave, remote continuous) applies the
   dedicated slow backoff instead of the normal cadence.

The pre-fix behavior fails assertions 2 and 3 (no parsable line, no backoff).
"""

import errno
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hsync_client.config import HsyncConfig
from hsync_client.constants import WATCHER_START_RETRY_DELAY_S
from hsync_client.exceptions import WatcherStartError
from hsync_client.utils import inotify_exhaustion_hint, start_observer_guarded

# The kernel strings watchdog surfaces on Linux (inotify_init / inotify_add_watch).
_INSTANCE_ERRNO = OSError(errno.EMFILE, "inotify instance limit reached")
_WATCH_ERRNO = OSError(errno.ENOSPC, "inotify watch limit reached")


class TestInotifyExhaustionHint:
    """Classifier: only the two inotify-limit errors produce a hint."""

    def test_instance_limit_hint(self):
        hint = inotify_exhaustion_hint(_INSTANCE_ERRNO)
        assert hint is not None
        assert "max_user_instances" in hint

    def test_watch_limit_hint(self):
        hint = inotify_exhaustion_hint(_WATCH_ERRNO)
        assert hint is not None
        assert "max_user_watches" in hint

    def test_other_oserror_returns_none(self):
        assert inotify_exhaustion_hint(OSError(errno.EACCES, "Permission denied")) is None

    def test_emfile_without_inotify_returns_none(self):
        # A plain too-many-open-files is not necessarily inotify: the caller
        # keeps its generic handling.
        assert inotify_exhaustion_hint(OSError(errno.EMFILE, "Too many open files")) is None

    def test_non_oserror_returns_none(self):
        assert inotify_exhaustion_hint(ValueError("inotify instance limit reached")) is None


class TestStartObserverGuarded:
    """The guarded start: classify, log parsably, raise typed."""

    def _observer_raising(self, exc: BaseException) -> MagicMock:
        observer = MagicMock()
        observer.start.side_effect = exc
        return observer

    def test_inotify_instance_failure_raises_watcher_start_error(self, tmp_path):
        observer = self._observer_raising(_INSTANCE_ERRNO)
        with patch("hsync_client.utils.log.error") as mock_error:
            with pytest.raises(WatcherStartError) as excinfo:
                start_observer_guarded(observer, MagicMock(), str(tmp_path))
        # Parsable label + path + hint in one log line.
        logged = mock_error.call_args[0][0]
        assert "[WATCHER ERROR]" in logged
        assert str(tmp_path) in logged
        assert "max_user_instances" in logged
        # The raw kernel error rides along for callers that need it.
        assert excinfo.value.original is _INSTANCE_ERRNO
        observer.schedule.assert_called_once()

    def test_inotify_watch_failure_raises_watcher_start_error(self, tmp_path):
        observer = self._observer_raising(_WATCH_ERRNO)
        with patch("hsync_client.utils.log.error"):
            with pytest.raises(WatcherStartError) as excinfo:
                start_observer_guarded(observer, MagicMock(), str(tmp_path))
        assert "max_user_watches" in str(excinfo.value)

    def test_other_oserror_propagates_unchanged(self, tmp_path):
        observer = self._observer_raising(OSError(errno.EACCES, "Permission denied"))
        with patch("hsync_client.utils.log.error") as mock_error:
            with pytest.raises(OSError) as excinfo:
                start_observer_guarded(observer, MagicMock(), str(tmp_path))
        assert excinfo.value.errno == errno.EACCES
        mock_error.assert_not_called()

    def test_success_calls_schedule_and_start(self, tmp_path):
        observer = MagicMock()
        start_observer_guarded(observer, MagicMock(), str(tmp_path))
        observer.schedule.assert_called_once()
        observer.start.assert_called_once()

    def test_recursive_flag_forwarded(self, tmp_path):
        observer = MagicMock()
        start_observer_guarded(observer, MagicMock(), str(tmp_path), recursive=False)
        assert observer.schedule.call_args.kwargs["recursive"] is False


def _lan_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Minimal config for lan_handler functions (mirrors test_lan_handler_advanced)."""
    defaults: dict[str, Any] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "LAN_PORT": 9000,
        "LAN_GROUP": None,
        "LOCAL_BUCKET": str(tmp_path),
        "LAN_KEY": None,
        "DISCOVERY_TIMEOUT": 1,
        "DRYRUN": False,
        "ONE_SHOT": True,
        "REQUEST_HEADERS": {},
        "API": {"buckets": "", "events": ""},
        "SERVER_COMPRESSION_ENABLED": False,
        "MASTER_CERT_FINGERPRINT": None,
        "SERVER_DELTA_ENABLED": False,
    }
    defaults.update(overrides)
    return cast(HsyncConfig, defaults)


def _master_peer() -> MagicMock:
    master = MagicMock()
    master.cid = "master-1"
    master.server_ready = True
    master.use_https = True
    master.cert_fingerprint = "a" * 40
    master.port = 9000
    master.address = "127.0.0.1"
    master.compression_enabled = False
    master.delta_enabled = False
    master.get_server_url.return_value = "https://127.0.0.1:9000"
    return master


class TestRunAsMasterWatcherBackoff:
    """A watcher that cannot start tears the half-started WSS server and backs off."""

    def test_master_watcher_failure_stops_ws_and_backs_off(self, tmp_path):
        from hsync_client.lan_handler import _run_as_master

        config = _lan_config(tmp_path)
        lan_sync = MagicMock()
        lan_sync.port = 9000
        existing_server = MagicMock()
        ws_instance = MagicMock()
        ws_instance.start.side_effect = WatcherStartError("inotify")

        sleep_calls: list[float] = []
        with (
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=ws_instance),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = _run_as_master(
                config=config,
                log=MagicMock(),
                lan_sync=lan_sync,
                cert_info=MagicMock(),
                lan_server=existing_server,
                lan_ws_server=None,
            )

        assert result == (existing_server, None), (
            "the half-started WSS server must not be handed back to the discovery loop"
        )
        ws_instance.stop.assert_called_once()
        assert sleep_calls == [WATCHER_START_RETRY_DELAY_S], (
            "the retry must use the dedicated slow backoff, not the discovery cadence"
        )

    def test_master_ready_signal_skipped_on_watcher_failure(self, tmp_path):
        """server_ready is not advertised when the watcher could not start."""
        from hsync_client.lan_handler import _run_as_master

        config = _lan_config(tmp_path)
        lan_sync = MagicMock()
        lan_sync.port = 9000
        ws_instance = MagicMock()
        ws_instance.start.side_effect = WatcherStartError("inotify")

        with (
            patch("hsync_client.lan_handler.LANWebSocketServer", return_value=ws_instance),
            patch("time.sleep"),
        ):
            _run_as_master(
                config=config,
                log=MagicMock(),
                lan_sync=lan_sync,
                cert_info=MagicMock(),
                lan_server=MagicMock(),
                lan_ws_server=None,
            )

        lan_sync.update_server_ready.assert_not_called()


class TestRunAsSlaveWatcherBackoff:
    """A slave whose watcher cannot start backs off before re-entering discovery."""

    def test_slave_watcher_failure_backs_off_and_loops(self, tmp_path):
        from hsync_client.lan_handler import _run_as_slave

        config = _lan_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _master_peer()
        mock_log = MagicMock()

        sleep_calls: list[float] = []
        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            # Mocked asyncio.run must not receive real coroutines: patched as
            # MagicMocks, no un-awaited coroutine objects are created.
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, WatcherStartError("inotify")]),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = _run_as_slave(config, mock_log, MagicMock(), None, False, peer_mgr)

        assert result == (True, 0), "continuous mode must survive the watcher failure"
        assert sleep_calls == [WATCHER_START_RETRY_DELAY_S], (
            "the retry must use the dedicated slow backoff, not the discovery cadence"
        )
        warnings = [str(c.args[0]) for c in mock_log.warning.call_args_list if c.args]
        assert not any("WebSocket connection ended" in w for w in warnings), (
            "a watcher failure must not be masked as a generic WebSocket disconnect"
        )

    def test_slave_generic_ws_error_keeps_old_fast_loop(self, tmp_path):
        """Non-watcher WebSocket failures keep the pre-existing generic retry."""
        from hsync_client.lan_handler import _run_as_slave

        config = _lan_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _master_peer()

        with (
            patch("hsync_client.lan_handler.set_config"),
            patch("hsync_client.lan_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.lan_handler.sync", new=MagicMock()),
            patch("hsync_client.lan_handler.lan_websocket_client", new=MagicMock()),
            patch("hsync_client.lan_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, RuntimeError("boom")]),
            patch("time.sleep") as mock_sleep,
        ):
            result = _run_as_slave(config, MagicMock(), MagicMock(), None, False, peer_mgr)

        assert result == (True, 0)
        mock_sleep.assert_not_called()


class TestRemoteConnectWatcherBackoff:
    """remote_connect: a WatcherStartError retries with the exponential backoff."""

    @pytest.mark.asyncio
    async def test_remote_watcher_failure_reconnects_with_backoff(self):
        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": None, "RECONNECT_DELAY": 0.01}

        attempts: list[int] = []

        async def mock_sync():
            return None

        async def mock_remote_events():
            attempts.append(1)
            if len(attempts) == 1:
                raise WatcherStartError("inotify")
            raise KeyboardInterrupt("stop loop")

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
            patch("hsync_client.remote_websocket.emit_event"),
        ):
            import contextlib

            with contextlib.suppress(KeyboardInterrupt):
                await remote_connect()

        assert len(attempts) == 2, (
            "the loop must retry after the watcher failure instead of terminating"
        )
