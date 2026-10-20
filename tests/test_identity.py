# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the persistent device identity and the instance lock.

The autouse ``_isolate_device_state`` fixture in conftest.py points
``HSYNC_DEVICE_DIR``/``HSYNC_LOCKS_DIR`` at a per-test tmp dir and resets the
identity cache, so these tests never touch the developer's real state dir.
"""

import os
import stat
from pathlib import Path

import pytest
from hsync_common.models.event import is_valid_cid

from hsync_client.identity import (
    DEVICE_DIR_ENV,
    LOCKS_DIR_ENV,
    DeviceIdentity,
    InstanceLock,
    InstanceLockError,
    default_device_dir,
    default_locks_dir,
    load_or_create_device_identity,
    reset_device_identity_cache,
)


@pytest.fixture(autouse=True)
def _reset_identity_cache():
    reset_device_identity_cache()
    yield
    reset_device_identity_cache()


class TestDeviceIdentity:
    def test_generates_valid_persistent_identity(self, tmp_path):
        identity = load_or_create_device_identity()

        assert is_valid_cid(identity.device_id)
        device_dir = default_device_dir()
        assert device_dir == tmp_path.parent / f"{tmp_path.name}-isolation" / "hsync_device"

        id_file = device_dir / "device_id"
        assert id_file.read_text().strip() == identity.device_id
        assert stat.S_IMODE(id_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(device_dir.stat().st_mode) == 0o700

        # Keypair persisted alongside the device ID with owner-only perms.
        assert identity.cert_info.cert_file is not None
        assert Path(identity.cert_info.cert_file).exists()
        assert stat.S_IMODE(Path(identity.cert_info.key_file).stat().st_mode) == 0o600

    def test_identity_is_stable_across_loads(self):
        first = load_or_create_device_identity()
        reset_device_identity_cache()
        second = load_or_create_device_identity()

        assert second.device_id == first.device_id
        assert second.cert_info.fingerprint == first.cert_info.fingerprint

    def test_loader_is_cached_within_a_process(self):
        first = load_or_create_device_identity()
        second = load_or_create_device_identity()
        assert first is second

    def test_invalid_device_id_file_is_regenerated(self):
        device_dir = default_device_dir()
        device_dir.mkdir(parents=True, exist_ok=True)
        id_file = device_dir / "device_id"
        id_file.write_text("not-a-valid-cid!!")

        identity = load_or_create_device_identity()

        assert is_valid_cid(identity.device_id)
        assert id_file.read_text().strip() == identity.device_id

    def test_cert_rotation_keeps_device_id_changes_fingerprint(self):
        """Deleting the .pem files rotates the keypair; the device ID stays."""
        first = load_or_create_device_identity()

        os.unlink(first.cert_info.cert_file)
        os.unlink(first.cert_info.key_file)
        reset_device_identity_cache()

        second = load_or_create_device_identity()
        assert second.device_id == first.device_id
        assert second.cert_info.fingerprint != first.cert_info.fingerprint


class TestDeviceDirResolution:
    def test_device_dir_env_override(self, monkeypatch):
        monkeypatch.setenv(DEVICE_DIR_ENV, "/custom/device/dir")
        assert str(default_device_dir()) == "/custom/device/dir"

    def test_xdg_state_home_default(self, monkeypatch):
        monkeypatch.delenv(DEVICE_DIR_ENV, raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
        assert str(default_device_dir()) == os.path.join("/xdg/state", "hsync", "device")

    def test_master_cert_dir_delegates_to_device_dir(self, monkeypatch):
        from hsync_client.ssl_utils import default_master_cert_dir

        monkeypatch.setenv(DEVICE_DIR_ENV, "/custom/device/dir")
        assert default_master_cert_dir() == default_device_dir()


class TestLocksDirResolution:
    def test_locks_dir_env_override(self, monkeypatch):
        monkeypatch.setenv(LOCKS_DIR_ENV, "/custom/locks")
        assert str(default_locks_dir()) == "/custom/locks"

    def test_runtime_dir_default(self, monkeypatch):
        monkeypatch.delenv(LOCKS_DIR_ENV, raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert str(default_locks_dir()) == os.path.join("/run/user/1000", "hsync", "locks")


class TestInstanceLock:
    def test_acquire_and_release(self, tmp_path):
        target = tmp_path / "syncdir"
        target.mkdir()
        lock = InstanceLock.acquire(str(target))
        try:
            assert lock.lock_path.exists()
            assert lock.lock_path.read_text().strip() == str(os.getpid())
        finally:
            lock.release()
        # Re-acquisition after release must succeed.
        InstanceLock.acquire(str(target)).release()

    def test_same_process_reacquire_is_a_noop(self, tmp_path):
        target = tmp_path / "syncdir"
        target.mkdir()
        first = InstanceLock.acquire(str(target))
        try:
            assert InstanceLock.acquire(str(target)) is first
        finally:
            first.release()

    def test_foreign_holder_is_refused(self, tmp_path):
        """A live flock held by another open file description blocks acquisition."""
        import fcntl

        target = tmp_path / "syncdir"
        target.mkdir()
        probe = InstanceLock(str(target))
        probe.lock_path.parent.mkdir(parents=True, exist_ok=True)

        foreign_fd = os.open(probe.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.write(foreign_fd, b"99999\n")
        fcntl.flock(foreign_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(InstanceLockError) as exc_info:
                InstanceLock.acquire(str(target))
            message = str(exc_info.value)
            assert "already syncing" in message
            assert os.path.realpath(str(target)) in message
            assert "99999" in message  # diagnostic PID from the lock file
        finally:
            fcntl.flock(foreign_fd, fcntl.LOCK_UN)
            os.close(foreign_fd)

        # Once the foreign holder releases, acquisition succeeds.
        InstanceLock.acquire(str(target)).release()

    def test_stale_lock_file_content_does_not_block(self, tmp_path):
        """A leftover lock file (crashed holder) never blocks: liveness is the flock."""
        target = tmp_path / "syncdir"
        target.mkdir()
        probe = InstanceLock(str(target))
        probe.lock_path.parent.mkdir(parents=True, exist_ok=True)
        probe.lock_path.write_text("12345\n")  # dead predecessor's PID

        InstanceLock.acquire(str(target)).release()

    def test_locks_are_per_directory(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        lock_a = InstanceLock.acquire(str(dir_a))
        try:
            lock_b = InstanceLock.acquire(str(dir_b))
            lock_b.release()
            assert lock_a.lock_path != lock_b.lock_path
        finally:
            lock_a.release()

    def test_pid_write_failure_releases_the_lock(self, tmp_path, monkeypatch):
        """A failed diagnostic write must not leave the lock held and orphaned."""
        import hsync_client.identity as identity

        target = tmp_path / "syncdir"
        target.mkdir()

        def _boom(_fd: int, _length: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(identity.os, "ftruncate", _boom)
        with pytest.raises(OSError):
            InstanceLock.acquire(str(target))
        assert InstanceLock._held == {}

        # The OS lock really was released: restoring the write lets the same
        # directory be acquired again (a leaked fd would still hold it).
        monkeypatch.undo()
        InstanceLock.acquire(str(target)).release()

    def test_release_all(self, tmp_path):
        target = tmp_path / "syncdir"
        target.mkdir()
        InstanceLock.acquire(str(target))
        InstanceLock.release_all()
        # Everything released: a fresh acquire succeeds immediately.
        InstanceLock.acquire(str(target)).release()

    def test_device_identity_type(self):
        identity = load_or_create_device_identity()
        assert isinstance(identity, DeviceIdentity)
