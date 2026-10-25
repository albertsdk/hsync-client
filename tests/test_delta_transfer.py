# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the client delta transfer orchestration (upload/download/P2P)."""

from __future__ import annotations

import json
import random
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hsync_common.delta import compute_signatures
from hsync_common.models.file import File

from hsync_client import delta_transfer as dt
from hsync_client.delta_transfer import (
    DeltaTransferError,
    download_file_delta,
    prepare_delta_download,
    prepare_delta_upload,
    upload_file_delta,
)

BLOCK = 4096  # DELTA_BLOCK_SIZE_SMALL


def _base_bytes(seed: int = 11, size: int = BLOCK * 12) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


@pytest.fixture
def client_config(monkeypatch, tmp_path):
    """A fully-featured CONFIG with delta enabled on both sides."""

    cfg = {
        "API": {
            "buckets": "https://server/api/v1/buckets",
            "events": "wss://server/api/v1/buckets",
        },
        "CID": "test-cid",
        "LOCAL_BUCKET": str(tmp_path / "local"),
        "REMOTE_BUCKET": "bucket1",
        "REQUEST_HEADERS": {"Authorization": "Bearer k"},
        "DELTA_ENABLED": True,
        "SERVER_DELTA_ENABLED": True,
        "COMPRESSION_ENABLED": False,
        "SERVER_COMPRESSION_ENABLED": False,
        "P2P_MODE": False,
        "HTTP_TIMEOUT": 10,
        "MAX_INBOUND_SIZE": 0,
    }
    monkeypatch.setattr(dt, "CONFIG", cfg)
    # Lower the delta size floor so small fixtures are eligible.
    monkeypatch.setattr(dt, "_delta_min_size", lambda: 1024)
    monkeypatch.setattr(
        "hsync_client.delta_transfer.get_base_hash", lambda p: _FAKE_BASE_HASHES.get(p)
    )
    Path(cfg["LOCAL_BUCKET"]).mkdir(parents=True, exist_ok=True)
    return cfg


_FAKE_BASE_HASHES: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _clean_state():
    _FAKE_BASE_HASHES.clear()
    yield
    _FAKE_BASE_HASHES.clear()


def _mk_file(tmp_path: Path, rel: str, data: bytes) -> str:
    target = tmp_path / "local" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return str(target)


# ---------------------------------------------------------------------------
# prepare_* gates
# ---------------------------------------------------------------------------


class TestPrepare:
    def test_upload_gate_ok(self, client_config, tmp_path):
        base = _base_bytes()
        new = base + b"changed"
        abs_path = _mk_file(tmp_path, "big.bin", new)
        _FAKE_BASE_HASHES[abs_path] = sha256(base).hexdigest()
        f = File(filepath=abs_path, size=len(new), sha256sum=sha256(new).hexdigest())
        ctx = prepare_delta_upload(f, "big.bin")
        assert ctx is not None
        assert ctx.base_hash == sha256(base).hexdigest()

    def test_upload_gate_no_capability(self, client_config, tmp_path, monkeypatch):
        monkeypatch.setitem(dt.CONFIG, "SERVER_DELTA_ENABLED", False)
        abs_path = _mk_file(tmp_path, "big.bin", _base_bytes())
        _FAKE_BASE_HASHES[abs_path] = sha256(b"x").hexdigest()
        f = File(filepath=abs_path, size=50000, sha256sum=sha256(b"y").hexdigest())
        assert prepare_delta_upload(f, "big.bin") is None

    def test_upload_gate_small_file(self, client_config, tmp_path):
        abs_path = _mk_file(tmp_path, "small.bin", b"tiny")
        _FAKE_BASE_HASHES[abs_path] = sha256(b"x").hexdigest()
        f = File(filepath=abs_path, size=4, sha256sum=sha256(b"tiny2").hexdigest())
        assert prepare_delta_upload(f, "small.bin") is None

    def test_upload_gate_no_base(self, client_config, tmp_path):
        abs_path = _mk_file(tmp_path, "big.bin", _base_bytes())
        f = File(filepath=abs_path, size=50000, sha256sum=sha256(b"y").hexdigest())
        assert prepare_delta_upload(f, "big.bin") is None

    def test_upload_gate_local_equals_base(self, client_config, tmp_path):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        _FAKE_BASE_HASHES[abs_path] = sha256(base).hexdigest()
        f = File(filepath=abs_path, size=len(base), sha256sum=sha256(base).hexdigest())
        assert prepare_delta_upload(f, "big.bin") is None

    def test_download_gate_ok(self, client_config, tmp_path):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        _FAKE_BASE_HASHES[abs_path] = sha256(base).hexdigest()
        new = base + b"remote-change"
        f = File(filepath="big.bin", size=len(new), sha256sum=sha256(new).hexdigest())
        ctx = prepare_delta_download(f, abs_path)
        assert ctx is not None
        assert ctx.local_sha256 == sha256(base).hexdigest()

    def test_download_gate_local_changed(self, client_config, tmp_path):
        """Local file is NOT the recorded base → conflict path, no delta."""
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base + b"local-edit")
        _FAKE_BASE_HASHES[abs_path] = sha256(base).hexdigest()
        new = _base_bytes(seed=5)
        f = File(filepath="big.bin", size=len(new), sha256sum=sha256(new).hexdigest())
        assert prepare_delta_download(f, abs_path) is None

    def test_download_gate_missing_local_file(self, client_config, tmp_path):
        abs_path = str(tmp_path / "local" / "ghost.bin")
        f = File(filepath="ghost.bin", size=50000, sha256sum=sha256(b"y").hexdigest())
        assert prepare_delta_download(f, abs_path) is None


