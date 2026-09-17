# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
import inspect
import json
import os
import stat
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

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                try:
                    data: dict = json.loads(text)
                except json.JSONDecodeError as exc:
                    self._log.warning(f"Invalid JSON on stdin: {exc}")
                    continue

                cmd = data.pop("cmd", None)
                if cmd is None:
                    self._log.warning("stdin command missing 'cmd' field")
                    continue

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
