# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Stdin command handler for hsync-client.

Reads JSON commands from stdin line-by-line and dispatches them to
registered handlers.  This provides a cross-platform, extensible
command channel between the desktop GUI (or any parent process)
and the running hsync-client.

Protocol
--------
Each command is a single JSON object terminated by a newline::

    {"cmd": "PREVIEW"}
    {"cmd": "RESYNC", "filter": "docs/"}

The ``cmd`` field is mandatory; additional fields are passed as
keyword arguments to the handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import select
import stat
import threading
from collections.abc import Callable
from typing import Any

# Maximum stdin line length (1 MiB). A JSON command line is a compact
# structured message (typically < 1 KB); this cap prevents an OOM DoS from
# a single unbounded readline() when the parent process sends a giant frame.
_MAX_STDIN_LINE_BYTES = 1 * 1024 * 1024


class _BoundedLineReader:
    """Chunked, capped stdin line reader (same wire contract as the legacy
    byte-at-a-time loop, without one await per byte).

    Semantics preserved exactly:

    - returns the line **including** the trailing newline;
    - a line whose total length (newline included) is exactly *max_bytes*
      is accepted; anything longer is rejected and drained;
    - on EOF, a partial line without a trailing newline is returned as-is,
      and a clean EOF yields ``b""``;
    - after a rejected over-cap line the reader is drained up to the next
      newline and ``b""`` is returned, so subsequent reads are not poisoned.
    """

    _READ_CHUNK = 64 * 1024

    def __init__(self) -> None:
        self._buf = bytearray()

    async def readline(self, reader: asyncio.StreamReader, max_bytes: int) -> bytes:
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1 and nl < max_bytes:
                line = bytes(self._buf[: nl + 1])
                del self._buf[: nl + 1]
                return line
            if len(self._buf) >= max_bytes:
                # Over-cap line without a newline inside the cap: drain up
                # to (and including) the next newline, keeping any trailing
                # bytes that belong to the following line.
                self._buf.clear()
                while True:
                    chunk = await reader.read(self._READ_CHUNK)
                    if not chunk:
                        return b""
                    nl = chunk.find(b"\n")
                    if nl != -1:
                        self._buf.extend(chunk[nl + 1 :])
                        return b""
            chunk = await reader.read(self._READ_CHUNK)
            if not chunk:
                line = bytes(self._buf)
                self._buf.clear()
                return line  # EOF (b"" when no partial line was buffered)
            self._buf.extend(chunk)


