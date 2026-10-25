"""
Tests for hsync_client.stdin_commands module.

Tests the StdinCommandHandler: registration, dispatch, stop mechanism,
JSON parsing, and error handling.
"""

import asyncio
import contextlib
import os
import stat
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hsync_client.stdin_commands import StdinCommandHandler


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until it is true or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class TestStdinCommandHandlerInit:
    """Tests for StdinCommandHandler initialization."""

    def test_init_creates_empty_handlers(self):
        """Handler dict is empty on creation."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        assert handler._handlers == {}

    def test_init_should_stop_false(self):
        """_should_stop is False on creation."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        assert handler._should_stop is False


class TestRegister:
    """Tests for command registration."""

    def test_register_sync_handler(self):
        """Sync handler can be registered."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("TEST", callback)
        assert "TEST" in handler._handlers
        assert handler._handlers["TEST"] is callback

    def test_register_async_handler(self):
        """Async handler can be registered."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = AsyncMock()
        handler.register("ASYNC_TEST", callback)
        assert "ASYNC_TEST" in handler._handlers

    def test_register_multiple_handlers(self):
        """Multiple handlers can be registered."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        handler.register("CMD1", MagicMock())
        handler.register("CMD2", MagicMock())
        assert len(handler._handlers) == 2

    def test_register_overwrites(self):
        """Registering same command overwrites previous handler."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        first = MagicMock()
        second = MagicMock()
        handler.register("CMD", first)
        handler.register("CMD", second)
        assert handler._handlers["CMD"] is second


