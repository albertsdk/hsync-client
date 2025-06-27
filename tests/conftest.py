# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Shared pytest fixtures for the hsync-client test suite.
"""

import pytest


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


# =============================================================================
# Black-box harness (test_bb_*.py)
#
# Behavioural, use-case-driven tests drive the *real* client through its public
# Typer CLI against a *real* in-process hsync-server (loopback uvicorn). No
# internal logic is mocked: only the filesystem (temp dirs) and the API key are
# test fixtures. The client venv has hsync-server as a dev dependency.
# =============================================================================

BB_API_KEY = "test-bb-api-key-0123456789abcdef"


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


def _self_signed_cert(cert_dir):
    """Generate a self-signed cert + unencrypted key for loopback HTTPS."""
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
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """Start the real hsync-server in-process over loopback HTTPS.

    The client hard-refuses to send the API key over plain HTTP, so the server
    runs with a self-signed cert and the client is driven with ``--insecure``.
    Server env is set only around the first import of ``hsync_server.main`` so
    the server's settings cache captures a temp buckets dir + known API key; the
    previous env is restored immediately afterwards (the server reads all config
    through its cached settings, and the client has its own separate settings
    cache, so client tests are unaffected). Yields (base_url, api_key).
    """
    import os
    import shutil
    import tempfile
    import threading
    import time
    import warnings

    import requests
    import uvicorn
    from urllib3.exceptions import InsecureRequestWarning

    warnings.simplefilter("ignore", InsecureRequestWarning)

    buckets_dir = tempfile.mkdtemp(prefix="hsync_bb_buckets_")
    cert_dir = tmp_path_factory.mktemp("bbssl")
    cert_file, key_file = _self_signed_cert(cert_dir)
    overrides = {
        "HSYNC_API_KEY": BB_API_KEY,
        "HSYNC_BUCKETS": buckets_dir,
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
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)

    from hsync_server.config import get_settings

    get_settings.cache_clear()
    from hsync_server.main import app  # populates the settings cache with the env above

    # Restore env: the server keeps its cached settings; the client is unaffected.
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        ssl_certfile=cert_file,
        ssl_keyfile=key_file,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"https://127.0.0.1:{port}"

    try:
        deadline = time.time() + 30
        last_exc = None
        while time.time() < deadline:
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
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            # Uvicorn didn't stop gracefully; the thread is a daemon so it
            # won't block process exit, but we log it for diagnostics.
            import warnings

            warnings.warn("hsync-server thread did not stop within 10s", stacklevel=2)
        shutil.rmtree(buckets_dir, ignore_errors=True)


@pytest.fixture
def bb_bucket(live_server) -> str:
    """Create a uniquely-named bucket on the live server for one test."""
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
    return name


@pytest.fixture
def bb_server_upload(live_server):
    """Upload a file to the live server over HTTPS (black-box setup helper)."""
    import hashlib

    import requests

    base, api_key = live_server

    def _upload(bucket: str, filename: str, content: bytes) -> None:
        r = requests.post(
            f"{base}/api/v1/buckets/{bucket}/files",
            data={
                "cid": "bbserver",
                "path": "",
                "sha256sum": hashlib.sha256(content).hexdigest(),
                "permissions": "644",
                "offset": "0",
                "size": "0",
                "compressed": "false",
            },
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
        return {f["name"] for f in r.json()}

    return _list


@pytest.fixture
def local_dir(tmp_path):
    """A temp directory used as the client's local sync path."""
    d = tmp_path / "local"
    d.mkdir()
    return str(d)


@pytest.fixture
def watch_sync(live_server):
    """Return a callable that starts the client in watch mode as a subprocess.

    The caller receives the :class:`subprocess.Popen` handle and must terminate
    it. Watch mode blocks forever, so it cannot be driven through CliRunner.
    """
    import os
    import subprocess
    import sys

    base_url, api_key = live_server

    def _start(bucket: str, local_path: str, *extra: str) -> subprocess.Popen:
        env = {**os.environ, "HSYNC_API_KEY": api_key}
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
            "-w",
            "--yes",
            "--insecure",
            "--min-free-disk-space",
            "0",
            *extra,
        ]
        return subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return _start


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
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
                *extra,
            ]
            with _ClientGlobalsGuard():
                return runner.invoke(__import__("hsync_client.cli", fromlist=["app"]).app, args)
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
            args = ["-b", bucket, "-s", base_url, "-p", local_path, "--insecure", *extra]
            with _ClientGlobalsGuard():
                return runner.invoke(__import__("hsync_client.cli", fromlist=["app"]).app, args)
        finally:
            if old is None:
                os.environ.pop("HSYNC_API_KEY", None)
            else:
                os.environ["HSYNC_API_KEY"] = old
            get_env_settings.cache_clear()

    return _run
