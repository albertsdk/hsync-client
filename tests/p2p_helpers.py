# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared helpers, fixtures and constants for P2P black-box tests.

These are imported by the individual test_bb_p2p_*.py modules.

Isolation contract (every helper here honours it):

* **Device identity** — every P2P subprocess gets its own ``HSYNC_DEVICE_DIR``
  (sibling of its synced dir). Two peers in one test are *distinct devices*
  (same host + same bucket + same device would not even discover each other),
  and nothing ever touches the developer's real ``~/.local/state/hsync``.
  Restarting a peer on the same dir reuses the same identity (device ID and
  certificate survive); deleting the ``.pem`` files rotates the certificate
  while keeping the device ID.
* **TOFU pins** — per-peer ``HSYNC_P2P_PIN_FILE``; pins never leak into the
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
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from hsync_common.labels import ActionLabel

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

BB_ADMIN_SECRET = "bb0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
BB_API_KEY = f"hs_HsyncAdminRendezvous01_{BB_ADMIN_SECRET}"
# The shared LAN key the P2P black-box pairs use. It must satisfy the client's
# raw-key floor (``hsync_common.protocol.rdv_tokens::MIN_GROUP_KEY_CHARS``):
# a shorter value is rejected at startup, so the pair would never connect.
_P2P_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_HSYNC_COMMON_DIR = str(Path(__file__).resolve().parent.parent.parent / "hsync-common")


def unauth_write_config(bucket, **overrides):
    """A P2P server ``CONFIG`` with no key and the dangerous write opt-in enabled."""
    config = {
        "LOCAL_BUCKET": str(bucket),
        "P2P_KEY": None,
        "P2P_ALLOW_UNAUTHENTICATED_WRITES": True,
    }
    config.update(overrides)
    return config


def _p2p_ready_path(local_path: str) -> str:
    """Ready file for a P2P subprocess: a *sibling* of the synced dir.

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


def _p2p_device_dir(local_path: str) -> str:
    """Device identity dir (``HSYNC_DEVICE_DIR``) for a P2P subprocess.

    Sibling of the synced dir, derived from it: restarting a peer on the same
    *local_path* reuses the same device ID and certificate (TOFU pins survive
    the restart). Distinct peers get distinct device identities — mandatory:
    same host + same bucket + same device ID would be ignored as "self" in
    rendezvous.
    """
    path = Path(local_path)
    return str(path.parent / f"{path.name}_device")


def _p2p_pin_file(local_path: str) -> str:
    """TOFU pin store (``HSYNC_P2P_PIN_FILE``) for a P2P subprocess."""
    path = Path(local_path)
    return str(path.parent / f"{path.name}_pins.json")


def _p2p_log_path(local_path: str) -> str:
    """Log file for a P2P subprocess: a *sibling* of the synced dir.

    Peer output is **redirected to this file**, never to an undrained pipe:
    a PIPE whose 64 KiB buffer fills during a large sync blocks the peer
    inside ``write()`` and stalls replication entirely (observed as a
    scale-dependent timeout: a small tree converges, a few hundred files
    hang). Sibling of the synced dir for the same reason as the ready file
    (never synced), and inside the pytest tmp dir so it is cleaned up.
    """
    path = Path(local_path)
    return str(path.parent / f"{path.name}.log")


def _p2p_env(local_path: str, extra: dict[str, str | None] | None = None) -> dict[str, str]:
    """Subprocess environment for a P2P client: scrubbed and fully wired.

    Starts from ``os.environ`` minus every ``HSYNC_*`` variable (the developer's
    own configuration must never leak into a test run), then applies the
    per-peer device identity, pin store, ready file and instance-lock dir.
    ``extra`` values override the defaults; a value of ``None`` *removes* the
    variable (e.g. ``{"HSYNC_P2P_KEY": None}`` for keyless trust-P2P tests).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    path = Path(local_path)
    env.update(
        {
            "HSYNC_P2P_KEY": _P2P_KEY,
            "PYTHONPATH": _HSYNC_COMMON_DIR,
            "HSYNC_READY_FILE": _p2p_ready_path(local_path),
            "HSYNC_DEVICE_DIR": _p2p_device_dir(local_path),
            "HSYNC_P2P_PIN_FILE": _p2p_pin_file(local_path),
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


def _p2p_start(
    local_path: str,
    bucket: str,
    *,
    prefer_master: bool = False,
    never_master: bool = False,
    p2p_listen_port: int = 0,
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str | None] | None = None,
    events_path: str | None = None,
) -> subprocess.Popen:
    """Start a P2P-mode client subprocess with isolated identity and state.

    Output is captured to the per-peer log *file* returned by
    :func:`_p2p_log_path` (stderr merged into stdout: loguru logs to stdout),
    so :func:`_stderr_text` and :func:`_wait_for_text_in_stderr` can observe the
    process log lines without ever blocking the peer on a full pipe. Pass
    ``p2p_listen_port`` to force the P2P master listening port.
    ``extra_args``/``extra_env`` are appended to the standard arguments /
    merged into the scrubbed environment (see :func:`_p2p_env`).
    ``events_path`` attaches the structured UI-event channel
    (``--events-json-fd``) to a regular file, so a test can assert what a GUI
    consumer would receive.
    """
    env = _p2p_env(local_path, extra_env)
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "p2p",
        "-b",
        bucket,
        "-p",
        local_path,
        "--allow-deletions",
        "--min-free-disk-space",
        "0",
        "--no-autotune",
        "--peer-rendezvous-timeout",
        "10",
        "--retry-delay",
        "1",
        "--stability-delay",
        "1",
        "--http-timeout",
        "10",
    ]
    if prefer_master:
        args.append("--prefer-master")
    if never_master:
        args.append("--never-master")
    if p2p_listen_port:
        args += ["--listen-port", str(p2p_listen_port)]
    args += list(extra_args)
    # Output goes to a log FILE, never to an undrained PIPE: a pipe fills up
    # (64 KiB) during a large sync and blocks the peer in write(), stalling
    # replication (see _p2p_log_path). The handle is kept open for the life of
    # the process and closed by _terminate; the path is stashed on the Popen so
    # _stderr_text can re-read the file even after the handle is closed.
    log_path = _p2p_log_path(local_path)
    log_fh = open(log_path, "wb")
    # The structured-event channel takes a descriptor, so it also goes to a
    # file: a pipe would fill up and stall the peer exactly like stdout would.
    events_fh = None
    pass_fds: tuple[int, ...] = ()
    if events_path:
        events_fh = open(events_path, "wb")
        os.set_inheritable(events_fh.fileno(), True)
        pass_fds = (events_fh.fileno(),)
        args += ["--events-json-fd", str(events_fh.fileno())]
    proc = subprocess.Popen(
        args,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        pass_fds=pass_fds,
    )
    proc._hsync_log_path = log_path  # type: ignore[attr-defined]
    proc._hsync_log_fh = log_fh  # type: ignore[attr-defined]
    proc._hsync_events_fh = events_fh  # type: ignore[attr-defined]
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
    events_fh = getattr(proc, "_hsync_events_fh", None)
    if events_fh is not None:
        with contextlib.suppress(OSError, ValueError):
            events_fh.close()
    # Fallback for a PIPE-backed Popen (or an external one): drain and close.
    if proc.stdout:
        proc.stdout.close()


