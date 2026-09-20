# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared helpers, fixtures and constants for LAN black-box tests.

These are imported by the individual test_bb_lan_*.py modules.

Isolation contract (every helper here honours it):

* **Device identity** — every LAN subprocess gets its own ``HSYNC_DEVICE_DIR``
  (sibling of its synced dir). Two peers in one test are *distinct devices*
  (same host + same bucket + same device would not even discover each other),
  and nothing ever touches the developer's real ``~/.local/state/hsync``.
  Restarting a peer on the same dir reuses the same identity (device ID and
  certificate survive); deleting the ``.pem`` files rotates the certificate
  while keeping the device ID.
* **TOFU pins** — per-peer ``HSYNC_LAN_PIN_FILE``; pins never leak into the
  developer's real pin store.
* **Ready file** — per-instance ``HSYNC_READY_FILE`` *outside* the synced dir
  (a file inside the bucket would be synced to the peer: it is not among the
  engine-owned ``INTERNAL_FILENAMES``). Readiness is confirmed only when the
  file contains exactly the expected subprocess PID.
* **Environment** — subprocess envs are scrubbed of every pre-existing
  ``HSYNC_*`` variable before test overrides are applied, so the developer's
  shell configuration can never leak into a run.
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from hsync_common.labels import ActionLabel

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

BB_ADMIN_SECRET = "bb0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
BB_API_KEY = f"hs_HsyncAdminBootstrap01_{BB_ADMIN_SECRET}"
_LAN_KEY = "test-lan-shared-secret"
_HSYNC_COMMON_DIR = str(Path(__file__).resolve().parent.parent.parent / "hsync-common")


def unauth_write_config(bucket, **overrides):
    """A LAN server ``CONFIG`` with no key and the dangerous write opt-in enabled."""
    config = {
        "LOCAL_BUCKET": str(bucket),
        "LAN_KEY": None,
        "LAN_ALLOW_UNAUTHENTICATED_WRITES": True,
    }
    config.update(overrides)
    return config


def _lan_ready_path(local_path: str) -> str:
    """Ready file for a LAN subprocess: a *sibling* of the synced dir.

    Never inside *local_path*: ``.hsync_ready`` is not an engine-internal name
    (``hsync_common.path_validation.INTERNAL_FILENAMES``) and dotfiles are
    synced by design, so an in-dir ready file would propagate to the peer and
    could carry the *other* process's PID. The harness confirms readiness by
    comparing the file content with the expected subprocess PID, so a stale
    file from a killed predecessor is never trusted (see
    :func:`_wait_for_client_ready`).
    """
    path = Path(local_path)
    return str(path.parent / f"{path.name}.ready")


def _lan_device_dir(local_path: str) -> str:
    """Device identity dir (``HSYNC_DEVICE_DIR``) for a LAN subprocess.

    Sibling of the synced dir, derived from it: restarting a peer on the same
    *local_path* reuses the same device ID and certificate (TOFU pins survive
    the restart). Distinct peers get distinct device identities — mandatory:
    same host + same bucket + same device ID would be ignored as "self" in
    discovery.
    """
    path = Path(local_path)
    return str(path.parent / f"{path.name}_device")


def _lan_pin_file(local_path: str) -> str:
    """TOFU pin store (``HSYNC_LAN_PIN_FILE``) for a LAN subprocess."""
    path = Path(local_path)
    return str(path.parent / f"{path.name}_pins.json")


def _lan_log_path(local_path: str) -> str:
    """Log file for a LAN subprocess: a *sibling* of the synced dir.

    Peer output is **redirected to this file**, never to an undrained pipe:
    a PIPE whose 64 KiB buffer fills during a large sync blocks the peer
    inside ``write()`` and stalls replication entirely (observed as a
    scale-dependent timeout: a small tree converges, a few hundred files
    hang). Sibling of the synced dir for the same reason as the ready file
    (never synced), and inside the pytest tmp dir so it is cleaned up.
    """
    path = Path(local_path)
    return str(path.parent / f"{path.name}.log")