# ---------------------------------------------------------------------------
# upload_file_delta
# ---------------------------------------------------------------------------


def _response(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    return resp


def _parse_streaming_multipart(body, content_type: str):
    """Parse the small multipart body the delta upload streams (test helper)."""
    raw = b"".join(body)
    boundary = content_type.split("boundary=")[1].strip().strip('"').encode()
    fields: dict[str, bytes] = {}
    file_bytes = b""
    for part in raw.split(b"--" + boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]  # the multipart CRLF before the next boundary
        if b'name="metadata"' in head:
            fields["metadata"] = data
        elif b'name="path"' in head:
            fields["path"] = data
        elif b'name="file"' in head:
            file_bytes = data
    return fields, file_bytes


class TestUploadDelta:
    def test_full_flow(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        new = bytearray(base)
        new[100:200] = b"Z" * 100
        new.extend(b"GROW")
        abs_path = _mk_file(tmp_path, "big.bin", bytes(new))
        base_hash = sha256(base).hexdigest()
        _FAKE_BASE_HASHES[abs_path] = base_hash

        server_base = tmp_path / "server-base.bin"
        server_base.write_bytes(base)
        sigs = compute_signatures(str(server_base), BLOCK)

        calls: dict[str, object] = {}

        def fake_get(uri, **kwargs):
            calls["get_uri"] = uri
            return _response(
                200,
                {
                    "block_size": BLOCK,
                    "file_size": len(base),
                    "sha256": base_hash,
                    "signatures": [s.to_dict() for s in sigs],
                },
            )

        def fake_post(uri, **kwargs):
            calls["post_uri"] = uri
            body = kwargs["data"]
            content_type = kwargs["headers"]["Content-Type"]
            # Streaming contract: a sized iterable (requests streams it with a
            # Content-Length), not a bytes/dict body built in RAM.
            calls["post_payload_is_stream"] = hasattr(body, "__len__") and not isinstance(
                body, (bytes, dict)
            )
            fields, file_bytes = _parse_streaming_multipart(body, content_type)
            calls["metadata"] = json.loads(fields["metadata"])
            calls["post_payload"] = file_bytes
            return _response(
                200, {"status": "ok", "sha256": sha256(bytes(new)).hexdigest(), "size": len(new)}
            )

        monkeypatch.setattr(dt, "get", fake_get)
        monkeypatch.setattr(dt, "post", fake_post)

        f = File(filepath=abs_path, size=len(new), sha256sum=sha256(bytes(new)).hexdigest())
        ctx = prepare_delta_upload(f, "big.bin")
        assert ctx is not None
        upload_file_delta(f, ctx)

        assert calls["get_uri"].endswith("/files/delta/signatures")
        assert calls["post_uri"].endswith("/files/delta/apply")
        metadata = calls["metadata"]
        assert metadata["base_sha256"] == base_hash
        assert metadata["new_sha256"] == sha256(bytes(new)).hexdigest()
        assert metadata["new_size"] == len(new)
        assert any(op["type"] == "copy" for op in metadata["ops"])
        # literal payload is much smaller than the whole file, and it is
        # streamed from a file object rather than passed as a bytes body.
        assert calls["post_payload_is_stream"] is True
        assert len(calls["post_payload"]) < len(new) // 3

    def test_signatures_404_is_recoverable(self, client_config, tmp_path, monkeypatch):
        abs_path = _mk_file(tmp_path, "big.bin", _base_bytes())
        monkeypatch.setattr(dt, "get", lambda *a, **k: _response(404, {"detail": "Not Found"}))
        f = File(filepath=abs_path, size=50000, sha256sum=sha256(b"y").hexdigest())
        ctx = dt.DeltaUploadContext(relpath="big.bin", base_hash=sha256(b"x").hexdigest())
        with pytest.raises(DeltaTransferError, match="signatures unavailable"):
            upload_file_delta(f, ctx)

    def test_remote_base_changed_aborts(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base + b"edit")
        other_hash = sha256(b"other").hexdigest()
        monkeypatch.setattr(
            dt,
            "get",
            lambda *a, **k: _response(
                200,
                {
                    "block_size": BLOCK,
                    "file_size": len(base),
                    "sha256": other_hash,
                    "signatures": [],
                },
            ),
        )
        f = File(
            filepath=abs_path, size=len(base) + 4, sha256sum=sha256(base + b"edit").hexdigest()
        )
        ctx = dt.DeltaUploadContext(relpath="big.bin", base_hash=sha256(base).hexdigest())
        with pytest.raises(DeltaTransferError, match="remote base changed"):
            upload_file_delta(f, ctx)

    def test_apply_409_is_recoverable(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        base_hash = sha256(base).hexdigest()
        server_base = tmp_path / "server-base.bin"
        server_base.write_bytes(base)
        sigs = compute_signatures(str(server_base), BLOCK)
        monkeypatch.setattr(
            dt,
            "get",
            lambda *a, **k: _response(
                200,
                {
                    "block_size": BLOCK,
                    "file_size": len(base),
                    "sha256": base_hash,
                    "signatures": [s.to_dict() for s in sigs],
                },
            ),
        )
        monkeypatch.setattr(
            dt, "post", lambda *a, **k: _response(409, {"detail": "base_file_changed"})
        )
        f = File(filepath=abs_path, size=len(base), sha256sum=base_hash)
        ctx = dt.DeltaUploadContext(relpath="big.bin", base_hash=base_hash)
        with pytest.raises(DeltaTransferError, match="apply rejected"):
            upload_file_delta(f, ctx)

    def test_apply_409_with_matching_target_is_success(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        new = base + b"edit"
        abs_path = _mk_file(tmp_path, "big.bin", new)
        base_hash = sha256(base).hexdigest()
        new_hash = sha256(new).hexdigest()
        server_base = tmp_path / "server-base.bin"
        server_base.write_bytes(base)
        sigs = compute_signatures(str(server_base), BLOCK)
        monkeypatch.setattr(
            dt,
            "get",
            lambda *a, **k: _response(
                200,
                {
                    "block_size": BLOCK,
                    "file_size": len(base),
                    "sha256": base_hash,
                    "signatures": [s.to_dict() for s in sigs],
                },
            ),
        )
        monkeypatch.setattr(
            dt,
            "post",
            lambda *a, **k: _response(
                409,
                {"detail": "base_file_changed", "actual_base_sha256": new_hash},
            ),
        )
        f = File(filepath=abs_path, size=len(new), sha256sum=new_hash)
        ctx = dt.DeltaUploadContext(relpath="big.bin", base_hash=base_hash)
        # The server's current file already holds this delta's target content:
        # the idempotent conflict is a success, not a fallback/error.
        upload_file_delta(f, ctx)

    def test_apply_5xx_raises_server_error(self, client_config, tmp_path, monkeypatch):
        from hsync_client.exceptions import ServerError

        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        base_hash = sha256(base).hexdigest()
        server_base = tmp_path / "server-base.bin"
        server_base.write_bytes(base)
        sigs = compute_signatures(str(server_base), BLOCK)
        monkeypatch.setattr(
            dt,
            "get",
            lambda *a, **k: _response(
                200,
                {
                    "block_size": BLOCK,
                    "file_size": len(base),
                    "sha256": base_hash,
                    "signatures": [s.to_dict() for s in sigs],
                },
            ),
        )
        monkeypatch.setattr(dt, "post", lambda *a, **k: _response(503))
        f = File(filepath=abs_path, size=len(base), sha256sum=base_hash)
        ctx = dt.DeltaUploadContext(relpath="big.bin", base_hash=base_hash)
        with pytest.raises(ServerError, match="Server error on delta apply"):
            upload_file_delta(f, ctx)


# ---------------------------------------------------------------------------
# download_file_delta
# ---------------------------------------------------------------------------


class TestDownloadDelta:
    def test_full_flow(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        new = bytearray(base)
        new[BLOCK : BLOCK + 64] = b"D" * 64
        abs_path = _mk_file(tmp_path, "big.bin", base)
        _FAKE_BASE_HASHES[abs_path] = sha256(base).hexdigest()

        # Simulate the server side: match client sigs against the new content.
        from hsync_common.delta import find_matches

        sigs = compute_signatures(abs_path, BLOCK)
        ops, literals = find_matches(bytes(new), sigs, BLOCK)
        token = "abc123"

        def fake_post(uri, **kwargs):
            return _response(
                200,
                {
                    "delta": True,
                    "block_size": BLOCK,
                    "new_sha256": sha256(bytes(new)).hexdigest(),
                    "instructions": {"new_size": len(new), "ops": [op.to_dict() for op in ops]},
                    "data_url": f"/api/v1/buckets/bucket1/files/delta/data/{token}",
                    "data_size": len(literals),
                    "compressed": False,
                },
            )

        class FakeRaw:
            def __init__(self, data: bytes):
                self._data = data
                self.decode_content = False

            def read(self, n=-1):
                data, self._data = self._data[:n], self._data[n:]
                return data

        class FakeStreamResponse:
            status_code = 200
            headers: dict = {}

            def __init__(self, data: bytes):
                self.raw = FakeRaw(data)
                self._data = data

            def iter_content(self, chunk_size=1024):
                for i in range(0, len(self._data), chunk_size):
                    yield self._data[i : i + chunk_size]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(dt, "post", fake_post)
        monkeypatch.setattr(dt, "get", lambda *a, **k: FakeStreamResponse(literals))

        f = File(filepath="big.bin", size=len(new), sha256sum=sha256(bytes(new)).hexdigest())
        ctx = prepare_delta_download(f, abs_path)
        assert ctx is not None

        events: list[tuple] = []
        from hsync_client import progress as progress_mod

        monkeypatch.setattr(
            progress_mod, "emit_event", lambda name, **kw: events.append((name, kw))
        )
        # In-flight samples travel on the best-effort progress path.
        monkeypatch.setattr(
            progress_mod, "emit_progress", lambda name, **kw: events.append((name, kw))
        )
        download_file_delta(f, ctx)

        assert Path(abs_path).read_bytes() == bytes(new)
        # No leftovers
        assert not Path(f"{abs_path}.delta.part").exists()
        assert not Path(f"{abs_path}.delta.part.lit").exists()

        from hsync_client.events import UiEventName

        progress_events = [kw for name, kw in events if name == UiEventName.progress]
        assert progress_events, "delta download must emit progress"
        assert progress_events[-1]["file"] == abs_path
        assert progress_events[-1]["total"] == len(literals)
        assert progress_events[-1]["transferred"] == len(literals)
        assert progress_events[-1]["op"] == "download"

    def test_delta_false_is_recoverable(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        monkeypatch.setattr(
            dt,
            "post",
            lambda *a, **k: _response(200, {"delta": False, "reason": "too_many_changed_blocks"}),
        )
        f = File(filepath="big.bin", size=50000, sha256sum=sha256(b"y").hexdigest())
        ctx = dt.DeltaDownloadContext(
            base_hash=sha256(base).hexdigest(),
            local_path=abs_path,
            local_sha256=sha256(base).hexdigest(),
        )
        with pytest.raises(DeltaTransferError, match="too_many_changed_blocks"):
            download_file_delta(f, ctx)

    def test_payload_410_is_recoverable(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        new = base + b"x"
        monkeypatch.setattr(
            dt,
            "post",
            lambda *a, **k: _response(
                200,
                {
                    "delta": True,
                    "block_size": BLOCK,
                    "new_sha256": sha256(new).hexdigest(),
                    "instructions": {
                        "new_size": len(new),
                        "ops": [
                            {"type": "data", "offset": 0, "length": len(new), "data_offset": 0}
                        ],
                    },
                    "data_url": "/api/v1/buckets/bucket1/files/delta/data/tok",
                    "data_size": len(new),
                    "compressed": False,
                },
            ),
        )
        gone = MagicMock()
        gone.status_code = 410
        gone.__enter__ = lambda s: s
        gone.__exit__ = lambda *a: False
        monkeypatch.setattr(dt, "get", lambda *a, **k: gone)
        f = File(filepath="big.bin", size=len(new), sha256sum=sha256(new).hexdigest())
        ctx = dt.DeltaDownloadContext(
            base_hash=sha256(base).hexdigest(),
            local_path=abs_path,
            local_sha256=sha256(base).hexdigest(),
        )
        with pytest.raises(DeltaTransferError, match="payload unavailable"):
            download_file_delta(f, ctx)

    def test_hostile_data_url_rejected(self, client_config, tmp_path, monkeypatch):
        base = _base_bytes()
        abs_path = _mk_file(tmp_path, "big.bin", base)
        new = base + b"x"
        monkeypatch.setattr(
            dt,
            "post",
            lambda *a, **k: _response(
                200,
                {
                    "delta": True,
                    "block_size": BLOCK,
                    "new_sha256": sha256(new).hexdigest(),
                    "instructions": {
                        "new_size": len(new),
                        "ops": [
                            {"type": "data", "offset": 0, "length": len(new), "data_offset": 0}
                        ],
                    },
                    "data_url": "https://evil.example.com/steal",
                    "data_size": 1,
                    "compressed": False,
                },
            ),
        )
        f = File(filepath="big.bin", size=len(new), sha256sum=sha256(new).hexdigest())
        ctx = dt.DeltaDownloadContext(
            base_hash=sha256(base).hexdigest(),
            local_path=abs_path,
            local_sha256=sha256(base).hexdigest(),
        )
        with pytest.raises(DeltaTransferError, match="data_url"):
            download_file_delta(f, ctx)


# ---------------------------------------------------------------------------
# P2P handlers (stdlib master server)
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Minimal stand-in for P2PRequestHandler (captures responses)."""

    def __init__(self, body: bytes = b""):
        from io import BytesIO

        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.headers: dict[str, str] = {}
        self.sent: list[tuple[int, object]] = []
        self.response_status: int | None = None
        self.response_headers: dict[str, str] = {}

    def _send_json_response(self, status, data):
        self.sent.append((status, data))

    def _send_error_response(self, status, message):
        self.sent.append((status, {"error": message}))

    def _send_error_response_safe(self, status, message):
        self.sent.append((status, {"error": message}))

    def send_response(self, status):
        self.response_status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def p2p_config(monkeypatch, tmp_path):
    from hsync_client import p2p_delta as ld

    cfg = {
        "LOCAL_BUCKET": str(tmp_path / "master"),
        "REMOTE_BUCKET": "bucket1",
        "DELTA_ENABLED": True,
        "COMPRESSION_ENABLED": False,
        "P2P_KEY": None,
    }
    monkeypatch.setattr(ld, "CONFIG", cfg)
    Path(cfg["LOCAL_BUCKET"]).mkdir(parents=True, exist_ok=True)
    ld.reset_delta_p2p_state()
    yield cfg
    ld.reset_delta_p2p_state()


class TestP2PDelta:
    def test_signatures_get(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        base = _base_bytes()
        target = Path(p2p_config["LOCAL_BUCKET"]) / "big.bin"
        target.write_bytes(base)
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {"path": ["big.bin"]})
        status, body = handler.sent[0]
        assert status == 200
        assert body["sha256"] == sha256(base).hexdigest()
        assert body["block_size"] == BLOCK
        assert len(body["signatures"]) == 12

    def test_signatures_get_disabled_is_404(self, p2p_config, monkeypatch):
        from hsync_client import p2p_delta as ld

        monkeypatch.setitem(ld.CONFIG, "DELTA_ENABLED", False)
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {"path": ["big.bin"]})
        assert handler.sent[0][0] == 404

    def test_signatures_post_and_data_roundtrip(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        base = _base_bytes()
        new = bytearray(base)
        new[200:264] = b"L" * 64
        target = Path(p2p_config["LOCAL_BUCKET"]) / "big.bin"
        target.write_bytes(bytes(new))

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = [s.to_dict() for s in compute_signatures(str(client_base), BLOCK)]
        body = json.dumps({"path": "big.bin", "block_size": BLOCK, "signatures": sigs}).encode()

        handler = _FakeHandler(body)
        handler.headers["Content-Length"] = str(len(body))
        ld.handle_delta_signatures_post(handler)
        status, resp_body = handler.sent[0]
        assert status == 200
        assert resp_body["delta"] is True
        token = resp_body["data_url"].rsplit("/", 1)[-1]

        # Fetch the payload through the data handler
        data_handler = _FakeHandler()
        ld.handle_delta_data(data_handler, "bucket1", token)
        assert data_handler.response_status == 200
        payload = data_handler.wfile.getvalue()
        assert len(payload) == resp_body["data_size"]

        # Rebuild locally
        from hsync_common.delta import apply_delta
        from hsync_common.models.delta import DeltaOp

        ops = [DeltaOp(**o) for o in resp_body["instructions"]["ops"]]
        out = tmp_path / "rebuilt.bin"
        apply_delta(str(client_base), ops, payload, str(out))
        assert out.read_bytes() == bytes(new)

        # Token is single-use
        again = _FakeHandler()
        ld.handle_delta_data(again, "bucket1", token)
        assert again.sent[0][0] == 404

    def test_signatures_post_not_worthwhile(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        new = _base_bytes(seed=1)
        base = _base_bytes(seed=2)
        target = Path(p2p_config["LOCAL_BUCKET"]) / "big.bin"
        target.write_bytes(new)
        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = [s.to_dict() for s in compute_signatures(str(client_base), BLOCK)]
        body = json.dumps({"path": "big.bin", "block_size": BLOCK, "signatures": sigs}).encode()
        handler = _FakeHandler(body)
        handler.headers["Content-Length"] = str(len(body))
        ld.handle_delta_signatures_post(handler)
        status, resp_body = handler.sent[0]
        assert status == 200
        assert resp_body["delta"] is False
        assert resp_body["reason"] == "too_many_changed_blocks"

    def test_data_wrong_bucket_is_404(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        token = ld._token_store.issue(
            bucket="bucket1", payload=b"data", compressed=False, meta={"relpath": "x"}
        )
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket2", token)
        assert handler.sent[0][0] == 404

    def test_apply_roundtrip(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        base = _base_bytes()
        new = bytearray(base)
        new[500:600] = b"A" * 100
        target = Path(p2p_config["LOCAL_BUCKET"]) / "big.bin"
        target.write_bytes(base)

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        from hsync_common.delta import find_matches

        sigs = compute_signatures(str(client_base), BLOCK)
        ops, literals = find_matches(bytes(new), sigs, BLOCK)
        metadata = {
            "cid": "slave-1",
            "base_sha256": sha256(base).hexdigest(),
            "new_sha256": sha256(bytes(new)).hexdigest(),
            "new_size": len(new),
            "block_size": BLOCK,
            "compressed": False,
            "ops": [op.to_dict() for op in ops],
        }
        lit = tmp_path / "lit.bin"
        lit.write_bytes(literals)
        handler = _FakeHandler()
        ld.handle_delta_apply(
            handler, {"path": "big.bin", "metadata": json.dumps(metadata)}, str(lit)
        )
        status, resp_body = handler.sent[0]
        assert status == 200
        assert resp_body["sha256"] == sha256(bytes(new)).hexdigest()
        assert target.read_bytes() == bytes(new)

    def test_apply_base_changed_is_409(self, p2p_config, tmp_path):
        from hsync_client import p2p_delta as ld

        base = _base_bytes()
        target = Path(p2p_config["LOCAL_BUCKET"]) / "big.bin"
        target.write_bytes(base)
        metadata = {
            "cid": "slave-1",
            "base_sha256": sha256(b"stale").hexdigest(),
            "new_sha256": sha256(base + b"x").hexdigest(),
            "new_size": len(base) + 1,
            "block_size": BLOCK,
            "compressed": False,
            "ops": [],
        }
        lit = tmp_path / "lit.bin"
        lit.write_bytes(b"")
        handler = _FakeHandler()
        ld.handle_delta_apply(
            handler, {"path": "big.bin", "metadata": json.dumps(metadata)}, str(lit)
        )
        status, resp_body = handler.sent[0]
        assert status == 409
        assert resp_body["detail"] == "base_file_changed"
