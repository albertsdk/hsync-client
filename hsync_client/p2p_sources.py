# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Pure batch source lookup and deterministic/testable P2P peer ranking."""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from time import monotonic
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

from hsync_common.constants import CONTENT_TYPE_JSON
from hsync_common.hashing import is_sha256_hex
from hsync_common.models.event import is_valid_cid
from hsync_common.models.file import File
from hsync_common.protocol.p2p import P2P_FILES_SOURCES_SUFFIX
from hsync_common.protocol.peer_download import (
    P2P_SOURCES_MAX_BODY_BYTES,
    P2P_SOURCES_MAX_CANDIDATES,
    P2P_SOURCES_MAX_FILES,
    P2PSourceCandidate,
    P2PSourceFile,
    P2PSourcesResponse,
    P2PSourcesResponseFile,
    build_p2p_sources_request,
    build_p2p_sources_response,
    validate_p2p_sources_response,
)
from hsync_common.protocol.rest import (
    REST_FILES_DOWNLOAD_SUFFIX,
    rest_bucket_path,
    rest_bucket_template,
)
from hsync_common.text import compact_json_bytes
from loguru import logger as log
from requests import RequestException, Response

from hsync_client.net_utils import same_subnet
from hsync_client.p2p_inventory import P2PInventoryStore
from hsync_client.pinning import PinStore, PinStoreError, default_pin_store
from hsync_client.trust_messages import peer_source_changed_line
from hsync_client.utils import http_post, normalize_bucket_relpath

P2P_SOURCE_CACHE_TTL_S = 10 * 60.0

# A seeder that answers ``503`` is saturated, not broken: the requester defers
# that source for the interval the seeder asked for, or this default when the
# response carries no usable ``Retry-After``.
BUSY_DEFAULT_BACKOFF_S = 0.25

# How long a download worker waits for one of a seeder's advertised read slots
# before taking the master (the guaranteed source). Bounded and internal: the
# wait exists so a two-slot seeder still serves a wide worker pool without the
# requester flooding it with ``503``-generating attempts.
P2P_PEER_SLOT_WAIT_S = 2.0


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


class ShuffleRng(Protocol):
    def shuffle(self, values: list[PeerSourceSnapshot]) -> None: ...


_FILES_DOWNLOAD_ROUTE = rest_bucket_template(REST_FILES_DOWNLOAD_SUFFIX)


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


def _read_bounded_response(response: Response) -> bytes:
    """Read one source response without buffering beyond the wire limit."""
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ValueError("invalid source response Content-Length")
        if int(raw_length) > P2P_SOURCES_MAX_BODY_BYTES:
            raise ValueError("source response exceeds size limit")

    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if len(body) + len(chunk) > P2P_SOURCES_MAX_BODY_BYTES:
            raise ValueError("source response exceeds size limit")
        body.extend(chunk)
    return bytes(body)


