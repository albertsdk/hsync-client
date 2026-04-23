# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Pure batch source lookup and deterministic/testable LAN peer ranking."""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address, ip_network
from time import monotonic
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

from hsync_common.constants import CONTENT_TYPE_JSON
from hsync_common.hashing import is_sha256_hex
from hsync_common.models.event import is_valid_cid
from hsync_common.models.file import File
from hsync_common.protocol.peer_download import (
    LAN_SOURCES_MAX_BODY_BYTES,
    LAN_SOURCES_MAX_CANDIDATES,
    LAN_SOURCES_MAX_FILES,
    LanSourceCandidate,
    LanSourceFile,
    LanSourcesResponse,
    LanSourcesResponseFile,
    build_lan_sources_request,
    build_lan_sources_response,
    validate_lan_sources_response,
)
from loguru import logger as log
from requests import RequestException, Response

from hsync_client.lan_inventory import LanInventoryStore
from hsync_client.pinning import PinStore, PinStoreError, default_pin_store
from hsync_client.utils import http_post, normalize_bucket_relpath

LAN_SOURCE_CACHE_TTL_S = 10 * 60.0


class PeerSourceSnapshot(Protocol):
    """Structural view consumed from immutable ``PeerSnapshot`` values."""

    @property
    def cid(self) -> str: ...

    @property
    def address(self) -> str: ...

    @property
    def port(self) -> int: ...

    @property
    def use_https(self) -> bool: ...

    @property
    def cert_fingerprint(self) -> str | None: ...

    @property
    def compression_enabled(self) -> bool: ...

    @property
    def peer_download_enabled(self) -> bool: ...

    @property
    def seed_enabled(self) -> bool: ...


class ShuffleRng(Protocol):
    def shuffle(self, values: list[PeerSourceSnapshot]) -> None: ...


def _compact_json_bytes(value: object) -> bytes:
    """Serialize one wire payload exactly as the LAN server does."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


_FILES_DOWNLOAD_ROUTE = "/api/v1/buckets/{bucket}/files/download"


def _download_url_matches_bucket(download_url: str, bucket: str) -> bool:
    """Bind a validated source endpoint to the requester's expected bucket."""
    try:
        path = urlsplit(download_url).path
    except (UnicodeError, ValueError):
        return False
    return path == _FILES_DOWNLOAD_ROUTE.format(bucket=bucket)


