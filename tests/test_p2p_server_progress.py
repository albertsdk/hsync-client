# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The P2P master reports the bytes it serves and the files it receives.

The Desktop renders ``progress`` samples and finalizes a row only on the
terminal ``transfer`` event, so a serving master must publish both: when the
Desktop starts first (and is therefore the master) it used to show no activity
at all, while the same transfers were visible on the slave side.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from hsync_common.compression import decompress_data

from hsync_client import progress as progress_mod
from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer
from tests.p2p_helpers import unauth_write_config

_PAYLOAD = b"0123456789abcdef" * 40_000  # 640 KB: several serve chunks


@pytest.fixture
def events(monkeypatch):
    """Capture the emitted UI events at the progress sink boundary."""
    captured: list[dict] = []

    def _record(event, **payload):
        captured.append({"event": str(event), **payload})

    monkeypatch.setattr(progress_mod, "emit_progress", _record)
    monkeypatch.setattr(progress_mod, "emit_event", _record)
    return captured


@pytest.fixture
def server():
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    yield srv, port
    srv.stop()


def _download(
    port: int, filename: str, *, accept_encoding: str = "identity"
) -> tuple[int, dict[str, str], bytes]:
    """GET a file from the master, returning (status, headers, body)."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "GET",
            f"/api/v1/buckets/test/files/download?filename={filename}",
            headers={"Accept-Encoding": accept_encoding},
        )
        response = conn.getresponse()
        body = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


def _wait_for_events(events: list[dict], *, event: str, op: str, timeout: float = 5.0) -> None:
    """Wait until the terminal ``event`` of direction *op* has been published.

    The HTTP client observes the last body byte while the handler thread is
    still finishing its bookkeeping, so asserting straight after the response
    would race the final sample and the terminal event.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e["event"] == event and e.get("op") == op for e in events):
            return
        time.sleep(0.01)


def _post_file(port: int, content: bytes, name: str = "received.bin"):
    """POST one file the way a slave uploads it to the master."""
    import requests

    return requests.post(
        f"http://localhost:{port}/api/v1/buckets/test/files",
        data={
            "cid": "PeerCid000000000000",
            "path": "",
            "size": str(len(content)),
            "sha256sum": hashlib.sha256(content).hexdigest(),
        },
        files={"file": (name, content)},
        timeout=10,
    )


class TestServeProgress:
    """The serving master reports the file it streams to a peer."""

    def test_identity_serve_reports_progress_and_one_transfer(self, server, tmp_path, events):
        _, port = server
        (tmp_path / "served.bin").write_bytes(_PAYLOAD)

        with patch(
            "hsync_client.p2p_server.CONFIG",
            unauth_write_config(tmp_path, COMPRESSION_ENABLED=False),
        ):
            status, _headers, body = _download(port, "served.bin")
        _wait_for_events(events, event="transfer", op="upload")

        assert status == 200
        assert body == _PAYLOAD
        progress = [e for e in events if e["event"] == "progress"]
        assert progress, "a served file must report in-flight progress"
        assert all(e["op"] == "upload" for e in progress)
        assert all(e["file"].endswith("served.bin") for e in progress)
        positions = [e["transferred"] for e in progress]
        assert positions == sorted(positions)
        assert progress[-1]["transferred"] == len(_PAYLOAD), progress[-3:]
        assert progress[-1]["total"] == len(_PAYLOAD)
        transfers = [e for e in events if e["event"] == "transfer"]
        assert len(transfers) == 1
        assert transfers[0]["op"] == "upload"
        assert transfers[0]["size"] == len(_PAYLOAD)
        assert transfers[0]["file"].endswith("served.bin")

    def test_compressed_serve_reports_the_wire_total_and_the_file_size(
        self, server, tmp_path, events
    ):
        """Progress counts the zstd bytes; the terminal event the file size."""
        _, port = server
        (tmp_path / "compressible.txt").write_bytes(_PAYLOAD)

        with patch(
            "hsync_client.p2p_server.CONFIG",
            unauth_write_config(tmp_path, COMPRESSION_ENABLED=True),
        ):
            status, headers, body = _download(port, "compressible.txt", accept_encoding="zstd")
        _wait_for_events(events, event="transfer", op="upload")

        assert status == 200
        assert headers.get("content-encoding") == "zstd"
        assert decompress_data(body) == _PAYLOAD
        progress = [e for e in events if e["event"] == "progress"]
        assert progress and progress[-1]["op"] == "upload"
        assert progress[-1]["total"] == int(headers["content-length"])
        assert progress[-1]["transferred"] == int(headers["content-length"])
        transfers = [e for e in events if e["event"] == "transfer"]
        assert [e["size"] for e in transfers] == [len(_PAYLOAD)]

    def test_an_aborted_serve_reports_transfer_failed(self, server, tmp_path, events, monkeypatch):
        """A serve that dies mid-stream must publish ``transfer_failed``.

        No terminal event would leave the Desktop row frozen at its last sample
        (a bar stuck at 100% once the bytes reached the socket), because only a
        terminal event resets an in-flight row.
        """
        import http.client

        from hsync_client import p2p_server as server_mod

        _, port = server
        (tmp_path / "served.bin").write_bytes(_PAYLOAD)

        real = server_mod._write_served_chunk
        calls = {"n": 0}

        def _boom(wfile, chunk, reporter, sent):
            calls["n"] += 1
            if calls["n"] >= 2:  # first chunk streams, then the peer is gone
                raise BrokenPipeError("client went away")
            return real(wfile, chunk, reporter, sent)

        monkeypatch.setattr(server_mod, "_write_served_chunk", _boom)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            with patch(
                "hsync_client.p2p_server.CONFIG",
                unauth_write_config(tmp_path, COMPRESSION_ENABLED=False),
            ):
                conn.request(
                    "GET",
                    "/api/v1/buckets/test/files/download?filename=served.bin",
                    headers={"Accept-Encoding": "identity"},
                )
                response = conn.getresponse()
                try:
                    response.read()
                except http.client.IncompleteRead:
                    pass  # the master aborted the truncated body on purpose
        finally:
            conn.close()

        _wait_for_events(events, event="transfer_failed", op="upload")
        failed = [e for e in events if e["event"] == "transfer_failed"]
        assert len(failed) == 1
        assert failed[0]["op"] == "upload"
        assert failed[0]["file"].endswith("served.bin")
        assert not [e for e in events if e["event"] == "transfer"]


class TestReceivedUploadProgress:
    """An upload pushed by a slave is reported as a local download."""

    def test_upload_reports_progress_and_one_transfer(self, server, tmp_path, events):
        _, port = server

        with patch(
            "hsync_client.p2p_server.CONFIG",
            unauth_write_config(tmp_path, MIN_FREE_DISK_SPACE=0, MAX_INBOUND_SIZE=0),
        ):
            response = _post_file(port, _PAYLOAD)
        _wait_for_events(events, event="transfer", op="download")

        assert response.status_code == 201
        assert (tmp_path / "received.bin").read_bytes() == _PAYLOAD
        progress = [e for e in events if e["event"] == "progress"]
        assert progress, "a received upload must report in-flight progress"
        assert all(e["op"] == "download" for e in progress)
        assert progress[-1]["transferred"] == len(_PAYLOAD)
        assert progress[-1]["total"] == len(_PAYLOAD)
        transfers = [e for e in events if e["event"] == "transfer"]
        assert len(transfers) == 1
        assert transfers[0]["op"] == "download"
        assert transfers[0]["size"] == len(_PAYLOAD)
        assert transfers[0]["file"].endswith("received.bin")
