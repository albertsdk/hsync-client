# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Focused immutable-snapshot, ranking, and REST tests for LAN sources."""

from __future__ import annotations

import http.client
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File
from hsync_common.protocol.peer_download import (
    LAN_SOURCES_MAX_BODY_BYTES,
    build_lan_inventory_frame,
    build_lan_sources_response,
)

from hsync_client.lan import Peer, PeerManager, PeerSnapshot
from hsync_client.lan_inventory import LanInventoryStore
from hsync_client.lan_server import LANServer
from hsync_client.lan_sources import (
    LanPeerSource,
    LanSourceResolver,
    LanSourceService,
    LanSourceTelemetry,
    PeerFailureKind,
)
from hsync_client.lan_websocket import SlaveCidRegistry
from hsync_client.pinning import PinStore

MASTER_CID = "M" * 21
REQUESTER_CID = "R" * 21
PEER_B = "B" * 21
PEER_C = "C" * 21
PEER_D = "D" * 21
PEER_E = "E" * 21
DIGEST = "a" * 64
CERT = "c" * 64


class NoShuffle:
    def shuffle(self, values: list) -> None:
        return


class StaticSourceService:
    bucket = "bucket"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, str]]]] = []

    def resolve(
        self,
        *,
        requester_cid: str,
        requester_address: str,
        files: list[dict[str, str]],
    ) -> dict:
        self.calls.append((requester_cid, requester_address, files))
        return build_lan_sources_response(
            files=[
                {"path": file["path"], "sha256": file["sha256"], "sources": []} for file in files
            ]
        )


def _post(
    port: int,
    path: str,
    body: bytes,
    *,
    content_type: str = "application/json",
    declared_length: int | None = None,
) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.putrequest("POST", path)
    connection.putheader("Content-Type", content_type)
    connection.putheader(
        "Content-Length",
        str(len(body) if declared_length is None else declared_length),
    )
    connection.endheaders()
    if body:
        connection.send(body)
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def _commit_holder(store: LanInventoryStore, cid: str, address: str) -> None:
    token = store.begin_session(cid, remote_address=address)
    frame = build_lan_inventory_frame(
        gen=1,
        seq=0,
        mode="snapshot",
        more=False,
        entries=[{"path": "docs/file.txt", "sha256": DIGEST, "size": 7}],
    )
    assert store.apply_frame(cid, token, frame)["status"] == "ok"


def _snapshot(cid: str, address: str, *, seed: bool = True) -> PeerSnapshot:
    return PeerSnapshot(
        cid=cid,
        address=address,
        port=8443,
        use_https=True,
        cert_fingerprint=CERT,
        compression_enabled=True,
        peer_download_enabled=True,
        seed_enabled=seed,
    )


def test_peer_manager_returns_immutable_detached_snapshots() -> None:
    manager = PeerManager(MASTER_CID, "bucket")
    manager.add_peer(
        Peer(
            cid=PEER_B,
            bucket="bucket",
            address="192.168.1.10",
            port=8443,
            peer_download_enabled=True,
            seed_enabled=True,
        )
    )
    snapshot = manager.get_alive_peer_snapshots()[0]
    with pytest.raises(FrozenInstanceError):
        snapshot.address = "192.168.1.99"  # type: ignore[misc]

    manager.add_peer(
        Peer(
            cid=PEER_B,
            bucket="bucket",
            address="192.168.1.11",
            port=8443,
        )
    )
    assert snapshot.address == "192.168.1.10"
    assert manager.get_alive_peer_snapshots()[0].address == "192.168.1.11"