class P2PSourceService:
    """Resolve exact indexed holders against current immutable peer snapshots."""

    def __init__(
        self,
        *,
        bucket: str,
        master_cid: str,
        inventory_store: P2PInventoryStore,
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
        same = [p for p in unique.values() if same_subnet(requester_address, p.address)]
        other = [p for p in unique.values() if not same_subnet(requester_address, p.address)]
        with self._rng_lock:
            self._rng.shuffle(same)
            self._rng.shuffle(other)
        return [*same, *other]

    @staticmethod
    def _compact_size(value: object) -> int:
        return len(compact_json_bytes(value))

    def resolve(
        self,
        *,
        requester_cid: str,
        requester_address: str,
        files: list[P2PSourceFile],
    ) -> P2PSourcesResponse:
        """Return bounded ranked candidates; runtime lookup faults degrade empty."""
        response_files = [
            P2PSourcesResponseFile(path=file["path"], sha256=file["sha256"], sources=[])
            for file in files
        ]
        try:
            holders = self._store.holders_for(files)
            peers = self._ranked_peers(requester_cid, requester_address)
        except Exception:
            log.exception("P2P source lookup failed; returning master-only fallback")
            return build_p2p_sources_response(files=response_files)

        candidates_by_file: list[list[P2PSourceCandidate]] = []
        for file in files:
            holder_cids = {holder.cid for holder in holders.get((file["path"], file["sha256"]), ())}
            candidates: list[P2PSourceCandidate] = []
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
                # A candidate exists only with a live capacity advertisement:
                # without it the requester could not respect the seeder's cap.
                serve_slots = self._store.session_serve_slots(peer.cid)
                if serve_slots is None:
                    continue
                candidates.append(
                    P2PSourceCandidate(
                        cid=peer.cid,
                        download_url=build_peer_download_url(self.bucket, peer.address, peer.port),
                        cert_fp=str(peer.cert_fingerprint),
                        compression=peer.compression_enabled,
                        serve_slots=serve_slots,
                    )
                )
                if len(candidates) >= P2P_SOURCES_MAX_CANDIDATES:
                    break
            candidates_by_file.append(candidates)

        # Start from the always-valid empty fallback and add candidates by rank
        # across the batch. This preserves a useful first choice for every file
        # while guaranteeing the encoded response never exceeds 2 MiB.
        base = {"files": response_files}
        current_size = self._compact_size(base)
        if current_size > P2P_SOURCES_MAX_BODY_BYTES:
            raise ValueError("request cannot produce a bounded sources response")
        for rank in range(P2P_SOURCES_MAX_CANDIDATES):
            for index, candidates in enumerate(candidates_by_file):
                if rank >= len(candidates):
                    continue
                candidate = candidates[rank]
                added_size = self._compact_size(candidate)
                if response_files[index]["sources"]:
                    added_size += 1
                if current_size + added_size > P2P_SOURCES_MAX_BODY_BYTES:
                    continue
                response_files[index]["sources"].append(candidate)
                current_size += added_size

        return build_p2p_sources_response(files=response_files)


@dataclass(frozen=True, slots=True)
class P2PPeerSource:
    """Immutable exact-content endpoint selected by the P2P master.

    ``serve_slots`` is the capacity the seeder advertised on the authenticated
    handshake and the master echoed back: the requester admits at most that many
    concurrent reads per CID, so it never has to discover the limit by provoking
    ``503`` responses (ADR-0030).
    """

    cid: str
    download_url: str
    cert_fingerprint: str
    compression: bool
    serve_slots: int


type P2PSourceKey = tuple[str, str]
type P2PCandidateMap = Mapping[P2PSourceKey, tuple[P2PPeerSource, ...]]


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
    busy_until: float = 0.0
    last_seen: float = 0.0

    def predicted_cost_s(self, size_bytes: int | None) -> float | None:
        """Predicted seconds to fetch *size_bytes* from this source.

        The linear cost model the measurements support: a fixed setup term (the
        request RTT, which includes the handshake a cold session pays) plus the
        payload divided by the observed body rate. ``None`` means this source has
        no usable measurement yet, so unknown sources keep sorting after measured
        ones. With ``size_bytes=None`` the setup term alone decides, which is the
        right proxy when the caller has no file at hand.
        """
        if self.rtt_ema is None:
            return None
        if size_bytes is None:
            return self.rtt_ema
        if self.throughput_ema is None or self.throughput_ema <= 0:
            return None
        return self.rtt_ema + max(size_bytes, 0) / self.throughput_ema


class P2PSourceTelemetry:
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

    def rank(
        self,
        sources: Iterable[P2PPeerSource],
        *,
        size: int | None = None,
    ) -> tuple[P2PPeerSource, ...]:
        """Rank candidates by measured health then predicted transfer cost.

        The key is the frozen tuple below: fewest failures first, then sources
        that have a usable measurement, then the predicted time to fetch *size*
        bytes (``setup + size/bandwidth``). Ranking on a predicted cost instead
        of a raw throughput removes the size bias — a peer that happened to serve
        a large file no longer outranks a lower-latency one for a small file, and
        vice versa. Callers that know the file pass its size; without it the setup
        term alone decides.
        """
        values = tuple(sources)
        now = self._clock()
        with self._lock:
            self._expire_locked(now)

            def rank_key(indexed: tuple[int, P2PPeerSource]) -> tuple:
                index, source = indexed
                metrics = self._metrics.get(source.cid)
                cost = metrics.predicted_cost_s(size) if metrics is not None else None
                return (
                    metrics.failures if metrics is not None else 0,
                    1 if cost is None else 0,
                    cost if cost is not None else 0.0,
                    index,
                    source.cid,
                )

            available = (
                (index, source)
                for index, source in enumerate(values)
                if (metrics := self._metrics.get(source.cid)) is None
                or (metrics.cooldown_until <= now and metrics.busy_until <= now)
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
            metrics.busy_until = 0.0
            metrics.last_seen = now

    def record_failure(
        self,
        cid: str,
        kind: PeerFailureKind,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        """Record a failed peer read.

        ``busy`` is a *scheduling* signal, not a health failure: a saturated
        seeder is working correctly and must not accumulate the failure count
        that drives the exponential exclusion. It only defers the source for the
        interval it asked for (``Retry-After``, else a short default), so the
        peer path resumes immediately after the congestion clears instead of
        being written off for the rest of the sync cycle.
        """
        now = self._clock()
        with self._lock:
            metrics = self._metrics.setdefault(cid, _SourceMetrics())
            metrics.last_seen = now
            if kind is PeerFailureKind.busy:
                metrics.busy_until = now + (
                    retry_after_s if retry_after_s is not None else BUSY_DEFAULT_BACKOFF_S
                )
                return
            metrics.failures += 1
            cooldown = min(60.0, float(2 ** min(metrics.failures - 1, 6)))
            metrics.cooldown_until = now + cooldown


_PROCESS_TELEMETRY = P2PSourceTelemetry()


class P2PSourceResolver:
    """Requester-side bounded source lookup, pinning, and telemetry owner."""

    def __init__(
        self,
        *,
        master_url: str,
        master_cert_fingerprint: str,
        requester_cid: str,
        bucket: str,
        p2p_key: str | None,
        local_bucket: str,
        enabled: bool,
        master_capable: bool = True,
        timeout: float = 30.0,
        pin_store: PinStore | None = None,
        telemetry: P2PSourceTelemetry | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._sources_url = f"{master_url}{rest_bucket_path(bucket, P2P_FILES_SOURCES_SUFFIX)}"
        self._master_cert_fingerprint = master_cert_fingerprint
        self._requester_cid = requester_cid
        self._bucket = bucket
        self._p2p_key = p2p_key
        self._local_bucket = local_bucket
        self._configured_enabled = enabled
        # TXT is the discovery-time hint. An authenticated WSS handshake may
        # later override it in either direction without overriding local opt-out.
        self._master_capable = master_capable
        self._timeout = timeout
        self._pin_store = pin_store or default_pin_store()
        self._telemetry = telemetry or _PROCESS_TELEMETRY
        self._clock = clock
        self._candidates: dict[P2PSourceKey, tuple[P2PPeerSource, ...]] = {}
        self._candidate_expiry: dict[P2PSourceKey, float] = {}
        self._permanently_invalid_cids: set[str] = set()
        self._lock = threading.Lock()
        # In-flight peer reads per seeder CID, bounded by each seeder's advertised
        # capacity. The condition doubles as the admission gate: a worker waits
        # (bounded) for a slot instead of firing an attempt the seeder would refuse,
        # so a saturated seeder is neither punished nor bypassed by its own fleet.
        self._inflight: dict[str, int] = {}
        self._slot_condition = threading.Condition(self._lock)

    def acquire_slot(
        self,
        source: P2PPeerSource,
        *,
        timeout_s: float = P2P_PEER_SLOT_WAIT_S,
    ) -> bool:
        """Claim one of *source*'s advertised read slots, waiting up to *timeout_s*.

        Returns ``False`` when the wait expires (or the peer path is disabled),
        which the caller turns into the master fallback — never into a failed
        attempt, so a saturated seeder is never punished as if it were broken.

        Waiting is what keeps a healthy seeder in use for a pool wider than its
        capacity: returning immediately instead would send every worker past its
        limit straight to the master, which is the behaviour peer download exists
        to avoid (measured: 6.7 of 24 files from the peer vs 24 of 24 while
        waiting).
        """
        deadline = self._clock() + timeout_s
        with self._slot_condition:
            while True:
                if not self.enabled:
                    return False
                if self._inflight.get(source.cid, 0) < source.serve_slots:
                    self._inflight[source.cid] = self._inflight.get(source.cid, 0) + 1
                    return True
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._slot_condition.wait(timeout=remaining)

    def release_slot(self, source: P2PPeerSource) -> None:
        """Release one read slot for *source* and wake a waiting worker."""
        with self._slot_condition:
            current = self._inflight.get(source.cid, 0)
            if current <= 1:
                self._inflight.pop(source.cid, None)
            else:
                self._inflight[source.cid] = current - 1
            self._slot_condition.notify_all()

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

    def _identity(self, file: File) -> P2PSourceKey | None:
        """Return the ``(path, sha256)`` an exact-hash lookup can be made for.

        Size is deliberately not part of this gate: eligibility is decided by
        live seeder capacity and the predicted cost of the transfer, not by a
        size threshold (ADR-0030).
        """
        path = normalize_bucket_relpath(file.filepath or "", self._local_bucket)
        digest = file.sha256sum or ""
        if path is None or not is_sha256_hex(digest) or digest != digest.lower():
            return None
        return path, digest

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": CONTENT_TYPE_JSON, "Content-Type": CONTENT_TYPE_JSON}
        if self._p2p_key:
            headers["Authorization"] = f"Bearer {self._p2p_key}"
        return headers

    def _expire_candidate_cache_locked(self, now: float) -> None:
        stale = [key for key, expiry in self._candidate_expiry.items() if expiry <= now]
        for key in stale:
            self._candidate_expiry.pop(key, None)
            self._candidates.pop(key, None)

    def _source_batches(
        self, identities: Iterable[P2PSourceKey]
    ) -> tuple[tuple[P2PSourceKey, ...], ...]:
        """Split identities by count, request bytes, and empty-response bytes."""
        request_base = len(compact_json_bytes({"cid": self._requester_cid, "files": []}))
        response_base = len(compact_json_bytes({"files": []}))
        request_size = request_base
        response_size = response_base
        current: list[P2PSourceKey] = []
        batches: list[tuple[P2PSourceKey, ...]] = []

        for identity in identities:
            path, digest = identity
            request_item = P2PSourceFile(path=path, sha256=digest)
            response_item = P2PSourcesResponseFile(
                path=path,
                sha256=digest,
                sources=[],
            )
            request_added = len(compact_json_bytes(request_item)) + bool(current)
            response_added = len(compact_json_bytes(response_item)) + bool(current)
            full = len(current) >= P2P_SOURCES_MAX_FILES
            too_large = (
                request_size + request_added > P2P_SOURCES_MAX_BODY_BYTES
                or response_size + response_added > P2P_SOURCES_MAX_BODY_BYTES
            )
            if current and (full or too_large):
                batches.append(tuple(current))
                current = []
                request_size = request_base
                response_size = response_base
                request_added = len(compact_json_bytes(request_item))
                response_added = len(compact_json_bytes(response_item))

            if (
                request_size + request_added > P2P_SOURCES_MAX_BODY_BYTES
                or response_size + response_added > P2P_SOURCES_MAX_BODY_BYTES
            ):
                log.debug("P2P source identity exceeds wire limit; using master-only fallback")
                continue
            current.append(identity)
            request_size += request_added
            response_size += response_added

        if current:
            batches.append(tuple(current))
        return tuple(batches)

    def resolve(self, files: Iterable[File]) -> P2PCandidateMap:
        """Resolve eligible files in strict batches of at most 500 identities."""
        if not self.enabled:
            return MappingProxyType({})
        identities: list[P2PSourceKey] = []
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
            log.debug("P2P source batch serialization failed; using master-only fallback")
            return MappingProxyType({})

        resolved: dict[P2PSourceKey, tuple[P2PPeerSource, ...]] = {}
        for batch in batches:
            response: Response | None = None
            try:
                payload = build_p2p_sources_request(
                    cid=self._requester_cid,
                    files=[P2PSourceFile(path=path, sha256=digest) for path, digest in batch],
                )
                request_body = compact_json_bytes(payload)
                empty_response = build_p2p_sources_response(
                    files=[
                        P2PSourcesResponseFile(path=path, sha256=digest, sources=[])
                        for path, digest in batch
                    ]
                )
                if (
                    len(request_body) > P2P_SOURCES_MAX_BODY_BYTES
                    or len(compact_json_bytes(empty_response)) > P2P_SOURCES_MAX_BODY_BYTES
                ):
                    raise ValueError("P2P source batch exceeds wire limit")
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
                clean = validate_p2p_sources_response(json.loads(content.decode("utf-8")))
            except (
                RequestException,
                RecursionError,
                TypeError,
                UnicodeError,
                ValueError,
            ):
                log.debug("P2P source lookup failed; using master-only fallback")
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
                sources: list[P2PPeerSource] = []
                for source in item["sources"]:
                    if not _download_url_matches_bucket(source["download_url"], self._bucket):
                        log.warning(
                            "Ignoring peer source {} whose URL targets another bucket",
                            source["cid"],
                        )
                        continue
                    sources.append(
                        P2PPeerSource(
                            cid=source["cid"],
                            download_url=source["download_url"],
                            cert_fingerprint=source["cert_fp"],
                            compression=source["compression"],
                            serve_slots=source["serve_slots"],
                        )
                    )
                resolved[key] = tuple(sources)
        now = self._clock()
        expires_at = now + P2P_SOURCE_CACHE_TTL_S
        with self._lock:
            self._expire_candidate_cache_locked(now)
            self._candidates.update(resolved)
            self._candidate_expiry.update(dict.fromkeys(resolved, expires_at))
        return MappingProxyType(dict(resolved))

    def candidates_for(
        self,
        file: File,
        candidates: P2PCandidateMap | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[P2PPeerSource, ...]:
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
        return self._telemetry.rank(
            (source for source in values if source.cid not in invalid),
            size=int(file.size or 0),
        )

    def request_headers(
        self,
        source: P2PPeerSource,
        *,
        resumed: bool,
        compression_enabled: bool,
    ) -> dict[str, str]:
        """Build source-specific auth and identity/zstd negotiation headers."""
        headers: dict[str, str] = {}
        if self._p2p_key:
            headers["Authorization"] = f"Bearer {self._p2p_key}"
        headers["Accept-Encoding"] = (
            "zstd" if compression_enabled and source.compression and not resumed else "identity"
        )
        return headers

    def authorize(self, source: P2PPeerSource) -> bool:
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
            log.warning(peer_source_changed_line(source.cid, self._bucket))
            self.record_failure(source, PeerFailureKind.pin_changed)
            return False
        return True

    def invalidate(self, cid: str) -> None:
        """Invalidate one source permanently for this P2P role session."""
        with self._lock:
            self._permanently_invalid_cids.add(cid)

    def record_success(
        self,
        source: P2PPeerSource,
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

    def record_failure(
        self,
        source: P2PPeerSource,
        kind: PeerFailureKind,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        if kind in (
            PeerFailureKind.unauthorized,
            PeerFailureKind.pin_changed,
        ):
            self.invalidate(source.cid)
            return
        self._telemetry.record_failure(source.cid, kind, retry_after_s=retry_after_s)