class TestStop:
    """Tests for stop mechanism."""

    def test_stop_sets_flag(self):
        """stop() sets _should_stop to True."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        handler.stop()
        assert handler._should_stop is True


def _make_reader(*lines: bytes) -> AsyncMock:
    """Return a mock StreamReader whose ``read(1)`` yields individual bytes.

    ``_BoundedLineReader`` reads byte-by-byte via ``reader.read(1)``,
    so the mock must return single-byte ``bytes`` objects, not lines.
    """
    byte_list: list[bytes] = []
    for line in lines:
        byte_list.extend(line[i : i + 1] for i in range(len(line)))
    byte_list.append(b"")  # EOF
    reader = AsyncMock()
    reader.read = AsyncMock(side_effect=byte_list)
    return reader


def _patch_pipe_setup(reader: AsyncMock):
    """Return a combined context-manager that patches the pipe setup path of
    ``run()``: ``os.dup``, ``os.fdopen``, ``asyncio.StreamReader`` (to return
    *reader*), ``connect_read_pipe`` via a mock event loop, and the stdin-type
    pre-check (a FIFO that is not a TTY, so the reader proceeds)."""
    mock_loop = MagicMock()
    mock_loop.connect_read_pipe = AsyncMock()

    class _FifoStat:
        st_mode = stat.S_IFIFO

    stack = contextlib.ExitStack()
    stack.enter_context(patch("os.dup", return_value=99))
    stack.enter_context(patch("os.fdopen", return_value=MagicMock()))
    stack.enter_context(patch("asyncio.StreamReader", return_value=reader))
    stack.enter_context(patch("asyncio.StreamReaderProtocol"))
    stack.enter_context(patch("asyncio.get_running_loop", return_value=mock_loop))
    stack.enter_context(patch("os.fstat", return_value=_FifoStat()))
    stack.enter_context(patch("os.isatty", return_value=False))
    return stack


class TestRunDispatch:
    """Tests for the run() async method - full dispatch loop."""

    @pytest.mark.asyncio
    async def test_dispatches_sync_handler(self):
        """Sync handler is called when a matching command arrives."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("PREVIEW", callback)

        reader = _make_reader(b'{"cmd": "PREVIEW"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        callback.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_dispatches_async_handler(self):
        """Async handler is awaited when a matching command arrives."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = AsyncMock()
        handler.register("SYNC", callback)

        reader = _make_reader(b'{"cmd": "SYNC"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        callback.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_dispatches_with_extra_kwargs(self):
        """Extra JSON fields are passed as keyword arguments."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("RESYNC", callback)

        reader = _make_reader(b'{"cmd": "RESYNC", "filter": "docs/"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        callback.assert_called_once_with(filter="docs/")

    @pytest.mark.asyncio
    async def test_invalid_json_logs_warning(self):
        """Invalid JSON line logs a warning and continues."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("OK", callback)

        reader = _make_reader(b"not json\n", b'{"cmd": "OK"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("Invalid JSON" in str(c) for c in log.warning.call_args_list)
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_cmd_field_logs_warning(self):
        """JSON without 'cmd' field logs a warning and continues."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("OK", callback)

        reader = _make_reader(b'{"action": "test"}\n', b'{"cmd": "OK"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("missing 'cmd'" in str(c) for c in log.warning.call_args_list)
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_command_logs_warning(self):
        """Unregistered command logs a warning and continues."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("KNOWN", callback)

        reader = _make_reader(b'{"cmd": "UNKNOWN"}\n', b'{"cmd": "KNOWN"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("Unknown stdin command" in str(c) for c in log.warning.call_args_list)
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_handler_exception_logs_error(self):
        """Exception in handler is caught and logged."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock(side_effect=RuntimeError("boom"))
        handler.register("FAIL", callback)

        reader = _make_reader(b'{"cmd": "FAIL"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("'FAIL' failed" in str(c) for c in log.error.call_args_list)

    @pytest.mark.asyncio
    async def test_async_handler_exception_logs_error(self):
        """Exception in async handler is caught and logged."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = AsyncMock(side_effect=ValueError("async boom"))
        handler.register("AFAIL", callback)

        reader = _make_reader(b'{"cmd": "AFAIL"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("'AFAIL' failed" in str(c) for c in log.error.call_args_list)

    @pytest.mark.asyncio
    async def test_stop_flag_breaks_loop(self):
        """Setting stop() before dispatch causes loop to exit after handler."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        def stop_callback():
            handler.stop()

        handler.register("STOP", stop_callback)
        # Second command should NOT be dispatched
        second = MagicMock()
        handler.register("AFTER", second)

        reader = _make_reader(
            b'{"cmd": "STOP"}\n',
            b'{"cmd": "AFTER"}\n',
        )
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        second.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_lines_are_skipped(self):
        """Blank lines are silently skipped."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        callback = MagicMock()
        handler.register("CMD", callback)

        reader = _make_reader(b"\n", b"   \n", b'{"cmd": "CMD"}\n')
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        callback.assert_called_once()
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_eof_stops_loop(self):
        """Empty read (EOF) stops the loop gracefully."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        reader = _make_reader()  # immediate EOF
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("stopped" in str(c) for c in log.debug.call_args_list)

    @pytest.mark.asyncio
    async def test_cancelled_error_stops_loop(self):
        """CancelledError breaks the loop cleanly."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        reader = AsyncMock()
        reader.read = AsyncMock(side_effect=asyncio.CancelledError)
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("stopped" in str(c) for c in log.debug.call_args_list)

    @pytest.mark.asyncio
    async def test_os_dup_failure_returns_early(self):
        """OSError on os.dup logs debug and returns."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        with patch("os.dup", side_effect=OSError("no fd")):
            await handler.run()

        assert any("unavailable" in str(c) for c in log.debug.call_args_list)

    @pytest.mark.asyncio
    async def test_connect_read_pipe_failure_returns_early(self):
        """OSError on connect_read_pipe logs debug and returns."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        mock_pipe = MagicMock()
        mock_loop = MagicMock()
        mock_loop.connect_read_pipe = AsyncMock(side_effect=OSError("pipe error"))

        class _FifoStat:
            st_mode = stat.S_IFIFO

        with (
            patch("os.dup", return_value=99),
            patch("os.fdopen", return_value=mock_pipe),
            patch("asyncio.StreamReader"),
            patch("asyncio.StreamReaderProtocol"),
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("os.fstat", return_value=_FifoStat()),
            patch("os.isatty", return_value=False),
        ):
            await handler.run()

        assert any("unavailable" in str(c) for c in log.debug.call_args_list)
        mock_pipe.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_skips_non_pipe_stdin(self):
        """A character-device stdin that is not a TTY (e.g. /dev/null) is skipped.

        Regression: epoll cannot register /dev/null (``EPERM``) and Python
        3.14's pipe transport no longer swallows that failure, so the reader
        must never be connected on such a stdin — it would surface as an
        unhandled loop-callback error and leave the transport half-broken.
        """
        log = MagicMock()
        handler = StdinCommandHandler(log)

        class _DevNullStat:
            st_mode = stat.S_IFCHR  # character device, like /dev/null

        with (
            patch("os.dup", return_value=99),
            patch("os.fdopen", return_value=MagicMock()) as mock_fdopen,
            patch("os.fstat", return_value=_DevNullStat()),
            patch("os.isatty", return_value=False),
            patch("asyncio.StreamReader"),
            patch("asyncio.StreamReaderProtocol"),
        ):
            await handler.run()

        assert any("not a pipe or terminal" in str(c) for c in log.debug.call_args_list)
        mock_fdopen.return_value.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_commands_dispatched_in_order(self):
        """Multiple commands are dispatched in the order received."""
        log = MagicMock()
        handler = StdinCommandHandler(log)
        order = []
        handler.register("A", lambda: order.append("A"))
        handler.register("B", lambda: order.append("B"))
        handler.register("C", lambda: order.append("C"))

        reader = _make_reader(
            b'{"cmd": "A"}\n',
            b'{"cmd": "B"}\n',
            b'{"cmd": "C"}\n',
        )
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert order == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_reader_generic_exception_logs_error_and_stops(self):
        """Unexpected exception in the read loop logs error and breaks."""
        log = MagicMock()
        handler = StdinCommandHandler(log)

        reader = AsyncMock()
        reader.read = AsyncMock(side_effect=RuntimeError("unexpected"))
        p = _patch_pipe_setup(reader)
        with p:
            await handler.run()

        assert any("stdin reader error" in str(c) for c in log.error.call_args_list)


class TestThreadReader:
    """The thread reader (p2p serving role) shares the async line contract."""

    def test_parse_rejects_malformed_lines(self):
        log = MagicMock()
        handler = StdinCommandHandler(log)

        assert handler._parse(b"not json\n") is None
        assert handler._parse(b'{"nocmd": 1}\n') is None
        assert handler._parse(b"   \n") is None
        assert any("Invalid JSON" in str(c) for c in log.warning.call_args_list)

    def test_parse_returns_the_command_and_fields(self):
        handler = StdinCommandHandler(MagicMock())

        assert handler._parse(b'{"cmd": "PREVIEW", "filter": "docs/"}\n') == (
            "PREVIEW",
            {"filter": "docs/"},
        )

    def test_dispatch_runs_sync_and_async_handlers(self):
        calls: list[str] = []
        handler = StdinCommandHandler(MagicMock())
        handler.register("SYNC", lambda **_kw: calls.append("sync"))

        async def _preview(**_kw) -> None:
            calls.append("preview")

        handler.register("PREVIEW", _preview)

        handler._dispatch_in_thread(b'{"cmd": "SYNC"}\n')
        handler._dispatch_in_thread(b'{"cmd": "PREVIEW"}\n')

        assert calls == ["sync", "preview"]

    def test_an_unknown_command_is_only_warned(self):
        log = MagicMock()
        handler = StdinCommandHandler(log)

        handler._dispatch_in_thread(b'{"cmd": "NOPE"}\n')

        assert any("Unknown stdin command" in str(c) for c in log.warning.call_args_list)

    def test_stop_thread_is_safe_without_a_reader(self):
        handler = StdinCommandHandler(MagicMock())

        handler.stop_thread()  # must not raise

    def test_the_thread_reader_dispatches_a_command_line(self):
        """The select loop still delivers a line written to stdin."""
        handler = StdinCommandHandler(MagicMock())
        seen: list[str] = []
        handler.register("SYNC", lambda **_kw: seen.append("sync"))

        read_fd, write_fd = os.pipe()
        real_dup = os.dup
        thread = threading.Thread(
            target=handler.run_in_thread,
            args=(threading.Event(),),
            daemon=True,
        )
        try:
            with patch("os.dup", side_effect=lambda _fd: real_dup(read_fd)):
                thread.start()
                assert _wait_for(lambda: handler._thread_fd is not None)
                os.write(write_fd, b'{"cmd": "SYNC"}\n')
                assert _wait_for(lambda: seen == ["sync"])
                handler.stop_thread()
                thread.join(timeout=2)
            assert not thread.is_alive()
        finally:
            for fd in (read_fd, write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_stop_thread_unblocks_a_pending_read_immediately(self):
        """``stop_thread`` wakes an idle reader instead of waiting a timeout.

        Regression: it used to close the duplicate of stdin, which does **not**
        interrupt a pending ``os.read`` (the syscall keeps the open file
        description alive), so ``_MasterChannel.stop`` always burned its whole
        5 s join timeout — exactly the budget the desktop allows between
        ``SIGTERM`` and ``SIGKILL``, which could kill the client mid-teardown.
        """
        handler = StdinCommandHandler(MagicMock())

        read_fd, write_fd = os.pipe()
        real_dup = os.dup
        thread = threading.Thread(
            target=handler.run_in_thread,
            args=(threading.Event(),),
            daemon=True,
        )
        try:
            with patch("os.dup", side_effect=lambda _fd: real_dup(read_fd)):
                thread.start()
                assert _wait_for(lambda: handler._thread_fd is not None)

                started = time.monotonic()
                handler.stop_thread()
                thread.join(timeout=2)
                elapsed = time.monotonic() - started
            assert not thread.is_alive()
            assert elapsed < 1.0
        finally:
            for fd in (read_fd, write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)