def test_source_ranking_prefers_same_subnet_and_requires_exact_live_holder(
    tmp_path: Path,
) -> None:
    store = LanInventoryStore(tmp_path / "inventory.sqlite3", "bucket")
    store.activate()
    holder_addresses = {
        PEER_B: "192.168.1.20",
        PEER_C: "198.51.100.20",
        PEER_D: "192.168.1.30",
        PEER_E: "192.168.1.50",
    }
    for cid, address in holder_addresses.items():
        _commit_holder(store, cid, address)

    snapshots = (
        _snapshot(PEER_B, "192.168.1.20"),
        _snapshot(PEER_C, "198.51.100.20"),
        _snapshot(PEER_D, "192.168.1.30"),
        _snapshot(PEER_E, "192.168.1.50"),
        _snapshot("N" * 21, "192.168.1.40", seed=False),
    )
    service = LanSourceService(
        bucket="bucket",
        master_cid=MASTER_CID,
        inventory_store=store,
        peer_snapshot_provider=lambda: snapshots,
        rng=NoShuffle(),
    )
    response = service.resolve(
        requester_cid=REQUESTER_CID,
        requester_address="192.168.1.50",
        files=[{"path": "docs/file.txt", "sha256": DIGEST}],
    )
    sources = response["files"][0]["sources"]
    assert [source["cid"] for source in sources] == [PEER_B, PEER_D, PEER_E, PEER_C]
    assert all(source["download_url"].startswith("https://") for source in sources)
    assert all(source["cert_fp"] == CERT for source in sources)

    spoofed = LanSourceService(
        bucket="bucket",
        master_cid=MASTER_CID,
        inventory_store=store,
        peer_snapshot_provider=lambda: (
            _snapshot(PEER_B, "192.168.1.99"),
            _snapshot(PEER_C, "198.51.100.20"),
        ),
        rng=NoShuffle(),
    ).resolve(
        requester_cid=REQUESTER_CID,
        requester_address="192.168.1.50",
        files=[{"path": "docs/file.txt", "sha256": DIGEST}],
    )
    assert [source["cid"] for source in spoofed["files"][0]["sources"]] == [PEER_C]

    missing = service.resolve(
        requester_cid=REQUESTER_CID,
        requester_address="192.168.1.50",
        files=[{"path": "docs/file.txt", "sha256": "f" * 64}],
    )
    assert missing["files"][0]["sources"] == []
    store.close()


def test_sources_endpoint_strict_validation_and_cid_ownership() -> None:
    service = StaticSourceService()
    registry = SlaveCidRegistry(master_cid=MASTER_CID)
    server = LANServer(
        0,
        cid_guard=registry,
        source_service=service,  # type: ignore[arg-type]
    )
    with patch("hsync_client.lan_server.CONFIG", {"LAN_KEY": None}):
        server.start()
        assert server._server is not None
        port = int(server._server.server_address[1])
        path = "/api/v1/buckets/bucket/files/sources"
        body = json.dumps(
            {
                "cid": REQUESTER_CID,
                "files": [{"path": "docs/file.txt", "sha256": DIGEST}],
            }
        ).encode()
        try:
            status, payload = _post(port, path, body)
            assert status == 200
            assert payload["files"][0]["sources"] == []

            status, _ = _post(port, path, body, content_type="text/plain")
            assert status == 400

            status, _ = _post(port, path, b"{}")
            assert status == 400

            status, _ = _post(
                port,
                path,
                b"",
                declared_length=LAN_SOURCES_MAX_BODY_BYTES + 1,
            )
            assert status == 413

            status, _ = _post(port, "/api/v1/buckets/other/files/sources", body)
            assert status == 404

            registry.try_reserve(REQUESTER_CID, "192.0.2.20", object())
            status, _ = _post(port, path, body)
            assert status == 403
        finally:
            server.stop()


def _source_resolver(tmp_path: Path, **overrides) -> LanSourceResolver:
    values = {
        "master_url": "https://10.0.0.1:8443",
        "master_cert_fingerprint": "f" * 64,
        "requester_cid": REQUESTER_CID,
        "bucket": "bucket",
        "lan_key": "shared-key",
        "local_bucket": str(tmp_path),
        "enabled": True,
        "min_size": 0,
        "pin_store": PinStore(tmp_path / "pins.json"),
    }
    values.update(overrides)
    return LanSourceResolver(**values)


def _maximum_remote_path(index: int) -> str:
    """Return a unique valid path at the 4096-byte wire boundary."""
    parts = ["a" * 255] * 15
    parts.extend(("b" * 127, f"{index:03d}" + "c" * 125))
    return "/".join(parts)


def _empty_sources_response(_url: str, **kwargs) -> MagicMock:
    request = json.loads(kwargs["data"])
    response = MagicMock(status_code=200)
    payload = build_lan_sources_response(
        files=[
            {"path": item["path"], "sha256": item["sha256"], "sources": []}
            for item in request["files"]
        ]
    )
    content = json.dumps(payload, separators=(",", ":")).encode()
    response.headers = {"Content-Length": str(len(content))}
    response.iter_content.return_value = [content]
    return response


def test_requester_batches_once_per_500_and_pins_master_session(tmp_path: Path) -> None:
    """Small wantlists use count bounds, compact JSON, auth, and the master pin."""
    files = [
        File(filepath=f"docs/{index}.bin", sha256sum=f"{index:064x}", size=1)
        for index in range(501)
    ]

    resolver = _source_resolver(tmp_path)
    with patch(
        "hsync_client.lan_sources.http_post", side_effect=_empty_sources_response
    ) as mock_post:
        candidates = resolver.resolve(files)

    assert len(candidates) == 501
    assert [len(json.loads(call.kwargs["data"])["files"]) for call in mock_post.call_args_list] == [
        500,
        1,
    ]
    for call in mock_post.call_args_list:
        assert len(call.kwargs["data"]) <= LAN_SOURCES_MAX_BODY_BYTES
        assert "json" not in call.kwargs
        assert call.kwargs["cert_fingerprint"] == "f" * 64
        assert call.kwargs["headers"]["Authorization"] == "Bearer shared-key"
        assert call.kwargs["stream"] is True


