# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Shared pytest fixtures for the hsync-client test suite.
"""

import os
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from .lan_helpers import (  # noqa: F401
    _upload_form_fields,
    lan_bucket,
    lan_master_dir,
    lan_slave_dir,
)


@pytest.fixture(autouse=True)
def _reset_realtime_pools():
    """Give every test a fresh realtime/transfer pool (test isolation).

    The realtime operation pools (``lan_websocket`` slave ops, ``remote_websocket``
    realtime uploads, ``sync`` transfer executor) are process-wide singletons.
    Without a reset, an operation queued but not yet executed by test A could
    run during test B — reading test B's patched CONFIG — and slow tests would
    see workers still busy from earlier tests.
    """
    from hsync_client import lan_websocket, remote_websocket
    from hsync_client import sync as sync_mod

    yield

    for module, attr in (
        (lan_websocket, "_realtime_executor"),
        (remote_websocket, "_remote_upload_executor"),
        (sync_mod, "_transfer_executor"),
    ):
        executor = getattr(module, attr, None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            setattr(module, attr, None)


@pytest.fixture(autouse=True)
def _prevent_os_environ_leakage(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Snapshot ``os.environ`` before each test and remove any keys added
    by the test afterwards.

    Tests that manipulate ``os.environ`` directly (without ``monkeypatch``)
    can leak variables into later tests.  This fixture acts as a safety net:
    it records the full set of keys before the test and deletes any new keys
    that appear when the test finishes.
    """
    before = set(os.environ.keys())
    yield
    after = set(os.environ.keys())
    added = after - before
    for key in sorted(added):
        monkeypatch.delenv(key, raising=False)


# HSYNC_* keys that ``_isolate_device_state`` manages (it overrides them with
# per-test temp dirs via ``monkeypatch.setenv``). The global scrub must NOT
# touch them: autouse-fixture ordering is not declaration order, so the scrub
# can run *after* ``_isolate_device_state`` and would otherwise delete the
# isolation values it just set.
_ISOLATION_MANAGED_HSYNC_KEYS = frozenset(
    {"HSYNC_DEVICE_DIR", "HSYNC_LOCKS_DIR", "HSYNC_STATE_DIR"}
)


@pytest.fixture(autouse=True)
def _scrub_hsync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub every ``HSYNC_*`` inherited from the developer's environment.

    The client and server read their configuration exclusively from ``HSYNC_*``
    env vars, so a shell-exported ``HSYNC_API_KEY`` / ``HSYNC_MAX_CONCURRENT_
    TRANSFERS`` / … would otherwise leak into assertions (e.g. ``EnvSettings``
    defaults, autotune-managed-keys) and make tests depend on the execution
    environment. ``monkeypatch.delenv`` restores the original values on
    teardown; tests that set their own ``HSYNC_*`` (via ``monkeypatch.setenv``
    or directly) still work because they run after this scrub.

    ``_ISOLATION_MANAGED_HSYNC_KEYS`` is excluded: those are managed by
    ``_isolate_device_state`` and must survive the scrub.
    """
    for key in [
        k for k in os.environ if k.startswith("HSYNC_") and k not in _ISOLATION_MANAGED_HSYNC_KEYS
    ]:
        monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def _isolate_device_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Keep every test away from the developer's real device identity and locks.

    The persistent device identity (``hsync_client.identity``) and the
    per-directory instance lock default to OS state/runtime directories;
    without isolation, in-process runs (CliRunner) would read/write the real
    ``~/.local/state/hsync`` and ``$XDG_RUNTIME_DIR/hsync``. Point both at a
    per-test tmp dir, reset the identity cache around each test, and release
    any lock an in-process run forgot to release.
    """
    monkeypatch.setenv("HSYNC_DEVICE_DIR", str(tmp_path / "hsync_device"))
    monkeypatch.setenv("HSYNC_LOCKS_DIR", str(tmp_path / "hsync_locks"))
    monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "hsync_state"))
    from hsync_client.identity import InstanceLock, reset_device_identity_cache

    reset_device_identity_cache()
    yield
    reset_device_identity_cache()
    InstanceLock.release_all()


