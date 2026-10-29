# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for P2P security: TOFU pinning, certificate validation, dry-run, and edge cases.

Requires HSYNC_BB_P2P=1 to run.
"""

import os
import time
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_ready_path,
    _p2p_start,
    _stderr_text,
    _terminate,
    _wait_for_client_ready,
    _wait_for_file,
    _wait_for_master,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


# =============================================================================
# Dry-run mode
# =============================================================================


class TestP2PDryRun:
    def test_dry_run_no_files_transferred(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """--dry-run prevents any file transfer."""
        # Given a master with a file and a dry-run slave
        (Path(p2p_master_dir) / "dry_test.txt").write_bytes(b"dry\n")

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--dry-run",),
            )
            try:
                # When the slave runs a dry-run sync
                time.sleep(5)

                # Then no file is transferred
                assert not (Path(p2p_slave_dir) / "dry_test.txt").exists(), (
                    "File was transferred despite --dry-run"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Pre-existing data
# =============================================================================


class TestP2PPreExistingData:
    def test_pre_existing_data_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Files already present on master before slave starts are synced."""
        # Given a master with pre-existing data
        (Path(p2p_master_dir) / "pre_existing.txt").write_bytes(b"pre-existing\n")

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # When the slave starts
                # Then the pre-existing file is synced
                assert _wait_for_file(str(Path(p2p_slave_dir) / "pre_existing.txt"), timeout=30), (
                    "Pre-existing file did not sync"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Unicode and deep paths
# =============================================================================


class TestP2PUnicodePath:
    def test_unicode_filename_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Unicode filename syncs correctly."""
        # Given a master with a Unicode-named file
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            (Path(p2p_master_dir) / "δοκιμή.txt").write_bytes(b"unicode\n")
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # When the slave starts
                # Then the Unicode-named file is synced
                assert _wait_for_file(str(Path(p2p_slave_dir) / "δοκιμή.txt"), timeout=30), (
                    "Unicode filename did not sync"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


class TestP2PDeepPath:
    def test_nested_deep_directory_syncs(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Deeply nested directory syncs correctly."""
        # Given a master with a deeply nested directory
        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            deep = Path(p2p_master_dir) / "level1" / "level2" / "level3" / "level4" / "level5"
            deep.mkdir(parents=True)
            (deep / "deep_file.txt").write_bytes(b"deep\n")
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                # When the slave starts
                # Then the deeply nested file is synced
                target = (
                    Path(p2p_slave_dir)
                    / "level1"
                    / "level2"
                    / "level3"
                    / "level4"
                    / "level5"
                    / "deep_file.txt"
                )
                assert _wait_for_file(str(target), timeout=60), "Deeply nested file did not sync"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Mass-delete guard
# =============================================================================


class TestP2PMassDelete:
    def test_mass_delete_guard_trips(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Mass deletion reconciled by a full sync trips the slave's guard.

        Real-time WS ``deleted`` events are applied one by one by design; the
        guard lives in the full-sync reconciliation. The scenario therefore
        takes the slave offline (master stop), deletes the files, and restarts
        the master: the slave rediscovers it and reconciles — 20 deletions
        reach the threshold of 2 and the guard trips.

        The slave runs ``--sync-direction download-only`` so it *mirrors* the
        master: a bidirectional slave would instead re-upload local-only files
        to an emptied master and no deletion would ever be computed.
        """
        # Given a master with 20 files and a download-only slave (threshold 2)
        for i in range(20):
            (Path(p2p_master_dir) / f"md{i:02d}.txt").write_bytes(b"mass-delete\n")

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--mass-delete-threshold", "2", "--sync-direction", "download-only"),
            )
            try:
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=60), (
                    "Slave did not complete the initial sync"
                )

                # When the master stops and the files are deleted locally
                _terminate(master)
                for i in range(20):
                    (Path(p2p_master_dir) / f"md{i:02d}.txt").unlink()

                # And the master restarts with the same identity, so the slave
                # reconnects and reconciles the 20 deletions
                master2 = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
                try:
                    # Then the mass-deletion guard trips
                    assert _wait_for_text_in_stderr(
                        slave, "Mass-deletion guard tripped", timeout=90
                    ), (
                        "Mass-delete guard did not trip on reconcile after reconnect"
                        f"\n--- slave log tail ---\n{_stderr_text(slave)[-3000:]}"
                    )
                finally:
                    _terminate(master2)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Symlink escape
# =============================================================================


class TestP2PSymlinkEscape:
    def test_escaping_symlink_not_synced(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """Symlink pointing outside the sync dir is not followed."""
        # Given a symlink on the master pointing outside the sync dir
        outside = Path("/tmp") / f"hsync-esc-{p2p_bucket}"
        outside.write_bytes(b"outside\n")

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            link = Path(p2p_master_dir) / "escape.txt"
            link.symlink_to(str(outside))
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30)
                time.sleep(3)
                # Then the escaping symlink is not followed into a regular file
                if (Path(p2p_slave_dir) / "escape.txt").exists():
                    content = (Path(p2p_slave_dir) / "escape.txt").read_bytes()
                    assert content != b"outside\n", "Escaping symlink content was followed"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
            outside.unlink(missing_ok=True)


# =============================================================================
# Ignore patterns
# =============================================================================


class TestP2PIgnorePatterns:
    def test_ignore_patterns_flag_respected(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """--ignore '*.tmp' prevents .tmp files from syncing."""
        # Given a master with one kept and one ignored file
        (Path(p2p_master_dir) / "keep.me").write_bytes(b"keep\n")
        (Path(p2p_master_dir) / "drop.tmp").write_bytes(b"drop\n")

        master = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)

            slave = _p2p_start(
                p2p_slave_dir,
                p2p_bucket,
                never_master=True,
                extra_args=("--ignore-pattern", "*.tmp"),
            )
            try:
                # Then the kept file syncs…
                assert _wait_for_file(str(Path(p2p_slave_dir) / "keep.me"), timeout=30), (
                    "keep.me should have synced"
                )
                time.sleep(5)
                # …and the ignored file does not
                assert not (Path(p2p_slave_dir) / "drop.tmp").exists(), (
                    "drop.tmp was synced despite --ignore *.tmp"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Logfile output
# =============================================================================


class TestP2PLogfile:
    def test_logfile_writes_output(self, p2p_master_dir, p2p_slave_dir, p2p_bucket):
        """--log-file writes the session log to a file.

        With an output file the log sink moves off stdout, so readiness cannot
        be parsed from the process output: wait on the master's ready file
        instead (a temporary slave still triggers the election).
        """
        # Given a master started with --log-file
        logfile = Path(p2p_master_dir) / "session.log"

        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            extra_args=("--log-file", str(logfile)),
        )
        try:
            temp_slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True)
            try:
                assert _wait_for_client_ready(
                    master, _p2p_ready_path(p2p_master_dir), timeout=60
                ), "Master did not become ready"
            finally:
                _terminate(temp_slave)

            # Then the log file exists and contains the session output
            assert logfile.exists(), "Logfile was not created"
            log_content = logfile.read_text()
            assert "HSYNC Client" in log_content
        finally:
            _terminate(master)


# =============================================================================
# Parity with the server-mode safety-guard suite (test_bb_server_safety_guards).
# The guards fire during run_sync startup, BEFORE P2P mDNS rendezvous, so the
# abort cases can be driven in-process without ever needing a second peer.
# =============================================================================


def _invoke_p2p_cli(monkeypatch, bucket: str, local: str, *extra: str, p2p_key: str | None = None):
    """Run the public CLI in-process with the lan subcommand and return the result.

    A *p2p_key* is set only when the target guard runs after the P2P-key write
    policy (e.g. the disk-space guard); the deletion-risk guard fires first and
    needs no key. Mirrors the server-mode ``run_cli`` harness: env cache
    cleared around the run and process-wide globals guarded.

    Ambient ``HSYNC_*`` variables are scrubbed (keeping only the autouse
    isolation dirs) so the developer's own configuration can never leak into the
    in-process run — the same guarantee the scrubbed ``_p2p_start`` subprocess
    harness provides.
    """
    from typer.testing import CliRunner

    from hsync_client.cli import app
    from hsync_client.config import get_env_settings

    from .conftest import _ClientGlobalsGuard

    _keep = {"HSYNC_DEVICE_DIR", "HSYNC_LOCKS_DIR", "HSYNC_STATE_DIR"}
    for key in [k for k in os.environ if k.startswith("HSYNC_") and k not in _keep]:
        monkeypatch.delenv(key, raising=False)
    if p2p_key is not None:
        monkeypatch.setenv("HSYNC_P2P_KEY", p2p_key)
    get_env_settings.cache_clear()
    runner = CliRunner()
    with _ClientGlobalsGuard():
        result = runner.invoke(app, ["p2p", "-b", bucket, "-p", local, *extra])
    get_env_settings.cache_clear()
    if result.output:
        print(f"--- hsync-client (lan) output ---\n{result.output}")
    return result


class TestP2PDiskSpaceGuard:
    def test_insufficient_disk_space_aborts(self, p2p_bucket, local_dir, monkeypatch):
        # Given a local dir with content and an impossibly high free-space requirement
        (Path(local_dir) / "a.txt").write_bytes(b"a")

        # When running lan with a free-space reserve outside the accepted range
        result = _invoke_p2p_cli(
            monkeypatch,
            p2p_bucket,
            local_dir,
            "--min-free-disk-space",
            "1TB",
            p2p_key="test-lan-key",
        )

        # Then the disk-space guard aborts (non-zero exit) before rendezvous, and
        # the guard-specific diagnostic is surfaced (logged or via the raised
        # exception), proving it was this guard — not an unrelated failure.
        assert result.exit_code != 0
        assert "min-free-disk-space" in (result.output + str(result.exception or "")).lower(), (
            f"abort was not the disk-space guard: {result.output!r} / {result.exception!r}"
        )


class TestP2PDeletionRiskGuard:
    def test_download_only_without_yes_aborts_in_noninteractive(
        self, p2p_bucket, local_dir, monkeypatch
    ):
        # Given a non-empty local dir and a download-only direction that could
        # delete local files, with no --allow-deletions confirmation
        (Path(local_dir) / "keep.txt").write_bytes(b"keep")

        # When running lan download-only without --allow-deletions
        result = _invoke_p2p_cli(
            monkeypatch, p2p_bucket, local_dir, "--sync-direction", "download-only"
        )

        # Then the deletion-risk guard aborts with its specific confirmation
        # diagnostic (proving it was this guard), and the local file is kept
        assert result.exit_code != 0
        combined = result.output.lower()
        assert "download-only mode" in combined or "deletion-risk" in combined, (
            f"abort was not the deletion-risk guard: {result.output!r}"
        )
        assert (Path(local_dir) / "keep.txt").exists()


class TestP2PInstanceLock:
    def test_second_process_on_same_directory_refused(self, p2p_master_dir, p2p_bucket):
        """Two live clients must never sync the same local directory.

        The second process on an already-locked directory refuses to start with
        a clear error (the per-directory flock is held for the whole run and
        released by the kernel even on SIGKILL)."""
        # Given a P2P client already running on a directory (waiting for peers)
        first = _p2p_start(p2p_master_dir, p2p_bucket, prefer_master=True)
        try:
            held = False
            deadline = time.time() + 5
            while time.time() < deadline:
                if first.poll() is not None:
                    break
                held = True
                time.sleep(0.3)
            assert held, "first client exited before acquiring the lock"

            # When a second client targets the same directory
            second = _p2p_start(p2p_master_dir, p2p_bucket, never_master=True)
            try:
                # Then it refuses to start: non-zero exit and a clear message
                refused = _wait_for_text_in_stderr(second, "already syncing", timeout=15)
                assert refused, "second client was not refused by the instance lock"
                second.wait(timeout=5)
                assert second.returncode != 0
            finally:
                _terminate(second)
        finally:
            _terminate(first)