def build_peer_download_url(bucket: str, address: str, port: int) -> str:
    """Build a complete IPv4/IPv6-safe HTTPS seeder download endpoint."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("invalid peer port")
    if "%" in address:
        raise ValueError("IPv6 zone identifiers are unsupported")
    parsed = ip_address(address)
    host = f"[{parsed.compressed}]" if parsed.version == 6 else parsed.compressed
    return f"https://{host}:{port}" + _FILES_DOWNLOAD_ROUTE.format(bucket=bucket)


def _same_subnet(requester_address: str, peer_address: str) -> bool:
    try:
        requester = ip_address(requester_address)
        peer = ip_address(peer_address)
    except ValueError:
        return False
    if requester.version != peer.version:
        return False
    prefix = 24 if requester.version == 4 else 64
    return peer in ip_network(f"{requester}/{prefix}", strict=False)


def _read_bounded_response(response: Response) -> bytes:
    """Read one source response without buffering beyond the wire limit."""
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ValueError("invalid source response Content-Length")
        if int(raw_length) > LAN_SOURCES_MAX_BODY_BYTES:
            raise ValueError("source response exceeds size limit")

    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if len(body) + len(chunk) > LAN_SOURCES_MAX_BODY_BYTES:
            raise ValueError("source response exceeds size limit")
        body.extend(chunk)
    return bytes(body)


class LanSourceService:
    """Resolve exact indexed holders against current immutable peer snapshots."""

    def __init__(
        self,
        *,
        bucket: str,
        master_cid: str,
        inventory_store: LanInventoryStore,
        peer_snapshot_provider: Callable[[], Iterable[PeerSourceSnapshot]],
        rng: ShuffleRng | None = None,
    ) -> None:
        self.bucket = bucket
        self.master_cid = master_cid
        self._store = inventory_store
        self._peer_snapshot_provider = peer_snapshot_provider
        self._rng = rng or random.SystemRandom()
        self._rng_lock = threading.Lock()

    @staticmethod
    def _eligible(peer: PeerSourceSnapshot, requester_cid: str, master_cid: str) -> bool:
        return (
            peer.cid not in {requester_cid, master_cid}
            and is_valid_cid(peer.cid)
            and peer.use_https
            and peer.peer_download_enabled
            and peer.seed_enabled
            and isinstance(peer.cert_fingerprint, str)
            and is_sha256_hex(peer.cert_fingerprint)
            and peer.cert_fingerprint == peer.cert_fingerprint.lower()
            and isinstance(peer.compression_enabled, bool)
        )

    def _ranked_peers(self, requester_cid: str, requester_address: str) -> list[PeerSourceSnapshot]:
        snapshots = tuple(self._peer_snapshot_provider())
        unique: dict[str, PeerSourceSnapshot] = {}
        for peer in snapshots:
            if not self._eligible(peer, requester_cid, self.master_cid):
                continue
            try:
                build_peer_download_url(self.bucket, peer.address, peer.port)
            except ValueError:
                continue
            unique.setdefault(peer.cid, peer)
        same = [p for p in unique.values() if _same_subnet(requester_address, p.address)]
        other = [p for p in unique.values() if not _same_subnet(requester_address, p.address)]
        with self._rng_lock:
            self._rng.shuffle(same)
            self._rng.shuffle(other)
        return [*same, *other]

    @staticmethod
    def _compact_size(value: object) -> int:
        return len(_compact_json_bytes(value))

    def resolve(
        self,
        *,
        requester_cid: str,
        requester_address: str,
        files: list[LanSourceFile],
    ) -> LanSourcesResponse:
        """Return bounded ranked candidates; runtime lookup faults degrade empty."""
        response_files = [
            LanSourcesResponseFile(path=file["path"], sha256=file["sha256"], sources=[])
            for file in files
        ]
        try:
            holders = self._store.holders_for(files)
            peers = self._ranked_peers(requester_cid, requester_address)
        except Exception:
            log.exception("LAN source lookup failed; returning master-only fallback")
            return build_lan_sources_response(files=response_files)

        candidates_by_file: list[list[LanSourceCandidate]] = []
        for file in files:
            holder_cids = {holder.cid for holder in holders.get((file["path"], file["sha256"]), ())}
            candidates: list[LanSourceCandidate] = []
            for peer in peers:
                if peer.cid not in holder_cids:
                    continue
                owner_address = self._store.session_address(peer.cid)
                if owner_address is None:
                    continue
                try:
                    if ip_address(owner_address) != ip_address(peer.address):
                        continue
                except ValueError:
                    continue
                candidates.append(
                    LanSourceCandidate(
                        cid=peer.cid,
                        download_url=build_peer_download_url(self.bucket, peer.address, peer.port),
                        cert_fp=str(peer.cert_fingerprint),
                        compression=peer.compression_enabled,
                    )
                )
                if len(candidates) >= LAN_SOURCES_MAX_CANDIDATES:
                    break
            candidates_by_file.append(candidates)

        # Start from the always-valid empty fallback and add candidates by rank
        # across the batch. This preserves a useful first choice for every file
        # while guaranteeing the encoded response never exceeds 2 MiB.
        base = {"files": response_files}
        current_size = self._compact_size(base)
        if current_size > LAN_SOURCES_MAX_BODY_BYTES:
            raise ValueError("request cannot produce a bounded sources response")
        for rank in range(LAN_SOURCES_MAX_CANDIDATES):
            for index, candidates in enumerate(candidates_by_file):
                if rank >= len(candidates):
                    continue
                candidate = candidates[rank]
                added_size = self._compact_size(candidate)
                if response_files[index]["sources"]:
                    added_size += 1
                if current_size + added_size > LAN_SOURCES_MAX_BODY_BYTES:
                    continue
                response_files[index]["sources"].append(candidate)
                current_size += added_size

        return build_lan_sources_response(files=response_files)


@dataclass(frozen=True, slots=True)
class LanPeerSource:
    """Immutable exact-content endpoint selected by the LAN master."""

    cid: str
    download_url: str
    cert_fingerprint: str
    compression: bool


type LanSourceKey = tuple[str, str]
type LanCandidateMap = Mapping[LanSourceKey, tuple[LanPeerSource, ...]]


class PeerFailureKind(StrEnum):
    """Stable failure classes used by source invalidation and telemetry."""

    transport = "transport"
    tls = "tls"
    conflict = "conflict"
    busy = "busy"
    malformed = "malformed"
    hash_mismatch = "hash_mismatch"
    unauthorized = "unauthorized"
    not_found = "not_found"
    pin_changed = "pin_changed"


@dataclass(slots=True)
class _SourceMetrics:
    rtt_ema: float | None = None
    throughput_ema: float | None = None
    failures: int = 0
    cooldown_until: float = 0.0
    last_seen: float = 0.0


class LanSourceTelemetry:
    """Thread-safe deterministic ranking state keyed only by source CID."""

    _TTL_SECONDS = 600.0
    _EMA_ALPHA = 0.2

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._clock = clock
        self._metrics: dict[str, _SourceMetrics] = {}
        self._lock = threading.Lock()

    def _expire_locked(self, now: float) -> None:
        stale = [
            cid
            for cid, metrics in self._metrics.items()
            if now - metrics.last_seen >= self._TTL_SECONDS
        ]
        for cid in stale:
            self._metrics.pop(cid, None)

    def rank(self, sources: Iterable[LanPeerSource]) -> tuple[LanPeerSource, ...]:
        """Apply the frozen deterministic ranking tuple to master-ranked sources."""
        values = tuple(sources)
        now = self._clock()
        with self._lock:
            self._expire_locked(now)

            def rank_key(indexed: tuple[int, LanPeerSource]) -> tuple:
                index, source = indexed
                metrics = self._metrics.get(source.cid)
                return (
                    metrics.failures if metrics is not None else 0,
                    1 if metrics is None or metrics.rtt_ema is None else 0,
                    metrics.rtt_ema if metrics is not None and metrics.rtt_ema is not None else 0.0,
                    1 if metrics is None or metrics.throughput_ema is None else 0,
                    -(metrics.throughput_ema or 0.0) if metrics is not None else 0.0,
                    index,
                    source.cid,
                )

            available = (
                (index, source)
                for index, source in enumerate(values)
                if (metrics := self._metrics.get(source.cid)) is None
                or metrics.cooldown_until <= now
            )
            return tuple(source for _, source in sorted(available, key=rank_key))

    def record_success(
        self,
        cid: str,
        *,
        rtt_seconds: float,
        bytes_transferred: int,
        body_seconds: float,
    ) -> None:
        now = self._clock()
        throughput = bytes_transferred / body_seconds if body_seconds > 0 else 0.0
        with self._lock:
            metrics = self._metrics.setdefault(cid, _SourceMetrics())
            alpha = self._EMA_ALPHA
            metrics.rtt_ema = (
                rtt_seconds
                if metrics.rtt_ema is None
                else alpha * rtt_seconds + (1 - alpha) * metrics.rtt_ema
            )
            metrics.throughput_ema = (
                throughput
                if metrics.throughput_ema is None
                else alpha * throughput + (1 - alpha) * metrics.throughput_ema
            )
            metrics.failures = 0
            metrics.cooldown_until = 0.0
            metrics.last_seen = now

    def record_failure(self, cid: str, _kind: PeerFailureKind) -> None:
        now = self._clock()
        with self._lock:
            metrics = self._metrics.setdefault(cid, _SourceMetrics())
            metrics.failures += 1
            cooldown = min(60.0, float(2 ** min(metrics.failures - 1, 6)))
            metrics.cooldown_until = now + cooldown
            metrics.last_seen = now


_PROCESS_TELEMETRY = LanSourceTelemetry()


class LanSourceResolver:
    """Requester-side bounded source lookup, pinning, and telemetry owner."""

    def __init__(
        self,
        *,
        master_url: str,
        master_cert_fingerprint: str,
        requester_cid: str,
        bucket: str,
        lan_key: str | None,
        local_bucket: str,
        enabled: bool,
        min_size: int,
        master_capable: bool = True,
        timeout: float = 30.0,
        pin_store: PinStore | None = None,
        telemetry: LanSourceTelemetry | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._sources_url = f"{master_url}/api/v1/buckets/{bucket}/files/sources"
        self._master_cert_fingerprint = master_cert_fingerprint
        self._requester_cid = requester_cid
        self._bucket = bucket
        self._lan_key = lan_key
        self._local_bucket = local_bucket
        self._configured_enabled = enabled
        # TXT is the discovery-time hint. An authenticated WSS handshake may
        # later override it in either direction without overriding local opt-out.
        self._master_capable = master_capable
        self._min_size = min_size
        self._timeout = timeout
        self._pin_store = pin_store or default_pin_store()
        self._telemetry = telemetry or _PROCESS_TELEMETRY
        self._clock = clock
        self._candidates: dict[LanSourceKey, tuple[LanPeerSource, ...]] = {}
        self._candidate_expiry: dict[LanSourceKey, float] = {}
        self._permanently_invalid_cids: set[str] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._configured_enabled and self._master_capable

    def set_authenticated_master_capability(self, enabled: bool) -> None:
        """Adopt the authenticated WSS capability as authoritative."""
        with self._lock:
            self._master_capable = enabled
            if not enabled:
                self._candidates.clear()
                self._candidate_expiry.clear()

    def _identity(self, file: File) -> LanSourceKey | None:
        path = normalize_bucket_relpath(file.filepath or "", self._local_bucket)
        digest = file.sha256sum or ""
        if (
            path is None
            or not is_sha256_hex(digest)
            or digest != digest.lower()
            or (file.size or 0) < self._min_size
        ):
            return None
        return path, digest

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": CONTENT_TYPE_JSON, "Content-Type": CONTENT_TYPE_JSON}
        if self._lan_key:
            headers["Authorization"] = f"Bearer {self._lan_key}"
        return headers

    def _expire_candidate_cache_locked(self, now: float) -> None:
        stale = [key for key, expiry in self._candidate_expiry.items() if expiry <= now]
        for key in stale:
            self._candidate_expiry.pop(key, None)
            self._candidates.pop(key, None)

    def _source_batches(
        self, identities: Iterable[LanSourceKey]
    ) -> tuple[tuple[LanSourceKey, ...], ...]:
        """Split identities by count, request bytes, and empty-response bytes."""
        request_base = len(_compact_json_bytes({"cid": self._requester_cid, "files": []}))
        response_base = len(_compact_json_bytes({"files": []}))
        request_size = request_base
        response_size = response_base
        current: list[LanSourceKey] = []
        batches: list[tuple[LanSourceKey, ...]] = []

        for identity in identities:
            path, digest = identity
            request_item = LanSourceFile(path=path, sha256=digest)
            response_item = LanSourcesResponseFile(
                path=path,
                sha256=digest,
                sources=[],
            )
            request_added = len(_compact_json_bytes(request_item)) + bool(current)
            response_added = len(_compact_json_bytes(response_item)) + bool(current)
            full = len(current) >= LAN_SOURCES_MAX_FILES
            too_large = (
                request_size + request_added > LAN_SOURCES_MAX_BODY_BYTES
                or response_size + response_added > LAN_SOURCES_MAX_BODY_BYTES
            )
            if current and (full or too_large):
                batches.append(tuple(current))
                current = []
                request_size = request_base
                response_size = response_base
                request_added = len(_compact_json_bytes(request_item))
                response_added = len(_compact_json_bytes(response_item))

            if (
                request_size + request_added > LAN_SOURCES_MAX_BODY_BYTES
                or response_size + response_added > LAN_SOURCES_MAX_BODY_BYTES
            ):
                log.debug("LAN source identity exceeds wire limit; using master-only fallback")
                continue
            current.append(identity)
            request_size += request_added
            response_size += response_added

        if current:
            batches.append(tuple(current))
        return tuple(batches)

    def resolve(self, files: Iterable[File]) -> LanCandidateMap:
        """Resolve eligible files in strict batches of at most 500 identities."""
        if not self.enabled:
            return MappingProxyType({})
        identities: list[LanSourceKey] = []
        seen_paths: set[str] = set()
        for file in files:
            identity = self._identity(file)
            if identity is None or identity[0] in seen_paths:
                continue
            seen_paths.add(identity[0])
            identities.append(identity)
        try:
            batches = self._source_batches(identities)
        except (TypeError, ValueError, UnicodeError):
            log.debug("LAN source batch serialization failed; using master-only fallback")
            return MappingProxyType({})

        resolved: dict[LanSourceKey, tuple[LanPeerSource, ...]] = {}
        for batch in batches:
            response: Response | None = None
            try:
                payload = build_lan_sources_request(
                    cid=self._requester_cid,
                    files=[LanSourceFile(path=path, sha256=digest) for path, digest in batch],
                )
                request_body = _compact_json_bytes(payload)
                empty_response = build_lan_sources_response(
                    files=[
                        LanSourcesResponseFile(path=path, sha256=digest, sources=[])
                        for path, digest in batch
                    ]
                )
                if (
                    len(request_body) > LAN_SOURCES_MAX_BODY_BYTES
                    or len(_compact_json_bytes(empty_response)) > LAN_SOURCES_MAX_BODY_BYTES
                ):
                    raise ValueError("LAN source batch exceeds wire limit")
                response = http_post(
                    self._sources_url,
                    cert_fingerprint=self._master_cert_fingerprint,
                    headers=self._headers(),
                    data=request_body,
                    verify=False,
                    timeout=self._timeout,
                    stream=True,
                )
                if response.status_code != 200:
                    continue
                content = _read_bounded_response(response)
                clean = validate_lan_sources_response(json.loads(content.decode("utf-8")))
            except (
                RequestException,
                RecursionError,
                TypeError,
                UnicodeError,
                ValueError,
            ):
                log.debug("LAN source lookup failed; using master-only fallback")
                continue
            finally:
                if response is not None:
                    response.close()
            if clean is None:
                continue
            response_ids = [(item["path"], item["sha256"]) for item in clean["files"]]
            if tuple(response_ids) != batch:
                continue
            for item in clean["files"]:
                key = (item["path"], item["sha256"])
                sources: list[LanPeerSource] = []
                for source in item["sources"]:
                    if not _download_url_matches_bucket(source["download_url"], self._bucket):
                        log.warning(
                            "Ignoring peer source {} whose URL targets another bucket",
                            source["cid"],
                        )
                        continue
                    sources.append(
                        LanPeerSource(
                            cid=source["cid"],
                            download_url=source["download_url"],
                            cert_fingerprint=source["cert_fp"],
                            compression=source["compression"],
                        )
                    )
                resolved[key] = tuple(sources)
        now = self._clock()
        expires_at = now + LAN_SOURCE_CACHE_TTL_S
        with self._lock:
            self._expire_candidate_cache_locked(now)
            self._candidates.update(resolved)
            self._candidate_expiry.update(dict.fromkeys(resolved, expires_at))
        return MappingProxyType(dict(resolved))

    def candidates_for(
        self,
        file: File,
        candidates: LanCandidateMap | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[LanPeerSource, ...]:
        """Return currently valid candidates, optionally doing a one-item lookup."""
        identity = self._identity(file)
        if identity is None or not self.enabled:
            return ()
        if refresh:
            candidates = self.resolve([file])
        now = self._clock()
        if candidates is None:
            with self._lock:
                self._expire_candidate_cache_locked(now)
                expiry = self._candidate_expiry.get(identity)
                if expiry is None:
                    return ()
                values = self._candidates.get(identity, ())
                invalid = set(self._permanently_invalid_cids)
        else:
            values = candidates.get(identity, ())
            with self._lock:
                expiry = self._candidate_expiry.get(identity)
                self._expire_candidate_cache_locked(now)
                if expiry is not None and expiry <= now:
                    return ()
                invalid = set(self._permanently_invalid_cids)
        return self._telemetry.rank(source for source in values if source.cid not in invalid)

    def request_headers(
        self,
        source: LanPeerSource,
        *,
        resumed: bool,
        compression_enabled: bool,
    ) -> dict[str, str]:
        """Build source-specific auth and identity/zstd negotiation headers."""
        headers: dict[str, str] = {}
        if self._lan_key:
            headers["Authorization"] = f"Bearer {self._lan_key}"
        headers["Accept-Encoding"] = (
            "zstd" if compression_enabled and source.compression and not resumed else "identity"
        )
        return headers

    def authorize(self, source: LanPeerSource) -> bool:
        """TOFU-pin a source before any connection; fail closed on state faults."""
        if not _download_url_matches_bucket(source.download_url, self._bucket):
            log.warning(f"Peer source {source.cid} advertised a URL for another bucket")
            self.invalidate(source.cid)
            return False
        try:
            decision = self._pin_store.check_and_record(
                source.cid,
                self._bucket,
                source.cert_fingerprint,
            )
        except (PinStoreError, ValueError) as exc:
            log.warning(f"Cannot establish durable trust for peer source {source.cid}: {exc}")
            self.invalidate(source.cid)
            return False
        if decision.value == "changed":
            self.record_failure(source, PeerFailureKind.pin_changed)
            return False
        return True

    def invalidate(self, cid: str) -> None:
        """Invalidate one source permanently for this LAN role session."""
        with self._lock:
            self._permanently_invalid_cids.add(cid)

    def record_success(
        self,
        source: LanPeerSource,
        *,
        rtt_seconds: float,
        bytes_transferred: int,
        body_seconds: float,
    ) -> None:
        self._telemetry.record_success(
            source.cid,
            rtt_seconds=rtt_seconds,
            bytes_transferred=bytes_transferred,
            body_seconds=body_seconds,
        )

    def record_failure(self, source: LanPeerSource, kind: PeerFailureKind) -> None:
        if kind in (
            PeerFailureKind.unauthorized,
            PeerFailureKind.pin_changed,
        ):
            self.invalidate(source.cid)
            return
        self._telemetry.record_failure(source.cid, kind)
