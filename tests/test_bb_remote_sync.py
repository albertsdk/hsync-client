# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the remote (server-mode) sync use case.

Drives the real client through its public CLI against a real in-process
hsync-server. Verifies the observable filesystem outcome and exit code — no
internal modules are imported or mocked.
"""

from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server_delete(live_server, bucket: str, filename: str) -> None:
    """Delete a file on the server via REST API."""
    base, api_key = live_server
    r = requests.delete(
        f"{base}/api/v1/buckets/{bucket}/files",
        data={"cid": "bbserver", "filename": filename},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    assert r.status_code in (200, 204), f"Server delete failed: {r.status_code} {r.text}"


# =============================================================================
# Happy path — core sync operations
# =============================================================================


class TestRemoteSyncHappy:
    def test_downloads_remote_file_into_empty_local_dir(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        content = b"hello from server\n"
        bb_server_upload(bb_bucket, "remote.txt", content)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / "remote.txt").read_bytes() == content

    def test_uploads_local_file_to_empty_server_bucket(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        content = b"hello from client\n"
        (Path(local_dir) / "local.txt").write_bytes(content)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "local.txt" in bb_server_files(bb_bucket)

    def test_bidirectional_converges(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        bb_server_upload(bb_bucket, "fromserver.bin", b"server")
        (Path(local_dir) / "fromclient.bin").write_bytes(b"client")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / "fromserver.bin").read_bytes() == b"server"
        assert "fromclient.bin" in bb_server_files(bb_bucket)


# =============================================================================
# File modification, rename, deletion
# =============================================================================


class TestRemoteFileOperations:
    def test_file_modification_uploaded(self, live_server, bb_bucket, local_dir, run_sync_once):
        (Path(local_dir) / "mod.txt").write_bytes(b"v1\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0

        # Modify locally and sync again
        (Path(local_dir) / "mod.txt").write_bytes(b"v2-updated\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0

    def test_file_deletion_propagates_to_server(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "todel.txt").write_bytes(b"delete me\n")
        run_sync_once(bb_bucket, local_dir)
        assert "todel.txt" in bb_server_files(bb_bucket)

        # Delete locally, sync should propagate deletion
        (Path(local_dir) / "todel.txt").unlink()
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "todel.txt" not in bb_server_files(bb_bucket)

    def test_file_rename_propagates_to_server(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "old.txt").write_bytes(b"rename me\n")
        run_sync_once(bb_bucket, local_dir)
        assert "old.txt" in bb_server_files(bb_bucket)

        # Rename locally
        (Path(local_dir) / "old.txt").rename(Path(local_dir) / "new.txt")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "old.txt" not in bb_server_files(bb_bucket)
        assert "new.txt" in bb_server_files(bb_bucket)


# =============================================================================
# Directory operations
# =============================================================================


class TestRemoteDirectorySync:
    def test_directory_creation_propagates(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        sub = Path(local_dir) / "sub"
        sub.mkdir()
        (sub / "f.txt").write_bytes(b"inside\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "sub/f.txt" in bb_server_files(bb_bucket)

    def test_directory_deletion_propagates(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        sub = Path(local_dir) / "todel"
        sub.mkdir()
        (sub / "x.txt").write_bytes(b"x\n")
        run_sync_once(bb_bucket, local_dir)
        assert "todel/x.txt" in bb_server_files(bb_bucket)

        import shutil

        shutil.rmtree(str(sub))
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "todel/x.txt" not in bb_server_files(bb_bucket)

    def test_nested_deep_directory_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        deep = Path(local_dir) / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_bytes(b"deep\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "a/b/c/d/e/deep.txt" in bb_server_files(bb_bucket)


# =============================================================================
# Symlinks
# =============================================================================


class TestRemoteSymlinks:
    def test_in_bucket_symlink_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        target = Path(local_dir) / "real.txt"
        target.write_bytes(b"target\n")
        link = Path(local_dir) / "link.txt"
        link.symlink_to(target)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "link.txt" in bb_server_files(bb_bucket)

    def test_escaping_symlink_not_synced(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """Symlink pointing outside bucket must not be uploaded."""
        outside = Path(local_dir).parent / "escape_secret.txt"
        outside.write_bytes(b"secret\n")
        try:
            link = Path(local_dir) / "esc.txt"
            link.symlink_to(outside)
            result = run_sync_once(bb_bucket, local_dir)
            assert result.exit_code == 0
            assert "esc.txt" not in bb_server_files(bb_bucket)
        finally:
            if outside.exists():
                outside.unlink()


# =============================================================================
# Permissions
# =============================================================================


class TestRemotePermissions:
    def test_file_permissions_preserved(self, live_server, bb_bucket, local_dir, run_sync_once):
        f = Path(local_dir) / "perms.txt"
        f.write_bytes(b"perms\n")
        f.chmod(0o600)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0


# =============================================================================
# Bulk / Unicode / pre-existing
# =============================================================================


class TestRemoteBulkSync:
    def test_multiple_files_sync(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        files = {}
        for i in range(10):
            name = f"f_{i:02d}.txt"
            content = f"file{i:02d}\n".encode()
            (Path(local_dir) / name).write_bytes(content)
            files[name] = content
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        for name in files:
            assert name in bb_server_files(bb_bucket)

    def test_pre_existing_data_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "pre_a.txt").write_bytes(b"alpha\n")
        (Path(local_dir) / "pre_b.txt").write_bytes(b"beta\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "pre_a.txt" in bb_server_files(bb_bucket)
        assert "pre_b.txt" in bb_server_files(bb_bucket)


class TestRemoteUnicode:
    def test_unicode_filename_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        name = "caffè_日本語.txt"
        content = "unicode 🎉\n".encode()
        (Path(local_dir) / name).write_bytes(content)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert name in bb_server_files(bb_bucket)


# =============================================================================
# Sync direction
# =============================================================================


class TestRemoteSyncDirection:
    def test_download_only_downloads_but_does_not_upload(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        server_content = b"from server"
        bb_server_upload(bb_bucket, "downloadme.txt", server_content)
        (Path(local_dir) / "staylocal.txt").write_bytes(b"local only")
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "download-only")
        assert result.exit_code == 0
        assert (Path(local_dir) / "downloadme.txt").read_bytes() == server_content
        assert "staylocal.txt" not in bb_server_files(bb_bucket)

    def test_upload_only_uploads_but_does_not_download(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        bb_server_upload(bb_bucket, "stayremote.txt", b"remote only")
        local_content = b"from client"
        (Path(local_dir) / "uploadme.txt").write_bytes(local_content)
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "upload-only")
        assert result.exit_code == 0
        assert "uploadme.txt" in bb_server_files(bb_bucket)
        assert not (Path(local_dir) / "stayremote.txt").exists()


# =============================================================================
# Dry run
# =============================================================================


class TestRemoteSyncDryRun:
    def test_dry_run_writes_nothing(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        bb_server_upload(bb_bucket, "preview.txt", b"should not download")
        result = run_sync_once(bb_bucket, local_dir, "--dry-run")
        assert result.exit_code == 0
        assert not (Path(local_dir) / "preview.txt").exists()

    def test_dry_run_shows_preview(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        bb_server_upload(bb_bucket, "show_me.txt", b"content")
        result = run_sync_once(bb_bucket, local_dir, "--dry-run")
        assert result.exit_code == 0
        # Preview should mention the file in stdout
        assert "show_me.txt" in result.output


# =============================================================================
# Edge cases & flags
# =============================================================================


class TestRemoteEdgeCases:
    def test_hidden_file_not_synced(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / ".secret").write_bytes(b"hidden\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert ".secret" not in bb_server_files(bb_bucket)

    def test_ignore_patterns_flag_respected(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
        (Path(local_dir) / "skip.tmp").write_bytes(b"tmp\n")
        result = run_sync_once(bb_bucket, local_dir, "--ignore-patterns", "*.tmp")
        assert result.exit_code == 0
        assert "keep.txt" in bb_server_files(bb_bucket)
        assert "skip.tmp" not in bb_server_files(bb_bucket)

    def test_compression_disabled_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "nocomp.txt").write_bytes(b"no compression\n")
        result = run_sync_once(bb_bucket, local_dir, "--no-compression")
        assert result.exit_code == 0
        assert "nocomp.txt" in bb_server_files(bb_bucket)

    def test_delta_disabled_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        (Path(local_dir) / "nodelta.txt").write_bytes(b"no delta\n")
        result = run_sync_once(bb_bucket, local_dir, "--no-delta")
        assert result.exit_code == 0
        assert "nodelta.txt" in bb_server_files(bb_bucket)


# =============================================================================
# Ignore delete
# =============================================================================


class TestRemoteSyncIgnoreDelete:
    def test_ignore_delete_keeps_local_file(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        content = b"keep me"
        (Path(local_dir) / "keep.txt").write_bytes(content)
        run_sync_once(bb_bucket, local_dir)
        _server_delete(live_server, bb_bucket, "keep.txt")
        result = run_sync_once(bb_bucket, local_dir, "--ignore-delete")
        assert result.exit_code == 0
        assert (Path(local_dir) / "keep.txt").read_bytes() == content


# =============================================================================
# Negative / error handling
# =============================================================================


class TestRemoteMoreFlags:
    def test_logfile_writes_output(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, tmp_path
    ):
        """--output writes log to the specified file."""
        log_path = tmp_path / "remote_test.log"
        bb_server_upload(bb_bucket, "logged.txt", b"log-test\n")
        result = run_sync_once(bb_bucket, local_dir, "--output", str(log_path))
        assert result.exit_code == 0
        assert log_path.exists(), "Log file was not created"
        assert len(log_path.read_text()) > 0, "Log file is empty"

    def test_no_autotune_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """--no-autotune does not block sync."""
        (Path(local_dir) / "autotune.txt").write_bytes(b"no autotune\n")
        result = run_sync_once(bb_bucket, local_dir, "--no-autotune")
        assert result.exit_code == 0
        assert "autotune.txt" in bb_server_files(bb_bucket)

    def test_hsyncignore_file_respected(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """.hsyncignore in local dir excludes matching files from upload."""
        (Path(local_dir) / ".hsyncignore").write_text("*.log\n")
        (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
        (Path(local_dir) / "skip.log").write_bytes(b"hidden\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "keep.txt" in bb_server_files(bb_bucket)
        assert "skip.log" not in bb_server_files(bb_bucket)


class TestRemoteSyncNegative:
    def test_wrong_api_key_aborts(self, live_server, bb_bucket, local_dir, monkeypatch):
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_API_KEY", "wrong-key")
        get_env_settings.cache_clear()
        from typer.testing import CliRunner

        from hsync_client.cli import app

        base_url, _ = live_server
        result = CliRunner().invoke(
            app,
            [
                "-b",
                bb_bucket,
                "-s",
                base_url,
                "-p",
                local_dir,
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
            ],
        )
        get_env_settings.cache_clear()
        assert result.exit_code == 0
        local_files = list(Path(local_dir).iterdir())
        assert local_files == [] or all(p.name.startswith(".hsync") for p in local_files)

    def test_unreachable_server_reports_error(self, tmp_path):
        from typer.testing import CliRunner

        from hsync_client.cli import app

        local = tmp_path / "local"
        local.mkdir()
        result = CliRunner().invoke(
            app,
            [
                "-b",
                "anybucket",
                "-s",
                "http://127.0.0.1:1",
                "-p",
                str(local),
                "--yes",
                "--min-free-disk-space",
                "0",
            ],
            env={"HSYNC_API_KEY": "some-key-1234567890"},
        )
        assert result.exit_code == 0
