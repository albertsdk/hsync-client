# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the cross-platform advisory file locks (``_filelock``).

Windows ships no ``fcntl``, so the POSIX ``flock`` backend is replaced by a
one-byte ``msvcrt.locking`` range. The Windows backend is exercised here with a
fake ``msvcrt`` module: the real one cannot run on the CI hosts, but the calls
(and the NUL-byte priming for empty files) are pinned.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

from hsync_client import _filelock


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


class TestPosixBackend:
    def test_try_lock_contends_between_descriptors(self, tmp_path):
        path = tmp_path / "dir.lock"
        first = _open(path)
        second = _open(path)
        try:
            assert _filelock.try_lock(first) is True
            # A second open file description behaves like another process.
            assert _filelock.try_lock(second) is False
            _filelock.unlock(first)
            assert _filelock.try_lock(second) is True
        finally:
            _filelock.unlock(second)
            os.close(first)
            os.close(second)

    def test_blocking_lock_then_unlock(self, tmp_path):
        path = tmp_path / "dir.lock"
        fd = _open(path)
        try:
            _filelock.lock_blocking(fd)
            _filelock.unlock(fd)
        finally:
            os.close(fd)

    def test_unlock_is_best_effort(self):
        # A closed descriptor must never raise from the release path.
        fd = os.open(os.devnull, os.O_RDWR)
        os.close(fd)
        _filelock.unlock(fd)


class _FakeMsvcrt:
    """Stand-in for the Windows ``msvcrt`` module (attribute access via vars)."""

    def __init__(self) -> None:
        self.LK_LOCK = 1
        self.LK_NBLCK = 2
        self.LK_UNLCK = 0
        self.busy = False
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if self.busy and mode in (self.LK_LOCK, self.LK_NBLCK):
            raise OSError(errno.EACCES, "locked")


class TestWindowsBackend:
    def _fake(self, monkeypatch, tmp_path) -> tuple[_FakeMsvcrt, int]:
        fake = _FakeMsvcrt()
        module = type(sys)("msvcrt")
        # ``_filelock`` reads the namespace via ``vars(msvcrt)``, so the fake
        # module needs the constants/callable as module attributes.
        module.LK_LOCK = fake.LK_LOCK  # type: ignore[attr-defined]
        module.LK_NBLCK = fake.LK_NBLCK  # type: ignore[attr-defined]
        module.LK_UNLCK = fake.LK_UNLCK  # type: ignore[attr-defined]
        module.locking = fake.locking  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "msvcrt", module)
        monkeypatch.setattr(_filelock, "_windows", lambda: True)
        return fake, _open(tmp_path / "dir.lock")

    def test_try_lock_primes_the_locked_byte_and_seeks(self, tmp_path, monkeypatch):
        fake, fd = self._fake(monkeypatch, tmp_path)
        try:
            assert _filelock.try_lock(fd) is True
            # Empty file: a NUL is written so the locked byte range exists.
            assert os.fstat(fd).st_size == 1
            assert fake.calls == [(fake.LK_NBLCK, 1, 0)]
        finally:
            os.close(fd)

    def test_try_lock_reports_a_foreign_holder(self, tmp_path, monkeypatch):
        fake, fd = self._fake(monkeypatch, tmp_path)
        fake.busy = True
        try:
            assert _filelock.try_lock(fd) is False
        finally:
            os.close(fd)

    def test_blocking_lock_uses_lk_lock(self, tmp_path, monkeypatch):
        fake, fd = self._fake(monkeypatch, tmp_path)
        try:
            _filelock.lock_blocking(fd)
            assert fake.calls == [(fake.LK_LOCK, 1, 0)]
        finally:
            os.close(fd)

    def test_unlock_uses_lk_unlock(self, tmp_path, monkeypatch):
        fake, fd = self._fake(monkeypatch, tmp_path)
        try:
            _filelock.unlock(fd)
            assert fake.calls == [(fake.LK_UNLCK, 1, 0)]
        finally:
            os.close(fd)


def test_identity_imports_without_fcntl():
    """Windows has no ``fcntl``: importing the lock owner must not need it."""
    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        "import hsync_client.identity as identity\n"
        "print(identity.InstanceLock.__name__)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "InstanceLock"