def _free_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _lan_env(local_path: str, extra: dict[str, str | None] | None = None) -> dict[str, str]:
    """Subprocess environment for a LAN client: scrubbed and fully wired.

    Starts from ``os.environ`` minus every ``HSYNC_*`` variable (the developer's
    own configuration must never leak into a test run), then applies the
    per-peer device identity, pin store, ready file and instance-lock dir.
    ``extra`` values override the defaults; a value of ``None`` *removes* the
    variable (e.g. ``{"HSYNC_LAN_KEY": None}`` for keyless trust-LAN tests).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    path = Path(local_path)
    env.update(
        {
            "HSYNC_LAN_KEY": _LAN_KEY,
            "PYTHONPATH": _HSYNC_COMMON_DIR,
            "HSYNC_READY_FILE": _lan_ready_path(local_path),
            "HSYNC_DEVICE_DIR": _lan_device_dir(local_path),
            "HSYNC_LAN_PIN_FILE": _lan_pin_file(local_path),
            "HSYNC_LOCKS_DIR": str(path.parent / "locks"),
            "HSYNC_STATE_DIR": str(path.parent / f"{path.name}_state"),
            # Deterministic offline clock probing for tests (ADR-0023): a dead
            # loopback endpoint keeps the NTP probe fast and internet-free, so
            # no test ever depends on a reachable public NTP server. Tests that
            # exercise a live SNTP stub override it via ``extra``.
            "HSYNC_NTP_HOSTS": "127.0.0.1:9",
        }
    )
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _lan_start(
    local_path: str,
    bucket: str,
    *,
    force_master: bool = False,
    never_master: bool = False,
    lan_port: int = 0,
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str | None] | None = None,
) -> subprocess.Popen:
    """Start a LAN-mode client subprocess with isolated identity and state.

    Output is captured to the per-peer log *file* returned by
    :func:`_lan_log_path` (stderr merged into stdout: loguru logs to stdout),
    so :func:`_stderr_text` and :func:`_wait_for_text_in_stderr` can observe the
    process log lines without ever blocking the peer on a full pipe. Pass
    ``lan_port`` to force the LAN master listening port.
    ``extra_args``/``extra_env`` are appended to the standard arguments /
    merged into the scrubbed environment (see :func:`_lan_env`).
    """
    env = _lan_env(local_path, extra_env)
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "lan",
        "-b",
        bucket,
        "-p",
        local_path,
        "--yes",
        "--min-free-disk-space",
        "0",
        "--no-autotune",
        "--discovery-timeout",
        "10",
        "--retry-delay",
        "1",
        "--stability-delay",
        "1",
        "--http-timeout",
        "10",
    ]
    if force_master:
        args.append("--force-master")
    if never_master:
        args.append("--never-master")
    if lan_port:
        args += ["--listen-port", str(lan_port)]
    args += list(extra_args)
    # Output goes to a log FILE, never to an undrained PIPE: a pipe fills up
    # (64 KiB) during a large sync and blocks the peer in write(), stalling
    # replication (see _lan_log_path). The handle is kept open for the life of
    # the process and closed by _terminate; the path is stashed on the Popen so
    # _stderr_text can re-read the file even after the handle is closed.
    log_path = _lan_log_path(local_path)
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        args,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    proc._hsync_log_path = log_path  # type: ignore[attr-defined]
    proc._hsync_log_fh = log_fh  # type: ignore[attr-defined]
    return proc


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate and reap a subprocess (idempotent: safe on dead processes)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log_fh = getattr(proc, "_hsync_log_fh", None)
    if log_fh is not None:
        with contextlib.suppress(OSError, ValueError):
            log_fh.close()
    # Fallback for a PIPE-backed Popen (or an external one): drain and close.
    if proc.stdout:
        proc.stdout.close()


def _stderr_text(proc: subprocess.Popen) -> str:
    """Return all output captured so far (non-blocking, non-destructive).

    Reads the peer log file written by :func:`_lan_start` (a file, not a pipe:
    the peer can never block on a full buffer). The whole file is returned on
    every call, so a log line seen while polling for one marker is still
    visible when a later assertion checks for another. A PIPE-backed Popen
    (e.g. one spawned directly by a test) is still supported through the
    incremental drain path.
    """
    log_path = getattr(proc, "_hsync_log_path", None)
    if log_path is not None:
        try:
            return Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    buf: bytes = getattr(proc, "_hsync_out_buf", b"")
    if proc.stdout is not None:
        try:
            import os as _os

            _os.set_blocking(proc.stdout.fileno(), False)
            while True:
                chunk = proc.stdout.read()
                if not chunk:
                    break
                buf += chunk
        except (OSError, ValueError, AttributeError):
            pass
    proc._hsync_out_buf = buf  # type: ignore[attr-defined]
    return buf.decode("utf-8", errors="replace")


def _wait_for_file(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.3)
    return False


def _wait_for_text_in_stderr(proc: subprocess.Popen, text: str, timeout: float = 30.0) -> bool:
    """Poll until *text* appears in the process's stderr, or return False.

    Output is checked *before* the liveness poll: a process that exits quickly
    (e.g. a slave whose initial sync is rejected) still has its final log
    lines drained and matched, instead of being reported as never having
    emitted them.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if text in _stderr_text(proc):
            return True
        if proc.poll() is not None:
            time.sleep(0.2)  # let the final buffered output arrive
            return text in _stderr_text(proc)
        time.sleep(0.3)
    return False