def _stderr_text(proc: subprocess.Popen) -> str:
    """Return all output captured so far (non-blocking, non-destructive).

    Reads the peer log file written by :func:`_p2p_start` (a file, not a pipe:
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
        # log so P2P flakes can be triaged from the failure output — the pipe
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

    A live inotify watcher is a **precondition** for every event-driven P2P
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
        "event-driven P2P stress measurement is invalid:\n  "
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


def _wait_for_p2p_server_ready(master: subprocess.Popen, port: int, timeout: float = 30.0) -> bool:
    """Poll the P2P master's health endpoint until it responds 200.

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
    after the first completed sync cycle, in P2P master mode once its HTTP
    and WebSocket servers are up, in P2P slave mode after the initial sync.

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
    waiting for the log line. ``slave_extra_args`` must mirror any rendezvous
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
    slave = _p2p_start(probe_dir, bucket, never_master=True, extra_args=slave_extra_args)
    try:
        assert _wait_for_text_in_stderr(master, "MASTER", timeout=timeout), (
            "Master did not become ready"
        )
    finally:
        _terminate(slave)


@pytest.fixture
def p2p_master_dir(tmp_path):
    """Temp directory for the P2P master."""
    d = tmp_path / "master"
    d.mkdir()
    return str(d)


@pytest.fixture
def p2p_slave_dir(tmp_path):
    """Temp directory for the P2P slave."""
    d = tmp_path / "slave"
    d.mkdir()
    return str(d)


@pytest.fixture
def p2p_bucket():
    """Unique bucket name for a P2P test."""
    import uuid

    return f"lan-bb-{uuid.uuid4().hex[:10]}"


# =============================================================================
# Helpers for driving a serving P2P master's public REST / WebSocket surface.
#
# The black-box parity suite (delta, resumable upload, causal arbitration,
# protocol handshake, WebSocket relay) drives the *real* P2P master over its
# public HTTPS REST and WSS endpoints — exactly the wire a peer uses. The
# master only starts serving once it has won an election, so a slave peer is
# kept connected for the duration of each block.
# =============================================================================


def _p2p_auth_headers() -> dict[str, str]:
    """Authorization headers for the P2P master's REST surface (Bearer P2P key)."""
    return {"Authorization": f"Bearer {_P2P_KEY}"}


def _p2p_cid(tag: str) -> str:
    """Build a wire-valid P2P cid (exactly 21 alphanumeric chars) from a tag.

    Mirrors the server-side ``_cid`` helper: ``is_valid_cid`` rejects any
    other shape, so short readable tags would be refused before auth.
    """
    base = f"bb{tag}"
    return base + "0" * (21 - len(base))


def _p2p_ws_url(p2p_listen_port: int, bucket: str, cid: str) -> str:
    """The P2P master's realtime WebSocket URL (same port as HTTPS)."""
    return f"wss://127.0.0.1:{p2p_listen_port}/api/v1/buckets/{bucket}/events/{cid}"


