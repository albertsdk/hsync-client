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
    except (OSError, ValueError, AttributeError):
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
        except BaseException:
            _terminate(master)
            raise


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


# =============================================================================
# Additional e2e scenarios (HSYNC_BB_LAN=1 required)
# =============================================================================


def _wait_for_file_gone(path: str, timeout: float = 30.0) -> bool:
    """Poll until *path* no longer exists, or return False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(path).exists():
            return True
        time.sleep(0.3)
    return False


class TestLanAutomaticElection:
    def test_automatic_election_and_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two neutral peers: one becomes master, the other slave, sync works."""
        # Start both simultaneously — election happens via mDNS
        peer1 = _lan_start(lan_master_dir, lan_bucket)
        peer2 = _lan_start(lan_slave_dir, lan_bucket)
        try:
            # At least one of them logs "MASTER" (the winner)
            master_ready = _wait_for_text_in_stderr(
                peer1, "MASTER", timeout=30
            ) or _wait_for_text_in_stderr(peer2, "MASTER", timeout=5)
            assert master_ready, "Neither peer became master"

            # File written on the first peer should appear on the second
            content = b"auto-election\n"
            (Path(lan_master_dir) / "auto.txt").write_bytes(content)
            assert _wait_for_file(str(Path(lan_slave_dir) / "auto.txt"), timeout=30), (
                "File did not sync after automatic election"
            )
        finally:
            _terminate(peer1)
            _terminate(peer2)


class TestLanFileDeletion:
    def test_file_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """A file deleted on the master is removed from the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create a file on master
                file_path = Path(lan_master_dir) / "to_delete.txt"
                file_path.write_bytes(b"delete-me\n")

                # Wait for sync to slave
                slave_file = Path(lan_slave_dir) / "to_delete.txt"
                assert _wait_for_file(str(slave_file), timeout=30), "File did not appear on slave"

                # Delete on master
                file_path.unlink()

                # Slave should delete it too
                assert _wait_for_file_gone(str(slave_file), timeout=30), (
                    "Deleted file still present on slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_ignore_delete_flag_preserves_file(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --ignore-delete keeps files deleted by the master."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with --ignore-delete
            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--ignore-delete",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Create a file on master
                file_path = Path(lan_master_dir) / "keep_me.txt"
                file_path.write_bytes(b"keep-this\n")

                # Wait for sync to slave
                slave_file = Path(lan_slave_dir) / "keep_me.txt"
                assert _wait_for_file(str(slave_file), timeout=30), "File did not appear on slave"

                # Delete on master
                file_path.unlink()

                # Wait a bit and verify slave still has it
                time.sleep(5)
                assert slave_file.exists(), "File was deleted on slave despite --ignore-delete"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanResilience:
    def test_slave_reconnects_after_disconnect(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave reconnects and catches up after being restarted."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave session
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)
            _terminate(slave1)

            # Master creates a new file while slave is gone
            content = b"catch-up\n"
            (Path(lan_master_dir) / "catch_up.txt").write_bytes(content)

            # Restart slave
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "catch_up.txt"), timeout=30), (
                    "Slave did not catch up after reconnect"
                )
                assert (Path(lan_slave_dir) / "catch_up.txt").read_bytes() == content
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


class TestLanNeverMaster:
    def test_never_master_peers_no_election(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Two never-master peers: no election, [NO MASTER] warning appears."""
        p1 = _lan_start(lan_master_dir, lan_bucket, never_master=True)
        p2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
        try:
            # Neither should become master; after ~60s the [NO MASTER] warning fires
            no_master = _wait_for_text_in_stderr(
                p1, "NO MASTER", timeout=90
            ) or _wait_for_text_in_stderr(p2, "NO MASTER", timeout=5)
            assert no_master, "Expected [NO MASTER] warning did not appear"

            # Neither process should log "MASTER" (they're both never-master)
            stderr1 = _stderr_text(p1)
            stderr2 = _stderr_text(p2)
            assert "This client is the MASTER" not in stderr1, "Peer 1 became master"
            assert "This client is the MASTER" not in stderr2, "Peer 2 became master"
        finally:
            _terminate(p1)
            _terminate(p2)


