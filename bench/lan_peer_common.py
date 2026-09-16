# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared loopback ``lan/1`` fixtures for the L2 peer-download benchmarks.

Bench-only harness, never imported by the shipped package. It stands up a real
single-process LAN topology — a read-only seeder and a master on loopback, each
with its own self-signed certificate — together with the requester-side global
state (``CONFIG``, state dirs, TOFU pin file, per-thread HTTPS sessions) that the
production transfer path reads. Because a role's configuration is patched into
``hsync_client.lan_server.CONFIG`` for the harness lifetime, one process can run
both roles with different buckets exactly like two real nodes.

Consumers: ``bench_lan_peer_transfer.py`` and ``bench_lan_sources.py`` only.

Conventions (see ``bench/README.md``): one fixture lifecycle per scenario, every
sample asserts the branch it claims to measure, and per-sample setup (dropping a
destination, evicting a thread session) stays inside :func:`measure_with_setup`'s
untimed region.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from unittest.mock import patch

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import BenchResult, scaled_warmup  # noqa: E402  (import after sys.path tweak)
from hsync_common.models.file import File  # noqa: E402
from requests import RequestException  # noqa: E402

from hsync_client import sync_state  # noqa: E402
from hsync_client import utils as client_utils  # noqa: E402
from hsync_client.config import CONFIG as GLOBAL_CONFIG  # noqa: E402
from hsync_client.config import HsyncConfig, set_config  # noqa: E402
from hsync_client.lan_server import LANServer, LANServerMode  # noqa: E402
from hsync_client.lan_sources import (  # noqa: E402
    LanPeerSource,
    LanSourceResolver,
    LanSourceService,
    build_peer_download_url,
)
from hsync_client.pinning import PinStore  # noqa: E402
from hsync_client.ssl_utils import load_or_create_master_cert  # noqa: E402
from hsync_client.state_backend import reset_bucket_folder_cache  # noqa: E402

# Bucket name shared by every bench node: it is part of the peer download URL,
# so the source URL, the seeder route and the requester's expected bucket must
# all agree (``lan_sources._download_url_matches_bucket``).
BENCH_BUCKET = "bench"

# The master is always reached over loopback; servers bind 0.0.0.0 as in
# production, so only the client side is pinned to the literal address.
_LOOPBACK = "127.0.0.1"


def free_port() -> int:
    """Return a currently free loopback TCP port (same helper shape as the tests)."""
    with socket.socket() as sock:
        sock.bind((_LOOPBACK, 0))
        return int(sock.getsockname()[1])


class LatencyRelay:
    """Transparent loopback TCP relay that delays bytes and counts connections.

    Models a Wi-Fi-like RTT on loopback without privileges (``tc netem`` needs
    root): TLS stays end to end because the relay only copies bytes, so the
    requester still pins the seeder's real certificate fingerprint. Each accepted
    socket is one TCP connection, i.e. exactly one TLS handshake — the quantity a
    worker-to-seeder affinity scheme would reduce, so it is counted here instead
    of inferred.
    """

    def __init__(self, host: str, port: int, *, delay_s: float = 0.0) -> None:
        self._target = (host, port)
        self._delay = delay_s
        self._connections = 0
        self._connections_lock = threading.Lock()
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((_LOOPBACK, 0))
        self._socket.listen(128)
        self._socket.settimeout(0.2)
        self._port = int(self._socket.getsockname()[1])

    @property
    def port(self) -> int:
        """Loopback port the requester connects to."""
        return self._port

    @property
    def connections(self) -> int:
        """TCP connections accepted so far (= TLS handshakes observed)."""
        with self._connections_lock:
            return self._connections

    def start(self) -> LatencyRelay:
        """Start accepting and forwarding in background daemon threads."""
        thread = threading.Thread(target=self._accept_loop, name="bench_relay", daemon=True)
        thread.start()
        self._accept_thread = thread
        return self

    def close(self) -> None:
        """Stop accepting; in-flight pumps end when their sockets close."""
        self._stop.set()
        with contextlib.suppress(OSError):
            self._socket.close()
        thread = self._accept_thread
        if thread is not None:
            thread.join(timeout=2.0)

    def __enter__(self) -> LatencyRelay:
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._connections_lock:
                self._connections += 1
            try:
                upstream = socket.create_connection(self._target, timeout=5.0)
            except OSError:
                client.close()
                continue
            client.settimeout(None)
            upstream.settimeout(None)
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pump,
                    args=(source, sink),
                    name="bench_relay_pump",
                    daemon=True,
                ).start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        """Copy one direction, delaying every chunk by the configured latency."""
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                if self._delay:
                    sleep(self._delay)
                sink.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_WR)


