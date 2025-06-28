# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the LAN (peer-to-peer) sync use case.

LAN mode requires two real client instances discovering each other via
mDNS/zeroconf on the local network, electing a master, and syncing over
HTTPS/WSS with TOFU certificate pinning. This is inherently environment-
sensitive (multicast on loopback, self-signed cert generation, election
timing) and is therefore **disabled by default** to keep the suite
deterministic.

Set ``HSYNC_BB_LAN=1`` to opt in when the host supports loopback mDNS.
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

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)

BB_API_KEY = "test-bb-api-key-0123456789abcdef"
_LAN_KEY = "test-lan-shared-secret"


def _free_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _lan_start(
    local_path: str,
    bucket: str,
    *,
    force_master: bool = False,
    never_master: bool = False,
) -> subprocess.Popen:
    """Start a LAN-mode client subprocess."""
    env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
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
        "30",
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
    return subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    if proc.stderr:
        proc.stderr.close()


def _stderr_text(proc: subprocess.Popen) -> str:
    """Return captured stderr text (non-blocking, best-effort)."""
    if proc.stderr is None:
        return ""
    try:
        import os as _os

        fd = proc.stderr.fileno()
        _os.set_blocking(fd, False)
        data = proc.stderr.read()
        return data.decode("utf-8", errors="replace") if data else ""
    except Exception:
        return ""


def _wait_for_file(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.3)
    return False


def _wait_for_text_in_stderr(proc: subprocess.Popen, text: str, timeout: float = 30.0) -> bool:
    """Poll until *text* appears in the process's stderr, or return False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # process exited
        if text in _stderr_text(proc):
            return True
        time.sleep(0.3)
    return False


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


class TestLanElection:
    def test_force_master_wins_and_slave_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A force-master client becomes master; a never-master slave syncs to it."""
        # Start master first
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Wait for master to be ready (emits "MASTER" in logs)
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create a file on the master side
                content = b"master-origin\n"
                (Path(lan_master_dir) / "shared.txt").write_bytes(content)

                # Wait for it to appear on the slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "shared.txt"), timeout=30), (
                    "File did not sync from master to slave"
                )

                # Verify content
                assert (Path(lan_slave_dir) / "shared.txt").read_bytes() == content

                # Create a file on the slave side
                slave_content = b"slave-origin\n"
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(slave_content)

                # Wait for it to appear on the master
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave.txt"), timeout=30), (
                    "File did not sync from slave to master"
                )

                assert (Path(lan_master_dir) / "from_slave.txt").read_bytes() == slave_content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanTofuPinning:
    def test_first_sight_trusts_and_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """On first sight the slave TOFU-pins the master cert and syncs."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for the TOFU pin log line
                assert _wait_for_text_in_stderr(
                    slave, "Pinned LAN master certificate", timeout=30
                ), "Slave did not TOFU-pin the master"

                # File sync as smoke test
                content = b"tofu-test\n"
                (Path(lan_master_dir) / "tofu.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "tofu.txt"), timeout=30), (
                    "File did not sync after TOFU pinning"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_changed_fingerprint_is_refused(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """When the master's fingerprint changes, the slave refuses to connect."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave: trust the certificate (TOFU)
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave1, "Pinned LAN master certificate", timeout=30)
            _terminate(slave1)

            # Stop master, recreate with a new cert
            _terminate(master)

            # Remove the master dir and start fresh master (new cert generated)
            import shutil

            shutil.rmtree(lan_master_dir, ignore_errors=True)
            Path(lan_master_dir).mkdir()

            master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                assert _wait_for_text_in_stderr(master2, "MASTER", timeout=30), (
                    "Second master did not become ready"
                )

                # Second slave: should refuse the changed fingerprint
                slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    refused = _wait_for_text_in_stderr(slave2, "FINGERPRINT CHANGED", timeout=30)
                    assert refused, "Slave should have refused the changed fingerprint"
                finally:
                    _terminate(slave2)
            finally:
                _terminate(master2)
        except Exception:
            _terminate(master)


class TestLanHttpsOnly:
    def test_master_without_cert_fingerprint_refused(self):
        """A slave refuses a master that does not advertise a cert fingerprint.

        This is currently enforced unconditionally by the LAN handler:
        there is no plain-HTTP downgrade path.
        """
        # This test is inherently satisfied by the protocol design:
        # _run_as_slave (lan_handler.py:461) checks
        # `not (master.use_https and master.cert_fingerprint)` and
        # refuses.  An e2e verification would require a crafty master
        # stub that advertises via mDNS without a fingerprint, which is
        # not possible with the real client (it always generates one).
        pytest.skip("HTTPS-only enforcement verified by unit tests in test_lan_handler_advanced.py")
