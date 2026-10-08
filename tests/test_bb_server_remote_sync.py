# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the remote (server-mode) sync use case.

Drives the real client through its public CLI against a real in-process
hsync-server. Verifies the observable filesystem outcome and exit code — no
internal modules are imported or mocked.

Requires HSYNC_BB_SERVER=1 to run.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
import requests

from tests.conftest import BB_CLIENT_CID

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server_delete(live_server, bucket: str, filename: str) -> None:
    """Delete a file on the server via REST API."""
    base, api_key = live_server
    r = requests.delete(
        f"{base}/api/v1/buckets/{bucket}/files",
        data={"cid": BB_CLIENT_CID, "filename": filename},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    assert r.status_code in (200, 204), f"Server delete failed: {r.status_code} {r.text}"


def _server_file_metadata(live_server, bucket: str, filename: str) -> dict:
    """Return the server-side metadata dict for a single file."""
    base, api_key = live_server
    r = requests.get(
        f"{base}/api/v1/buckets/{bucket}/files",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    assert r.status_code == 200, r.text
    for f in r.json():
        if f["filepath"] == filename:
            return f
    raise AssertionError(f"{filename} not found on server (bucket={bucket})")


def _server_vclock(live_server, bucket: str, filename: str) -> dict:
    """Return the causal version clock a file advertises on the server.

    Reads the public REST listing — exactly what any real client would see —
    and returns the ``version.vclock`` dict (empty when the server has no
    recorded version for the path).
    """
    meta = _server_file_metadata(live_server, bucket, filename)
    version = meta.get("version") or {}
    vclock = version.get("vclock")
    return dict(vclock) if isinstance(vclock, dict) else {}


# =============================================================================
# Happy path — core sync operations
# =============================================================================


class TestRemoteSyncHappy:
    def test_downloads_remote_file_into_empty_local_dir(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a file already on the server
        content = b"hello from server\n"
        bb_server_upload(bb_bucket, "remote.txt", content)

        # When a one-shot sync runs into an empty local dir
        result = run_sync_once(bb_bucket, local_dir)

        # Then the remote file is downloaded with the right content
        assert result.exit_code == 0
        assert (Path(local_dir) / "remote.txt").read_bytes() == content

    def test_uploads_local_file_to_empty_server_bucket(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a local file and an empty server bucket
        content = b"hello from client\n"
        (Path(local_dir) / "local.txt").write_bytes(content)

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then the local file is uploaded to the server
        assert result.exit_code == 0
        assert "local.txt" in bb_server_files(bb_bucket)

    def test_bidirectional_converges(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a file on each side
        bb_server_upload(bb_bucket, "fromserver.bin", b"server")
        (Path(local_dir) / "fromclient.bin").write_bytes(b"client")

        # When a one-shot bidirectional sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then both sides converge on the union of the files
        assert result.exit_code == 0
        assert (Path(local_dir) / "fromserver.bin").read_bytes() == b"server"
        assert "fromclient.bin" in bb_server_files(bb_bucket)


# =============================================================================
# File modification, rename, deletion
# =============================================================================


class TestRemoteFileOperations:
    def test_file_modification_uploaded(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_download
    ):
        # Given a file synced at version v1
        (Path(local_dir) / "mod.txt").write_bytes(b"v1\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0

        # When it is modified locally and synced again
        (Path(local_dir) / "mod.txt").write_bytes(b"v2-updated\n")
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0

        # Then the updated content is on the server
        assert bb_server_download(bb_bucket, "mod.txt") == b"v2-updated\n"

    def test_file_deletion_propagates_to_server(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a file synced to the server
        (Path(local_dir) / "todel.txt").write_bytes(b"delete me\n")
        run_sync_once(bb_bucket, local_dir)
        assert "todel.txt" in bb_server_files(bb_bucket)

        # When it is deleted locally and synced again
        (Path(local_dir) / "todel.txt").unlink()
        result = run_sync_once(bb_bucket, local_dir)

        # Then the deletion propagates to the server
        assert result.exit_code == 0
        assert "todel.txt" not in bb_server_files(bb_bucket)

    def test_file_rename_propagates_to_server(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a file synced under its old name
        (Path(local_dir) / "old.txt").write_bytes(b"rename me\n")
        run_sync_once(bb_bucket, local_dir)
        assert "old.txt" in bb_server_files(bb_bucket)

        # When it is renamed locally and synced again
        (Path(local_dir) / "old.txt").rename(Path(local_dir) / "new.txt")
        result = run_sync_once(bb_bucket, local_dir)

        # Then the rename propagates (old gone, new present)
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
        # Given a local subdirectory with a file
        sub = Path(local_dir) / "sub"
        sub.mkdir()
        (sub / "f.txt").write_bytes(b"inside\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then the directory and its file are on the server
        assert result.exit_code == 0
        assert "sub/f.txt" in bb_server_files(bb_bucket)

    def test_directory_deletion_propagates(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a synced subdirectory
        sub = Path(local_dir) / "todel"
        sub.mkdir()
        (sub / "x.txt").write_bytes(b"x\n")
        run_sync_once(bb_bucket, local_dir)
        assert "todel/x.txt" in bb_server_files(bb_bucket)

        import shutil

        # When it is removed locally and synced again
        shutil.rmtree(str(sub))
        result = run_sync_once(bb_bucket, local_dir)

        # Then the deletion propagates to the server
        assert result.exit_code == 0
        assert "todel/x.txt" not in bb_server_files(bb_bucket)

    def test_nested_deep_directory_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a deeply nested local directory tree
        deep = Path(local_dir) / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_bytes(b"deep\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then the deep path is replicated on the server
        assert result.exit_code == 0
        assert "a/b/c/d/e/deep.txt" in bb_server_files(bb_bucket)


# =============================================================================
# Symlinks
# =============================================================================


class TestRemoteSymlinks:
    def test_in_bucket_symlink_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """An in-bucket symlink must be replicated STRUCTURALLY (regola 1).

        The server listing must advertise the LINK (``type == "symlink"`` +
        its ``target``), not the bytes/content of the target file — the old
        reconcile dereferenced the link and uploaded the target content as a
        regular file, making this assertion fail."""
        # Given a target file and an in-bucket symlink
        target = Path(local_dir) / "real.txt"
        target.write_bytes(b"target\n")
        link = Path(local_dir) / "link.txt"
        link.symlink_to(target)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert "link.txt" in bb_server_files(bb_bucket)

        # Then the server lists it structurally (type + target, no content hash)
        base, api_key = live_server
        listing = requests.get(
            f"{base}/api/v1/buckets/{bb_bucket}/files",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            verify=False,
        ).json()
        link_entry = next(f for f in listing if f["filepath"] == "link.txt")
        assert link_entry.get("type") == "symlink", (
            f"expected structural symlink entry, got {link_entry!r}"
        )
        assert link_entry.get("target") == "real.txt", (
            f"expected target 'real.txt', got {link_entry.get('target')!r}"
        )
        assert "sha256sum" not in link_entry or link_entry.get("sha256sum") is None, (
            "a structural symlink entry must not carry a dereferenced content hash"
        )

    def test_escaping_symlink_not_synced(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """Symlink pointing outside bucket must not be uploaded."""
        # Given a symlink pointing outside the sync dir
        outside = Path(local_dir).parent / "escape_secret.txt"
        outside.write_bytes(b"secret\n")
        try:
            link = Path(local_dir) / "esc.txt"
            link.symlink_to(outside)

            # When a one-shot sync runs
            result = run_sync_once(bb_bucket, local_dir)

            # Then the escaping symlink is not uploaded
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
        # Given a local file with restrictive permissions
        f = Path(local_dir) / "perms.txt"
        f.write_bytes(b"perms\n")
        f.chmod(0o600)

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then the server records the 600 permissions
        assert result.exit_code == 0
        meta = _server_file_metadata(live_server, bb_bucket, "perms.txt")
        assert meta["permissions"] == "600"


# =============================================================================
# Bulk / Unicode / pre-existing
# =============================================================================


class TestRemoteBulkSync:
    def test_multiple_files_sync(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given ten local files
        files = {}
        for i in range(10):
            name = f"f_{i:02d}.txt"
            content = f"file{i:02d}\n".encode()
            (Path(local_dir) / name).write_bytes(content)
            files[name] = content

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then all ten files are on the server
        assert result.exit_code == 0
        for name in files:
            assert name in bb_server_files(bb_bucket)

    def test_pre_existing_data_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given pre-existing local files
        (Path(local_dir) / "pre_a.txt").write_bytes(b"alpha\n")
        (Path(local_dir) / "pre_b.txt").write_bytes(b"beta\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then both are uploaded
        assert result.exit_code == 0
        assert "pre_a.txt" in bb_server_files(bb_bucket)
        assert "pre_b.txt" in bb_server_files(bb_bucket)


class TestRemoteUnicode:
    def test_unicode_filename_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a Unicode-named local file
        name = "caffè_日本語.txt"
        content = "unicode 🎉\n".encode()
        (Path(local_dir) / name).write_bytes(content)

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then it is uploaded with its Unicode name intact
        assert result.exit_code == 0
        assert name in bb_server_files(bb_bucket)


# =============================================================================
# Sync direction
# =============================================================================


class TestRemoteSyncDirection:
    def test_download_only_downloads_but_does_not_upload(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a remote file and a local-only file
        server_content = b"from server"
        bb_server_upload(bb_bucket, "downloadme.txt", server_content)
        (Path(local_dir) / "staylocal.txt").write_bytes(b"local only")

        # When syncing with --sync-direction download-only
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "download-only")

        # Then the remote file is downloaded and the local file is not uploaded
        assert result.exit_code == 0
        assert (Path(local_dir) / "downloadme.txt").read_bytes() == server_content
        assert "staylocal.txt" not in bb_server_files(bb_bucket)

    def test_upload_only_uploads_but_does_not_download(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, bb_server_files
    ):
        # Given a remote file and a local file
        bb_server_upload(bb_bucket, "stayremote.txt", b"remote only")
        local_content = b"from client"
        (Path(local_dir) / "uploadme.txt").write_bytes(local_content)

        # When syncing with --sync-direction upload-only
        result = run_sync_once(bb_bucket, local_dir, "--sync-direction", "upload-only")

        # Then the local file is uploaded and the remote file is not downloaded
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
        # Given a remote file
        bb_server_upload(bb_bucket, "preview.txt", b"should not download")

        # When syncing with --dry-run
        result = run_sync_once(bb_bucket, local_dir, "--dry-run")

        # Then nothing is written locally
        assert result.exit_code == 0
        assert not (Path(local_dir) / "preview.txt").exists()

    def test_dry_run_shows_preview(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a remote file
        bb_server_upload(bb_bucket, "show_me.txt", b"content")

        # When syncing with --dry-run
        result = run_sync_once(bb_bucket, local_dir, "--dry-run")

        # Then the preview mentions the file in stdout
        assert result.exit_code == 0
        assert "show_me.txt" in result.output


# =============================================================================
# Edge cases & flags
# =============================================================================


class TestRemoteEdgeCases:
    def test_default_ignored_file_not_synced(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """Files matching DEFAULT_IGNORE_PATTERNS (OS/editor temp files) are not synced."""
        # Given OS/editor temp files that match the default ignore patterns
        (Path(local_dir) / ".DS_Store").write_bytes(b"macos cache\n")
        (Path(local_dir) / "draft.txt~").write_bytes(b"editor backup\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then they are not uploaded
        assert result.exit_code == 0
        assert ".DS_Store" not in bb_server_files(bb_bucket)
        assert "draft.txt~" not in bb_server_files(bb_bucket)

    def test_generic_dotfile_is_synced(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """Generic dotfiles (.env, .config, …) ARE synced by design.

        Following Syncthing/rsync/Nextcloud, only OS cache and editor temp
        files are excluded by default; user dotfiles carry real data.
        """
        # Given a generic dotfile
        (Path(local_dir) / ".env").write_bytes(b"SECRET=1\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then it is uploaded (dotfiles are synced by design)
        assert result.exit_code == 0
        assert ".env" in bb_server_files(bb_bucket)

    def test_ignore_patterns_flag_respected(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given one kept and one ignored file
        (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
        (Path(local_dir) / "skip.tmp").write_bytes(b"tmp\n")

        # When syncing with --ignore-pattern '*.tmp'
        result = run_sync_once(bb_bucket, local_dir, "--ignore-pattern", "*.tmp")

        # Then the kept file syncs and the ignored one does not
        assert result.exit_code == 0
        assert "keep.txt" in bb_server_files(bb_bucket)
        assert "skip.tmp" not in bb_server_files(bb_bucket)

    def test_compression_disabled_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a local file
        (Path(local_dir) / "nocomp.txt").write_bytes(b"no compression\n")

        # When syncing with --no-compression
        result = run_sync_once(bb_bucket, local_dir, "--no-compression")

        # Then it still syncs
        assert result.exit_code == 0
        assert "nocomp.txt" in bb_server_files(bb_bucket)

    def test_delta_disabled_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        # Given a local file
        (Path(local_dir) / "nodelta.txt").write_bytes(b"no delta\n")

        # When syncing with --no-delta
        result = run_sync_once(bb_bucket, local_dir, "--no-delta")

        # Then it still syncs
        assert result.exit_code == 0
        assert "nodelta.txt" in bb_server_files(bb_bucket)


# =============================================================================
# Ignore delete
# =============================================================================


class TestRemoteSyncIgnoreDelete:
    def test_ignore_delete_keeps_local_file(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload
    ):
        # Given a synced local file that is then deleted on the server
        content = b"keep me"
        (Path(local_dir) / "keep.txt").write_bytes(content)
        run_sync_once(bb_bucket, local_dir)
        _server_delete(live_server, bb_bucket, "keep.txt")

        # When syncing with --ignore-delete
        result = run_sync_once(bb_bucket, local_dir, "--ignore-delete")

        # Then the local copy survives the remote deletion
        assert result.exit_code == 0
        assert (Path(local_dir) / "keep.txt").read_bytes() == content


# =============================================================================
# Conflicted copies
# =============================================================================


class TestRemoteConflictedCopy:
    def test_conflicted_copy_propagates_to_other_client(
        self,
        live_server,
        bb_bucket,
        local_dir,
        run_sync_once,
        bb_server_files,
        bb_server_upload,
        bb_server_download,
    ):
        """When two remote writers upload different content for the same path
        with *concurrent* causal histories, the deterministic SHA-256 tie-break
        selects the winner and the server preserves the loser as a conflicted
        copy — every other client then converges on both files.

        The first writer is the real client (public CLI); the second writer is
        driven through the documented REST upload API with a concurrent vector
        clock, simulating a second remote client that diverged without
        reconciling in between (a sequential CLI sync would first adopt the
        base and therefore never diverge, so the concurrent writer is emulated
        directly on the wire).
        """

        # Given a unique filename and version A already uploaded by client A
        name = f"shared-{uuid.uuid4().hex}.txt"
        version_a = b"version A content\n"
        version_b = b"version B content\n"
        (Path(local_dir) / name).write_bytes(version_a)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        # When a second remote writer uploads different content with a
        # concurrent causal history (different author cid) via the public
        # REST API
        second_cid = "TestClientY1234567890"
        second_vclock = json.dumps({second_cid: 1})
        base, api_key = live_server
        r = requests.post(
            f"{base}/api/v1/buckets/{bb_bucket}/files",
            data={
                "cid": second_cid,
                "path": "",
                "sha256sum": hashlib.sha256(version_b).hexdigest(),
                "permissions": "644",
                "offset": "0",
                "size": "0",
                "compressed": "false",
                "vclock": second_vclock,
                "version_id": "",
            },
            files={"file": (name, version_b, "application/octet-stream")},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=False,
        )
        # The concurrent upload is a *valid* arbitration outcome in both
        # directions: accepted (201) when version B wins the tie-break,
        # conflict_lost (409) when it loses — never a transport error.
        assert r.status_code in (201, 409), f"unexpected upload outcome: {r.status_code} {r.text}"

        # Then the deterministic tie-break picks the winner. The winner stays
        # at the original path; the loser is preserved as a conflicted copy.
        def sha256_of(content: bytes) -> str:
            return hashlib.sha256(content).hexdigest()

        winner, loser = sorted((version_a, version_b), key=sha256_of, reverse=True)
        listing = bb_server_files(bb_bucket)
        assert name in listing, f"Main path missing from listing: {listing}"
        conflicts = [p for p in listing if "_conflicted_copy_" in p and name.split(".")[0] in p]
        assert len(conflicts) == 1, (
            f"Expected exactly 1 conflicted copy for {name}, found {len(conflicts)}: {conflicts}"
        )
        conflict_path = conflicts[0]
        assert bb_server_download(bb_bucket, conflict_path) == loser, (
            f"Conflicted copy {conflict_path} does not preserve the losing content"
        )

        # And a re-sync of client A must converge on the winner at the main
        # path and the loser at the conflicted copy. The arbitration already
        # happened server-side (the concurrent upload resolved the tie-break
        # there), so for client A both files are plain downloads with no
        # local divergence: the cycle exits clean (0).
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / name).read_bytes() == winner
        conflict_local = Path(local_dir) / conflict_path.split("/")[-1]
        assert conflict_local.read_bytes() == loser, (
            f"Local conflicted copy not synced: {conflict_local} missing loser content"
        )


# =============================================================================
# Causal conflict handling between two writers of the same file
# =============================================================================


class TestRemoteCausalConflict:
    """Divergence between this user and another remote writer on one file.

    The other writer is emulated through the public REST upload API (exactly
    what a real second client sends on the wire): a remote edit that descends
    from the agreed version is uploaded with a *dominant* clock, while edits
    that never saw the other side carry independent clocks. The payload bytes
    are chosen in advance so the deterministic tie-break winner (highest
    SHA-256) is known. Black-box: only exit codes, the local filesystem and
    the public REST listing are observed.
    """

    @staticmethod
    def _remote_upload(live_server, bucket, name, content, cid, vclock) -> requests.Response:
        """Upload content for a path as if it were another real client."""
        base, api_key = live_server
        return requests.post(
            f"{base}/api/v1/buckets/{bucket}/files",
            data={
                "cid": cid,
                "path": "",
                "sha256sum": hashlib.sha256(content).hexdigest(),
                "permissions": "644",
                "offset": "0",
                "size": "0",
                "compressed": "false",
                "vclock": json.dumps(vclock),
                "version_id": "",
            },
            files={"file": (name, content, "application/octet-stream")},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            verify=False,
        )

    @staticmethod
    def _local_copies(local_dir: str, stem: str) -> list[Path]:
        return [
            p for p in Path(local_dir).iterdir() if p.name.startswith(f"{stem}_conflicted_copy_")
        ]

    @staticmethod
    def _server_copies(listing: set[str], stem: str) -> list[str]:
        return [p for p in listing if "_conflicted_copy_" in p and stem in p]

    def test_remote_change_downloads_silently_without_conflict_copy(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """My file untouched, changed by someone else → I just adopt it.

        No conflict copy must ever appear: neither locally (the engine must
        not preserve a file that has not diverged) nor on the server.
        """
        name = f"silent-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        base_content = b"shared base v1\n"
        remote_new = b"remote v2 zz\n"

        (Path(local_dir) / name).write_bytes(base_content)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        # Another user edits the file while I am offline: their version
        # descends from what we agreed on, so the server accepts it plainly.
        second_cid = "TestClientY1234567890"
        dominant = dict(_server_vclock(live_server, bb_bucket, name))
        dominant[second_cid] = dominant.get(second_cid, 0) + 1
        r = self._remote_upload(live_server, bb_bucket, name, remote_new, second_cid, dominant)
        assert r.status_code == 201, f"remote edit rejected: {r.status_code} {r.text}"

        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / name).read_bytes() == remote_new, (
            "The remote version was not adopted silently"
        )
        assert self._local_copies(local_dir, stem) == [], (
            "A silent download must never produce a local conflict copy"
        )
        listing = bb_server_files(bb_bucket)
        assert name in listing
        assert self._server_copies(listing, stem) == [], (
            "A non-divergent update must never produce a server conflict copy"
        )

    def test_client_loses_local_copy_preserved_and_propagated(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        """I edit offline, the other side wins the hash tie-break: my version
        is preserved as a *_conflicted_copy_* next to the file, the other side
        takes the path, and my preserved copy is later synced back to the
        server — no data loss."""
        name = f"lose-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        base_content = b"shared base v1\n"
        remote_content = b"remote v2 zz\n"
        local_edit = b"local v2 aa\n"

        (Path(local_dir) / name).write_bytes(base_content)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        # The other writer changes the file while I am offline.
        second_cid = "TestClientY1234567890"
        dominant = dict(_server_vclock(live_server, bb_bucket, name))
        dominant[second_cid] = dominant.get(second_cid, 0) + 1
        r = self._remote_upload(live_server, bb_bucket, name, remote_content, second_cid, dominant)
        assert r.status_code == 201, f"remote edit rejected: {r.status_code} {r.text}"

        # I also edit it, with different content (lower hash: I lose the
        # tie-break).
        assert hashlib.sha256(remote_content).hexdigest() > hashlib.sha256(local_edit).hexdigest()
        (Path(local_dir) / name).write_bytes(local_edit)

        result = run_sync_once(bb_bucket, local_dir)
        # The conflicting download preserves my losing copy: the cycle
        # reports the resolved conflict via exit code 4 (CONFLICTS).
        assert result.exit_code == 4
        assert (Path(local_dir) / name).read_bytes() == remote_content, (
            "The winner must take the original path"
        )
        copies = self._local_copies(local_dir, stem)
        assert len(copies) == 1, f"expected 1 local conflict copy, got {copies}"
        assert copies[0].read_bytes() == local_edit, (
            "The conflict copy must preserve my losing content"
        )

        # My preserved copy is a brand-new path: the next sync propagates it
        # to the server without re-flagging the original file.
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        listing = bb_server_files(bb_bucket)
        conflicts = self._server_copies(listing, stem)
        assert len(conflicts) == 1, f"expected 1 server conflict copy, got {conflicts}"
        assert bb_server_download(bb_bucket, conflicts[0]) == local_edit, (
            "The server copy must carry my losing content"
        )

    def test_client_wins_server_preserves_remote(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        """I edit offline and my version wins the hash tie-break: my file keeps
        the original path (no local copy), and the *server* preserves the other
        side's version as a conflicted copy."""
        name = f"win-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        base_content = b"shared base v1\n"
        remote_content = b"remote v2 aa\n"
        local_edit = b"local v2 zz\n"

        (Path(local_dir) / name).write_bytes(base_content)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        second_cid = "TestClientY1234567890"
        dominant = dict(_server_vclock(live_server, bb_bucket, name))
        dominant[second_cid] = dominant.get(second_cid, 0) + 1
        r = self._remote_upload(live_server, bb_bucket, name, remote_content, second_cid, dominant)
        assert r.status_code == 201, f"remote edit rejected: {r.status_code} {r.text}"

        # My edit has the higher hash: I win the tie-break.
        assert hashlib.sha256(local_edit).hexdigest() > hashlib.sha256(remote_content).hexdigest()
        (Path(local_dir) / name).write_bytes(local_edit)

        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / name).read_bytes() == local_edit, (
            "My winning version must keep the original path"
        )
        assert self._local_copies(local_dir, stem) == [], (
            "The winning side must never create a local conflict copy"
        )

        listing = bb_server_files(bb_bucket)
        assert name in listing
        assert bb_server_download(bb_bucket, name) == local_edit, (
            "The server must adopt my winning content"
        )
        conflicts = self._server_copies(listing, stem)
        assert len(conflicts) == 1, f"expected 1 server conflict copy, got {conflicts}"
        assert bb_server_download(bb_bucket, conflicts[0]) == remote_content, (
            "The server conflict copy must preserve the losing remote content"
        )

    def test_new_file_both_sides_client_wins(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        """A brand-new file was created independently on both sides (no shared
        ancestor): my content has the higher hash, so my file wins the path and
        the server preserves the other side's as a conflicted copy."""
        name = f"newwin-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        remote_content = b"remote new aa\n"
        local_content = b"local new zz\n"

        second_cid = "TestClientY1234567890"
        r = self._remote_upload(
            live_server, bb_bucket, name, remote_content, second_cid, {second_cid: 1}
        )
        assert r.status_code == 201, f"remote upload rejected: {r.status_code} {r.text}"

        # Independent creation on my side too (higher hash: I win).
        assert (
            hashlib.sha256(local_content).hexdigest() > hashlib.sha256(remote_content).hexdigest()
        )
        (Path(local_dir) / name).write_bytes(local_content)

        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / name).read_bytes() == local_content
        assert self._local_copies(local_dir, stem) == [], (
            "The winning side never runs a conflict download"
        )

        listing = bb_server_files(bb_bucket)
        assert bb_server_download(bb_bucket, name) == local_content
        conflicts = self._server_copies(listing, stem)
        assert len(conflicts) == 1, f"expected 1 server conflict copy, got {conflicts}"
        assert bb_server_download(bb_bucket, conflicts[0]) == remote_content, (
            "The server conflict copy must preserve the losing remote content"
        )

    def test_new_file_both_sides_client_loses(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        """Independent creation of the same new path, my hash loses the
        tie-break: the other side takes the path and my version is preserved
        as a local conflicted copy."""
        name = f"newlose-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        remote_content = b"remote new oops\n"
        local_content = b"local new aa\n"

        second_cid = "TestClientY1234567890"
        r = self._remote_upload(
            live_server, bb_bucket, name, remote_content, second_cid, {second_cid: 1}
        )
        assert r.status_code == 201, f"remote upload rejected: {r.status_code} {r.text}"

        assert (
            hashlib.sha256(remote_content).hexdigest() > hashlib.sha256(local_content).hexdigest()
        )
        (Path(local_dir) / name).write_bytes(local_content)

        result = run_sync_once(bb_bucket, local_dir)
        # A conflict_download preserved my losing copy: exit code 4 (CONFLICTS).
        assert result.exit_code == 4
        assert (Path(local_dir) / name).read_bytes() == remote_content, (
            "The higher-hash remote content must take the original path"
        )
        copies = self._local_copies(local_dir, stem)
        assert len(copies) == 1, f"expected 1 local conflict copy, got {copies}"
        assert copies[0].read_bytes() == local_content, (
            "The conflict copy must preserve my losing content"
        )
        listing = bb_server_files(bb_bucket)
        assert self._server_copies(listing, stem) == [], (
            "The server never sees the losing local content before it is re-uploaded"
        )
        assert bb_server_download(bb_bucket, name) == remote_content

    def test_identical_new_file_both_sides_no_conflict(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """Both sides independently create the same path with the same bytes:
        they are already in sync — no transfer, no conflicted copy anywhere."""
        name = f"same-{uuid.uuid4().hex}.txt"
        stem = name.split(".")[0]
        content = b"identical fresh content\n"

        second_cid = "TestClientY1234567890"
        r = self._remote_upload(live_server, bb_bucket, name, content, second_cid, {second_cid: 1})
        assert r.status_code == 201, f"remote upload rejected: {r.status_code} {r.text}"

        (Path(local_dir) / name).write_bytes(content)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / name).read_bytes() == content
        assert self._local_copies(local_dir, stem) == [], (
            "Identical content is not a divergence: no local copy"
        )

        listing = bb_server_files(bb_bucket)
        assert name in listing
        assert self._server_copies(listing, stem) == [], (
            "Identical content is not a divergence: no server copy"
        )


# =============================================================================
# Negative / error handling
# =============================================================================


class TestRemoteMoreFlags:
    def test_logfile_writes_output(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_upload, tmp_path
    ):
        """--log-file writes log to the specified file."""
        # Given a remote file and a target log path
        log_path = tmp_path / "remote_test.log"
        bb_server_upload(bb_bucket, "logged.txt", b"log-test\n")

        # When syncing with --log-file
        result = run_sync_once(bb_bucket, local_dir, "--log-file", str(log_path))

        # Then the log file is created and non-empty
        assert result.exit_code == 0
        assert log_path.exists(), "Log file was not created"
        assert len(log_path.read_text()) > 0, "Log file is empty"

    def test_no_autotune_still_syncs(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """--no-autotune does not block sync."""
        # Given a local file
        (Path(local_dir) / "autotune.txt").write_bytes(b"no autotune\n")

        # When syncing with --no-autotune
        result = run_sync_once(bb_bucket, local_dir, "--no-autotune")

        # Then it still syncs
        assert result.exit_code == 0
        assert "autotune.txt" in bb_server_files(bb_bucket)

    def test_hsyncignore_file_respected(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files
    ):
        """.hsyncignore in local dir excludes matching files from upload."""
        # Given a .hsyncignore rule and one matching + one non-matching file
        (Path(local_dir) / ".hsyncignore").write_text("*.log\n")
        (Path(local_dir) / "keep.txt").write_bytes(b"keep\n")
        (Path(local_dir) / "skip.log").write_bytes(b"hidden\n")

        # When a one-shot sync runs
        result = run_sync_once(bb_bucket, local_dir)

        # Then the matching file is excluded and the other is uploaded
        assert result.exit_code == 0
        assert "keep.txt" in bb_server_files(bb_bucket)
        assert "skip.log" not in bb_server_files(bb_bucket)


class TestRemoteSyncNegative:
    def test_wrong_api_key_aborts(self, live_server, bb_bucket, local_dir, monkeypatch):
        from hsync_client.config import get_env_settings

        # Given a wrong API key injected into the environment
        monkeypatch.setenv("HSYNC_API_KEY", "wrong-key")
        get_env_settings.cache_clear()
        from typer.testing import CliRunner

        from hsync_client.cli import app

        from .conftest import _ClientGlobalsGuard, bb_server_cli_args

        base_url, _ = live_server

        # When a one-shot sync runs in-process with the wrong key
        with _ClientGlobalsGuard():
            result = CliRunner().invoke(
                app,
                [
                    "server",
                    "-b",
                    bb_bucket,
                    *bb_server_cli_args(base_url),
                    "-p",
                    local_dir,
                    "--once",
                    "--allow-deletions",
                    "--insecure",
                    "--min-free-disk-space",
                    "0",
                ],
            )
        get_env_settings.cache_clear()

        # Then the CLI aborts (auth failure) and does not touch the local dir
        assert result.exit_code != 0
        local_files = list(Path(local_dir).iterdir())
        assert local_files == [] or all(p.name.startswith(".hsync") for p in local_files)

    def test_unreachable_server_reports_error(self, tmp_path):
        from typer.testing import CliRunner

        from hsync_client.cli import app

        from .conftest import _ClientGlobalsGuard

        # Given a local dir and an unreachable server (closed port)
        local = tmp_path / "local"
        local.mkdir()

        # When a one-shot sync runs in-process against it
        with _ClientGlobalsGuard():
            result = CliRunner().invoke(
                app,
                [
                    "server",
                    "-b",
                    "anybucket",
                    "127.0.0.1",
                    "--port",
                    "1",
                    "-p",
                    str(local),
                    "--once",
                    "--allow-deletions",
                    "--min-free-disk-space",
                    "0",
                ],
                env={"HSYNC_API_KEY": "some-key-1234567890"},
            )

        # Then the CLI exits non-zero (sync failure)
        assert result.exit_code != 0

    def test_tls_without_insecure_aborts(self, live_server, bb_bucket, local_dir):
        """Connecting to an HTTPS server with a self-signed cert without
        --insecure must abort (TLS verification failure)."""
        # Given an HTTPS live server with a self-signed certificate
        from typer.testing import CliRunner

        from hsync_client.cli import app

        from .conftest import _ClientGlobalsGuard, bb_server_cli_args

        base_url, api_key = live_server

        # When the client syncs without --insecure (but with a valid API key
        # so the failure is TLS verification, not a missing-key abort)
        with _ClientGlobalsGuard():
            result = CliRunner().invoke(
                app,
                [
                    "server",
                    "-b",
                    bb_bucket,
                    *bb_server_cli_args(base_url),
                    "-p",
                    local_dir,
                    "--once",
                    "--allow-deletions",
                    "--min-free-disk-space",
                    "0",
                ],
                env={"HSYNC_API_KEY": api_key},
            )

        # Then the CLI aborts (TLS verification failure) and does not sync
        assert result.exit_code != 0
        local_files = list(Path(local_dir).iterdir())
        assert local_files == [] or all(p.name.startswith(".hsync") for p in local_files)


# =============================================================================
# Parity with the P2P black-box suite: file-size boundaries, symlink delete,
# bandwidth throttle, subdir, concurrent writers, tombstone catch-up.
# Each mirrors a test_bb_p2p_* scenario so the same replication behaviour is
# verified in client-server mode.
# =============================================================================


class TestRemoteEmptyFile:
    def test_empty_file_round_trips(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        # Given an empty (0-byte) file
        name = f"empty-{uuid.uuid4().hex[:8]}.bin"
        content = b""
        (Path(local_dir) / name).write_bytes(content)
        print(f"[bb] empty-file: name={name} bucket={bb_bucket}")

        # When syncing once
        result = run_sync_once(bb_bucket, local_dir)

        # Then the upload succeeds, the server lists it, and a download round-trips 0 bytes
        assert result.exit_code == 0
        assert name in bb_server_files(bb_bucket)
        assert bb_server_download(bb_bucket, name) == content


class TestRemoteLargeFile:
    def test_large_file_round_trips(
        self,
        live_server,
        bb_bucket,
        local_dir,
        run_sync_once,
        bb_server_files,
        bb_server_download,
        bb_sample,
    ):
        # Given a 1 MiB file of random content
        s = bb_sample(suffix=".bin", prefix="large", size=1024 * 1024)
        name = s.name
        content = s.payload
        (Path(local_dir) / name).write_bytes(content)

        # When syncing once
        result = run_sync_once(bb_bucket, local_dir)

        # Then the upload succeeds, the server lists it, and the download matches byte-for-byte
        assert result.exit_code == 0, s.ctx
        assert name in bb_server_files(bb_bucket), s.ctx
        assert bb_server_download(bb_bucket, name) == content, s.ctx


class TestRemoteSymlinkDeleteKeepsTarget:
    def test_deleting_symlink_on_server_preserves_target(
        self, live_server, bb_bucket, local_dir, run_sync_once, bb_server_files, bb_server_download
    ):
        # Given a target file and an in-bucket symlink to it, both synced to the server
        target_content = b"symlink-target\n"
        (Path(local_dir) / "target.txt").write_bytes(target_content)
        (Path(local_dir) / "link.txt").symlink_to("target.txt")
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0
        assert "link.txt" in bb_server_files(bb_bucket)
        assert "target.txt" in bb_server_files(bb_bucket)

        # When the symlink is deleted on the server via the public REST API
        _server_delete(live_server, bb_bucket, "link.txt")

        # Then the link is gone from the server but the target survives and is intact
        # (the delete must remove the LINK entry, never dereference it to the target)
        files = bb_server_files(bb_bucket)
        assert "link.txt" not in files
        assert "target.txt" in files
        assert bb_server_download(bb_bucket, "target.txt") == target_content


class TestRemoteBandwidthThrottle:
    def test_bandwidth_throttle_allows_sync(
        self,
        live_server,
        bb_bucket,
        local_dir,
        run_sync_once,
        bb_server_files,
        bb_server_download,
        bb_sample,
    ):
        # Given a 50 KB file of random content
        s = bb_sample(suffix=".bin", prefix="throttle", size=50_000)
        name = s.name
        content = s.payload
        (Path(local_dir) / name).write_bytes(content)

        # When syncing with generous upload/download bandwidth limits
        result = run_sync_once(
            bb_bucket,
            local_dir,
            "--limit-upload",
            "10MB",
            "--limit-download",
            "10MB",
        )

        # Then the upload succeeds and the content round-trips
        assert result.exit_code == 0, s.ctx
        assert name in bb_server_files(bb_bucket), s.ctx
        assert bb_server_download(bb_bucket, name) == content, s.ctx


class TestRemoteFilterPath:
    def test_filter_path_restricts_download(
        self, live_server, bb_bucket, local_dir, run_sync_once, tmp_path, bb_server_download
    ):
        # Given the server holds files in two subdirectories (uploaded by a first,
        # unrestricted sync) — mirroring the P2P test where the master already
        # holds both subtrees before the slave filters
        (Path(local_dir) / "kept").mkdir()
        (Path(local_dir) / "kept" / "a.txt").write_bytes(b"in-kept\n")
        (Path(local_dir) / "excluded").mkdir()
        (Path(local_dir) / "excluded" / "b.txt").write_bytes(b"in-excluded\n")
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        # When a fresh empty directory syncs with --subdir restricting to "kept"
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        result = run_sync_once(bb_bucket, str(fresh), "--subdir", "kept")

        # Then only the filtered subtree is downloaded; the other stays absent locally
        assert result.exit_code == 0
        assert (fresh / "kept" / "a.txt").exists()
        assert not (fresh / "excluded").exists()


class TestRemoteConcurrentWrites:
    def test_two_writers_different_files_converge(
        self,
        live_server,
        bb_bucket,
        local_dir,
        run_sync_once,
        bb_server_upload,
        bb_server_files,
        bb_server_download,
    ):
        # Given client A has uploaded fileA
        a_content = b"from-client-A\n"
        (Path(local_dir) / "fileA.txt").write_bytes(a_content)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0
        assert "fileA.txt" in bb_server_files(bb_bucket)

        # When a second remote writer (emulated via the public REST API) creates a different file
        b_content = b"from-client-B\n"
        bb_server_upload(bb_bucket, "fileB.txt", b_content)

        # Then client A's next sync converges on both files (no conflict, different paths)
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        assert (Path(local_dir) / "fileA.txt").read_bytes() == a_content
        assert (Path(local_dir) / "fileB.txt").read_bytes() == b_content
        assert {"fileA.txt", "fileB.txt"}.issubset(bb_server_files(bb_bucket))


class TestRemoteTombstoneCatchUp:
    def test_offline_server_delete_cleaned_up_on_resync(
        self,
        live_server,
        bb_bucket,
        local_dir,
        run_sync_once,
        bb_server_upload,
        bb_server_files,
    ):
        # Given a file synced to the local dir
        content = b"to-be-deleted-remotely\n"
        bb_server_upload(bb_bucket, "catchup.txt", content)
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0
        assert (Path(local_dir) / "catchup.txt").exists()

        # When the file is deleted on the server (recording a tombstone) while
        # the client is offline, then the client resyncs
        _server_delete(live_server, bb_bucket, "catchup.txt")
        result = run_sync_once(bb_bucket, local_dir)

        # Then the local copy is removed (tombstone catch-up): a single
        # tombstone-confirmed delete must not be blocked by the mass-delete guard
        assert result.exit_code == 0
        assert not (Path(local_dir) / "catchup.txt").exists()
        assert "catchup.txt" not in bb_server_files(bb_bucket)