def _wait_for_slave_events(slave: subprocess.Popen, timeout: float = 30.0) -> bool:
    """Wait until the slave is **ready** — watcher live + master handshake done.

    The observable threshold is the ``[READY]`` log line, emitted right after
    ``Listening for events from master`` once the local watcher is running and
    the handshake completed. A file mutated on the master after the slave's
    initial-sync listing but before this threshold falls into a blind spot:
    it is neither in the served listing nor received as an event, and only the
    periodic resync (``RESYNC``, default 300s) would recover it — far beyond
    any sync timeout used here. Tests that mutate the master once a slave has
    synced must wait for this marker first.
    """
    ok = _wait_for_text_in_stderr(slave, "[READY]", timeout=timeout)
    if not ok and os.environ.get("BB_DIAG") == "1":
        # Diagnostic only (opt-in): on timeout, surface the buffered subprocess
        # log so LAN flakes can be triaged from the failure output — the pipe
        # dies with the Popen in teardown and the log would otherwise be lost.
        # The gate is NOT HSYNC_*-prefixed: the conftest scrubs every HSYNC_*
        # variable from the pytest process before tests are collected.
        print(
            f"\n--- [BB_DIAG] slave pid={slave.pid} not READY "
            f"after {timeout}s; full log follows ---\n{_stderr_text(slave)}"
            "\n--- [BB_DIAG] end of slave log ---"
        )
    return ok


_WATCHER_DEGRADED_LABELS = (
    ActionLabel.WATCHER_FALLBACK.value,
    ActionLabel.FD_EXHAUSTED.value,
)


def watcher_degraded_lines(proc: subprocess.Popen) -> list[str]:
    """Return the peer's realtime-watcher degradation lines, if any.

    A live inotify watcher is a **precondition** for every event-driven LAN
    test: this harness generates the master tree and expects it to be
    broadcast as events. When the machine exhausts
    ``fs.inotify.max_user_instances`` (a per-user kernel limit easily reached
    on a developer desktop — editors and browsers hold instances too) the client
    correctly degrades to ``PollingObserver``, and a large burst can then be
    missed until the periodic ``RESYNC`` (default 300s). The observable failure
    would otherwise be an opaque convergence timeout; returning the log lines
    lets callers report the real cause.
    """
    text = _stderr_text(proc)
    return [line for line in text.splitlines() if any(m in line for m in _WATCHER_DEGRADED_LABELS)]


def assert_realtime_watcher(proc: subprocess.Popen, role: str) -> None:
    """Fail fast with an actionable message when *proc*'s watcher degraded.

    Called by the stress harness *before* generating a burst: measuring
    event-driven replication with a polling watcher is not a product failure
    but an unsatisfied environment precondition (unlike the production
    behaviour, which stays correct through RESYNC).
    """
    lines = watcher_degraded_lines(proc)
    if not lines:
        return
    raise AssertionError(
        f"{role} (pid={proc.pid}) lost the realtime (inotify) watcher — the "
        "event-driven LAN stress measurement is invalid:\n  "
        + "\n  ".join(lines[:5])
        + "\n  Fix: raise the per-user limit "
        "(sudo sysctl fs.inotify.max_user_instances=512) or close other "
        "watcher-heavy applications, then rerun."
    )


