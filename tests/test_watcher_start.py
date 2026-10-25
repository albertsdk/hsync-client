# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests for the file-watcher degradation (watchdog limits).

On a machine whose kernel inotify limits are exhausted (Errno 24 when
``fs.inotify.max_user_instances`` is full, Errno 28 when
``fs.inotify.max_user_watches`` is full), watchdog raises a bare ``OSError``
that previously surfaced as an opaque transport failure and blocked realtime
sync. The hardening under test:

1. ``watcher.watcher_oserror_class`` classifies the two inotify failures and the
   generic file-descriptor exhaustion;
2. ``watcher.WatcherManager`` downgrades to a ``PollingObserver`` on a limit (at
   startup and, deferred, at runtime), logs a parsable ``[WATCHER FALLBACK]`` /
   ``[FD EXHAUSTED]`` line, and restores inotify (``[WATCHER RESTORE]``) once
   the limits free up;
3. every retry loop (P2P master, P2P slave, remote continuous) applies the
   dedicated slow backoff instead of the normal cadence when both backends fail;
4. a backend swap racing ``stop()`` never adopts the freshly built observer:
   it is torn down instead, so no live watcher can outlive ``stop()``.
"""

import errno
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hsync_client.config import HsyncConfig
from hsync_client.constants import (
    WATCHER_START_RETRY_DELAY_S,
    WATCHER_UPGRADE_BACKOFF_FACTOR,
    WATCHER_UPGRADE_PROBE_INITIAL_S,
)
from hsync_client.exceptions import WatcherStartError
from hsync_client.watcher import WatcherLimit, WatcherManager, watcher_oserror_class


@pytest.fixture(autouse=True)
def _no_real_inventory_store(mock_inventory_store: MagicMock) -> None:
    """Mock servers never close a real store (see ``mock_inventory_store``)."""


# The kernel strings watchdog surfaces on Linux (inotify_init / inotify_add_watch)
# plus the generic descriptor-exhaustion failures.
_INSTANCE_ERRNO = OSError(errno.EMFILE, "inotify instance limit reached")
_WATCH_ERRNO = OSError(errno.ENOSPC, "inotify watch limit reached")
_FD_ERRNO = OSError(errno.EMFILE, "Too many open files")


class TestInotifyEventFilter:
    """The inotify watch filter excludes the no-op read-event classes.

    Regression for the burst-throttling storm: every download the P2P master
    serves opens and closes a bucket file without writing, and the default
    watchdog mask (FileOpenedEvent + FileClosedNoWriteEvent included) parses,
    queues and dispatches tens of thousands of those no-op events, stalling
    the real events behind them (~6x slower dispatch measured with downloads
    running). FileClosedEvent must stay: it is the file-ready signal after a
    save.
    """

    def test_inotify_schedule_carries_trimmed_filter(self):
        from watchdog.events import (
            FileClosedEvent,
            FileClosedNoWriteEvent,
            FileOpenedEvent,
        )

        from hsync_client.watcher import _INOTIFY_EVENT_FILTER

        filter_set = set(_INOTIFY_EVENT_FILTER)
        assert FileOpenedEvent not in filter_set, "open events must be excluded"
        assert FileClosedNoWriteEvent not in filter_set, "close-no-write events must be excluded"
        assert FileClosedEvent in filter_set, "close-write (file-ready) must stay"

        captured: dict[str, Any] = {}

        class _FakeObserver:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def schedule(self, handler: Any, path: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                pass

        manager = WatcherManager("/tmp/fake-bucket")
        manager._registrations.append((object(), "/tmp/fake-bucket", True))  # type: ignore[attr-defined]
        with patch(
            "hsync_client.watcher.WatcherManager._new_observer", return_value=_FakeObserver()
        ):
            manager._build_and_start("inotify")

        assert captured["event_filter"] == _INOTIFY_EVENT_FILTER

    def test_polling_schedule_carries_no_filter(self):
        from hsync_client.watcher import _INOTIFY_EVENT_FILTER

        captured: dict[str, Any] = {}

        class _FakeObserver:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def schedule(self, handler: Any, path: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            def start(self) -> None:
                pass

        manager = WatcherManager("/tmp/fake-bucket")
        manager._registrations.append((object(), "/tmp/fake-bucket", True))  # type: ignore[attr-defined]
        with patch(
            "hsync_client.watcher.WatcherManager._new_observer", return_value=_FakeObserver()
        ):
            manager._build_and_start("polling")

        assert captured["event_filter"] is None
        assert _INOTIFY_EVENT_FILTER  # the tuple itself is never empty


class TestWatcherOserrorClass:
    """Classifier: inotify limits, FD exhaustion, and pass-through."""

    def test_instance_limit(self):
        assert watcher_oserror_class(_INSTANCE_ERRNO) is WatcherLimit.INOTIFY_INSTANCES

    def test_watch_limit(self):
        assert watcher_oserror_class(_WATCH_ERRNO) is WatcherLimit.INOTIFY_WATCHES

    def test_emfile_fd_exhaustion(self):
        assert watcher_oserror_class(_FD_ERRNO) is WatcherLimit.FD_EXHAUSTION

    def test_enfile_fd_exhaustion(self):
        err = OSError(errno.ENFILE, "Too many open files in system")
        assert watcher_oserror_class(err) is WatcherLimit.FD_EXHAUSTION

    def test_other_oserror_returns_none(self):
        assert watcher_oserror_class(OSError(errno.EACCES, "Permission denied")) is None

    def test_non_oserror_returns_none(self):
        assert watcher_oserror_class(ValueError("inotify instance limit reached")) is None


def _build_raising(exc: BaseException):
    def _build(backend: str) -> None:
        raise exc

    return _build


def _manager_with_build(tmp_path, inotify_exc=None, poll_exc=None) -> WatcherManager:
    manager = WatcherManager(str(tmp_path))

    def _build(backend: str) -> MagicMock:
        exc = inotify_exc if backend == "inotify" else poll_exc
        observer = MagicMock()
        if exc is not None:
            # Stand-in for the real _build_and_start failing part-way: the
            # raised OSError must escape the build itself so WatcherManager
            # applies its in-flight failure handling.
            observer.start.side_effect = exc
            observer.start()  # raise now: the mock build fails in-flight
        return observer

    manager._build_and_start = _build  # type: ignore[method-assign]
    return manager


class TestWatcherManagerStart:
    """Startup: inotify success, downgrade to polling, and the typed failure."""

    def test_inotify_success(self, tmp_path):
        manager = _manager_with_build(tmp_path)
        handler = MagicMock()
        with (
            patch("hsync_client.watcher.warn_if_inotify_watch_limit_low"),
            patch("hsync_client.watcher.warn_if_open_fds_high"),
            patch.object(WatcherManager, "_schedule_supervisor") as schedule,
        ):
            manager.start(handler)
        assert manager._backend == "inotify"
        assert manager._observer is not None
        assert (handler, str(tmp_path), True) in manager._registrations
        schedule.assert_not_called()

    def test_inotify_watch_limit_downgrades_to_polling(self, tmp_path):
        manager = _manager_with_build(tmp_path, inotify_exc=_WATCH_ERRNO)
        with (
            patch("hsync_client.watcher.warn_if_inotify_watch_limit_low"),
            patch("hsync_client.watcher.warn_if_open_fds_high"),
            patch.object(WatcherManager, "_schedule_supervisor") as schedule,
            patch("hsync_client.watcher.log.warning") as warn,
        ):
            manager.start(MagicMock())
        assert manager._backend == "polling"
        schedule.assert_called_once()
        assert any("[WATCHER FALLBACK]" in str(c.args[0]) for c in warn.call_args_list if c.args)

    def test_fd_exhaustion_downgrades_to_polling(self, tmp_path):
        manager = _manager_with_build(tmp_path, inotify_exc=_FD_ERRNO)
        with (
            patch("hsync_client.watcher.warn_if_inotify_watch_limit_low"),
            patch("hsync_client.watcher.warn_if_open_fds_high"),
            patch.object(WatcherManager, "_schedule_supervisor"),
            patch("hsync_client.watcher.log.warning") as warn,
        ):
            manager.start(MagicMock())
        assert manager._backend == "polling"
        assert any("[FD EXHAUSTED]" in str(c.args[0]) for c in warn.call_args_list if c.args)

    def test_polling_also_fails_raises_watcher_start_error(self, tmp_path):
        manager = _manager_with_build(tmp_path, inotify_exc=_WATCH_ERRNO, poll_exc=_FD_ERRNO)
        with (
            patch("hsync_client.watcher.warn_if_inotify_watch_limit_low"),
            patch("hsync_client.watcher.warn_if_open_fds_high"),
            patch("hsync_client.watcher.log.error") as err,
        ):
            with pytest.raises(WatcherStartError) as excinfo:
                manager.start(MagicMock())
        assert "max_user_watches" in str(excinfo.value)
        assert excinfo.value.original is _WATCH_ERRNO
        err.assert_called_once()

    def test_other_oserror_propagates(self, tmp_path):
        manager = _manager_with_build(
            tmp_path, inotify_exc=OSError(errno.EACCES, "Permission denied")
        )
        with (
            patch("hsync_client.watcher.warn_if_inotify_watch_limit_low"),
            patch("hsync_client.watcher.warn_if_open_fds_high"),
        ):
            with pytest.raises(OSError) as excinfo:
                manager.start(MagicMock())
        assert excinfo.value.errno == errno.EACCES


class TestWatcherManagerScheduleDirectory:
    """Runtime dynamic watch: schedule, skip on a limit, re-raise otherwise."""

    def test_success_schedules_on_observer(self, tmp_path):
        from hsync_client.watcher import _INOTIFY_EVENT_FILTER

        manager = WatcherManager(str(tmp_path))
        observer = MagicMock()
        manager._observer = observer
        handler = MagicMock()
        manager.schedule_directory(handler, "/new/dir")
        observer.schedule.assert_called_once_with(
            handler, "/new/dir", recursive=True, event_filter=_INOTIFY_EVENT_FILTER
        )
        assert (handler, "/new/dir", True) in manager._registrations

    def test_inotify_limit_skips_and_requests_downgrade(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        observer = MagicMock()
        observer.schedule.side_effect = _WATCH_ERRNO
        manager._observer = observer
        with (
            patch.object(WatcherManager, "_request_downgrade") as request,
            patch("hsync_client.watcher.log.warning") as warn,
        ):
            manager.schedule_directory(MagicMock(), "/new/dir")
        request.assert_called_once()
        assert any("[WATCHER FALLBACK]" in str(c.args[0]) for c in warn.call_args_list if c.args)

    def test_other_oserror_raises(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        observer = MagicMock()
        observer.schedule.side_effect = OSError(errno.EACCES, "Permission denied")
        manager._observer = observer
        with pytest.raises(OSError) as excinfo:
            manager.schedule_directory(MagicMock(), "/new/dir")
        assert excinfo.value.errno == errno.EACCES


class TestWatcherManagerDowngrade:
    """Deferred downgrade: swap to polling at the safe point."""

    def test_swaps_to_polling_and_stops_old(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        polling = MagicMock()
        manager._observer = old
        manager._backend = "inotify"
        manager._pending_downgrade = True
        manager._build_and_start = lambda backend: polling  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.warning") as warn:
            manager._downgrade()
        assert manager._observer is polling
        assert manager._backend == "polling"
        assert manager._pending_downgrade is False
        old.stop.assert_called_once()
        assert any("[WATCHER FALLBACK]" in str(c.args[0]) for c in warn.call_args_list if c.args)

    def test_failure_keeps_inotify(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        manager._observer = old
        manager._backend = "inotify"
        manager._pending_downgrade = True
        manager._build_and_start = _build_raising(OSError(errno.ENFILE, "nope"))  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.error") as err:
            manager._downgrade()
        assert manager._observer is old
        assert manager._backend == "inotify"
        err.assert_called_once()


class TestWatcherManagerUpgrade:
    """Recovery: restore inotify once the limits free up."""

    def test_restores_inotify(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        inotify = MagicMock()
        manager._observer = old
        manager._backend = "polling"
        manager._probe_interval = 60.0
        manager._build_and_start = lambda backend: inotify  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.warning") as warn:
            manager._try_upgrade()
        assert manager._observer is inotify
        assert manager._backend == "inotify"
        assert manager._probe_interval == WATCHER_UPGRADE_PROBE_INITIAL_S
        old.stop.assert_called_once()
        assert any("[WATCHER RESTORE]" in str(c.args[0]) for c in warn.call_args_list if c.args)

    def test_failure_grows_backoff(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        manager._observer = MagicMock()
        manager._backend = "polling"
        manager._probe_interval = WATCHER_UPGRADE_PROBE_INITIAL_S
        manager._build_and_start = _build_raising(_WATCH_ERRNO)  # type: ignore[method-assign]
        manager._try_upgrade()
        assert manager._backend == "polling"
        assert (
            manager._probe_interval
            == WATCHER_UPGRADE_PROBE_INITIAL_S * WATCHER_UPGRADE_BACKOFF_FACTOR
        )

    def test_success_clears_pending_downgrade(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        manager._observer = MagicMock()
        manager._backend = "polling"
        manager._pending_downgrade = True
        inotify = MagicMock()
        manager._build_and_start = lambda backend: inotify  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.warning"):
            manager._try_upgrade()
        assert manager._backend == "inotify"
        # A downgrade request that landed while polling is stale once the
        # fresh inotify build succeeded: keeping it would flip the watcher
        # straight back to polling on the next supervisor tick.
        assert manager._pending_downgrade is False


class TestWatcherManagerStopRace:
    """A stop() landing while a backend build is in flight must not adopt the
    fresh observer: it is torn down instead, so no live watcher (and no
    duplicate event delivery) can outlive stop()."""

    def test_stop_during_downgrade_discards_candidate(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        candidate = MagicMock()
        manager._observer = old
        manager._backend = "inotify"
        manager._pending_downgrade = True

        def _build(backend: str) -> MagicMock:
            manager.stop()  # stop() lands mid-build (real builds walk the tree)
            return candidate

        manager._build_and_start = _build  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.warning") as warn:
            manager._downgrade()
        assert manager._observer is None, "stop() must not be overridden by the swap"
        candidate.stop.assert_called_once()
        logged = [str(c.args[0]) for c in warn.call_args_list if c.args]
        assert not any("[WATCHER FALLBACK]" in w for w in logged)

    def test_stop_during_upgrade_discards_candidate(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        candidate = MagicMock()
        manager._observer = old
        manager._backend = "polling"
        manager._probe_interval = 60.0

        def _build(backend: str) -> MagicMock:
            manager.stop()  # stop() lands mid-build (real builds walk the tree)
            return candidate

        manager._build_and_start = _build  # type: ignore[method-assign]
        with patch("hsync_client.watcher.log.warning") as warn:
            manager._try_upgrade()
        assert manager._observer is None, "stop() must not be overridden by the swap"
        candidate.stop.assert_called_once()
        logged = [str(c.args[0]) for c in warn.call_args_list if c.args]
        assert not any("[WATCHER RESTORE]" in w for w in logged)


class TestWatcherManagerDowngradeBackoff:
    """Downgrade retries grow at the same capped cadence as upgrade probes."""

    def test_downgrade_failure_keeps_backend_and_grows_backoff(self, tmp_path):
        manager = WatcherManager(str(tmp_path))
        old = MagicMock()
        manager._observer = old
        manager._backend = "inotify"
        manager._pending_downgrade = True
        manager._probe_interval = WATCHER_UPGRADE_PROBE_INITIAL_S
        manager._build_and_start = _build_raising(OSError(errno.ENFILE, "nope"))  # type: ignore[method-assign]
        with (
            patch("hsync_client.watcher.log.error") as err,
            patch.object(WatcherManager, "_schedule_supervisor"),
        ):
            manager._downgrade()
        err.assert_called_once()
        assert manager._backend == "inotify"
        assert manager._observer is old
        assert (
            manager._probe_interval
            == WATCHER_UPGRADE_PROBE_INITIAL_S * WATCHER_UPGRADE_BACKOFF_FACTOR
        )


def _p2p_config(tmp_path, **overrides: Any) -> HsyncConfig:
    """Minimal config for p2p_handler functions (mirrors test_p2p_handler_advanced)."""
    defaults: dict[str, Any] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "P2P_LISTEN_PORT": 9000,
        "P2P_GROUP": None,
        "LOCAL_BUCKET": str(tmp_path),
        "P2P_KEY": None,
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
    master.connected = 0
    master.compression_enabled = False
    master.delta_enabled = False
    master.get_server_url.return_value = "https://127.0.0.1:9000"
    return master


class TestRunAsMasterWatcherBackoff:
    """A watcher that cannot start tears the half-started WSS server and backs off."""

    def test_master_watcher_failure_stops_ws_and_backs_off(self, tmp_path):
        from hsync_client.p2p_handler import _run_as_master

        config = _p2p_config(tmp_path)
        p2p_sync = MagicMock()
        p2p_sync.port = 9000
        existing_server = MagicMock()
        ws_instance = MagicMock()
        # The control plane is installed on the unified listener; a bare
        # MagicMock would look "running" and skip the watcher-start path.
        ws_instance.running = False
        ws_instance.start.side_effect = WatcherStartError("inotify")

        sleep_calls: list[float] = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            result = _run_as_master(
                config=config,
                log=MagicMock(),
                p2p_sync=p2p_sync,
                p2p_server=existing_server,
                p2p_ws_server=ws_instance,
            )

        assert result == (existing_server, ws_instance), (
            "the control plane stays attached to the unified server, not restarted"
        )
        ws_instance.stop.assert_called_once()
        existing_server.stop_serving.assert_any_call()
        assert sleep_calls == [WATCHER_START_RETRY_DELAY_S], (
            "the retry must use the dedicated slow backoff, not the discovery cadence"
        )

    def test_watcher_failure_retires_http_role_before_ws_stop(self, tmp_path):
        """The HTTP role is retired before the control plane clears the registry."""
        from hsync_client.p2p_handler import _run_as_master

        config = _p2p_config(tmp_path)
        p2p_sync = MagicMock()
        p2p_sync.port = 9000
        order: list[str] = []
        server = MagicMock()
        server.stop_serving.side_effect = lambda: order.append("http_off")
        ws = MagicMock()
        ws.running = False
        ws.start.side_effect = WatcherStartError("inotify")
        ws.stop.side_effect = lambda: order.append("ws_stop")

        with patch("time.sleep"):
            _run_as_master(
                config=config,
                log=MagicMock(),
                p2p_sync=p2p_sync,
                p2p_server=server,
                p2p_ws_server=ws,
            )

        assert order == ["http_off", "ws_stop"], (
            "the master HTTP role must stop BEFORE the control plane clears the CID registry"
        )

    def test_master_ready_signal_skipped_on_watcher_failure(self, tmp_path):
        """server_ready is not advertised when the watcher could not start."""
        from hsync_client.p2p_handler import _run_as_master

        config = _p2p_config(tmp_path)
        p2p_sync = MagicMock()
        p2p_sync.port = 9000
        p2p_server = MagicMock()
        ws_instance = MagicMock()
        ws_instance.running = False
        ws_instance.start.side_effect = WatcherStartError("inotify")

        with patch("time.sleep"):
            _run_as_master(
                config=config,
                log=MagicMock(),
                p2p_sync=p2p_sync,
                p2p_server=p2p_server,
                p2p_ws_server=ws_instance,
            )

        p2p_sync.update_server_ready.assert_not_called()

    def test_master_does_not_rebind_the_listener(self, tmp_path):
        """The listener is bound once at startup; the master only installs its role."""
        from hsync_client.p2p_handler import _run_as_master

        config = _p2p_config(tmp_path)
        p2p_sync = MagicMock()
        p2p_sync.port = 9000
        server = MagicMock()
        ws = MagicMock()
        ws.running = False
        ws.connected_clients = 0
        ws.start.side_effect = lambda **kwargs: setattr(ws, "running", True)

        with patch("time.sleep"):
            _run_as_master(
                config=config,
                log=MagicMock(),
                p2p_sync=p2p_sync,
                p2p_server=server,
                p2p_ws_server=ws,
            )

        server.start.assert_not_called()
        p2p_sync.update_server_ready.assert_called_once()


class TestRunAsSlaveWatcherBackoff:
    """A slave whose watcher cannot start backs off before re-entering discovery."""

    def test_slave_watcher_failure_backs_off_and_loops(self, tmp_path):
        from hsync_client.p2p_handler import _run_as_slave

        config = _p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _master_peer()
        mock_log = MagicMock()

        sleep_calls: list[float] = []
        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            # Mocked asyncio.run must not receive real coroutines: patched as
            # MagicMocks, no un-awaited coroutine objects are created.
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, WatcherStartError("inotify")]),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = _run_as_slave(config, mock_log, False, peer_mgr)

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
        from hsync_client.p2p_handler import _run_as_slave

        config = _p2p_config(tmp_path, ONE_SHOT=False)
        peer_mgr = MagicMock()
        peer_mgr.get_master.return_value = _master_peer()

        with (
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("hsync_client.p2p_handler.p2p_websocket_client", new=MagicMock()),
            patch("hsync_client.p2p_handler._with_stdin_commands", new=MagicMock()),
            patch("asyncio.run", side_effect=[None, RuntimeError("boom")]),
            patch("time.sleep") as mock_sleep,
        ):
            result = _run_as_slave(config, MagicMock(), False, peer_mgr)

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