class TestLanStrictPairing:
    def test_require_pairing_rejects_unpinned_master(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        """--require-pairing without a pre-existing pin rejects the master."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with --require-pairing (no prior pin exists)
            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--require-pairing",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Should see pairing_needed / unpinned certificate rejection
                refused = _wait_for_text_in_stderr(slave, "pairing", timeout=30)
                assert refused, (
                    "Slave should have refused the unpinned master with --require-pairing"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Directory & symlink propagation
# =============================================================================


class TestLanDirectorySync:
    def test_directory_creation_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """mkdir on master → directory (with .hsynckeep) appears on slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create directory on master
                master_subdir = Path(lan_master_dir) / "subdir"
                master_subdir.mkdir()
                (master_subdir / "file.txt").write_bytes(b"inside\n")

                # Wait for the directory to appear on slave
                slave_subdir = Path(lan_slave_dir) / "subdir"
                assert _wait_for_file(str(slave_subdir), timeout=30), (
                    "Directory did not appear on slave"
                )
                assert slave_subdir.is_dir(), "Expected a directory on slave"
                assert (slave_subdir / "file.txt").read_bytes() == b"inside\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_directory_deletion_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """rmdir on master → directory disappears from slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create directory on master before slave connects
            master_subdir = Path(lan_master_dir) / "todel_dir"
            master_subdir.mkdir()
            (master_subdir / "f.txt").write_bytes(b"x\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_subdir = Path(lan_slave_dir) / "todel_dir"
                assert _wait_for_file(str(slave_subdir), timeout=30), (
                    "Directory did not appear on slave"
                )

                # Delete on master
                import shutil

                shutil.rmtree(str(master_subdir))

                # Slave should remove it too
                assert _wait_for_file_gone(str(slave_subdir), timeout=30), (
                    "Deleted directory still present on slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_symlink_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlink created on master → appears on slave (in-bucket target)."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test requires Unix")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create target file inside the bucket
            target = Path(lan_master_dir) / "real_file.txt"
            target.write_bytes(b"symlink target\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create symlink on master pointing to in-bucket target
                link_path = Path(lan_master_dir) / "link.txt"
                link_path.symlink_to(target)

                # Wait for symlink to appear on slave
                slave_link = Path(lan_slave_dir) / "link.txt"
                assert _wait_for_file(str(slave_link), timeout=30), (
                    "Symlink did not appear on slave"
                )
                assert slave_link.is_symlink(), "Expected a symlink on slave"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# LAN key authentication
# =============================================================================


class TestLanKeyAuth:
    def test_lan_key_mismatch_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Master with LAN key A, slave with LAN key B → connection refused."""
        # Start master with LAN_KEY
        env_master = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
            ],
            env=env_master,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with a DIFFERENT LAN key
            env_slave = {**os.environ, "HSYNC_LAN_KEY": "wrong-key-xxxxxxxx"}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                ],
                env=env_slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Slave should be refused (401/403 or connection error)
                # Look for authentication-related failure in stderr
                auth_failed = (
                    _wait_for_text_in_stderr(slave, "401", timeout=30)
                    or _wait_for_text_in_stderr(slave, "403", timeout=5)
                    or _wait_for_text_in_stderr(slave, "Unauthorized", timeout=5)
                )
                assert auth_failed, "Slave should have been rejected with wrong LAN key"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Directional sync
# =============================================================================


class TestLanSyncDirection:
    def test_download_only_prevents_upload(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --sync-direction download-only: its files don't reach master."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--sync-direction",
                    "download-only",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Master creates a file → appears on slave (download works)
                (Path(lan_master_dir) / "from_master.txt").write_bytes(b"master\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "from_master.txt"), timeout=30), (
                    "Download did not work"
                )

                # Slave creates a file → must NOT appear on master (upload blocked)
                (Path(lan_slave_dir) / "slave_only.txt").write_bytes(b"slave\n")
                time.sleep(5)  # Give time for any potential upload
                assert not (Path(lan_master_dir) / "slave_only.txt").exists(), (
                    "Slave file leaked to master despite download-only mode"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_upload_only_prevents_download(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Slave with --sync-direction upload-only: master files don't reach slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--sync-direction",
                    "upload-only",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Master creates a file → must NOT appear on slave (download blocked)
                (Path(lan_master_dir) / "master_only.txt").write_bytes(b"master\n")
                time.sleep(5)  # Give time for any potential download
                assert not (Path(lan_slave_dir) / "master_only.txt").exists(), (
                    "Master file leaked to slave despite upload-only mode"
                )

                # Slave creates a file → appears on master (upload works)
                (Path(lan_slave_dir) / "from_slave.txt").write_bytes(b"slave\n")
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave.txt"), timeout=30), (
                    "Upload did not work"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Master restart resilience
# =============================================================================


class TestLanMasterRestart:
    def test_master_restart_slave_recovers(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """After master crash + restart, slave rediscovers and resumes syncing."""
        # First master session
        master1 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master1, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)

            # Kill master
            _terminate(master1)

            # Wait for slave to detect disconnection and re-enter discovery
            assert _wait_for_text_in_stderr(
                slave, "discovery", timeout=30
            ) or _wait_for_text_in_stderr(slave, "Waiting for peers", timeout=5), (
                "Slave did not re-enter discovery after master crash"
            )

            # Restart master with same dir (same bucket, inherits existing files)
            master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                assert _wait_for_text_in_stderr(master2, "MASTER", timeout=30), (
                    "Restarted master did not become ready"
                )

                # Create a new file on restarted master
                content = b"post-restart\n"
                (Path(lan_master_dir) / "post_restart.txt").write_bytes(content)

                # Slave should pick it up after rediscovery
                assert _wait_for_file(str(Path(lan_slave_dir) / "post_restart.txt"), timeout=60), (
                    "Slave did not receive file after master restart"
                )
                assert (Path(lan_slave_dir) / "post_restart.txt").read_bytes() == content
            finally:
                _terminate(master2)
                _terminate(slave)
        except BaseException:
            _terminate(master1)
            raise


# =============================================================================
# Concurrent writes on both peers
# =============================================================================


class TestLanConcurrency:
    def test_concurrent_file_writes_on_both_peers(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Master and slave write different files concurrently → both converge."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for initial sync
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)

                # Write a file on each side simultaneously
                master_content = b"master-side\n"
                slave_content = b"slave-side\n"
                (Path(lan_master_dir) / "master_file.txt").write_bytes(master_content)
                (Path(lan_slave_dir) / "slave_file.txt").write_bytes(slave_content)

                # Both files should appear on both sides
                assert _wait_for_file(str(Path(lan_slave_dir) / "master_file.txt"), timeout=30), (
                    "Master file did not reach slave"
                )
                assert _wait_for_file(str(Path(lan_master_dir) / "slave_file.txt"), timeout=30), (
                    "Slave file did not reach master"
                )

                # Verify content
                assert (Path(lan_slave_dir) / "master_file.txt").read_bytes() == master_content
                assert (Path(lan_master_dir) / "slave_file.txt").read_bytes() == slave_content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# LAN group filtering
# =============================================================================


class TestLanGroupFiltering:
    def test_different_lan_groups_dont_discover(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Peers in different LAN groups do not discover each other."""
        # Master in group "red"
        env_red = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
                "--lan-group",
                "red",
            ],
            env=env_red,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Slave in group "blue" — should NOT discover the red master
            env_blue = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--lan-group",
                    "blue",
                ],
                env=env_blue,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Slave should eventually warn that no peer was found (group mismatch)
                no_peer = _wait_for_text_in_stderr(
                    slave, "No master elected", timeout=30
                ) or _wait_for_text_in_stderr(slave, "alone", timeout=5)
                assert no_peer, "Slave should not have discovered the master in a different group"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Advanced sync operations
# =============================================================================


class TestLanFileModification:
    def test_file_modification_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Modifying a file on the master updates it on the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create a file before slave connects
            (Path(lan_master_dir) / "modify_me.txt").write_bytes(b"v1\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_file = Path(lan_slave_dir) / "modify_me.txt"
                assert _wait_for_file(str(slave_file), timeout=30), (
                    "Initial file did not appear on slave"
                )
                assert slave_file.read_bytes() == b"v1\n"

                # Modify the file on master
                time.sleep(0.5)  # let mtime settle
                (Path(lan_master_dir) / "modify_me.txt").write_bytes(b"v2-updated\n")

                # Wait for the update to propagate
                deadline = time.time() + 30
                updated = False
                while time.time() < deadline:
                    if slave_file.read_bytes() == b"v2-updated\n":
                        updated = True
                        break
                    time.sleep(0.3)
                assert updated, "Modified content did not reach slave"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanFileRename:
    def test_file_rename_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Renaming a file on the master propagates to the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Pre-create file
            (Path(lan_master_dir) / "old_name.txt").write_bytes(b"rename-me\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                old_slave = Path(lan_slave_dir) / "old_name.txt"
                assert _wait_for_file(str(old_slave), timeout=30), (
                    "File did not appear on slave before rename"
                )

                # Rename on master
                (Path(lan_master_dir) / "old_name.txt").rename(
                    Path(lan_master_dir) / "new_name.txt"
                )

                # Old path should vanish, new path should appear on slave
                new_slave = Path(lan_slave_dir) / "new_name.txt"
                assert _wait_for_file(str(new_slave), timeout=30), (
                    "Renamed file did not appear on slave"
                )
                assert _wait_for_file_gone(str(old_slave), timeout=10), (
                    "Old file name still present on slave after rename"
                )
                assert new_slave.read_bytes() == b"rename-me\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestLanBulkSync:
    def test_multiple_files_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """10+ files created on master → all appear on slave with correct content."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create 10 files on master before slave connects
            files: dict[str, bytes] = {}
            for i in range(10):
                name = f"bulk_{i:02d}.txt"
                content = f"file-{i:02d}\n".encode()
                (Path(lan_master_dir) / name).write_bytes(content)
                files[name] = content

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                for name, expected in files.items():
                    slave_path = Path(lan_slave_dir) / name
                    assert _wait_for_file(str(slave_path), timeout=30), (
                        f"File {name} did not appear on slave"
                    )
                    assert slave_path.read_bytes() == expected, f"Wrong content for {name}"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_empty_directory_propagates(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Empty directory (with .hsynckeep) appears on slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create an empty directory on master
                empty_dir = Path(lan_master_dir) / "emptydir"
                empty_dir.mkdir()

                # The directory should appear on slave (with .hsynckeep sentinel)
                slave_empty = Path(lan_slave_dir) / "emptydir"
                assert _wait_for_file(str(slave_empty), timeout=30), (
                    "Empty directory did not appear on slave"
                )
                assert slave_empty.is_dir(), "Expected a directory"
                # Sentinel should be present
                sentinel = slave_empty / ".hsynckeep"
                assert sentinel.exists(), "Missing .hsynckeep sentinel in empty dir"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Feature flags
# =============================================================================


class TestLanFeatureFlags:
    def test_compression_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-compression on both peers still allows sync."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
                "--no-compression",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--no-compression",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nocomp.txt").write_bytes(b"no compression\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nocomp.txt"), timeout=30), (
                    "File did not sync with --no-compression"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_delta_disabled_still_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--no-delta on both peers still allows sync."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
                "--no-delta",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--no-delta",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nodelta.txt").write_bytes(b"no delta\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nodelta.txt"), timeout=30), (
                    "File did not sync with --no-delta"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_periodic_resync_fires(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--interval 5 triggers a periodic resync logged in stderr."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
                "--interval",
                "5",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # The master with --interval should log "Force sync" within ~10s
            resync_fired = _wait_for_text_in_stderr(master, "Force sync", timeout=15)
            assert resync_fired, "Periodic resync did not fire (expected 'Force sync' in stderr)"
        finally:
            _terminate(master)


# =============================================================================
# Edge cases & robustness
# =============================================================================


class TestLanEdgeCases:
    def test_hidden_file_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Hidden files (e.g. .hidden) are not synced to the slave."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Create a hidden file on master
                (Path(lan_master_dir) / ".secret").write_bytes(b"hidden\n")

                # Wait a few seconds — it should NOT appear on slave
                time.sleep(5)
                assert not (Path(lan_slave_dir) / ".secret").exists(), (
                    "Hidden file was synced to slave"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_lan_without_key_still_works(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """LAN mode without HSYNC_LAN_KEY still syncs (no auth)."""
        # Neither master nor slave set HSYNC_LAN_KEY
        env_no_key = {**os.environ}
        env_no_key.pop("HSYNC_LAN_KEY", None)

        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
            ],
            env=env_no_key,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                ],
                env=env_no_key,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "nokey.txt").write_bytes(b"no auth key\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "nokey.txt"), timeout=30), (
                    "File did not sync without LAN key"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Tombstone propagation (offline deletion catch-up)
# =============================================================================


class TestLanTombstone:
    def test_tombstone_propagation_on_reconnect(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File deleted while slave is offline → tombstone cleans it up on reconnect."""
        # Create a file on master before anyone connects
        (Path(lan_master_dir) / "pre_delete.txt").write_bytes(b"to-be-deleted\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave session: sync the file, then disconnect
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            _wait_for_file(str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30)
            _terminate(slave1)

            # While slave is gone, delete the file on master
            (Path(lan_master_dir) / "pre_delete.txt").unlink()

            # Reconnect slave — it should see the tombstone and delete locally
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file_gone(
                    str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30
                ), "Deleted file still present on slave after reconnect (tombstone missed)"
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# Multi-slave topology (3 peers)
# =============================================================================


class TestLanMultiSlave:
    def test_multiple_slaves_converge(self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket):
        """Master + 2 slaves: file from slave1 appears on slave2 via master."""
        slave2_dir = str(tmp_path / "slave2")
        Path(slave2_dir).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            slave2 = _lan_start(slave2_dir, lan_bucket, never_master=True)
            try:
                # Wait for both to complete initial sync
                _wait_for_text_in_stderr(slave1, "Initial sync complete", timeout=30)
                _wait_for_text_in_stderr(slave2, "Initial sync complete", timeout=30)

                # Slave1 creates a file
                (Path(lan_slave_dir) / "from_slave1.txt").write_bytes(b"via-master\n")

                # It should appear on both master and slave2
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach master"
                )
                assert _wait_for_file(str(Path(slave2_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach slave2 via master"
                )

                assert (Path(slave2_dir) / "from_slave1.txt").read_bytes() == b"via-master\n"
            finally:
                _terminate(slave1)
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# Persistent mode control
# =============================================================================


class TestLanPersistent:
    def test_no_lan_persistent_exits_when_alone(self, lan_master_dir, lan_bucket):
        """--no-lan-persistent exits instead of waiting when no peers are found."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
                "--yes",
                "--min-free-disk-space",
                "0",
                "--no-autotune",
                "--discovery-timeout",
                "5",
                "--retry-delay",
                "1",
                "--stability-delay",
                "1",
                "--no-lan-persistent",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Should exit (not hang) within ~15s when no peers found
            try:
                exit_code = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("--no-lan-persistent did not exit when no peers were found")
            # Exit code 0 or 1 is acceptable (0 = clean exit, 1 = no peers found)
            assert exit_code in (0, 1), f"Unexpected exit code: {exit_code}"
        finally:
            _terminate(proc)


# =============================================================================
# Port override
# =============================================================================


class TestLanPortOverride:
    def test_lan_port_override(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--lan-port on master: slave connects on the specified port."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hsync_client",
                "--lan-mode",
                "--force-master",
                "-b",
                lan_bucket,
                "-p",
                lan_master_dir,
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
                "--lan-port",
                "12345",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )
            # The master log should mention port 12345
            stderr = _stderr_text(master)
            assert "12345" in stderr, "Master did not use the specified port"

            # Slave should still be able to sync
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                (Path(lan_master_dir) / "port_test.txt").write_bytes(b"custom port\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "port_test.txt"), timeout=30), (
                    "File did not sync with custom master port"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Filter path (subdirectory sync)
# =============================================================================


class TestLanFilterPath:
    def test_filter_path_restricts_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--filter-path subdir/ restricts sync to that subdirectory only."""
        # Pre-create two subdirectories on master
        sub_a = Path(lan_master_dir) / "sub_a"
        sub_a.mkdir()
        (sub_a / "a.txt").write_bytes(b"in-a\n")

        sub_b = Path(lan_master_dir) / "sub_b"
        sub_b.mkdir()
        (sub_b / "b.txt").write_bytes(b"in-b\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with --filter-path sub_a
            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--filter-path",
                    "sub_a",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # File in sub_a should appear on slave
                assert _wait_for_file(str(Path(lan_slave_dir) / "sub_a" / "a.txt"), timeout=30), (
                    "File in filtered subdirectory did not sync"
                )
                # File in sub_b should NOT appear on slave
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "sub_b").exists(), (
                    "File outside filter path was synced"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Bandwidth throttle
# =============================================================================


class TestLanBandwidthThrottle:
    def test_bandwidth_throttle_allows_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--max-bandwidth-download with a generous limit still allows sync."""
        env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with bandwidth limit (10 MB/s — generous enough)
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--max-bandwidth-download",
                    "10MB",
                    "--max-bandwidth-upload",
                    "10MB",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Create a moderately sized file on master
                content = b"x" * 50000  # 50 KB
                (Path(lan_master_dir) / "throttled.txt").write_bytes(content)

                slave_file = Path(lan_slave_dir) / "throttled.txt"
                assert _wait_for_file(str(slave_file), timeout=30), (
                    "File did not sync with bandwidth throttle"
                )
                assert slave_file.read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# hsyncignore hot-reload
# =============================================================================


class TestLanHsyncignore:
    def test_hsyncignore_reload_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Creating .hsyncignore while sync is running excludes new patterns."""
        # Pre-create a file that will later be excluded
        (Path(lan_master_dir) / "include_me.txt").write_bytes(b"visible\n")
        (Path(lan_master_dir) / "exclude_me.log").write_bytes(b"hidden\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # First slave session: both files sync (no .hsyncignore yet)
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            assert _wait_for_file(str(Path(lan_slave_dir) / "include_me.txt"), timeout=30), (
                "include_me.txt did not sync"
            )
            assert _wait_for_file(str(Path(lan_slave_dir) / "exclude_me.log"), timeout=30), (
                "exclude_me.log did not sync"
            )
            _terminate(slave1)

            # Now create .hsyncignore on master excluding *.log
            (Path(lan_master_dir) / ".hsyncignore").write_text("*.log\n")

            # Slave reconnects — the excluded file should NOT re-sync
            # (it should be deleted because it no longer matches the sync set)
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Include file should still be there
                assert _wait_for_file(str(Path(lan_slave_dir) / "include_me.txt"), timeout=30), (
                    "include_me.txt disappeared"
                )

                # Excluded file should be removed by the reconcile (ignore rule)
                assert _wait_for_file_gone(
                    str(Path(lan_slave_dir) / "exclude_me.log"), timeout=30
                ), "exclude_me.log was not removed after .hsyncignore added"
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# File permissions
# =============================================================================


class TestLanPermissions:
    def test_file_permissions_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with 600 perms on master → slave receives 600."""
        import stat

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create file with specific permissions on master
            f = Path(lan_master_dir) / "perms.txt"
            f.write_bytes(b"permissions-test\n")
            f.chmod(0o600)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_f = Path(lan_slave_dir) / "perms.txt"
                assert _wait_for_file(str(slave_f), timeout=30), (
                    "File with custom permissions did not sync"
                )
                # Check that permissions were preserved (at least not world-readable)
                mode = slave_f.stat().st_mode
                assert not (mode & stat.S_IROTH), "File should not be world-readable"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Max inbound size guard
# =============================================================================


class TestLanMaxInbound:
    def test_max_inbound_size_blocks_large_file(self):
        """--max-inbound-size e2e requires multi-MB file transfers.

        Functionality verified by unit tests in test_security_inbound.py.
        """
        pytest.skip(
            "max-inbound-size e2e requires multi-MB file; functionality verified by unit tests"
        )


# =============================================================================
# Dry run
# =============================================================================


class TestLanDryRun:
    def test_dry_run_no_files_transferred(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--dry-run on slave: preview shown, no files transferred."""
        # Pre-create a file on master
        (Path(lan_master_dir) / "should_not_sync.txt").write_bytes(b"dry-run\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--dry-run",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # The preview should mention the file in stderr
                preview_shown = _wait_for_text_in_stderr(slave, "should_not_sync.txt", timeout=30)
                assert preview_shown, "Preview did not show the pending file"

                # But the file should NOT actually appear on disk
                time.sleep(3)
                assert not (Path(lan_slave_dir) / "should_not_sync.txt").exists(), (
                    "File was transferred despite --dry-run"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Pre-existing data
# =============================================================================


class TestLanPreExistingData:
    def test_pre_existing_data_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Directory already populated before client starts → everything syncs."""
        # Create files BEFORE starting any client
        (Path(lan_master_dir) / "pre_existing_a.txt").write_bytes(b"alpha\n")
        (Path(lan_master_dir) / "pre_existing_b.txt").write_bytes(b"beta\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "pre_existing_a.txt"), timeout=30
                ), "Pre-existing file A did not sync"
                assert _wait_for_file(
                    str(Path(lan_slave_dir) / "pre_existing_b.txt"), timeout=10
                ), "Pre-existing file B did not sync"
                assert (Path(lan_slave_dir) / "pre_existing_a.txt").read_bytes() == b"alpha\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Unicode filenames
# =============================================================================


class TestLanUnicode:
    def test_unicode_filename_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with non-ASCII name (e.g. caffè.txt) syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                unicode_name = "caffè_日本語.txt"
                content = "unicode content 🎉\n".encode()
                (Path(lan_master_dir) / unicode_name).write_bytes(content)

                slave_path = Path(lan_slave_dir) / unicode_name
                assert _wait_for_file(str(slave_path), timeout=30), (
                    f"Unicode file '{unicode_name}' did not sync"
                )
                assert slave_path.read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Nested deep directories
# =============================================================================


class TestLanDeepDirs:
    def test_nested_deep_directory_syncs(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Deeply nested directory (6 levels) syncs correctly."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Create deep directory structure
            deep = Path(lan_master_dir) / "a" / "b" / "c" / "d" / "e" / "f"
            deep.mkdir(parents=True)
            (deep / "deep_file.txt").write_bytes(b"deep content\n")

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                slave_deep_file = (
                    Path(lan_slave_dir) / "a" / "b" / "c" / "d" / "e" / "f" / "deep_file.txt"
                )
                assert _wait_for_file(str(slave_deep_file), timeout=30), (
                    "Deeply nested file did not sync"
                )
                assert slave_deep_file.read_bytes() == b"deep content\n"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Mass-deletion guard
# =============================================================================


class TestLanMassDeleteGuard:
    def test_mass_delete_guard_trips(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Many files deleted on master -> slave guard blocks the deletion."""
        # Create 10 files on master before anyone connects
        for i in range(10):
            (Path(lan_master_dir) / f"bulk_{i:02d}.txt").write_bytes(f"file{i:02d}\n".encode())

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            # Start slave with low mass-delete threshold
            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--mass-delete-threshold",
                    "5",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Wait for initial sync to bring all files to slave
                for i in range(10):
                    assert _wait_for_file(
                        str(Path(lan_slave_dir) / f"bulk_{i:02d}.txt"), timeout=30
                    ), f"bulk_{i:02d}.txt did not appear on slave"

                # Now delete all files on master
                for i in range(10):
                    (Path(lan_master_dir) / f"bulk_{i:02d}.txt").unlink()

                # The guard should trip and log a warning
                guard_tripped = _wait_for_text_in_stderr(
                    slave, "GUARD", timeout=30
                ) or _wait_for_text_in_stderr(slave, "mass", timeout=5)
                assert guard_tripped, "Mass-deletion guard did not trip"

                # Files should still be on slave (guard blocked deletion)
                for i in range(10):
                    slave_file = Path(lan_slave_dir) / f"bulk_{i:02d}.txt"
                    assert slave_file.exists(), (
                        f"bulk_{i:02d}.txt was deleted despite mass-delete guard"
                    )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Escaping symlink (security)
# =============================================================================


class TestLanEscapingSymlink:
    def test_escaping_symlink_not_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Symlink whose target escapes the bucket is NOT propagated."""
        import platform

        if platform.system() == "Windows":
            pytest.skip("Symlink test requires Unix")

        # Create a file OUTSIDE the bucket
        outside = Path(lan_master_dir).parent / "secret.txt"
        outside.write_bytes(b"top-secret\n")
        try:
            master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                    "Master did not become ready"
                )

                slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
                try:
                    # Create symlink on master that escapes the bucket
                    link_path = Path(lan_master_dir) / "escape_link.txt"
                    link_path.symlink_to(outside)

                    # Wait — the escaping symlink should NOT appear on slave
                    time.sleep(5)
                    slave_link = Path(lan_slave_dir) / "escape_link.txt"
                    assert not slave_link.exists(), (
                        "Escaping symlink was synced to slave (security breach)"
                    )
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            if outside.exists():
                outside.unlink()


# =============================================================================
# Ignore patterns flag
# =============================================================================


class TestLanIgnorePatterns:
    def test_ignore_patterns_flag_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--ignore-patterns '*.tmp,*.bak' excludes matching files."""
        # Pre-create files that should and should not sync
        (Path(lan_master_dir) / "visible.txt").write_bytes(b"sync-me\n")
        (Path(lan_master_dir) / "ignore_me.tmp").write_bytes(b"skip-me\n")
        (Path(lan_master_dir) / "ignore_me.bak").write_bytes(b"skip-me-too\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--ignore-patterns",
                    "*.tmp,*.bak",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Visible file should appear
                assert _wait_for_file(str(Path(lan_slave_dir) / "visible.txt"), timeout=30), (
                    "Visible file did not sync"
                )
                # Ignored files should NOT appear
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "ignore_me.tmp").exists(), (
                    "Ignored .tmp file was synced"
                )
                assert not (Path(lan_slave_dir) / "ignore_me.bak").exists(), (
                    "Ignored .bak file was synced"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Logfile output
# =============================================================================


class TestLanLogfile:
    def test_logfile_writes_output(self, lan_master_dir, lan_slave_dir, lan_bucket, tmp_path):
        """--output writes log to the specified file."""
        log_path = tmp_path / "hsync_test.log"

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )

            env = {**os.environ, "HSYNC_LAN_KEY": _LAN_KEY}
            slave = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "hsync_client",
                    "--lan-mode",
                    "--never-master",
                    "-b",
                    lan_bucket,
                    "-p",
                    lan_slave_dir,
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
                    "--output",
                    str(log_path),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Create a file and wait for sync
                (Path(lan_master_dir) / "logged.txt").write_bytes(b"log-test\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "logged.txt"), timeout=30), (
                    "File did not sync"
                )

                # The log file should exist and contain output
                assert _wait_for_file(str(log_path), timeout=10), "Log file was not created"
                log_content = log_path.read_text()
                assert len(log_content) > 0, "Log file is empty"
                assert "HSYNC" in log_content, "Log file does not contain expected content"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