def _wait_for_file_gone(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* no longer exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(path).exists():
            return True
        time.sleep(0.3)
    return False


def _conflict_copies(directory: str, stem: str) -> list[Path]:
    """Conflict copies present in *directory* for a given file stem."""
    return [p for p in Path(directory).iterdir() if p.name.startswith(f"{stem}_conflicted_copy_")]


def _wait_for_lan_server_ready(master: subprocess.Popen, port: int, timeout: float = 30.0) -> bool:
    """Poll the LAN master's health endpoint until it responds 200.

    Uses the real HTTP health endpoint (``/api/v1/health``) instead of
    parsing log output. Returns ``True`` when the server is reachable
    and responding, ``False`` on timeout or if the process exits.
    """
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if master.poll() is not None:
            return False
        try:
            r = requests.get(
                f"https://127.0.0.1:{port}/api/v1/health",
                timeout=1.0,
                verify=False,
            )
            if r.status_code == 200:
                return True
        except (
            ConnectionError,
            ConnectionRefusedError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ):
            pass
        time.sleep(0.2)
    return False


def _wait_for_client_ready(
    proc: subprocess.Popen,
    ready_file: str,
    timeout: float = 30.0,
) -> bool:
    """Wait for the client subprocess to signal readiness via its PID file.

    The client writes its PID into *ready_file* (atomically, only when
    ``HSYNC_READY_FILE`` is set in its environment): in watch/remote mode
    after the first completed sync cycle, in LAN master mode once its HTTP
    and WebSocket servers are up, in LAN slave mode after the initial sync.

    Readiness is accepted **only** when the file content is exactly
    ``proc.pid``. A stale file (dead predecessor) or a foreign PID (PID
    reuse, or a file that somehow belongs to another process) is never
    trusted; the file is never deleted by the harness — the right process
    overwrites it atomically.

    Returns ``True`` when the client is confirmed ready, ``False`` on
    timeout or if the subprocess exits prematurely.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            content = Path(ready_file).read_text().strip()
        except OSError:
            content = ""
        if content and content == str(proc.pid):
            return True
        time.sleep(0.2)
    return False


def _wait_for_master(
    master: subprocess.Popen,
    slave_dir: str,
    bucket: str,
    timeout: float = 30,
    slave_extra_args: tuple[str, ...] = (),
) -> None:
    """Start a temporary slave so the master discovers it and runs the election.

    The master only emits ``"MASTER"`` after ``add_peer`` triggers
    ``_elect_master``, so we must have at least one slave running before
    waiting for the log line. ``slave_extra_args`` must mirror any discovery
    partition flags the master was started with (e.g. ``--group``) or the
    two would never discover each other.

    The temporary slave runs on a dedicated probe directory — never on
    *slave_dir* — so it has its own device identity and TOFU pin store. A
    probe that shared *slave_dir*'s identity/pin store could TOFU-pin the
    master before it is terminated (a race made more likely as ``server_ready``
    is advertised faster), leaving the real slave with a ``matched`` pin instead
    of a first-sight ``trusted`` one. The probe only has to trigger the
    election; it must never leak state into the real slave's store.

    Returns once the master logs ``"MASTER"``, or raises ``AssertionError``.
    The temporary slave is terminated before returning.
    """
    probe_dir = tempfile.mkdtemp(prefix="probe-", dir=str(Path(slave_dir).parent))
    slave = _lan_start(probe_dir, bucket, never_master=True, extra_args=slave_extra_args)
    try:
        assert _wait_for_text_in_stderr(master, "MASTER", timeout=timeout), (
            "Master did not become ready"
        )
    finally:
        _terminate(slave)


@pytest.fixture
def lan_master_dir(tmp_path):
    """Temp directory for the LAN master."""
    d = tmp_path / "master"
    d.mkdir()
    return str(d)


@pytest.fixture
def lan_slave_dir(tmp_path):
    """Temp directory for the LAN slave."""
    d = tmp_path / "slave"
    d.mkdir()
    return str(d)


@pytest.fixture
def lan_bucket():
    """Unique bucket name for a LAN test."""
    import uuid

    return f"lan-bb-{uuid.uuid4().hex[:10]}"


# =============================================================================
# Helpers for driving a serving LAN master's public REST / WebSocket surface.
#
# The black-box parity suite (delta, resumable upload, causal arbitration,
# protocol handshake, WebSocket relay) drives the *real* LAN master over its
# public HTTPS REST and WSS endpoints — exactly the wire a peer uses. The
# master only starts serving once it has won an election, so a slave peer is
# kept connected for the duration of each block.
# =============================================================================


def _lan_auth_headers() -> dict[str, str]:
    """Authorization headers for the LAN master's REST surface (Bearer LAN key)."""
    return {"Authorization": f"Bearer {_LAN_KEY}"}


def _lan_cid(tag: str) -> str:
    """Build a wire-valid LAN cid (exactly 21 alphanumeric chars) from a tag.

    Mirrors the server-side ``_cid`` helper: ``is_valid_cid`` rejects any
    other shape, so short readable tags would be refused before auth.
    """
    base = f"bb{tag}"
    return base + "0" * (21 - len(base))


def _lan_ws_url(lan_port: int) -> str:
    """The LAN master's WebSocket URL (it listens on ``lan_port + 1``)."""
    return f"wss://127.0.0.1:{lan_port + 1}/"


def _reserve_port_pair() -> int:
    """Find a free port *P* whose *P+1* (the LAN WebSocket port) is also free.

    The LAN master serves HTTPS on *P* and WSS on *P+1*; reserving both avoids an
    intermittent WebSocket bind failure when the adjacent port happens to be in
    use. Probes by binding (not holding), which is sufficient for the sequential
    LAN black-box runs.
    """
    import socket

    for _ in range(50):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        nxt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            nxt.bind(("127.0.0.1", port + 1))
        except OSError:
            continue
        finally:
            nxt.close()
        return port
    raise RuntimeError("could not find a free consecutive HTTP/WebSocket port pair")


@contextlib.contextmanager
def _lan_serving_master(
    master_dir: str,
    slave_dir: str,
    bucket: str,
    *,
    master_extra: tuple[str, ...] = (),
    slave_extra: tuple[str, ...] = (),
):
    """Start a force-master + slave so the master elects and serves HTTPS.

    The LAN master only starts its HTTP and WebSocket servers once it has won
    an election, which requires at least one peer via mDNS. A never-master
    slave is therefore kept connected for the duration of the block so the
    master keeps serving. Yields the LAN HTTP port *P* (WebSocket is on *P+1*);
    a verified-free consecutive port pair is allocated internally. Both
    processes are terminated on exit.
    """
    lan_port = _reserve_port_pair()
    master = _lan_start(
        master_dir, bucket, force_master=True, lan_port=lan_port, extra_args=master_extra
    )
    try:
        slave = _lan_start(slave_dir, bucket, never_master=True, extra_args=slave_extra)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=60), (
                "master did not win election and start serving"
            )
            # The master writes its PID into the ready file once HTTP + WS servers
            # are up (the same readiness threshold the existing LAN suite waits on).
            assert _wait_for_client_ready(master, _lan_ready_path(master_dir), timeout=60), (
                "master HTTP/WS servers did not become ready"
            )
            yield lan_port
        finally:
            _terminate(slave)
    finally:
        _terminate(master)


