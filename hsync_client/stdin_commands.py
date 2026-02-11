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
import json
from typing import Any, Callable, Dict


class StdinCommandHandler:
    """Async reader that dispatches JSON commands received on stdin."""

    def __init__(self, log: Any) -> None:
        self._log = log
        self._handlers: Dict[str, Callable[..., Any]] = {}
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
        import os

        loop = asyncio.get_event_loop()
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

        try:
            await loop.connect_read_pipe(lambda: protocol, pipe)
        except (OSError, ValueError) as exc:
            self._log.debug(f"stdin command reader unavailable: {exc}")
            pipe.close()
            return

        self._log.debug("stdin command reader started")

        while True:
            try:
                line = await reader.readline()
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
                    if asyncio.iscoroutinefunction(handler):
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

