# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Extended tests for the P2P master delta handlers (``hsync_client.p2p_delta``).

Covers the paths left out by ``test_delta_transfer.py``: input validation and
security gating on every endpoint, error status codes (400/403/404/409/410/
422), zstd compression of literal payloads, and the full token lifecycle
(issue, lazy reaper, single-use consumption, reset).

``p2p_delta`` owns one process-scoped temp directory for parked payloads, removed
at interpreter exit. A test that swaps ``_payload_dir`` MUST remove whatever it
creates (see ``test_payload_dir_is_a_singleton``) or the suite leaks one empty
``hsync-delta-lan-*`` directory per session.
"""

from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

import pytest
import zstandard as zstd
from hsync_common.constants import PARTIAL_DELTA_SUFFIX
from hsync_common.delta import DeltaTokenStore, compute_signatures, find_matches

from hsync_client import p2p_delta as ld
from hsync_client import progress as progress_mod

BLOCK = 4096  # DELTA_BLOCK_SIZE_SMALL


def _issue(bucket: str = "bucket1", relpath: str = "x", payload: bytes = b"data", compressed=False):
    """Issue a token through the shared store (``None`` when the cap is reached)."""
    try:
        return ld._token_store.issue(
            bucket=bucket,
            payload=payload,
            compressed=compressed,
            meta={"relpath": relpath},
        )
    except ld.TokenStoreFull:
        return None


def _swap_store(monkeypatch, **kwargs) -> DeltaTokenStore:
    """Replace the module store with a fresh one bound to the module temp dir."""
    store = DeltaTokenStore(
        ttl=kwargs.pop("ttl", ld._token_store.ttl),
        max_tokens=kwargs.pop("max_tokens", ld._MAX_TOKENS),
        tmp_dir=lambda: Path(ld._get_payload_dir()),
        **kwargs,
    )
    monkeypatch.setattr(ld, "_token_store", store)
    return store


def _base_bytes(seed: int = 11, size: int = BLOCK * 12) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


class _FakeHandler:
    """Minimal stand-in for P2PRequestHandler (captures responses)."""

    def __init__(self, body: bytes = b""):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers: dict[str, str] = {}
        self.sent: list[tuple[int, object]] = []
        self.response_status: int | None = None
        self.response_headers: dict[str, str] = {}

    def _send_json_response(self, status, data):
        self.sent.append((status, data))

    def _send_error_response(self, status, message):
        self.sent.append((status, {"error": message}))

    def send_response(self, status):
        self.response_status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def p2p_config(monkeypatch, tmp_path):
    """CONFIG with delta enabled on a tmp LOCAL_BUCKET; clean token state."""
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


def _master_file(p2p_config: dict, rel: str = "big.bin", data: bytes | None = None) -> Path:
    target = Path(p2p_config["LOCAL_BUCKET"]) / rel
    target.write_bytes(data if data is not None else _base_bytes())
    return target


def _post_handler(payload: dict | bytes) -> _FakeHandler:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    handler = _FakeHandler(body)
    handler.headers["Content-Length"] = str(len(body))
    return handler


def _sigs_for(path: str, block_size: int = BLOCK) -> list[dict]:
    return [s.to_dict() for s in compute_signatures(path, block_size)]


# ---------------------------------------------------------------------------
# _resolve_servable via handle_delta_signatures_get
# ---------------------------------------------------------------------------


class TestResolveServable:
    def test_missing_path_param_is_400(self, p2p_config):
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {})
        assert handler.sent[0][0] == 400

    def test_path_traversal_is_403(self, p2p_config):
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {"path": ["../evil.bin"]})
        assert handler.sent[0][0] == 403

    def test_skipped_path_is_403(self, p2p_config, monkeypatch):
        _master_file(p2p_config)
        monkeypatch.setattr(ld, "should_skip", lambda _path: True)
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {"path": ["big.bin"]})
        assert handler.sent[0][0] == 403

    def test_missing_file_is_404(self, p2p_config):
        handler = _FakeHandler()
        ld.handle_delta_signatures_get(handler, {"path": ["ghost.bin"]})
        assert handler.sent[0][0] == 404


# ---------------------------------------------------------------------------
# handle_delta_signatures_post validation
# ---------------------------------------------------------------------------


class TestSignaturesPostValidation:
    def test_disabled_is_404(self, p2p_config, monkeypatch):
        monkeypatch.setitem(ld.CONFIG, "DELTA_ENABLED", False)
        handler = _post_handler({"path": "big.bin", "block_size": BLOCK, "signatures": []})
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 404

    def test_garbage_content_length_is_400(self, p2p_config):
        handler = _FakeHandler(b"{}")
        handler.headers["Content-Length"] = "not-a-number"
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_zero_content_length_is_400(self, p2p_config):
        handler = _FakeHandler()
        handler.headers["Content-Length"] = "0"
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_oversized_body_is_400(self, p2p_config):
        handler = _FakeHandler()
        handler.headers["Content-Length"] = str(ld._MAX_SIG_BODY_BYTES + 1)
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_invalid_json_is_400(self, p2p_config):
        handler = _post_handler(b"{not json")
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_non_dict_body_is_400(self, p2p_config):
        handler = _post_handler(b"[1, 2, 3]")
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_path_traversal_is_403(self, p2p_config):
        handler = _post_handler({"path": "../evil.bin", "block_size": BLOCK, "signatures": []})
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 403

    @pytest.mark.parametrize("block_size", ["abc", 0, -5, 4.5, None])
    def test_invalid_block_size_is_400(self, p2p_config, block_size):
        _master_file(p2p_config)
        handler = _post_handler({"path": "big.bin", "block_size": block_size, "signatures": []})
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    @pytest.mark.parametrize("sigs", ["nope", 42, [{"bad": "entry"}], [1, 2, 3]])
    def test_invalid_signatures_is_400(self, p2p_config, sigs):
        _master_file(p2p_config)
        handler = _post_handler({"path": "big.bin", "block_size": BLOCK, "signatures": sigs})
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 400

    def test_literal_too_large_reason(self, p2p_config, tmp_path, monkeypatch):
        """Delta rejected because the literal payload exceeds the cap."""
        new = _base_bytes(seed=1)
        base = _base_bytes(seed=2)
        _master_file(p2p_config, data=new)
        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = _sigs_for(str(client_base))

        monkeypatch.setattr(ld, "is_delta_worthwhile", lambda *a, **k: False)
        monkeypatch.setattr(ld, "DELTA_MAX_LITERAL_SIZE", 1)
        handler = _post_handler({"path": "big.bin", "block_size": BLOCK, "signatures": sigs})
        ld.handle_delta_signatures_post(handler)
        status, body = handler.sent[0]
        assert status == 200
        assert body["delta"] is False
        assert body["reason"] == "literal_payload_too_large"

    def test_token_exhaustion_is_409(self, p2p_config, tmp_path, monkeypatch):
        """When the token registry is full the POST answers 409."""
        base = _base_bytes()
        new = bytearray(base)
        new[200:264] = b"L" * 64
        _master_file(p2p_config, data=bytes(new))
        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = _sigs_for(str(client_base))

        monkeypatch.setattr(
            ld,
            "_token_store",
            DeltaTokenStore(
                ttl=ld._token_store.ttl,
                max_tokens=0,
                tmp_dir=lambda: Path(ld._get_payload_dir()),
            ),
        )
        handler = _post_handler({"path": "big.bin", "block_size": BLOCK, "signatures": sigs})
        ld.handle_delta_signatures_post(handler)
        assert handler.sent[0][0] == 409

    def test_compressed_roundtrip(self, p2p_config, tmp_path, monkeypatch):
        """With compression enabled the payload is parked zstd-compressed."""
        base = _base_bytes()
        new = bytearray(base)
        new[200:264] = b"C" * 64
        _master_file(p2p_config, data=bytes(new))
        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = _sigs_for(str(client_base))

        monkeypatch.setitem(ld.CONFIG, "COMPRESSION_ENABLED", True)
        monkeypatch.setattr(ld, "should_compress", lambda *a, **k: True)

        handler = _post_handler({"path": "big.bin", "block_size": BLOCK, "signatures": sigs})
        ld.handle_delta_signatures_post(handler)
        status, body = handler.sent[0]
        assert status == 200
        assert body["delta"] is True
        assert body["compressed"] is True
        token = body["data_url"].rsplit("/", 1)[-1]

        data_handler = _FakeHandler()
        ld.handle_delta_data(data_handler, "bucket1", token)
        assert data_handler.response_status == 200
        from hsync_client.compression import CONTENT_ENCODING_ZSTD

        assert data_handler.response_headers.get("Content-Encoding") == CONTENT_ENCODING_ZSTD
        payload = (
            zstd.ZstdDecompressor().stream_reader(io.BytesIO(data_handler.wfile.getvalue())).read()
        )
        assert len(payload) == body["data_size"]


# ---------------------------------------------------------------------------
# handle_delta_apply validation
# ---------------------------------------------------------------------------


def _apply(handler: _FakeHandler, metadata: dict | str, literal: bytes, path: str = "big.bin"):
    lit_path = Path(handler._tmp_dir) / "lit.bin"
    lit_path.write_bytes(literal)
    fields = {
        "path": path,
        "metadata": metadata if isinstance(metadata, str) else json.dumps(metadata),
    }
    ld.handle_delta_apply(handler, fields, str(lit_path))


@pytest.fixture
def apply_handler(tmp_path):
    handler = _FakeHandler()
    handler._tmp_dir = tmp_path
    return handler


def _valid_metadata(base: bytes, new: bytes, ops: list[dict] | None = None) -> dict:
    return {
        "cid": "SlaveCid1234567890abc",
        "base_sha256": sha256(base).hexdigest(),
        "new_sha256": sha256(new).hexdigest(),
        "new_size": len(new),
        "block_size": BLOCK,
        "compressed": False,
        "ops": ops if ops is not None else [],
    }


class TestApplyValidation:
    def test_disabled_is_404(self, p2p_config, apply_handler, monkeypatch):
        monkeypatch.setitem(ld.CONFIG, "DELTA_ENABLED", False)
        _apply(apply_handler, {}, b"")
        assert apply_handler.sent[0][0] == 404

    def test_path_traversal_is_403(self, p2p_config, apply_handler):
        _apply(apply_handler, {}, b"", path="../evil.bin")
        assert apply_handler.sent[0][0] == 403

    def test_skipped_path_is_403(self, p2p_config, apply_handler, monkeypatch):
        _master_file(p2p_config)
        monkeypatch.setattr(ld, "should_skip", lambda _path: True)
        _apply(apply_handler, {}, b"")
        assert apply_handler.sent[0][0] == 403

    def test_missing_file_is_404(self, p2p_config, apply_handler):
        _apply(apply_handler, {}, b"", path="ghost.bin")
        assert apply_handler.sent[0][0] == 404

    def test_invalid_metadata_json_is_400(self, p2p_config, apply_handler):
        _master_file(p2p_config)
        _apply(apply_handler, "{not json", b"")
        assert apply_handler.sent[0][0] == 400

    def test_non_dict_metadata_is_400(self, p2p_config, apply_handler):
        _master_file(p2p_config)
        _apply(apply_handler, "[1, 2]", b"")
        assert apply_handler.sent[0][0] == 400

    @pytest.mark.parametrize(
        "field,value",
        [
            ("base_sha256", "not-hex"),
            ("new_sha256", "not-hex"),
            ("new_size", -1),
            ("new_size", "big"),
        ],
    )
    def test_invalid_metadata_fields_are_400(self, p2p_config, apply_handler, field, value):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        meta = _valid_metadata(base, base + b"x")
        meta[field] = value
        _apply(apply_handler, meta, b"")
        assert apply_handler.sent[0][0] == 400

    @pytest.mark.parametrize("ops", ["nope", 42, [{"type": "copy"}], [1, 2]])
    def test_invalid_ops_are_400(self, p2p_config, apply_handler, ops):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        meta = _valid_metadata(base, base + b"x", ops=ops)
        _apply(apply_handler, meta, b"")
        assert apply_handler.sent[0][0] == 400

    def test_copy_op_out_of_bounds_is_422(self, p2p_config, apply_handler):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        ops = [{"type": "copy", "offset": 0, "length": len(base) + 10, "data_offset": 0}]
        meta = _valid_metadata(base, b"x" * (len(base) + 10), ops=ops)
        _apply(apply_handler, meta, b"")
        status, body = apply_handler.sent[0]
        assert status == 422
        assert body["detail"] == "copy_op_out_of_bounds"

    def test_data_op_out_of_bounds_is_422(self, p2p_config, apply_handler):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        ops = [{"type": "data", "offset": 0, "length": 10, "data_offset": 0}]
        meta = _valid_metadata(base, b"x" * 10, ops=ops)
        _apply(apply_handler, meta, b"short")
        status, body = apply_handler.sent[0]
        assert status == 422
        assert body["detail"] == "data_op_out_of_bounds"

    def test_ops_coverage_mismatch_is_422(self, p2p_config, apply_handler):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        ops = [{"type": "copy", "offset": 0, "length": 100, "data_offset": 0}]
        meta = _valid_metadata(base, b"x" * 200, ops=ops)
        meta["new_size"] = 200
        _apply(apply_handler, meta, b"")
        status, body = apply_handler.sent[0]
        assert status == 422
        assert body["detail"] == "ops_coverage_mismatch"

    def test_hash_mismatch_is_422_and_cleans_part(self, p2p_config, apply_handler):
        base = _base_bytes()
        target = _master_file(p2p_config, data=base)
        ops = [{"type": "copy", "offset": 0, "length": len(base), "data_offset": 0}]
        meta = _valid_metadata(base, b"completely different content", ops=ops)
        meta["new_size"] = len(base)
        _apply(apply_handler, meta, b"")
        status, body = apply_handler.sent[0]
        assert status == 422
        assert body["detail"] == "reconstructed_file_hash_mismatch"
        assert not Path(f"{target}{PARTIAL_DELTA_SUFFIX}").exists()

    def test_leftover_cleanup_failure_is_tolerated(self, p2p_config, apply_handler, monkeypatch):
        """An OSError while removing the .part leftover must not propagate."""
        base = _base_bytes()
        target = _master_file(p2p_config, data=base)
        part_path = f"{target}{PARTIAL_DELTA_SUFFIX}"
        ops = [{"type": "copy", "offset": 0, "length": len(base), "data_offset": 0}]
        meta = _valid_metadata(base, b"completely different content", ops=ops)
        meta["new_size"] = len(base)

        real_remove = ld.os.remove

        def flaky_remove(path, *a, **k):
            if path == part_path:
                raise OSError("simulated cleanup failure")
            return real_remove(path, *a, **k)

        monkeypatch.setattr(ld.os, "remove", flaky_remove)
        _apply(apply_handler, meta, b"")
        assert apply_handler.sent[0][0] == 422  # hash mismatch, no crash in finally

    def test_size_mismatch_is_422(self, p2p_config, apply_handler, monkeypatch):
        """Size check after a (patched) hash pass: reconstructed size must match."""
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        ops = [{"type": "copy", "offset": 0, "length": len(base), "data_offset": 0}]
        meta = _valid_metadata(base, b"y" * len(base), ops=ops)

        def fake_apply(_base, _ops, _literal, out_path):
            Path(out_path).write_bytes(b"short")

        # The rebuild + verification pair lives in hsync-common (single
        # implementation shared with the server and the download path), so the
        # seam to fake is that module, not the p2p handler.
        monkeypatch.setattr("hsync_common.delta.reconstruct.apply_delta", fake_apply)
        monkeypatch.setattr(
            "hsync_common.delta.reconstruct.compute_sha256",
            lambda _p: meta["new_sha256"],
        )
        _apply(apply_handler, meta, b"")
        status, body = apply_handler.sent[0]
        assert status == 422
        assert body["detail"] == "reconstructed_file_size_mismatch"

    def test_corrupt_compressed_payload_is_400(self, p2p_config, apply_handler):
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        meta = _valid_metadata(base, base, ops=[])
        meta["compressed"] = True
        _apply(apply_handler, meta, b"this is not zstd data at all")
        assert apply_handler.sent[0][0] == 400

    def test_compressed_bomb_is_400(self, p2p_config, apply_handler, monkeypatch):
        """A compressed payload decompressing past the cap is rejected."""
        base = _base_bytes()
        _master_file(p2p_config, data=base)
        monkeypatch.setattr(ld, "DELTA_MAX_LITERAL_SIZE", 8)
        big_literal = zstd.ZstdCompressor().compress(b"A" * 100)
        meta = _valid_metadata(base, base, ops=[])
        meta["compressed"] = True
        _apply(apply_handler, meta, big_literal)
        assert apply_handler.sent[0][0] == 400

    def test_compressed_roundtrip_cleans_leftover(self, p2p_config, apply_handler, tmp_path):
        base = _base_bytes()
        new = bytearray(base)
        new[500:600] = b"A" * 100
        target = _master_file(p2p_config, data=base)

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = compute_signatures(str(client_base), BLOCK)
        ops, literals = find_matches(bytes(new), sigs, BLOCK)
        compressed = zstd.ZstdCompressor().compress(literals)

        meta = _valid_metadata(base, bytes(new), ops=[op.to_dict() for op in ops])
        meta["compressed"] = True
        _apply(apply_handler, meta, compressed)
        status, body = apply_handler.sent[0]
        assert status == 200
        assert body["sha256"] == sha256(bytes(new)).hexdigest()
        assert target.read_bytes() == bytes(new)
        # The decompressed temp file must not survive the request.
        leftover = Path(str(apply_handler._tmp_dir / "lit.bin") + ".decompressed")
        assert not leftover.exists()

    def test_success_applies_cid_mtime_permissions(
        self, p2p_config, apply_handler, tmp_path, monkeypatch
    ):
        base = _base_bytes()
        new = bytearray(base)
        new[500:600] = b"A" * 100
        target = _master_file(p2p_config, data=base)

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = compute_signatures(str(client_base), BLOCK)
        ops, literals = find_matches(bytes(new), sigs, BLOCK)

        monkeypatch.setitem(ld.CONFIG, "P2P_KEY", "secret")
        marked: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "hsync_client.p2p_websocket.mark_inbound_origin",
            lambda fp, cid: marked.append((fp, cid)),
        )
        perms: list[tuple[str, object]] = []
        monkeypatch.setattr(
            "hsync_client.utils.set_permissions",
            lambda fp, p: perms.append((fp, p)),
        )

        meta = _valid_metadata(base, bytes(new), ops=[op.to_dict() for op in ops])
        meta["cid"] = "PeerCID1234567890abcd"
        meta["mtime"] = 1_600_000_000.0
        meta["permissions"] = "0o644"
        _apply(apply_handler, meta, literals)
        status, _body = apply_handler.sent[0]
        assert status == 200
        assert marked == [(str(target), "PeerCID1234567890abcd")]
        assert perms == [(str(target), "0o644")]
        assert abs(target.stat().st_mtime - 1_600_000_000.0) < 2

    def test_invalid_mtime_is_ignored(self, p2p_config, apply_handler, tmp_path):
        base = _base_bytes()
        new = base + b"tail"
        _master_file(p2p_config, data=base)

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = compute_signatures(str(client_base), BLOCK)
        ops, literals = find_matches(new, sigs, BLOCK)

        meta = _valid_metadata(base, new, ops=[op.to_dict() for op in ops])
        meta["mtime"] = "not-a-float"
        _apply(apply_handler, meta, literals)
        assert apply_handler.sent[0][0] == 200


# ---------------------------------------------------------------------------
# handle_delta_data
# ---------------------------------------------------------------------------


class TestDeltaData:
    def test_disabled_is_404(self, p2p_config, monkeypatch):
        monkeypatch.setitem(ld.CONFIG, "DELTA_ENABLED", False)
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket1", "whatever")
        assert handler.sent[0][0] == 404

    def test_unknown_token_is_404(self, p2p_config):
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket1", "no-such-token")
        status, body = handler.sent[0]
        assert status == 404
        assert body["detail"] == "token_not_found"

    def test_expired_token_is_410(self, p2p_config, monkeypatch):
        # TTL 0 makes the entry expired at the endpoint's is_expired check; the
        # lazy reaper is stubbed so it cannot remove the token first (that path
        # is the 404 asserted by the cleanup tests).
        store = _swap_store(monkeypatch, ttl=0)
        monkeypatch.setattr(store, "cleanup", lambda *a, **k: 0)
        token = _issue()
        assert token is not None
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket1", token)
        status, body = handler.sent[0]
        assert status == 410
        assert body["detail"] == "token_expired"

    def test_missing_payload_file_is_410(self, p2p_config):
        token = _issue()
        assert token is not None
        entry = ld._token_store.lookup(token)
        assert entry is not None
        Path(entry.payload_file).unlink()
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket1", token)
        status, body = handler.sent[0]
        assert status == 410
        assert body["detail"] == "token_expired"

    def test_payload_removal_failure_is_tolerated(self, p2p_config, monkeypatch):
        token = _issue()
        assert token is not None
        entry = ld._token_store.lookup(token)
        assert entry is not None
        payload_file = entry.payload_file

        real_unlink = Path.unlink

        def flaky_unlink(self, *a, **k):
            if str(self) == payload_file:
                raise OSError("simulated removal failure")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        handler = _FakeHandler()
        ld.handle_delta_data(handler, "bucket1", token)
        assert handler.response_status == 200
        assert handler.wfile.getvalue() == b"data"


# ---------------------------------------------------------------------------
# Token store internals
# ---------------------------------------------------------------------------


class TestTokenStore:
    def test_cleanup_drops_expired_tokens_and_files(self, p2p_config):
        token = _issue()
        assert token is not None
        entry = ld._token_store.lookup(token)
        assert entry is not None
        payload_file = Path(entry.payload_file)
        ld._token_store.cleanup(now=time.time() + ld._token_store.ttl + 1)
        assert ld._token_store.lookup(token) is None
        assert not payload_file.exists()

    def test_cleanup_tolerates_missing_payload_file(self, p2p_config):
        token = _issue()
        assert token is not None
        entry = ld._token_store.lookup(token)
        assert entry is not None
        Path(entry.payload_file).unlink()
        ld._token_store.cleanup(now=time.time() + ld._token_store.ttl + 1)  # must not raise
        assert ld._token_store.lookup(token) is None

    def test_issue_from_missing_file_returns_none(self, p2p_config, tmp_path):
        """A payload file that vanished before adoption skips the delta."""
        token = ld._issue_token_from_file(
            bucket="bucket1",
            relpath="x",
            payload_file=str(tmp_path / "missing.bin"),
            size=4,
            compressed=False,
        )
        assert token is None

    def test_issue_token_registry_full_returns_none(self, p2p_config, monkeypatch):
        _swap_store(monkeypatch, max_tokens=0)
        # The payload dir is a process-wide singleton shared across tests:
        # assert no *new* file survives the failed issue, not that it is empty.
        before = set(Path(ld._get_payload_dir()).iterdir())
        token = _issue()
        assert token is None
        after = set(Path(ld._get_payload_dir()).iterdir())
        assert after == before

    def test_reset_removes_payloads_and_tolerates_missing(self, p2p_config):
        t1 = _issue(payload=b"a")
        t2 = _issue(relpath="y", payload=b"b")
        assert t1 and t2
        e1 = ld._token_store.lookup(t1)
        e2 = ld._token_store.lookup(t2)
        assert e1 is not None and e2 is not None
        p1, p2 = Path(e1.payload_file), Path(e2.payload_file)
        p2.unlink()  # force the missing-file branch
        ld.reset_delta_p2p_state()
        assert ld._token_store.count == 0
        assert not p1.exists()

    def test_payload_dir_is_a_singleton(self, p2p_config, monkeypatch):
        # The directory is process-scoped, so this test must not orphan the one it
        # creates: it drops it explicitly before ``monkeypatch`` restores the
        # previous ``_payload_dir``.
        existing = ld._get_payload_dir()
        assert ld._get_payload_dir() == existing

        monkeypatch.setattr(ld, "_payload_dir", None)
        created = ld._get_payload_dir()

        assert created != existing
        assert Path(created).is_dir()
        assert ld._get_payload_dir() == created

        ld._discard_payload_dir()

    def test_shutdown_cleanup_removes_the_payload_dir(self, p2p_config):
        """The registered shutdown hook drops the directory and its leftovers."""
        directory = ld._get_payload_dir()
        token = _issue()
        assert token is not None
        assert Path(directory).is_dir()

        ld._discard_payload_dir()

        assert not Path(directory).exists()
        assert ld._payload_dir is None
        # The hook resets the module state, so a later request in the same
        # process (a restarted seeder) gets a fresh directory instead of a
        # dangling one.
        assert Path(ld._get_payload_dir()).is_dir()

    def test_process_exit_leaves_no_payload_dir_behind(self):
        """Regression: parking a payload must not leak a temp dir per process.

        The directory was created once and never removed, so every client run
        left an empty ``hsync-delta-lan-*`` in the system temp dir. The check runs
        in a child process because only a real interpreter exit exercises the
        registered ``atexit`` handler.
        """
        script = (
            "import hsync_client.p2p_delta as ld\n"
            "token = ld._token_store.issue(bucket='b', payload=b'x', compressed=False)\n"
            "print(ld._get_payload_dir())\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )

        directory = Path(result.stdout.strip().splitlines()[-1])
        assert directory.name.startswith("hsync-delta-lan-")
        assert not directory.exists(), f"{directory} survived the child process exit"

    def test_process_scoped_temp_dirs_register_their_cleanup(self):
        """Convention guard: a production ``mkdtemp`` must carry a cleanup path.

        A process-scoped directory with no removal leaks one directory per run —
        the defect this module shipped with. Every module that creates one must
        either register an ``atexit`` hook or delete the tree itself, and this
        guard fails as soon as a new one appears without either.
        """
        package = Path(ld.__file__).parent
        offenders = [
            source.relative_to(package).as_posix()
            for source in sorted(package.rglob("*.py"))
            if "tempfile.mkdtemp(" in (text := source.read_text(encoding="utf-8"))
            and "atexit.register(" not in text
            and "rmtree(" not in text
        ]

        assert offenders == [], f"process-scoped temp dirs without cleanup: {offenders}"


# ---------------------------------------------------------------------------
# UI-event reporting on the master's delta paths
# ---------------------------------------------------------------------------


def _captured_events(monkeypatch) -> list[dict]:
    """Capture the emitted UI events at the progress sink boundary."""
    events: list[dict] = []

    def _record(event, **payload):
        events.append({"event": str(event), **payload})

    monkeypatch.setattr(progress_mod, "emit_progress", _record)
    monkeypatch.setattr(progress_mod, "emit_event", _record)
    return events


class TestDeltaServingReports:
    """Serving a parked payload is the file's upload: progress + one transfer."""

    def test_serving_the_payload_reports_progress_and_the_file(self, p2p_config, monkeypatch):
        master_file = _master_file(p2p_config, "big.bin", b"master-content")
        events = _captured_events(monkeypatch)
        token = _issue(relpath="big.bin", payload=b"literal-payload")
        assert token is not None
        handler = _FakeHandler()

        ld.handle_delta_data(handler, "bucket1", token)

        assert handler.response_status == 200
        assert handler.wfile.getvalue() == b"literal-payload"
        progress = [e for e in events if e["event"] == "progress"]
        assert progress, "a served delta payload must report progress"
        assert all(e["op"] == "upload" for e in progress)
        assert progress[-1]["file"] == str(master_file)
        assert progress[-1]["transferred"] == len(b"literal-payload")
        assert progress[-1]["total"] == len(b"literal-payload")
        transfers = [e for e in events if e["event"] == "transfer"]
        assert transfers == [
            {
                "event": "transfer",
                "op": "upload",
                "file": str(master_file),
                "size": len(b"master-content"),
            }
        ]


class TestDeltaApplyReports:
    """Applying a peer's delta commits a file: one terminal download event."""

    def test_applied_delta_reports_the_committed_file(
        self, p2p_config, apply_handler, tmp_path, monkeypatch
    ):
        base = _base_bytes()
        new = bytearray(base)
        new[100:200] = b"B" * 100
        target = _master_file(p2p_config, data=base)
        events = _captured_events(monkeypatch)

        client_base = tmp_path / "client-base.bin"
        client_base.write_bytes(base)
        sigs = compute_signatures(str(client_base), BLOCK)
        ops, literals = find_matches(bytes(new), sigs, BLOCK)
        meta = _valid_metadata(base, bytes(new), ops=[op.to_dict() for op in ops])

        _apply(apply_handler, meta, literals)

        assert apply_handler.sent[0][0] == 200
        transfers = [e for e in events if e["event"] == "transfer"]
        assert transfers == [
            {
                "event": "transfer",
                "op": "download",
                "file": str(target),
                "size": len(new),
            }
        ]