def _lan_upload(
    base_url: str,
    bucket: str,
    filename: str,
    content: bytes,
    *,
    path: str = "",
    sha256sum: str | None = None,
    permissions: str = "644",
    offset: int = 0,
    size: int = 0,
    compressed: str = "false",
    cid: str | None = None,
    vclock: str = "",
    version_id: str = "",
):
    """Upload a file to a serving LAN master over its public REST endpoint.

    Mirrors the central server's multipart upload contract so the same wire
    fields drive both modes. *cid* defaults to a wire-valid 21-char value.
    """
    import hashlib

    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        data={
            "cid": cid or _lan_cid("upld"),
            "path": path,
            "sha256sum": sha256sum
            if sha256sum is not None
            else hashlib.sha256(content).hexdigest(),
            "permissions": permissions,
            "offset": str(offset),
            "size": str(size),
            "compressed": compressed,
            "vclock": vclock,
            "version_id": version_id,
        },
        files={"file": (filename, content, "application/octet-stream")},
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def _lan_download(base_url: str, bucket: str, filename: str) -> bytes:
    """Download a file's content from a serving LAN master over REST."""
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files/download",
        params={"filename": filename},
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"LAN download failed: {r.status_code} {r.text}"
    return r.content


def _lan_files(base_url: str, bucket: str) -> set[str]:
    """List files on a serving LAN master over REST (set of filepaths)."""
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"LAN list failed: {r.status_code} {r.text}"
    return {f["filepath"] for f in r.json()}