def test_maximum_paths_bound_request_and_guaranteed_empty_response(tmp_path: Path) -> None:
    """A 500-item max-path wantlist splits before its empty response exceeds 2 MiB."""
    files = [
        File(
            filepath=str(tmp_path / _maximum_remote_path(index)),
            sha256sum=f"{index:064x}",
            size=1,
        )
        for index in range(500)
    ]
    assert all(len(_maximum_remote_path(index).encode("utf-8")) == 4096 for index in range(500))

    resolver = _source_resolver(tmp_path)
    with patch(
        "hsync_client.lan_sources.http_post", side_effect=_empty_sources_response
    ) as mock_post:
        candidates = resolver.resolve(files)

    assert len(candidates) == 500
    batches = [json.loads(call.kwargs["data"])["files"] for call in mock_post.call_args_list]
    assert [len(batch) for batch in batches] == [499, 1]
    for call, batch in zip(mock_post.call_args_list, batches, strict=True):
        assert len(call.kwargs["data"]) <= LAN_SOURCES_MAX_BODY_BYTES
        empty_response = build_lan_sources_response(
            files=[{**item, "sources": []} for item in batch]
        )
        encoded = json.dumps(empty_response, separators=(",", ":")).encode()
        assert len(encoded) <= LAN_SOURCES_MAX_BODY_BYTES


def test_authenticated_capability_overrides_stale_txt_hint(tmp_path: Path) -> None:
    """Authenticated WSS capability may enable lookup after TXT advertised false."""
    resolver = _source_resolver(tmp_path, master_capable=False)
    assert resolver.enabled is False

    resolver.set_authenticated_master_capability(True)

    assert resolver.enabled is True

    locally_disabled = _source_resolver(tmp_path, enabled=False, master_capable=True)
    locally_disabled.set_authenticated_master_capability(True)
    assert locally_disabled.enabled is False


@pytest.mark.parametrize(
    "failure_target",
    (
        "hsync_client.lan_sources.build_lan_sources_request",
        "hsync_client.lan_sources._compact_json_bytes",
    ),
)
def test_source_builder_and_serialization_faults_are_master_only(
    tmp_path: Path, failure_target: str
) -> None:
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    resolver = _source_resolver(tmp_path)

    with (
        patch(failure_target, side_effect=ValueError("invalid wire payload")),
        patch("hsync_client.lan_sources.http_post") as mock_post,
    ):
        assert resolver.resolve([file]) == {}
    mock_post.assert_not_called()


def test_source_response_content_length_is_rejected_before_buffering(tmp_path: Path) -> None:
    """A master cannot make requester source lookup buffer beyond the 2 MiB cap."""
    response = MagicMock(status_code=200)
    response.headers = {"Content-Length": str(LAN_SOURCES_MAX_BODY_BYTES + 1)}
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    resolver = _source_resolver(tmp_path)

    with patch("hsync_client.lan_sources.http_post", return_value=response):
        assert resolver.resolve([file]) == {}

    response.iter_content.assert_not_called()
    response.close.assert_called_once()


def test_resolved_candidate_map_expires_after_ten_minutes(tmp_path: Path) -> None:
    """A long-running sync falls back instead of reusing stale peer coordinates."""
    now = [0.0]
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    payload = build_lan_sources_response(
        files=[
            {
                "path": "docs/file.bin",
                "sha256": DIGEST,
                "sources": [
                    {
                        "cid": PEER_B,
                        "download_url": (
                            "https://10.0.0.2:8443/api/v1/buckets/bucket/files/download"
                        ),
                        "cert_fp": "b" * 64,
                        "compression": False,
                    }
                ],
            }
        ]
    )
    content = json.dumps(payload, separators=(",", ":")).encode()
    response = MagicMock(
        status_code=200,
        headers={"Content-Length": str(len(content))},
    )
    response.iter_content.return_value = [content]
    resolver = _source_resolver(tmp_path, clock=lambda: now[0])

    with patch("hsync_client.lan_sources.http_post", return_value=response):
        candidates = resolver.resolve([file])

    assert [source.cid for source in resolver.candidates_for(file, candidates)] == [PEER_B]
    now[0] = 601.0
    assert resolver.candidates_for(file, candidates) == ()