@pytest.fixture(autouse=True)
def reset_sync_state_stores() -> None:
    """Reset the global sync-state store cache before each test.

    ``hsync_client.sync_state._stores`` is a module-level dict keyed by the
    configured ``LOCAL_BUCKET``.  Tests that patch ``CONFIG`` to a temp dir
    populate entries that survive into later, unrelated tests that also import
    ``sync_state``.  Clearing it here gives every test a clean slate.
    """
    from hsync_common.models.sqlstore import reset_state_db_cache

    from hsync_client import sync_state, tombstones

    sync_state._stores.clear()
    tombstones._stores.clear()
    reset_state_db_cache()


@pytest.fixture(autouse=True)
def _reset_clock_offset() -> Generator[None]:
    """Clear the process-wide clock offset around each test.

    The LAN startup NTP probe runs on a background thread; a slow probe from a
    previous test must not leak its offset. Tests that need a specific offset
    set it themselves.
    """
    from hsync_common.clock import offset_store

    offset_store.clear()
    yield
    offset_store.clear()


@pytest.fixture(autouse=True)
def _stub_lan_ntp_probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not spawn real SNTP workers from in-process ``run_lan_mode`` tests.

    ``test_timesync`` exercises the real launcher (with the probe body mocked).
    """
    if getattr(request.module, "__name__", "").endswith("test_timesync"):
        return
    monkeypatch.setattr(
        "hsync_client.timesync.start_ntp_clock_offset_probe",
        lambda logger=None: None,
    )


@pytest.fixture(autouse=True)
def deterministic_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Force plain, uncolored CLI output for every test.

    Typer forces a colored terminal whenever ``GITHUB_ACTIONS`` (or
    ``FORCE_COLOR``/``PY_COLORS``) is set, so on CI its Rich highlighter wraps
    option names like ``--force-master`` in ANSI codes, splitting the substring
    and breaking assertions on ``result.output``. That decision is baked into
    ``typer.rich_utils.FORCE_TERMINAL`` at import time, so clearing the env vars
    later is useless; instead we override the module global, which
    ``_get_rich_console`` re-reads on every call. ``False`` disables color/style
    entirely while still honouring an explicit ``COLUMNS`` (unlike ``TERM=dumb``,
    which would clamp width to 80 and wrap width-sensitive help tests).
    """
    import typer.rich_utils as rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False, raising=False)


