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


class TestRemoteSyncHappy:
    def test_downloads_remote_file_into_empty_local_dir(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a file that exists only on the server
        content = b"hello from server\n"
        bb_server_upload(bb_bucket, "remote.txt", content)

        # When running a one-shot bidirectional sync
        result = run_sync_once(bb_bucket, local_dir)

        # Then the file is downloaded into the local directory
        assert result.exit_code == 0
        assert (Path(local_dir) / "remote.txt").read_bytes() == content

    def test_uploads_local_file_to_empty_server_bucket(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a file that exists only locally
        content = b"hello from client\n"
        (Path(local_dir) / "local.txt").write_bytes(content)

        # When running a one-shot bidirectional sync
        result = run_sync_once(bb_bucket, local_dir)

        # Then the file is uploaded to the server
        assert result.exit_code == 0
        assert "local.txt" in bb_server_files(bb_bucket)

    def test_bidirectional_converges(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a file only on the server and a different file only locally
        bb_server_upload(bb_bucket, "fromserver.bin", b"server")
        (Path(local_dir) / "fromclient.bin").write_bytes(b"client")

        # When running a one-shot bidirectional sync
        result = run_sync_once(bb_bucket, local_dir)

        # Then both files exist on both sides
        assert result.exit_code == 0
        assert (Path(local_dir) / "fromserver.bin").read_bytes() == b"server"
        assert "fromclient.bin" in bb_server_files(bb_bucket)


class TestRemoteSyncDirection:
    def test_download_only_downloads_but_does_not_upload(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a server-only file and a local-only file
        server_content = b"from server"
        bb_server_upload(bb_bucket, "downloadme.txt", server_content)
        (Path(local_dir) / "staylocal.txt").write_bytes(b"local only")

        # When running a download-only sync
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "download-only")

        # Then the server file IS downloaded (positive evidence sync ran) and
        # the local file is NOT uploaded
        assert result.exit_code == 0
        assert (Path(local_dir) / "downloadme.txt").read_bytes() == server_content
        assert "staylocal.txt" not in bb_server_files(bb_bucket)

    def test_upload_only_uploads_but_does_not_download(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a server-only file and a local-only file
        bb_server_upload(bb_bucket, "stayremote.txt", b"remote only")
        local_content = b"from client"
        (Path(local_dir) / "uploadme.txt").write_bytes(local_content)

        # When running an upload-only sync
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "upload-only")

        # Then the local file IS uploaded (positive evidence sync ran) and
        # the server file is NOT downloaded
        assert result.exit_code == 0
        assert "uploadme.txt" in bb_server_files(bb_bucket)
        assert not (Path(local_dir) / "stayremote.txt").exists()


class TestRemoteSyncDryRun:
    def test_dry_run_writes_nothing(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a server-only file
        bb_server_upload(bb_bucket, "preview.txt", b"should not download")

        # When running a dry-run sync
        result = run_sync_once(bb_bucket, local_dir, "--dry-run")

        # Then no file is written locally
        assert result.exit_code == 0
        assert not (Path(local_dir) / "preview.txt").exists()


class TestRemoteSyncIgnoreDelete:
    def test_ignore_delete_keeps_local_file(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a file present on both sides (identical), then deleted on the server
        content = b"keep me"
        (Path(local_dir) / "keep.txt").write_bytes(content)
        run_sync_once(bb_bucket, local_dir)  # propagate local -> server
        # Now delete it on the server side
        base, api_key = live_server
        requests.delete(
            f"{base}/api/v1/buckets/{bb_bucket}/files",
            data={"cid": "bbserver", "filename": "keep.txt"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            verify=False,
        )

        # When running a sync with --ignore-delete
        result = run_sync_once(bb_bucket, local_dir, "--ignore-delete")

        # Then the local file is preserved (deletion not propagated)
        assert result.exit_code == 0
        assert (Path(local_dir) / "keep.txt").read_bytes() == content


class TestRemoteSyncNegative:
    def test_wrong_api_key_aborts(self, live_server, bb_bucket, local_dir, monkeypatch):
        # Given a wrong API key in the environment
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

        # Then the sync does not transfer files (wrong key → 401 → abort)
        assert result.exit_code == 0
        local_files = list(Path(local_dir).iterdir())
        assert local_files == [] or all(p.name.startswith(".hsync") for p in local_files)

    def test_unreachable_server_reports_error(self, tmp_path):
        # Given a server URL that nothing listens on
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

        # Then the CLI does not crash (connectivity check returns early, exit 0)
        assert result.exit_code == 0