def _upload_form_fields(
    content: bytes,
    *,
    cid: str,
    path: str = "",
    sha256sum: str | None = None,
    permissions: str = "644",
    offset: int = 0,
    size: int = 0,
    compressed: str = "false",
    vclock: str = "",
    version_id: str = "",
) -> dict[str, str]:
    """Build the multipart form-data fields for a file upload (shared wire contract).

    The single source of the upload field set, reused by the LAN upload driver
    (``_lan_upload``) and the client ``bb_server_upload`` fixture so a wire-field
    change only has to be made in one place (consumers: ``hsync-client`` BB
    suites). The central server's ``bb_upload`` lives in a separate repo.
    """
    import hashlib

    return {
        "cid": cid,
        "path": path,
        "sha256sum": sha256sum if sha256sum is not None else hashlib.sha256(content).hexdigest(),
        "permissions": permissions,
        "offset": str(offset),
        "size": str(size),
        "compressed": compressed,
        "vclock": vclock,
        "version_id": version_id,
    }


def _lan_upload(
    base_url: str,
    bucket: str,
    filename: str,
    content: bytes,
    *,
    path: str = "",
    sha256sum: str | None = None,
    permissions: str = "644",
    offset: int = 0,
    size: int = 0,
    compressed: str = "false",
    cid: str | None = None,
    vclock: str = "",
    version_id: str = "",
):
    """Upload a file to a serving LAN master over its public REST endpoint.

    Mirrors the central server's multipart upload contract so the same wire
    fields drive both modes. *cid* defaults to a wire-valid 21-char value.
    """
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        data=_upload_form_fields(
            content,
            cid=cid or _lan_cid("upld"),
            path=path,
            sha256sum=sha256sum,
            permissions=permissions,
            offset=offset,
            size=size,
            compressed=compressed,
            vclock=vclock,
            version_id=version_id,
        ),
        files={"file": (filename, content, "application/octet-stream")},
        headers=_lan_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def thread_cpu_totals(pid: int) -> dict[str, float]:
    """Return per-thread CPU seconds (utime+stime) for *pid*, keyed by name.

    Linux ``/proc/<pid>/task/*/stat`` reader used by the optional LAN lane
    attribution (``HSYNC_BB_STRESS_PROFILE_CPU=1``, plan §13.13) to split the
    slave's CPU by thread across a burst. Sums threads that share a name.
    Returns ``{}`` when ``/proc`` is unavailable (non-Linux, or the process
    exited); never raises.
    """
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        ticks = 100.0
    totals: dict[str, float] = {}
    try:
        entries = list(Path(f"/proc/{pid}/task").iterdir())
    except OSError:
        return {}
    for entry in entries:
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue
        lparen = raw.find("(")
        rparen = raw.rfind(")")
        if lparen < 0 or rparen < 0:
            continue
        name = raw[lparen + 1 : rparen]
        # Fields after ") " start at state (index 0); utime=11, stime=12.
        fields = raw[rparen + 2 :].split()
        try:
            cpu_ticks = int(fields[11]) + int(fields[12])
        except (IndexError, ValueError):
            continue
        totals[name] = totals.get(name, 0.0) + cpu_ticks / ticks
    return totals
