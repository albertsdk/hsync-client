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

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

BB_ADMIN_SECRET = "bb0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
BB_API_KEY = f"hs_HsyncAdminBootstrap01_{BB_ADMIN_SECRET}"
_LAN_KEY = "test-lan-shared-secret"
_HSYNC_COMMON_DIR = str(Path(__file__).resolve().parent.parent.parent / "hsync-common")


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

    Output is captured with stderr merged into stdout (loguru logs to stdout),
    so :func:`_stderr_text` and :func:`_wait_for_text_in_stderr` can observe the
    process log lines. Pass ``lan_port`` to force the LAN master listening port.
    ``extra_args``/``extra_env`` are appended to the standard arguments /
    merged into the scrubbed environment (see :func:`_lan_env`).
    """
    env = _lan_env(local_path, extra_env)
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "--lan-mode",
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
        "--timeout",
        "10",
    ]
    if force_master:
        args.append("--force-master")
    if never_master:
        args.append("--never-master")
    if lan_port:
        args += ["--lan-port", str(lan_port)]
    args += list(extra_args)
    return subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate and reap a subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    # Drain pipes to avoid ResourceWarning
    if proc.stdout:
        proc.stdout.close()
    # stderr is merged into stdout (PIPE → STDOUT), so no separate pipe to drain.


def _stderr_text(proc: subprocess.Popen) -> str:
    """Return all output captured so far (non-blocking, non-destructive).

    Reads from stdout since stderr is merged into it (loguru logs to stdout).
    Bytes are drained incrementally from the pipe and accumulated on the
    process object, so repeated calls always return the full text seen so far
    rather than only the bytes buffered at read time. This keeps substring
    polling reliable: a log line consumed while waiting for one marker is still
    visible when a later assertion checks for another.
    """
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
    """Wait until the slave is listening on the master's event channel.

    A file mutated on the master after the slave's initial-sync listing but
    before the slave is listening on the WebSocket falls into a blind spot:
    it is neither in the served listing nor received as an event, and only
    the periodic resync (``RESYNC``, default 300s) would recover it — far
    beyond any sync timeout used here. Tests that mutate the master once a
    slave has synced must wait for this marker first (the slave-side watcher
    is also guaranteed started by then: it is scheduled before the WebSocket
    connect).
    """
    return _wait_for_text_in_stderr(slave, "Listening for events from master", timeout=timeout)


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
    partition flags the master was started with (e.g. ``--lan-group``) or the
    two would never discover each other.

    The temporary slave runs on *slave_dir* and must be gone (terminated)
    before the real slave starts on the same dir: the per-directory instance
    lock makes two live clients on one dir impossible by design. Do **not**
    call this while a real slave is already running on *slave_dir* — wait for
    the master's ready file (``_wait_for_client_ready``) instead.

    Returns once the master logs ``"MASTER"``, or raises ``AssertionError``.
    The temporary slave is terminated before returning.
    """
    slave = _lan_start(slave_dir, bucket, never_master=True, extra_args=slave_extra_args)
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