def _reserve_port() -> int:
    """Find a free TCP port for the unified P2P listener.

    The master serves HTTPS REST and WSS on ONE port; probing by binding (not
    holding) is sufficient for the sequential P2P black-box runs.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@contextlib.contextmanager
def _p2p_serving_master(
    master_dir: str,
    slave_dir: str,
    bucket: str,
    *,
    master_extra: tuple[str, ...] = (),
    slave_extra: tuple[str, ...] = (),
):
    """Start a prefer-master + slave so the master elects and serves HTTPS.

    The P2P master only starts serving once it has won an election, which
    requires at least one peer via mDNS. A never-master slave is therefore kept
    connected for the duration of the block so the master keeps serving. Yields
    the unified P2P port (HTTPS REST and WSS share it); a verified-free port is
    allocated internally. Both processes are terminated on exit.
    """
    p2p_listen_port = _reserve_port()
    master = _p2p_start(
        master_dir,
        bucket,
        prefer_master=True,
        p2p_listen_port=p2p_listen_port,
        extra_args=master_extra,
    )
    try:
        slave = _p2p_start(slave_dir, bucket, never_master=True, extra_args=slave_extra)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=60), (
                "master did not win election and start serving"
            )
            # The master writes its PID into the ready file once HTTP + WS servers
            # are up (the same readiness threshold the existing P2P suite waits on).
            assert _wait_for_client_ready(master, _p2p_ready_path(master_dir), timeout=60), (
                "master HTTP/WS servers did not become ready"
            )
            yield p2p_listen_port
        finally:
            _terminate(slave)
    finally:
        _terminate(master)


def _p2p_download(base_url: str, bucket: str, filename: str) -> bytes:
    """Download a file's content from a serving P2P master over REST."""
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files/download",
        params={"filename": filename},
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"P2P download failed: {r.status_code} {r.text}"
    return r.content


def _p2p_files(base_url: str, bucket: str) -> set[str]:
    """List files on a serving P2P master over REST (set of filepaths)."""
    import requests

    r = requests.get(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 200, f"P2P list failed: {r.status_code} {r.text}"
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

    The single source of the upload field set, reused by the P2P upload driver
    (``_p2p_upload``) and the client ``bb_server_upload`` fixture so a wire-field
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


def _p2p_upload(
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
    """Upload a file to a serving P2P master over its public REST endpoint.

    Mirrors the central server's multipart upload contract so the same wire
    fields drive both modes. *cid* defaults to a wire-valid 21-char value.
    """
    import requests

    return requests.post(
        f"{base_url}/api/v1/buckets/{bucket}/files",
        data=_upload_form_fields(
            content,
            cid=cid or _p2p_cid("upld"),
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
        headers=_p2p_auth_headers(),
        timeout=30.0,
        verify=False,
    )


def thread_cpu_totals(pid: int) -> dict[str, float]:
    """Return per-thread CPU seconds (utime+stime) for *pid*, keyed by name.

    Linux ``/proc/<pid>/task/*/stat`` reader used by the optional P2P lane
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


def _rendezvous_env() -> dict[str, str]:
    """Scrubbed environment for the sibling rendezvous process.

    Like :func:`_p2p_env` but without the client wiring: the rendezvous must start
    from a clean slate (no ``HSYNC_*`` leakage from the developer's shell).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}


def _rendezvous_bin() -> str | None:
    """Path to the sibling ``hsync-rendezvous`` console script, or ``None``.

    The rendezvous is a separate repository; integration tests skip when it is
    not built instead of failing the client suite.
    """
    candidate = (
        Path(__file__).resolve().parents[2]
        / "hsync-rendezvous"
        / "python"
        / ".venv"
        / "bin"
        / "hsync-rendezvous"
    )
    return str(candidate) if candidate.exists() else None


def _reserve_udp_port() -> int:
    """Pick a free UDP port for the rendezvous's QUIC listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _rendezvous_start(port: int, ready_file: Path, *, relay: bool = False) -> subprocess.Popen:
    """Spawn the sibling rendezvous on loopback (scrubbed env + ready file).

    ``relay=True`` enables the data relay (``--relay``), so two peers that no
    inbound NAT mapping reaches can sync through the node. Raises
    ``FileNotFoundError`` when the sibling repo is not built; callers translate
    that into a skip.
    """
    binary = _rendezvous_bin()
    if binary is None:
        raise FileNotFoundError("hsync-rendezvous is not built (sibling repo venv missing)")
    args = [
        binary,
        "run",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--ready-file",
        str(ready_file),
    ]
    if relay:
        args.append("--relay")
    return subprocess.Popen(
        args,
        env=_rendezvous_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_rendezvous(proc: subprocess.Popen, ready_file: Path, timeout: float = 20.0) -> bool:
    """Wait until the rendezvous writes its ready file (or exits)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.exists():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def _stop_rendezvous(proc: subprocess.Popen) -> None:
    """Terminate the rendezvous, escalating to SIGKILL, and reap it."""
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)