def test_wrong_bucket_source_is_rejected_before_tofu(tmp_path: Path) -> None:
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    payload = build_lan_sources_response(
        files=[
            {
                "path": "docs/file.bin",
                "sha256": DIGEST,
                "sources": [
                    {
                        "cid": PEER_B,
                        "download_url": (
                            "https://10.0.0.2:8443/api/v1/buckets/other/files/download"
                        ),
                        "cert_fp": "b" * 64,
                        "compression": False,
                    }
                ],
            }
        ]
    )
    content = json.dumps(payload, separators=(",", ":")).encode()
    response = MagicMock(status_code=200, headers={"Content-Length": str(len(content))})
    response.iter_content.return_value = [content]
    resolver = _source_resolver(tmp_path)

    with patch("hsync_client.lan_sources.http_post", return_value=response):
        candidates = resolver.resolve([file])

    assert candidates[("docs/file.bin", DIGEST)] == ()
    assert PinStore(tmp_path / "pins.json").get(PEER_B, "bucket") is None


def test_requester_tofu_pins_each_source_and_refuses_rotation(tmp_path: Path) -> None:
    """Seeder TOFU is per (source CID, bucket), independent from master pairing."""
    resolver = _source_resolver(tmp_path)
    source = LanPeerSource(
        cid=PEER_B,
        download_url="https://10.0.0.2:8443/api/v1/buckets/bucket/files/download",
        cert_fingerprint="b" * 64,
        compression=True,
    )

    assert resolver.authorize(source) is True
    assert PinStore(tmp_path / "pins.json").get(PEER_B, "bucket") == "b" * 64

    rotated = LanPeerSource(
        cid=PEER_B,
        download_url=source.download_url,
        cert_fingerprint="d" * 64,
        compression=True,
    )
    assert resolver.authorize(rotated) is False
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    assert resolver.candidates_for(file, {("docs/file.bin", DIGEST): (rotated,)}) == ()


def test_source_request_headers_never_leak_master_session_state(tmp_path: Path) -> None:
    """Each peer request derives only LAN auth plus explicit identity/zstd negotiation."""
    resolver = _source_resolver(tmp_path)
    source = LanPeerSource(
        cid=PEER_B,
        download_url="https://10.0.0.2:8443/api/v1/buckets/bucket/files/download",
        cert_fingerprint="b" * 64,
        compression=True,
    )

    assert resolver.request_headers(
        source,
        resumed=False,
        compression_enabled=True,
    ) == {"Authorization": "Bearer shared-key", "Accept-Encoding": "zstd"}
    assert resolver.request_headers(
        source,
        resumed=True,
        compression_enabled=True,
    ) == {"Authorization": "Bearer shared-key", "Accept-Encoding": "identity"}


def test_permanent_source_failure_lasts_for_role_session(tmp_path: Path) -> None:
    resolver = _source_resolver(tmp_path)
    file = File(filepath="docs/file.bin", sha256sum=DIGEST, size=1)
    source = LanPeerSource(
        cid=PEER_B,
        download_url="https://10.0.0.2:8443/api/v1/buckets/bucket/files/download",
        cert_fingerprint="b" * 64,
        compression=False,
    )
    candidates = {("docs/file.bin", DIGEST): (source,)}
    resolver.record_failure(source, PeerFailureKind.unauthorized)
    assert resolver.candidates_for(file, candidates) == ()


def test_telemetry_ranking_cooldown_and_ttl_are_deterministic() -> None:
    """Health, RTT, bandwidth, original rank, and TTL follow the frozen tuple."""
    now = [0.0]
    telemetry = LanSourceTelemetry(clock=lambda: now[0])
    sources = tuple(
        LanPeerSource(
            cid=cid,
            download_url=f"https://10.0.0.{index}:8443/api/v1/buckets/bucket/files/download",
            cert_fingerprint=f"{index:064x}",
            compression=False,
        )
        for index, cid in enumerate((PEER_B, PEER_C, PEER_D), start=2)
    )
    telemetry.record_success(
        PEER_C,
        rtt_seconds=0.2,
        bytes_transferred=100,
        body_seconds=1.0,
    )
    telemetry.record_success(
        PEER_D,
        rtt_seconds=0.1,
        bytes_transferred=50,
        body_seconds=1.0,
    )
    assert [source.cid for source in telemetry.rank(sources)] == [PEER_D, PEER_C, PEER_B]

    telemetry.record_failure(PEER_D, PeerFailureKind.conflict)
    assert [source.cid for source in telemetry.rank(sources)] == [PEER_C, PEER_B]

    now[0] = 1.01
    assert [source.cid for source in telemetry.rank(sources)] == [PEER_C, PEER_B, PEER_D]

    telemetry.record_failure(PEER_B, PeerFailureKind.not_found)
    now[0] = 602.0
    assert telemetry.rank(sources) == sources
