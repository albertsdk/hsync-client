"""
Regression tests for the LAN master anti-aliasing invariant (ADR-0022 §3.4.7).

A pure per-listing tick without persistence would alias {master:1} across two
distinct edits: a slave would then "dominate" a genuine divergence and silently
overwrite the master's newer edit. These tests prove the master's publication
points — the file listing and the realtime watcher broadcast — persist the
self-record, so the advertised clock actually progresses.
"""

import time
from http.client import HTTPConnection
from unittest.mock import patch

from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANServer


def _bucket_entry(listing: list[dict], filepath: str) -> dict:
    """Return the listing entry for *filepath*.

    The listing is filtered explicitly instead of using ``listing[0]``: the
    master advertises every syncable bucket file sorted by mtime, and the
    test's bucket may legitimately carry unrelated entries (the suite's
    isolation directories live outside ``tmp_path``, but a test-local bucket
    is not guaranteed to hold a single file). ``next`` raises StopIteration
    on a missing entry, failing the test loudly.
    """
    return next(entry for entry in listing if entry.get("filepath") == filepath)


def _server_fixture():
    port = get_free_port()
    server = LANServer(port=port)
    server.start()
    time.sleep(0.1)
    return server, port


def _get_listing(port: int) -> list[dict]:
    import json

    conn = HTTPConnection("localhost", port, timeout=5)
    conn.request("GET", "/api/v1/buckets/test/files")
    response = conn.getresponse()
    assert response.status == 200
    data = json.loads(response.read().decode())
    conn.close()
    return data


def _cid() -> str:
    """Well-formed 21-char cid for the node under test."""
    return "TestClientX1234567890"


def test_listing_self_record_persists_and_clock_progresses(tmp_path):
    """Two successive contents must be advertised with *different* clocks.

    First listing of a never-synced file registers {cid:1}; after the local
    content changes, the next listing must advertise {cid:2} (the self-record
    persisted by the previous listing, then ticked). Regression for the
    aliasing bug where both edits shared {cid:1}.
    """
    from hsync_common.models.file import invalidate_sha256_cache

    from hsync_client.sync_state import get_base_hash

    f = tmp_path / "doc.txt"
    f.write_text("v1\n")

    cfg = {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None, "CID": _cid()}
    with (
        patch("hsync_client.lan_server.CONFIG", cfg),
        patch("hsync_client.state_backend.CONFIG", cfg),
    ):
        srv, port = _server_fixture()
        try:
            listing = _get_listing(port)
            version = _bucket_entry(listing, "doc.txt")["version"]
            assert version["vclock"] == {_cid(): 1}, version

            # The self-record must be persisted: the stored base hash mirrors
            # the advertised content.
            assert get_base_hash(str(f)) is not None

            # Edit the file. The realtime watcher invalidates the per-process
            # SHA-256 cache on observed changes; the listing path relies on
            # that same invalidation before re-hashing, mirror it here.
            f.write_text("v2 — different content\n")
            invalidate_sha256_cache(str(f))
            listing = _get_listing(port)
            version = _bucket_entry(listing, "doc.txt")["version"]
            assert version["vclock"] == {_cid(): 2}, version
        finally:
            srv.stop()


def test_listing_idempotent_when_content_unchanged(tmp_path):
    """Repeated listings of unchanged content must not advance the clock."""
    f = tmp_path / "doc.txt"
    f.write_text("stable\n")

    cfg = {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None, "CID": _cid()}
    with (
        patch("hsync_client.lan_server.CONFIG", cfg),
        patch("hsync_client.state_backend.CONFIG", cfg),
    ):
        srv, port = _server_fixture()
        try:
            first = _bucket_entry(_get_listing(port), "doc.txt")["version"]["vclock"]
            second = _bucket_entry(_get_listing(port), "doc.txt")["version"]["vclock"]
            assert first == second == {_cid(): 1}
        finally:
            srv.stop()


def test_watcher_self_records_local_edit_but_not_inbound(tmp_path):
    """The master watcher persists a self-record for local edits only.

    Inbound edits carry an origin in the inbound-origin map (their clock was
    already registered with the merged history at upload acceptance) and must
    NOT be re-ticked as the master's own edit.
    """
    from hsync_common.models.file import File as LocalFile

    from hsync_client import lan_websocket as lw
    from hsync_client.sync_state import clear_base, flush, get_base_hash

    f = tmp_path / "doc.txt"
    f.write_text("local\n")

    ws_server = lw.LANWebSocketServer(port=0)
    handler = lw.LANMasterFileEventHandler(ws_server)

    file_obj = LocalFile.get(str(f))

    cfg = {
        "LOCAL_BUCKET": str(tmp_path),
        "REMOTE_BUCKET": "test",
        "LAN_KEY": None,
        "CID": _cid(),
    }
    with (
        patch("hsync_client.state_backend.CONFIG", cfg),
        patch("hsync_client.lan_websocket.CONFIG", cfg),
    ):
        # Local edit: no inbound origin → self-record happens.
        handler._on_file_ready(file_obj, str(f))
        assert get_base_hash(str(f)) is not None

        # Inbound edit: origin present → no self-record (store untouched).
        lw.mark_inbound_origin(str(f), "TestClientY1234567890")
        clear_base(str(f))
        flush()
        assert get_base_hash(str(f)) is None
        handler._on_file_ready(file_obj, str(f))
        assert get_base_hash(str(f)) is None