@contextlib.contextmanager
def env_override(key: str, value: str) -> Iterator[None]:
    """Temporarily set an environment variable, restoring the previous value."""
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def payload_bytes(size: int) -> bytes:
    """Deterministic, incompressible payload of exactly *size* bytes.

    A chained SHA-256 expansion keeps the content reproducible (no PRNG seed
    juggling, as the suite requires) while staying incompressible, so a size
    comparison is not distorted by zstd side effects.
    """
    if size <= 0:
        return b""
    chunks: list[bytes] = []
    total = 0
    seed = b"hsync-bench-peer-download"
    while total < size:
        seed = hashlib.sha256(seed).digest()
        chunks.append(seed)
        total += len(seed)
    return b"".join(chunks)[:size]


def write_payload(directory: Path, relpath: str, size: int) -> tuple[bytes, str]:
    """Write a deterministic payload to ``directory/relpath``.

    Returns:
        ``(content, sha256_hex)`` — the digest is what the wire identity uses.
    """
    content = payload_bytes(size)
    target = directory / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return content, hashlib.sha256(content).hexdigest()


def build_file(relpath: str, content: bytes, digest: str) -> File:
    """Build the remote metadata a master listing would advertise for *relpath*.

    ``File`` derives ``name`` from ``filepath``; only the fields the transfer path
    consumes are set, so the sample measures the download and not fixture setup.
    """
    return File(
        filepath=relpath,
        mtime=1_700_000_000,
        size=len(content),
        sha256sum=digest,
        permissions="644",
    )


def clear_http_sessions() -> None:
    """Evict this thread's cached HTTPS sessions (models a fresh requester thread)."""
    client_utils._http_session_cache.store = {}


def measure_with_setup(
    name: str,
    setup: Callable[[], None],
    func: Callable[[], None],
    *,
    count: int,
) -> BenchResult:
    """Time ``func`` ``count`` times, running ``setup`` before each sample untimed.

    ``common.measure`` has no per-sample hook, and these scenarios must delete the
    downloaded file (and sometimes evict the thread session) *between* samples
    without paying for it inside the measured region.
    """
    warmup = scaled_warmup(count)
    for _ in range(warmup):
        setup()
        func()
    samples: list[float] = []
    for _ in range(count):
        setup()
        start = perf_counter()
        func()
        samples.append(perf_counter() - start)
    return BenchResult(name, samples, count)


