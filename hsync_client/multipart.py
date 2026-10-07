# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Streaming multipart/form-data parsing for the P2P master server.

Self-contained component extracted from ``p2p_server.py``: it reads small form
fields into memory (hard-capped) and streams the file part body through a writer
callback, so a large upload is never buffered in RAM. Also provides the
decompression-bomb containment writer (:class:`_CappedBytesWriter`) used on the
receiving side of compressed uploads.

Consumers: ``hsync_client.p2p_server`` (upload and delta-apply endpoints).
"""

import re

from loguru import logger as log

# Precompiled multipart parsing patterns: the boundary marker and the
# Content-Disposition name/filename attributes are matched once per request /
# per part, so the regex objects are built at import time instead of being
# recompiled (or cache-looked-up) on every multipart body.
_MULTIPART_BOUNDARY_RE = re.compile(r"boundary=([^\s;]+)")
_MULTIPART_NAME_RE = re.compile(r'name="([^"]*)"')
_MULTIPART_FILENAME_RE = re.compile(r'filename="([^"]*)"')

# Multipart parsing bounds: form-field values and part-header lines are
# buffered in memory, so they need a hard cap — an unbounded buffer lets a
# peer exhaust master RAM with a single giant "field" (the file part itself
# is streamed to disk and needs no such cap).
MAX_FORM_FIELD_BYTES = 64 * 1024
MAX_PART_HEADER_LINE_BYTES = 8 * 1024


class _CappedBytesWriter:
    """File-like wrapper that aborts once more than *cap* bytes are written.

    Placed between a decompressor and the destination file so a hostile
    upload cannot expand a small compressed payload past the effective cap
    — min(declared size, ``MAX_INBOUND_SIZE``) — (decompression bomb
    containment on the receiving side).
    """

    def __init__(self, write, cap: int) -> None:
        self._write = write
        self._cap = cap
        self.written = 0

    def write(self, data: bytes):
        self.written += len(data)
        if self.written > self._cap:
            raise ValueError(f"Upload exceeds the maximum allowed size ({self._cap} bytes)")
        return self._write(data)

    def close(self) -> None:
        """No-op: the underlying file is owned and closed by the caller."""


# =============================================================================
# Multipart Parser
# =============================================================================


class _StreamingMultipartParser:
    """
    Streaming multipart/form-data parser.

    Reads small form fields into memory and streams the file part body via
    a writer callback (bytes -> None), avoiding loading huge uploads into RAM.

    Usage:
        parser = _StreamingMultipartParser(rfile, content_length, boundary)
        form_fields, file_meta = parser.parse(file_writer)

    Where file_meta is (field_name, filename) or None if no file part was found.
    """

    _READ_CHUNK = 64 * 1024

    def __init__(self, rfile, content_length: int, boundary: bytes) -> None:
        self._rfile = rfile
        self._remaining = content_length
        # Boundary lines in multipart are prefixed with "--"
        self._boundary = b"--" + boundary
        # Mutable buffer: ``del buf[:emit]`` trims consumed bytes in-place
        # (O(1) amortized) instead of re-copying the whole tail per flush.
        self._buf = bytearray()
        # Scan restart offset after a flush. A terminator can straddle the
        # flush cut; because the kept safety window (tlen+2 bytes) is larger
        # than a terminator, only the last tlen-1 kept bytes can ever complete
        # a straddling match, so the scan restarts there instead of at 0.
        self._search_from = 0

    def _read_more(self, n: int = _READ_CHUNK) -> bytes:
        n = min(n, self._remaining)
        if n <= 0:
            return b""
        chunk = self._rfile.read(n)
        if chunk:
            self._remaining -= len(chunk)
            self._buf += chunk
        return chunk

    def _read_line(self) -> bytes | None:
        """Read one CRLF-terminated line, replenishing the buffer as needed.

        Returns the line without the trailing CRLF, or None on EOF.
        Raises ValueError if the line grows past the header-line cap.
        """
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            # No CRLF in the buffer yet: if the accumulated bytes (which form
            # a single unterminated line) exceed the cap, reject. Only check
            # when there is no CRLF, because a buffer WITH a CRLF contains a
            # complete (possibly short) line plus lookahead for the next read.
            if len(self._buf) > MAX_PART_HEADER_LINE_BYTES:
                log.warning(
                    f"multipart header line too long ({len(self._buf)} bytes, "
                    f"cap {MAX_PART_HEADER_LINE_BYTES}): "
                    f"buf_head={bytes(self._buf[:256])!r}"
                )
                raise ValueError("multipart header line too long")
            if not self._read_more():
                break
        return None

    def _read_until_boundary(self, writer) -> str:
        """Write body bytes to ``writer`` until the next boundary line.

        ``writer`` is a callable accepting bytes (or None to discard).
        Returns "next" if another part follows, "end" for the closing boundary,
        or "eof" if the stream ended before any boundary was found.
        """
        terminator = b"\r\n" + self._boundary
        tlen = len(terminator)
        # Need at least tlen+2 bytes to safely emit, to disambiguate the
        # trailing CRLF before the boundary from in-body content.
        safety = tlen + 2

        while True:
            idx = self._buf.find(terminator, self._search_from)
            if idx != -1:
                if writer is not None and idx > 0:
                    writer(bytes(self._buf[:idx]))
                # Consume up to and including the boundary line
                after_boundary = idx + tlen
                # The two bytes after the boundary are either b"\r\n" (next
                # part) or b"--" (final boundary).
                while len(self._buf) < after_boundary + 2 and self._read_more():
                    pass
                marker = bytes(self._buf[after_boundary : after_boundary + 2])
                del self._buf[: after_boundary + 2]
                self._search_from = 0
                if marker == b"--":
                    # Final boundary; discard the optional epilogue CRLF
                    if self._buf.startswith(b"\r\n"):
                        del self._buf[:2]
                    return "end"
                # Next part: consume past CRLF
                return "next"

            # No boundary yet: flush everything except the trailing safety window
            if len(self._buf) > safety:
                emit = len(self._buf) - safety
                if writer is not None:
                    writer(bytes(self._buf[:emit]))
                del self._buf[:emit]
                # A terminator may straddle the flush cut: restart the scan at
                # most tlen-1 bytes from the end (the kept window is >= that).
                self._search_from = max(0, len(self._buf) - (tlen - 1))

            if not self._read_more():
                # EOF: flush whatever remains
                if writer is not None and self._buf:
                    writer(bytes(self._buf))
                self._buf.clear()
                return "eof"

    def parse(self, file_open) -> tuple[dict[str, str], tuple[str, str] | None]:
        """Parse the multipart body.

        ``file_open`` is a callable invoked once when the file part header
        is decoded, with signature
        ``file_open(form_fields, field_name, filename) -> writer``
        where ``writer`` is a callable ``bytes -> None`` receiving successive
        chunks of the file body. ``form_fields`` is the dict of form fields
        decoded so far (all fields preceding the file part are guaranteed to
        be present). Pass ``None`` to ignore the file body.

        Returns ``(form_fields, file_meta)`` where ``file_meta`` is
        ``(field_name, filename)`` if a file part was seen.
        """
        form_fields: dict[str, str] = {}
        file_meta: tuple[str, str] | None = None

        # Skip preamble: read lines until we hit the first boundary line
        while True:
            line = self._read_line()
            if line is None:
                return form_fields, file_meta
            if line == self._boundary:
                break
            if line == self._boundary + b"--":
                return form_fields, file_meta

        while True:
            headers: dict[bytes, bytes] = {}
            while True:
                line = self._read_line()
                if line is None:
                    return form_fields, file_meta
                if line == b"":
                    break
                if b":" in line:
                    k, _, v = line.partition(b":")
                    headers[k.strip().lower()] = v.strip()

            cd = headers.get(b"content-disposition", b"").decode("utf-8", "replace")
            name_m = _MULTIPART_NAME_RE.search(cd)
            filename_m = _MULTIPART_FILENAME_RE.search(cd)

            if filename_m and name_m and file_open is not None and file_meta is None:
                field_name = name_m.group(1)
                filename = filename_m.group(1)
                file_meta = (field_name, filename)
                writer = file_open(form_fields, field_name, filename)
                marker = self._read_until_boundary(writer)
            elif name_m:
                # Form field: buffered in memory, so hard-capped (the file
                # part is the only part allowed to be large, and it streams).
                field_buf = bytearray()

                def _field_writer(data: bytes, _buf=field_buf) -> None:
                    if len(_buf) + len(data) > MAX_FORM_FIELD_BYTES:
                        raise ValueError(
                            f"multipart form field exceeds {MAX_FORM_FIELD_BYTES} bytes"
                        )
                    _buf.extend(data)

                marker = self._read_until_boundary(_field_writer)
                form_fields[name_m.group(1)] = bytes(field_buf).decode("utf-8", "replace")
            else:
                marker = self._read_until_boundary(None)

            if marker in ("end", "eof"):
                return form_fields, file_meta


# =============================================================================