class StdinCommandHandler:
    """Async reader that dispatches JSON commands received on stdin."""

    def __init__(self, log: Any) -> None:
        self._log = log
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._should_stop = False
        # Duplicate of stdin owned by the thread reader (see run_in_thread) and
        # the wake pipe that unblocks its pending read on stop.
        self._thread_fd: int | None = None
        self._wake_w: int | None = None
        self._thread_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────

    def stop(self) -> None:
        """Signal the run loop to exit after the current handler returns."""
        self._should_stop = True

    def register(self, cmd: str, handler: Callable[..., Any]) -> None:
        """Register *handler* for *cmd*.

        The handler is called with all JSON fields **except** ``cmd``
        as keyword arguments.  It may be a regular function or a
        coroutine.
        """
        self._handlers[cmd] = handler

    # ── line contract (shared by both readers) ────────────────────────

    def _parse(self, line: bytes) -> tuple[str, dict] | None:
        """Decode and validate one command line; ``None`` when it is unusable."""
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._log.warning(f"Invalid JSON on stdin: {exc}")
            return None
        if not isinstance(data, dict):
            self._log.warning("stdin command must be a JSON object")
            return None
        cmd = data.pop("cmd", None)
        if cmd is None:
            self._log.warning("stdin command missing 'cmd' field")
            return None
        return str(cmd), data

    def run_in_thread(self, stop: threading.Event) -> None:
        """Blocking reader for callers that have no event loop.

        The p2p serving role runs outside any asyncio loop for most of its life,
        so it needs the same command channel as the slave paths. The loop waits on
        stdin **and** on a wake pipe, because closing stdin does not interrupt a
        pending ``os.read`` (the syscall keeps the open file description alive):
        :meth:`stop_thread` writes a byte to the wake pipe instead. Without it,
        ``_MasterChannel.stop`` always burned its whole 5 s join timeout — the same
        budget the desktop allows between ``SIGTERM`` and ``SIGKILL``.
        """
        try:
            dup_fd = os.dup(0)
        except OSError as exc:
            self._log.debug(f"stdin command reader unavailable: {exc}")
            return
        # Same fd-type guard as ``run``: epoll cannot register /dev/null or a
        # regular file, and the channel contract is a parent pipe or a terminal.
        mode = os.fstat(dup_fd).st_mode
        if not (
            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or (stat.S_ISCHR(mode) and os.isatty(dup_fd))
        ):
            self._log.debug("stdin command reader unavailable: stdin is not a pipe or terminal")
            os.close(dup_fd)
            return
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        with self._thread_lock:
            if self._thread_fd is not None:
                for fd in (dup_fd, wake_r, wake_w):
                    os.close(fd)
                raise RuntimeError("a stdin thread reader is already active")
            self._thread_fd = dup_fd
            self._wake_w = wake_w
        self._log.debug("stdin command reader started (thread)")
        buf = bytearray()
        try:
            while not stop.is_set():
                try:
                    ready, _, _ = select.select([dup_fd, wake_r], [], [])
                except (OSError, ValueError):
                    break  # a descriptor was closed under us
                if wake_r in ready:
                    break  # stop_thread
                try:
                    chunk = os.read(dup_fd, _BoundedLineReader._READ_CHUNK)
                except OSError:
                    break  # stdin closed by the parent
                if not chunk:
                    break  # EOF: the parent process closed the pipe
                buf.extend(chunk)
                while True:
                    newline = buf.find(b"\n")
                    if newline == -1:
                        if len(buf) > _MAX_STDIN_LINE_BYTES:
                            buf.clear()  # drop an over-cap line, keep reading
                        break
                    line = bytes(buf[: newline + 1])
                    del buf[: newline + 1]
                    if len(line) > _MAX_STDIN_LINE_BYTES:
                        continue
                    self._dispatch_in_thread(line)
        finally:
            with self._thread_lock:
                self._thread_fd = None
                self._wake_w = None
                # Closed under the lock: a concurrent ``stop_thread`` writes to
                # the pipe from inside it, so the byte can never reach a
                # descriptor number the process has already reused.
                for fd in (wake_r, wake_w):
                    with contextlib.suppress(OSError):
                        os.close(fd)
            with contextlib.suppress(OSError):
                os.close(dup_fd)
            self._log.debug("stdin command reader stopped")

    def stop_thread(self) -> None:
        """Unblock :meth:`run_in_thread` through its wake pipe."""
        with self._thread_lock:
            wake_w = self._wake_w
            if wake_w is None:
                return
            with contextlib.suppress(OSError):
                os.write(wake_w, b"\0")

    def _dispatch_in_thread(self, line: bytes) -> None:
        parsed = self._parse(line)
        if parsed is None:
            return
        cmd, data = parsed
        handler = self._handlers.get(cmd)
        if handler is None:
            self._log.warning(f"Unknown stdin command: {cmd}")
            return
        try:
            if inspect.iscoroutinefunction(handler):
                # No loop on this thread: drive the coroutine to completion.
                asyncio.run(handler(**data))
            else:
                handler(**data)
        except Exception as exc:
            self._log.error(f"stdin command '{cmd}' failed: {exc}")

    async def run(self) -> None:
        """Read lines from stdin and dispatch commands until EOF."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)

        # connect_read_pipe needs a raw binary file object.
        # Open a *duplicate* of fd 0 so we don't interfere with
        # Python's own sys.stdin wrapper.
        try:
            dup_fd = os.dup(0)
            pipe = os.fdopen(dup_fd, "rb", 0)
        except OSError as exc:
            self._log.debug(f"stdin command reader unavailable: {exc}")
            return

        # epoll cannot register every file type: /dev/null (a character device
        # without a poll handler) and regular files fail with EPERM. Since
        # Python 3.14 the pipe transport no longer swallows that failure, so
        # it would surface as an unhandled loop callback error and leave the
        # transport half-broken. The stdin channel contract is a parent
        # process pipe (desktop GUI) or an interactive terminal, so skip the
        # reader up front when stdin is anything else (e.g. /dev/null under a
        # service manager or a test launcher).
        mode = os.fstat(dup_fd).st_mode
        if not (
            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or (stat.S_ISCHR(mode) and os.isatty(dup_fd))
        ):
            self._log.debug("stdin command reader unavailable: stdin is not a pipe or terminal")
            pipe.close()
            return

        try:
            await loop.connect_read_pipe(lambda: protocol, pipe)
        except (OSError, ValueError) as exc:
            self._log.debug(f"stdin command reader unavailable: {exc}")
            pipe.close()
            return

        self._log.debug("stdin command reader started")

        line_reader = _BoundedLineReader()

        while True:
            try:
                line = await line_reader.readline(reader, _MAX_STDIN_LINE_BYTES)
                if not line:  # EOF
                    break

                parsed = self._parse(line)
                if parsed is None:
                    continue
                cmd, data = parsed

                handler = self._handlers.get(cmd)
                if handler is None:
                    self._log.warning(f"Unknown stdin command: {cmd}")
                    continue

                # Call handler (sync or async) with remaining fields
                try:
                    if inspect.iscoroutinefunction(handler):
                        await handler(**data)
                    else:
                        handler(**data)
                except Exception as exc:
                    self._log.error(f"stdin command '{cmd}' failed: {exc}")

                if self._should_stop:
                    break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(f"stdin reader error: {exc}")
                break

        self._log.debug("stdin command reader stopped")
