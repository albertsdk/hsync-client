"""Regression: the aiohttp transport must preserve ``rfile.read(n)`` semantics.

``BaseHTTPRequestHandler.rfile.read(n)`` returns exactly *n* bytes (or fewer
only at EOF). aiohttp's ``StreamReader.read(n)`` returns as soon as *some* data
is buffered, so the adapter has to loop; otherwise a segmented request body
makes the endpoint layer see a short read and reject it ("Incomplete request
body", truncated JSON) even though every byte arrives.
"""

import asyncio
from types import SimpleNamespace

from aiohttp import HttpVersion

from hsync_client.p2p_server import _AiohttpTransport


class _ChunkedContent:
    """A body stream that releases one buffered chunk per ``read`` call."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = b"".join(self._chunks)
            self._chunks.clear()
            return data
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk


def _build_transport(*chunks: bytes) -> tuple[_AiohttpTransport, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    request = SimpleNamespace(
        content=_ChunkedContent(*chunks),
        method="GET",
        version=HttpVersion(1, 1),
        rel_url=SimpleNamespace(raw_path_qs="/"),
        headers={},
        remote="127.0.0.1",
    )
    transport = _AiohttpTransport(request, loop, SimpleNamespace())
    # Run the bridged coroutine on the same thread instead of crossing threads.
    transport._loop_run = lambda coro, timeout=None: loop.run_until_complete(coro)  # type: ignore[method-assign]
    return transport, loop


def test_read_returns_exactly_the_requested_size_across_partial_chunks():
    transport, loop = _build_transport(b"ab", b"cd", b"ef")
    try:
        assert transport.rfile.read(6) == b"abcdef"
    finally:
        loop.close()


def test_read_stops_at_eof_when_the_body_is_short():
    transport, loop = _build_transport(b"abc")
    try:
        assert transport.rfile.read(10) == b"abc"
    finally:
        loop.close()


def test_read_all_returns_the_whole_body():
    transport, loop = _build_transport(b"ab", b"cd")
    try:
        assert transport.rfile.read(-1) == b"abcd"
        assert transport.rfile.read() == b""
    finally:
        loop.close()