def _terminate_child_hsync_processes() -> None:
    """Terminate orphaned hsync_client/hsync_server subprocesses of this pytest.

    The LAN black-box tests spawn ``hsync_client`` subprocesses via
    ``_lan_start`` (``lan_helpers.py``). When an assertion fires before the
    test's ``finally: _terminate(...)`` runs — or the kill doesn't reach the
    children in time — stale mDNS peers linger (``PEER_TIMEOUT = 15s``) and
    cascade into later tests (split-brain elections, port conflicts, stale
    discovery). ``_terminate`` only handles the ``Popen`` it is given; this
    helper is the safety net for the cases it cannot reach.

    ``pgrep -P <pytest_pid> -f <pattern>`` filters to *direct* children of this
    pytest process only, so unrelated ``hsync_client`` processes running on the
    host (e.g. active development) are never touched. Linux-only by design:
    the LAN BB suite already requires Linux inotify / ``on_closed`` behaviour
    (``file_event_handler.py`` ``SUPPORTS_ON_CLOSED``), so ``pgrep`` is
    guaranteed available. Falls back to a silent skip if ``pgrep`` is missing.
    """
    import os
    import signal
    import subprocess
    import time

    pid = os.getpid()
    for pattern in ("hsync_client",):
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(pid), "-f", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return  # pgrep missing: skip (tests manage their own Popen)
        for line in out.stdout.split():
            try:
                os.kill(int(line), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.3)
    # SIGKILL survivors
    for pattern in ("hsync_client",):
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(pid), "-f", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return
        for line in out.stdout.split():
            try:
                os.kill(int(line), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture(autouse=True)
def _kill_orphan_hsync_processes() -> Generator[None]:
    """Reap orphaned LAN subprocesses after each test (see helper above).

    Function-scoped because the mDNS cascade happens *between* tests: a peer
    orphaned by test N interferes with the election of test N+1. Overhead is
    two ``pgrep`` calls per test (~ms).
    """
    yield
    _terminate_child_hsync_processes()


# =============================================================================
# Black-box harness (test_bb_*.py)
#
# Behavioural, use-case-driven tests drive the *real* client through its public
# Typer CLI against a *real* hsync-server running as a separate subprocess
# (loopback HTTPS). No internal logic is mocked: only the filesystem (temp
# dirs) and the API key are test fixtures. hsync-server is an optional sibling
# Poetry group (`blackbox`): install it with `poetry install --with blackbox`
# before running this suite.
# =============================================================================

BB_ADMIN_SECRET = "bb0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd"
BB_API_KEY = f"hs_HsyncAdminBootstrap01_{BB_ADMIN_SECRET}"
BB_CLIENT_CID = "TestClientX1234567890"  # exactly 21 alphanumeric chars per CID_PATTERN


# ---------------------------------------------------------------------------
# Black-box test-data helpers
#
# ``bb_sample`` builds reproducible random artefacts (file name + payload +
# seed). Every generated sample is recorded on the test node so that, on
# failure, its name and seed are appended to the report for reproducibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBSample:
    """A reproducible random test artefact."""

    name: str
    payload: bytes
    seed: int

    @property
    def ctx(self) -> str:
        """Context string for reproducibility logs."""
        return f"[bb-sample] name={self.name} seed={self.seed}"


@pytest.fixture
def bb_sample(request: pytest.FixtureRequest):
    """Return a factory that builds reproducible random test data.

    Usage::

        s = bb_sample(size=1024)
        (Path(local_dir) / s.name).write_bytes(s.payload)
        ... assert ..., s.ctx
    """
    import random
    import uuid

    recorded: list[BBSample] = []
    request.node._bb_samples = recorded  # type: ignore[attr-defined]

    def _make(suffix: str = ".txt", size: int = 0, prefix: str = "bb") -> BBSample:
        name = f"{prefix}-{uuid.uuid4().hex[:8]}{suffix}"
        seed = random.randrange(2**32)
        payload = random.Random(seed).randbytes(size) if size else b""
        sample = BBSample(name=name, payload=payload, seed=seed)
        recorded.append(sample)
        return sample

    return _make


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """On failure, append recorded black-box sample seeds to the report.

    Uses ``hookwrapper=True`` so the default ``TestReport`` is still created
    by pytest's built-in implementation — a plain ``firstresult`` override
    would shadow it and break report-dependent fixture teardown.
    """
    outcome = yield
    if call.when == "call":
        report = outcome.get_result()
        if report.failed:
            samples = getattr(item, "_bb_samples", None)
            if samples:
                report.sections.append(
                    (
                        "bb-samples",
                        "Black-box samples (name + seed for reproducibility):\n"
                        + "\n".join(s.ctx for s in samples),
                    )
                )


def _free_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _ClientGlobalsGuard:
    """Save/restore the client's process-wide globals around an in-process run.

    ``run_sync`` mutates the global ``CONFIG`` dict in-place, installs a
    process-wide ``IgnoreMatcher``, replaces the event emitter, and toggles
    debug mode — all of which leak into later tests when the real sync runs
    under ``CliRunner``. This guard snapshots those globals before the run and
    restores them after, so the black-box suite cannot perturb the existing
    (mock-based) tests.
    """

    def __enter__(self):
        import copy

        from hsync_client import config as cfg_mod

        self._cfg_mod = cfg_mod
        self._saved_config = copy.deepcopy(dict(cfg_mod.CONFIG))
        self._saved_debug = cfg_mod.DEBUG
        from hsync_common.ignore import get_active_matcher, set_active_matcher

        self._saved_matcher = get_active_matcher()
        self._set_matcher = set_active_matcher

        from hsync_client import events as events_mod

        self._events_mod = events_mod
        self._saved_emitter = events_mod.get_emitter()
        return self

    def __exit__(self, *exc):
        # Restore CONFIG in-place (the same object every module imported).
        # Must clear the real dict, not a copy: dict(CONFIG).clear() would
        # only clear a throwaway snapshot and leave run_sync's new keys.
        self._cfg_mod.CONFIG.clear()
        self._cfg_mod.CONFIG.update(self._saved_config)
        self._cfg_mod.set_debug(self._saved_debug)
        if self._set_matcher is not None:
            self._set_matcher(self._saved_matcher)
        # Restore the event emitter (run_sync always calls init_emitter,
        # replacing the prior instance even when the fd is None).
        if self._events_mod is not None:
            self._events_mod._emitter = self._saved_emitter  # noqa: SLF001
        return False


BB_SSL_KEY_PASSWORD = "test-bb-ssl-key-password"


def _self_signed_cert(cert_dir):
    """Generate a self-signed cert + password-encrypted key for loopback HTTPS.

    The server requires the SSL private key to be stored encrypted on disk, so
    the key is encrypted with ``BB_SSL_KEY_PASSWORD`` (passed to the server
    subprocess via ``HSYNC_SSL_KEYFILE_PASSWORD``).
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(BB_SSL_KEY_PASSWORD.encode()),
        )
    )
    return str(cert_path), str(key_path)


def _server_env(buckets_dir: str, state_dir: str) -> dict:
    """Build the scrubbed, isolated env for a hsync-server subprocess.

    Every pre-existing ``HSYNC_*`` variable is stripped so the developer's own
    configuration can never leak into the test server. ``HSYNC_BUCKETS`` and
    ``HSYNC_STATE_DIR`` are pointed at per-test temp dirs so neither bucket
    content nor the shared ``syncstate.db`` account store is reused across
    tests — a shared ``syncstate.db`` caused startup ``SQLITE_BUSY`` / stale
    schema collisions when several server modules ran in one session. The
    scrypt cost is lowered to keep bootstrap fast.
    """
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(
        {
            "HSYNC_ADMIN_KEY": BB_ADMIN_SECRET,
            "HSYNC_SCRYPT_N": "16",
            "HSYNC_SCRYPT_MAXMEM": "33554432",
            "HSYNC_BUCKETS": buckets_dir,
            "HSYNC_STATE_DIR": state_dir,
            "HSYNC_RATE_LIMIT_DEFAULT": "1000000/minute",
            "HSYNC_RATE_LIMIT_UPLOAD": "1000000/minute",
            "HSYNC_RATE_LIMIT_DOWNLOAD": "1000000/minute",
            "HSYNC_RATE_LIMIT_DELTA": "1000000/minute",
            "HSYNC_RATE_LIMIT_WEBSOCKET": "1000000/minute",
            "HSYNC_RATE_LIMIT_WS_MESSAGES": "1000000/second",
            "HSYNC_WS_AUTH_TIMEOUT": "5",
            "HSYNC_WS_HEARTBEAT_INTERVAL": "5",
            "HSYNC_WS_HEARTBEAT_TIMEOUT": "3",
            "HSYNC_MIN_FREE_DISK_SPACE": "0",
            "HSYNC_ENABLE_DOCS": "false",
            "HSYNC_SSL_KEYFILE_PASSWORD": BB_SSL_KEY_PASSWORD,
        }
    )
    return env


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start the real hsync-server as a *separate subprocess* over loopback HTTPS.

    A subprocess (not an in-process uvicorn thread) is essential for these
    tests to be meaningful:

    * **Isolation of hsync_common globals** — an in-process server shares the
      process-wide ``IgnoreMatcher`` installed by the client's ``run_sync``
      (``set_active_matcher``), so the client's ignore rules would contaminate
      server-side file listing and server-side filtering bugs could never be
      detected.
    * **No settings-cache race** — an in-process server relies on the settings
      cache being populated while the env overrides are set; if any earlier
      test already imported ``hsync_server.main``, the server would silently
      start with the user's real environment.
    * **Clean teardown** — an in-process uvicorn thread routinely failed to
      stop within the join timeout (lingering keep-alive connections).

    The client hard-refuses to send the API key over plain HTTP, so the server
    runs with a self-signed cert and the client is driven with ``--insecure``.
    Yields (base_url, api_key).
    """
    import shutil
    import subprocess
    import sys
    import tempfile
    import time
    import warnings

    import requests
    from urllib3.exceptions import InsecureRequestWarning

    warnings.simplefilter("ignore", InsecureRequestWarning)

    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_buckets_")
    state_dir = tempfile.mkdtemp(prefix="hsync_bb_state_")
    cert_dir = tmp_path_factory.mktemp("bbssl")
    cert_file, key_file = _self_signed_cert(cert_dir)
    port = _free_port()

    env = _server_env(buckets_dir, state_dir)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hsync_server",
            "--port",
            str(port),
            "--ssl",
            "--ssl-cert",
            cert_file,
            "--ssl-key",
            key_file,
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    base_url = f"https://127.0.0.1:{port}"

    try:
        deadline = time.time() + 30
        last_exc = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"hsync-server subprocess exited early with code {proc.returncode}"
                )
            try:
                if requests.get(f"{base_url}/health", timeout=1.0, verify=False).status_code == 200:
                    break
            except (
                ConnectionError,
                ConnectionRefusedError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                last_exc = exc
            time.sleep(0.15)
        else:
            raise RuntimeError(f"hsync-server did not become healthy: {last_exc}")

        yield base_url, BB_API_KEY
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(buckets_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)


class _RestartableServer:
    """A real hsync-server subprocess that can be stopped and restarted.

    The buckets dir, state dir and TLS material are created once per server and
    reused across restarts so bucket data and the account store persist,
    exactly like a real deployment whose server process is restarted. Exposes
    ``base_url`` / ``api_key`` / ``restart()`` / ``stop()``.
    """

    def __init__(
        self,
        base_url: str,
        port: int,
        buckets_dir: str,
        state_dir: str,
        cert_file: str,
        key_file: str,
    ):
        self.base_url = base_url
        self.api_key = BB_API_KEY
        self._port = port
        self._buckets_dir = buckets_dir
        self._state_dir = state_dir
        self._cert_file = cert_file
        self._key_file = key_file
        self._proc = _start_server_proc(port, buckets_dir, state_dir, cert_file, key_file)
        _wait_for_server_ready_https(base_url, self._proc)

    def restart(self) -> None:
        """Stop the current server and start a fresh one on the same state."""
        _stop_proc(self._proc)
        self._proc = _start_server_proc(
            self._port, self._buckets_dir, self._state_dir, self._cert_file, self._key_file
        )
        _wait_for_server_ready_https(self.base_url, self._proc)

    def stop(self) -> None:
        _stop_proc(self._proc)


@pytest.fixture
def restartable_live_server(tmp_path):
    """A function-scoped, restartable real hsync-server over loopback HTTPS.

    Yields a :class:`_RestartableServer` whose state (buckets dir + TLS cert)
    survives calls to ``restart()``, so a test can simulate a server outage and
    prove the client reconnects and re-converges.
    """
    import shutil
    import tempfile

    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_buckets_")
    state_dir = tempfile.mkdtemp(prefix="hsync_bb_state_")
    cert_dir = tmp_path / "bbssl"
    cert_dir.mkdir()
    cert_file, key_file = _self_signed_cert(cert_dir)
    port = _free_port()
    server = _RestartableServer(
        f"https://127.0.0.1:{port}",
        port,
        buckets_dir,
        state_dir,
        str(cert_file),
        str(key_file),
    )
    try:
        yield server
    finally:
        server.stop()
        shutil.rmtree(buckets_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)


def _stop_proc(proc) -> None:
    """Terminate and reap a server subprocess, killing if needed."""
    import subprocess

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _start_server_proc(port: int, buckets_dir: str, state_dir: str, cert_file: str, key_file: str):
    """Start a real hsync-server over loopback HTTPS with a scrubbed, isolated env.

    The env is scrubbed of every pre-existing ``HSYNC_*`` variable and pointed
    at isolated buckets/state dirs (see :func:`_server_env`). The caller owns
    termination via :func:`_stop_proc`.
    """
    import subprocess
    import sys

    env = _server_env(buckets_dir, state_dir)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hsync_server",
            "--port",
            str(port),
            "--ssl",
            "--ssl-cert",
            cert_file,
            "--ssl-key",
            key_file,
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_server_ready_https(base_url: str, proc, timeout: float = 30.0) -> None:
    """Poll the server's ``/health`` endpoint until it responds 200."""
    import time
    import warnings

    import requests
    from urllib3.exceptions import InsecureRequestWarning

    warnings.simplefilter("ignore", InsecureRequestWarning)

    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"hsync-server subprocess exited early with code {proc.returncode}")
        try:
            if requests.get(f"{base_url}/health", timeout=1.0, verify=False).status_code == 200:
                return
        except (
            ConnectionError,
            ConnectionRefusedError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_exc = exc
        time.sleep(0.15)
    raise RuntimeError(f"hsync-server did not become healthy: {last_exc}")


@pytest.fixture
def bb_bucket(live_server) -> Generator[str]:
    """Create a uniquely-named bucket on the live server for one test; delete it
    afterwards so the module-scoped server never accumulates stale buckets."""
    import uuid

    import requests

    base, api_key = live_server
    name = f"bb-{uuid.uuid4().hex[:10]}"
    r = requests.post(
        f"{base}/api/v1/buckets",
        params={"bucket": name},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    assert r.status_code == 201, r.text
    yield name
    requests.delete(
        f"{base}/api/v1/buckets/{name}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )


@pytest.fixture
def bb_server_upload(live_server):
    """Upload a file to the live server over HTTPS (black-box setup helper)."""
    import requests

    base, api_key = live_server

    def _upload(
        bucket: str,
        filename: str,
        content: bytes,
        cid: str = BB_CLIENT_CID,
        vclock: str = "",
        version_id: str = "",
    ) -> None:
        r = requests.post(
            f"{base}/api/v1/buckets/{bucket}/files",
            data=_upload_form_fields(content, cid=cid, vclock=vclock, version_id=version_id),
            files={"file": (filename, content, "application/octet-stream")},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=False,
        )
        assert r.status_code == 201, f"server upload failed: {r.status_code} {r.text}"

    return _upload


@pytest.fixture
def bb_server_files(live_server):
    """List files on the live server for a bucket, returning a set of names."""
    import requests

    base, api_key = live_server

    def _list(bucket: str) -> set[str]:
        r = requests.get(
            f"{base}/api/v1/buckets/{bucket}/files",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            verify=False,
        )
        assert r.status_code == 200, r.text
        return {f["filepath"] for f in r.json()}

    return _list


@pytest.fixture
def bb_server_download(live_server):
    """Download a file's content from the live server via the public REST API."""
    import requests

    base, api_key = live_server

    def _download(bucket: str, filename: str) -> bytes:
        r = requests.get(
            f"{base}/api/v1/buckets/{bucket}/files/download",
            params={"filename": filename, "cid": BB_CLIENT_CID},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            verify=False,
        )
        assert r.status_code == 200, f"Server download failed: {r.status_code} {r.text}"
        return r.content

    return _download


@pytest.fixture
def local_dir(tmp_path):
    """A temp directory used as the client's local sync path."""
    d = tmp_path / "local"
    d.mkdir()
    return str(d)


def _watch_client_log_tail(log_path: str, limit: int = 8192) -> str:
    """Return the last ``limit`` bytes of a watch client's stderr log."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)  # SEEK_END
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return "<client log unavailable>"


def _start_watch_client(
    base_url: str,
    api_key: str,
    bucket: str,
    local_path: str,
    ready_file: str,
    locks_dir: str,
    log_path: str | None = None,
    *extra: str,
) -> "subprocess.Popen":
    """Start the real client in continuous mode as a subprocess (shared harness).

    The env is scrubbed of every pre-existing ``HSYNC_*`` variable so the
    developer's own configuration can never leak into the run; the instance-
    lock dir is per-test so lock files never land in the real
    ``$XDG_RUNTIME_DIR``. The caller receives the :class:`subprocess.Popen`
    handle and must terminate it.

    ``log_path`` (optional) directs stderr to a temp file instead of
    ``DEVNULL`` so its tail can be included in failure reports — the log must
    never go to a pipe that would block the subprocess once full.
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(
        {
            "HSYNC_API_KEY": api_key,
            "HSYNC_READY_FILE": ready_file,
            "HSYNC_LOCKS_DIR": locks_dir,
            "HSYNC_STATE_DIR": str(Path(local_path).parent / "hsync_state"),
        }
    )
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "-b",
        bucket,
        "-s",
        base_url,
        "-p",
        local_path,
        "--yes",
        "--insecure",
        "--min-free-disk-space",
        "0",
        *extra,
    ]
    stderr_file = open(log_path, "wb") if log_path else None
    try:
        return subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file if stderr_file is not None else subprocess.DEVNULL,
        )
    finally:
        if stderr_file is not None:
            stderr_file.close()


@pytest.fixture
def watch_sync(live_server, tmp_path):
    """Return a callable that starts the client in continuous mode as a subprocess.

    The caller receives the :class:`subprocess.Popen` handle and must terminate
    it. Continuous mode blocks forever, so it cannot be driven through CliRunner.
    """
    base_url, api_key = live_server

    def _start(bucket: str, local_path: str, *extra: str) -> subprocess.Popen:
        # Ready file goes into tmp_path (NOT local_path) so it never
        # contaminates the synced directory. pytest destroys tmp_path
        # after the test, and atexit cleans it up on normal shutdown.
        return _start_watch_client(
            base_url,
            api_key,
            bucket,
            local_path,
            str(tmp_path / "client.ready"),
            str(tmp_path / "locks"),
            str(tmp_path / "client.log"),
            *extra,
        )

    return _start


@pytest.fixture
def watch_sync_restartable(restartable_live_server, tmp_path):
    """Like [watch_sync] but drives the client against the restartable server."""

    def _start(bucket: str, local_path: str, *extra: str) -> subprocess.Popen:
        return _start_watch_client(
            restartable_live_server.base_url,
            restartable_live_server.api_key,
            bucket,
            local_path,
            str(tmp_path / "client.ready"),
            str(tmp_path / "locks"),
            str(tmp_path / "client.log"),
            *extra,
        )

    return _start


@pytest.fixture
def bb_restartable_bucket(restartable_live_server) -> Generator[str]:
    """Create a uniquely-named bucket on the restartable server; delete it after."""
    import uuid

    import requests

    base = restartable_live_server.base_url
    headers = {"Authorization": f"Bearer {restartable_live_server.api_key}"}
    name = f"bb-rs-{uuid.uuid4().hex[:8]}"
    r = requests.post(
        f"{base}/api/v1/buckets",
        params={"bucket": name},
        headers=headers,
        timeout=15.0,
        verify=False,
    )
    assert r.status_code == 201, r.text
    yield name
    requests.delete(
        f"{base}/api/v1/buckets/{name}",
        headers=headers,
        timeout=15.0,
        verify=False,
    )


@pytest.fixture
def bb_restartable_server_files(restartable_live_server):
    """List files on the restartable server for a bucket, returning a set of names."""
    import requests

    base = restartable_live_server.base_url
    headers = {"Authorization": f"Bearer {restartable_live_server.api_key}"}

    def _list(bucket: str) -> set[str]:
        r = requests.get(
            f"{base}/api/v1/buckets/{bucket}/files",
            headers=headers,
            timeout=15.0,
            verify=False,
        )
        assert r.status_code == 200, r.text
        return {f["filepath"] for f in r.json()}

    return _list


@pytest.fixture
def run_sync_once(live_server):
    """Return a callable that runs a one-shot client sync via the public CLI.

    The client's API key is injected through ``HSYNC_API_KEY`` (with its env
    cache cleared around the run) so no key ever appears on the command line.
    ``--insecure`` skips the self-signed cert verification over loopback HTTPS.
    """
    import os

    from typer.testing import CliRunner

    from hsync_client.config import get_env_settings

    runner = CliRunner()
    base_url, api_key = live_server

    def _run(bucket: str, local_path: str, *extra: str):
        old = os.environ.get("HSYNC_API_KEY")
        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()
        try:
            args = [
                "-b",
                bucket,
                "-s",
                base_url,
                "-p",
                local_path,
                "--once",
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
                *extra,
            ]
            with _ClientGlobalsGuard():
                result = runner.invoke(__import__("hsync_client.cli", fromlist=["app"]).app, args)
            # CliRunner captures all client output; echo it so pytest includes
            # the client log in the failure report instead of hiding it.
            if result.output:
                print(f"--- hsync-client output ---\n{result.output}")
            return result
        finally:
            if old is None:
                os.environ.pop("HSYNC_API_KEY", None)
            else:
                os.environ["HSYNC_API_KEY"] = old
            get_env_settings.cache_clear()

    return _run


@pytest.fixture
def run_cli(live_server):
    """Like [run_sync_once] but adds no safety/cleanup flags of its own.

    Used by safety-guard tests that need to control ``--yes`` /
    ``--min-free-disk-space`` / ``--sync-direction`` precisely.
    """
    import os

    from typer.testing import CliRunner

    from hsync_client.config import get_env_settings

    runner = CliRunner()
    base_url, api_key = live_server

    def _run(bucket: str, local_path: str, *extra: str):
        old = os.environ.get("HSYNC_API_KEY")
        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()
        try:
            args = ["-b", bucket, "-s", base_url, "-p", local_path, "--once", "--insecure", *extra]
            with _ClientGlobalsGuard():
                result = runner.invoke(__import__("hsync_client.cli", fromlist=["app"]).app, args)
            # CliRunner captures all client output; echo it so pytest includes
            # the client log in the failure report instead of hiding it.
            if result.output:
                print(f"--- hsync-client output ---\n{result.output}")
            return result
        finally:
            if old is None:
                os.environ.pop("HSYNC_API_KEY", None)
            else:
                os.environ["HSYNC_API_KEY"] = old
            get_env_settings.cache_clear()

    return _run