class LanServerHarness:
    """One loopback ``lan/1`` node: bucket, device identity, certificate, role.

    The node's role configuration lives in ``hsync_client.lan_server.CONFIG`` for
    the whole harness lifetime, so the seeder and the master can coexist in one
    process with independent buckets — the same separation two processes have.
    """

    def __init__(
        self,
        mode: LANServerMode,
        root: Path,
        *,
        name: str,
        cid: str,
        peer_read_cap: int = 2,
        bucket: str = BENCH_BUCKET,
        source_service: LanSourceService | None = None,
    ) -> None:
        self.mode = mode
        self.name = name
        self.bucket = bucket
        # The port is reserved before ``start`` so a requester env can be built
        # from ``base_url`` while the certificate (hence the fingerprint) is not
        # known yet; ``LanRequesterEnv`` reads both lazily at ``start``.
        self._port = free_port()
        node_root = root / name
        self._bucket_dir = node_root / "bucket"
        self._device_dir = node_root / "device"
        self._cid = cid
        self._peer_read_cap = peer_read_cap
        self._source_service = source_service
        self._fingerprint = ""
        self._server: LANServer | None = None
        self._patcher: Any = None

    @property
    def bucket_dir(self) -> Path:
        """Local directory this node serves from."""
        return self._bucket_dir

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"https://{_LOOPBACK}:{self._port}"

    @property
    def cert_fingerprint(self) -> str:
        """Lowercase SHA-256 fingerprint peers pin (empty before ``start``)."""
        return self._fingerprint

    @property
    def download_url(self) -> str:
        """Complete exact-hash download endpoint for this node's bucket."""
        return build_peer_download_url(self.bucket, _LOOPBACK, self._port)

    def peer_source(self, *, download_url: str | None = None) -> LanPeerSource:
        """Build the immutable source a master would return for this node.

        ``serve_slots`` is this node's real read cap (the seeder binds with it),
        so the requester-side admission under test matches production.
        ``download_url`` overrides the endpoint — used to route through a
        :class:`LatencyRelay` — while the certificate fingerprint stays this
        node's, because the relay forwards bytes and TLS remains end to end.
        """
        return LanPeerSource(
            cid=self._cid,
            download_url=download_url or self.download_url,
            cert_fingerprint=self._fingerprint,
            compression=False,
            serve_slots=self._peer_read_cap,
        )

    def relayed_peer_source(self, relay_port: int) -> LanPeerSource:
        """Build this node's source as reached through a relay on *relay_port*."""
        return self.peer_source(
            download_url=build_peer_download_url(self.bucket, _LOOPBACK, relay_port)
        )

    def start(self) -> LanServerHarness:
        """Generate/load the node certificate and bind the role server."""
        self._bucket_dir.mkdir(parents=True, exist_ok=True)
        self._device_dir.mkdir(parents=True, exist_ok=True)
        with env_override("HSYNC_DEVICE_DIR", str(self._device_dir)):
            cert = load_or_create_master_cert(f"hsync-bench-{self.name}")
        self._fingerprint = cert.fingerprint
        server_config = {
            "LOCAL_BUCKET": str(self._bucket_dir),
            "REMOTE_BUCKET": self.bucket,
            "LAN_KEY": None,
            "CID": self._cid,
            "API": {"buckets": f"{self.base_url}/api/v1/buckets"},
        }
        self._patcher = patch("hsync_client.lan_server.CONFIG", server_config)
        self._patcher.start()
        self._server = LANServer(
            port=self._port,
            use_https=True,
            cert_file=cert.cert_file,
            key_file=cert.key_file,
            mode=self.mode,
            bucket=self.bucket,
            peer_read_cap=self._peer_read_cap,
            source_service=self._source_service,
        )
        try:
            self._server.start()
        except BaseException:
            self._patcher.stop()
            self._patcher = None
            raise
        return self

    def stop(self) -> None:
        """Stop the server and restore the patched role config."""
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._patcher is not None:
            self._patcher.stop()
            self._patcher = None

    def __enter__(self) -> LanServerHarness:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class LanRequesterEnv:
    """Requester-side global state: ``CONFIG``, state dirs, pin file, caches."""

    def __init__(
        self,
        root: Path,
        *,
        master: LanServerHarness,
        cid: str,
        bucket: str = BENCH_BUCKET,
    ) -> None:
        self._root = root / "requester"
        self._bucket_dir = self._root / "bucket"
        self._state_dir = self._root / "state"
        self._pin_file = self._root / "lan_known_peers.json"
        self._master = master
        self._cid = cid
        self._bucket = bucket
        self._saved_config: dict[str, Any] = {}
        self._saved_env: list[tuple[str, str | None]] = []

    @property
    def bucket_dir(self) -> Path:
        """Local bucket the requester downloads into."""
        return self._bucket_dir

    @property
    def cid(self) -> str:
        return self._cid

    def start(self) -> LanRequesterEnv:
        """Install the requester config, temp state dirs and clean caches."""
        self._bucket_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._remember_env("HSYNC_STATE_DIR", str(self._state_dir))
        self._remember_env("HSYNC_LAN_PIN_FILE", str(self._pin_file))
        self._saved_config = dict(GLOBAL_CONFIG)
        set_config(
            HsyncConfig(
                API={"buckets": f"{self._master.base_url}/api/v1/buckets"},
                REMOTE_BUCKET=self._bucket,
                LOCAL_BUCKET=str(self._bucket_dir),
                CID=self._cid,
                MASTER_CERT_FINGERPRINT=self._master.cert_fingerprint,
                REQUEST_HEADERS={},
                HTTP_TIMEOUT=30,
                COMPRESSION_ENABLED=False,
                MIN_FREE_DISK_SPACE=0,
                MAX_INBOUND_SIZE=0,
                # One attempt per sample: a hidden retry would silently turn a
                # failed source into a slow success inside the measured region.
                TRANSFER_RETRY_ATTEMPTS=1,
                TRANSFER_RETRY_MIN_WAIT=0.05,
                TRANSFER_RETRY_MAX_WAIT=0.5,
                LAN_PEER_SOURCES_MAX=3,
            )
        )
        self._reset_caches()
        return self

    def stop(self) -> None:
        """Restore the previous environment and config, then drop every cache."""
        for key, previous in reversed(self._saved_env):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._saved_env.clear()
        if self._saved_config:
            set_config(self._saved_config)  # type: ignore[arg-type]
        self._reset_caches()

    def _remember_env(self, key: str, value: str) -> None:
        self._saved_env.append((key, os.environ.get(key)))
        os.environ[key] = value

    def _reset_caches(self) -> None:
        sync_state._stores.clear()
        reset_bucket_folder_cache()
        clear_http_sessions()

    def resolver(self, *, enabled: bool = True) -> LanSourceResolver:
        """Build the real requester resolver the production path consumes."""
        return LanSourceResolver(
            master_url=self._master.base_url,
            master_cert_fingerprint=self._master.cert_fingerprint,
            requester_cid=self._cid,
            bucket=self._bucket,
            lan_key=None,
            local_bucket=str(self._bucket_dir),
            enabled=enabled,
            master_capable=True,
            timeout=30.0,
            pin_store=PinStore(self._pin_file),
        )

    def remove_destinations(
        self,
        relpaths: Iterable[str],
        *,
        drop_sessions: bool = False,
    ) -> None:
        """Delete downloaded files and their partials so the next sample transfers.

        With ``drop_sessions`` the calling thread's cached HTTPS sessions are
        evicted as well, so the next sample pays a full TCP+TLS handshake (the
        cold path); the master path is always measured warm because a LAN slave
        keeps its master session open for the whole role lifetime.
        """
        for relpath in relpaths:
            target = self._bucket_dir / relpath
            for path in (target, Path(f"{target}.part")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        if drop_sessions:
            clear_http_sessions()

    def remove_destination(self, relpath: str, *, drop_sessions: bool = False) -> None:
        """Single-path convenience wrapper around :meth:`remove_destinations`."""
        self.remove_destinations((relpath,), drop_sessions=drop_sessions)

    def __enter__(self) -> LanRequesterEnv:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def peer_get(
    uri: str,
    params: dict[str, str],
    *,
    cert_fingerprint: str,
) -> tuple[float, int, int]:
    """One production-shaped exact-hash peer GET.

    Returns:
        ``(latency_s, http_status, body_bytes)``; a transport failure is reported
        as status ``0`` so a caller can tell it apart from an HTTP error.
    """
    started = perf_counter()
    try:
        response = client_utils.http_get(
            uri,
            cert_fingerprint=cert_fingerprint,
            params=params,
            stream=True,
            timeout=(2.0, 30.0),
            verify=False,
        )
    except RequestException:
        return perf_counter() - started, 0, 0
    received = 0
    status = int(response.status_code)
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                received += len(chunk)
    finally:
        response.close()
    return perf_counter() - started, status, received


def concurrent_peer_gets(
    targets: Sequence[tuple[str, dict[str, str]]],
    *,
    cert_fingerprint: str,
    workers: int,
) -> list[tuple[float, int, int]]:
    """Run one exact-hash peer GET per target, ``workers`` at a time.

    Each worker thread builds its own pinned session (the session cache is
    thread-local), exactly like the production transfer pool. Results keep the
    input order, so a caller can attribute a status to a target.
    """
    worker = partial(peer_get, cert_fingerprint=cert_fingerprint)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_peer") as pool:
        futures = [pool.submit(worker, uri, params) for uri, params in targets]
        return [future.result() for future in futures]


class DownloadOutcomeRecorder:
    """Count where the bytes of each production download actually came from.

    Wraps the real ``transfer._download_from_source_attempt`` (all arguments are
    forwarded unchanged) so a scenario can prove the branch it claims: peer body
    served, peer refused for saturation, or a full download taken from the master
    because every peer candidate failed. Used by the cycle scenarios, whose
    acceptance criteria are stated in those terms.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peer_ok = 0
        self._peer_busy = 0
        self._peer_other_failures = 0
        self._master_full = 0

    def reset(self) -> None:
        """Zero every counter (one call per cycle sample)."""
        with self._lock:
            self._peer_ok = 0
            self._peer_busy = 0
            self._peer_other_failures = 0
            self._master_full = 0

    @property
    def peer_ok(self) -> int:
        return self._peer_ok

    @property
    def peer_busy(self) -> int:
        return self._peer_busy

    @property
    def peer_other_failures(self) -> int:
        return self._peer_other_failures

    @property
    def master_full(self) -> int:
        return self._master_full

    def summary(self) -> str:
        """One-line outcome summary, stable enough to diff across runs."""
        return (
            f"peer_ok={self._peer_ok} peer_busy={self._peer_busy} "
            f"peer_failed={self._peer_other_failures} master_full={self._master_full}"
        )

    def note_peer_result(self, *, failed_kind: str | None) -> None:
        """Record one peer download attempt (``failed_kind=None`` means served)."""
        with self._lock:
            if failed_kind is None:
                self._peer_ok += 1
            elif failed_kind == "busy":
                self._peer_busy += 1
            else:
                self._peer_other_failures += 1

    def note_master_full(self) -> None:
        """Record one full download served by the master (i.e. a fallback)."""
        with self._lock:
            self._master_full += 1


def install_outcome_recorder(recorder: DownloadOutcomeRecorder) -> Any:
    """Patch the production per-attempt downloader to feed *recorder*.

    Returns the started ``unittest.mock.patch`` object: the caller must stop it
    (or use it as a context manager) so the module attribute is restored.
    """
    from hsync_client import transfer as transfer_module

    original = transfer_module._download_from_source_attempt

    def recorder_wrapper(
        file: Any,
        inventory_observer: Any = None,
        *,
        source: Any = None,
        source_resolver: Any = None,
        allow_delta: bool = True,
        delta_only: bool = False,
        force_restart: bool = False,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "source": source,
            "source_resolver": source_resolver,
            "allow_delta": allow_delta,
            "delta_only": delta_only,
            "force_restart": force_restart,
        }
        if inventory_observer is None:
            handled = original(file, **kwargs)
        else:
            handled = original(file, inventory_observer, **kwargs)
        # The delta probe is a separate attempt with no bytes crossing a source
        # choice, and the master call is the fallback (or the baseline path).
        if delta_only:
            return handled
        if source is not None:
            recorder.note_peer_result(failed_kind=None)
        elif allow_delta is False:
            recorder.note_master_full()
        return handled

    patcher = patch.object(transfer_module, "_download_from_source_attempt", recorder_wrapper)
    patcher.start()
    return patcher


def install_failure_recorder(recorder: DownloadOutcomeRecorder) -> Any:
    """Patch ``LanSourceResolver.record_failure`` to observe every peer failure.

    Failures are caught inside the transfer attempt loop, so the outcome
    recorder alone cannot see them. The resolver hook is the production
    observation point (it is also what drives the cooldown/invalidation), and
    the original method runs unchanged afterwards.
    """
    from hsync_client.lan_sources import LanSourceResolver

    original = LanSourceResolver.record_failure

    def recording_failure(self: Any, source: Any, kind: Any) -> None:
        recorder.note_peer_result(failed_kind=str(kind))
        original(self, source, kind)

    patcher = patch.object(LanSourceResolver, "record_failure", recording_failure)
    patcher.start()
    return patcher
