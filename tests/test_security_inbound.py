# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Security regression tests for untrusted remote input.

Covers the inbound-validation hardening: remote peers (central server, LAN
master or any LAN client) must not be able to steer local filesystem
operations outside the bucket, rewrite engine state files, plant setuid
binaries or exhaust resources with decompression bombs.
"""

import io
import stat
import time
from hashlib import sha256
from unittest.mock import MagicMock, patch

import pytest
import zstandard as zstd
from hsync_common.models.file import File

from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.lan_server import LANServer
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestInboundEventPathValidation:
    """apply_inbound_event must drop unsafe remote paths (remote + LAN slave)."""

    async def test_traversal_delete_rejected(self, tmp_path):
        """A deleted event with ../ must not remove files outside the bucket."""
        outside = tmp_path.parent / "outside_keep.txt"
        outside.write_text("keep me")
        event = {
            "name": "deleted",
            "src": {"filepath": "../outside_keep.txt", "sha256sum": "x"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
        assert outside.exists()

    async def test_absolute_path_rename_rejected(self, tmp_path):
        src_file = tmp_path / "src.txt"
        src_file.write_text("content")
        event = {
            "name": "renamed",
            "src": {"filepath": "src.txt"},
            "dst": {"filepath": "/tmp/evil_dst.txt"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
        assert src_file.exists()
        assert not (tmp_path / "evil_dst.txt").exists()

    async def test_backslash_path_rejected(self, tmp_path):
        """Windows-separator differential must never reach the filesystem."""
        event = {
            "name": "deleted",
            "src": {"filepath": "..\\..\\evil.txt"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
        # Nothing to assert on disk: the point is no exception and no delete.
        assert (tmp_path).exists()

    @pytest.mark.parametrize(
        "internal", [".hsyncignore", ".hsyncstate", ".hsynctombstones", "sub/.hsyncignore"]
    )
    async def test_internal_engine_files_rejected(self, tmp_path, internal):
        """Engine state files must never be written via the sync channel."""
        event = {
            "name": "added",
            "src": {"filepath": internal, "sha256sum": "a" * 64, "size": 1, "mtime": 1},
        }
        with (
            patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_update.assert_not_called()
        assert not (tmp_path / internal).exists()

    async def test_security_rejected_event_emitted(self, tmp_path):
        event = {
            "name": "deleted",
            "src": {"filepath": "../evil.txt"},
        }
        with (
            patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.event_dispatch.emit_event") as mock_emit,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
        from hsync_common.protocol.events import UiEventName

        names = [c.args[0] for c in mock_emit.call_args_list]
        assert UiEventName.security_rejected in names

    async def test_valid_event_still_applied(self, tmp_path):
        """A well-formed event keeps working (no false positive)."""
        src_dir_event = {
            "name": "added",
            "src": {"path": "docs/sub", "permissions": "755"},
        }
        with (
            patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            await apply_inbound_event(src_dir_event, REMOTE_DISPATCH_CTX)
        assert (tmp_path / "docs" / "sub").is_dir()

    async def test_added_directory_logs_create_line(self, tmp_path):
        """Regression: an inbound `added` directory must emit a [CREATE] log line.

        Before the fix, ``_apply_add_or_modify`` delegated to ``update_dir``
        without logging; directories appeared in the log only via the
        subsequent `.hsynckeep` sentinel upload, with no line for the
        directory creation itself.
        """
        from loguru import logger

        from hsync_client.logger import _format_record_plain

        src_dir_event = {
            "name": "added",
            "src": {"path": "newdir", "permissions": "755"},
        }

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(sink, level="INFO", format=_format_record_plain, catch=False)
        try:
            with (
                patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
                patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            ):
                await apply_inbound_event(src_dir_event, REMOTE_DISPATCH_CTX)
        finally:
            logger.remove(handler_id)

        assert (tmp_path / "newdir").is_dir()
        # The directory create must be logged (label resolves to [CREATE]
        # because REMOTE_DISPATCH_CTX.log_prefix == "").
        assert any("[CREATE]" in m and "newdir" in m for m in captured)

    async def test_added_directory_rejected_by_update_dir_not_logged_as_create(self, tmp_path):
        """A security-rejected directory must not be logged as [CREATE].

        ``update_dir`` performs the symlink-escape check (``resolve_within``)
        and returns ``None`` with a ``[SECURITY]`` line if a remote path
        escapes the bucket via an in-bucket symlink. The new log line in
        ``_apply_add_or_modify`` must be gated on that success so a rejected
        directory is not misleadingly logged as ``[CREATE]`` first.
        """
        from loguru import logger

        from hsync_client.logger import _format_record_plain

        src_dir_event = {
            "name": "added",
            "src": {"path": "evil", "permissions": "755"},
        }

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(sink, level="INFO", format=_format_record_plain, catch=False)
        try:
            with (
                patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
                patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
                patch("hsync_client.event_dispatch.update_dir", return_value=None),
            ):
                await apply_inbound_event(src_dir_event, REMOTE_DISPATCH_CTX)
        finally:
            logger.remove(handler_id)

        assert not (tmp_path / "evil").exists()
        # No [CREATE] line must be emitted for a rejected directory.
        assert not any("[CREATE]" in m for m in captured)


class TestSymlinkTargetValidation:
    """_apply_symlink must refuse targets escaping the bucket."""

    async def test_absolute_target_outside_bucket_rejected(self, tmp_path):
        ctx = EventDispatchContext(create_symlink_locally=True)
        event = {
            "name": "symlink",
            "src": {"type": "symlink", "filepath": "link.txt", "target": "/etc/passwd"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, ctx)
        assert not (tmp_path / "link.txt").exists()

    async def test_relative_target_escaping_bucket_rejected(self, tmp_path):
        ctx = EventDispatchContext(create_symlink_locally=True)
        event = {
            "name": "symlink",
            "src": {"type": "symlink", "filepath": "sub/link.txt", "target": "../../out"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, ctx)
        assert not (tmp_path / "sub" / "link.txt").exists()

    async def test_target_within_bucket_allowed(self, tmp_path):
        ctx = EventDispatchContext(create_symlink_locally=True)
        target = tmp_path / "real.txt"
        target.write_text("data")
        event = {
            "name": "symlink",
            "src": {"type": "symlink", "filepath": "link.txt", "target": "real.txt"},
        }
        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            await apply_inbound_event(event, ctx)
        link = tmp_path / "link.txt"
        assert link.is_symlink()
        assert link.readlink().name == "real.txt"


class TestLocalDestPath:
    """transfer._local_dest_path confines downloads to the bucket."""

    def test_traversal_returns_none(self, tmp_path):
        from hsync_client.transfer import _local_dest_path

        f = File(filepath="../evil.txt", sha256sum="a" * 64, size=1)
        with patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert _local_dest_path(f) is None

    def test_absolute_returns_none(self, tmp_path):
        from hsync_client.transfer import _local_dest_path

        f = File(filepath="/etc/passwd", sha256sum="a" * 64, size=1)
        with patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert _local_dest_path(f) is None

    def test_internal_name_returns_none(self, tmp_path):
        from hsync_client.transfer import _local_dest_path

        f = File(filepath=".hsyncignore", sha256sum="a" * 64, size=1)
        with patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert _local_dest_path(f) is None

    def test_valid_relative_resolved(self, tmp_path):
        from hsync_client.transfer import _local_dest_path

        f = File(filepath="sub/file.txt", sha256sum="a" * 64, size=1)
        with patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            dest = _local_dest_path(f)
        assert dest == str((tmp_path / "sub" / "file.txt").resolve())

    def test_download_file_refuses_unsafe_path(self, tmp_path):
        from hsync_client.transfer import download_file

        f = File(filepath="../evil.txt", sha256sum="a" * 64, size=1)
        with (
            patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.get") as mock_get,
        ):
            download_file(f)
            mock_get.assert_not_called()


class TestDownloadDecompressionBomb:
    """Compressed downloads must be capped at the declared size."""

    def test_oversized_compressed_payload_rejected(self, tmp_path):
        from hsync_client.transfer import download_file

        # A 1 KB frame expanding to 5 MB, while the listing declares 100 bytes.
        content = b"B" * 5_000_000
        compressed = zstd.ZstdCompressor().compress(content)
        assert len(compressed) < 20_000

        f = File(filepath="bomb.txt", sha256sum="a" * 64, size=100)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": "zstd"}
        mock_response.raw = io.BytesIO(compressed)

        config = {
            "API": {"buckets": "http://localhost:1/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {},
            "LOCAL_BUCKET": str(tmp_path),
            "TRANSFER_RETRY_ATTEMPTS": 1,
        }
        with (
            patch("hsync_client.transfer.CONFIG", config),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(f)

        # Neither the final file nor the partial artifact may survive.
        assert not (tmp_path / "bomb.txt").exists()
        assert not (tmp_path / "bomb.txt.part").exists()


class TestListingValidation:
    """get_sync_actions must skip unsafe listing entries."""

    def _run(self, remote_files, tmp_path):
        from hsync_client.sync import get_sync_actions

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            return get_sync_actions(remote_files, [])

    def test_traversal_entry_skipped(self, tmp_path):
        actions = self._run(
            [{"filepath": "../evil.txt", "size": 1, "mtime": 1, "sha256sum": "a" * 64}],
            tmp_path,
        )
        assert actions == []

    def test_internal_name_entry_skipped(self, tmp_path):
        actions = self._run(
            [{"filepath": ".hsyncignore", "size": 1, "mtime": 1, "sha256sum": "a" * 64}],
            tmp_path,
        )
        assert actions == []

    def test_valid_entry_kept(self, tmp_path):
        actions = self._run(
            [{"filepath": "ok.txt", "size": 1, "mtime": 1, "sha256sum": "a" * 64}],
            tmp_path,
        )
        assert len(actions) == 1

    def test_uncomparable_entry_counts_as_remotely_present(self, tmp_path):
        """A listing entry that cannot be compared (local stat OSError) must
        be skipped for comparison but still count as remotely present —
        otherwise a download-only slave would DELETE the local copy."""
        from hsync_client.sync import get_sync_actions

        local = tmp_path / "present.txt"
        local.write_text("keep me")
        remote_entry = {"filepath": "present.txt", "size": 1, "mtime": 1, "sha256sum": "a" * 64}

        with (
            patch(
                "hsync_client.sync.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "download-only"},
            ),
            patch("hsync_client.sync.File.get", side_effect=OSError("EMFILE")),
        ):
            actions = get_sync_actions(
                [remote_entry],
                [{"filepath": "present.txt"}],
                lan_mode=True,
            )

        assert all(a.action != "delete" for a in actions)
        assert actions == []


class TestUpdateGuards:
    def test_update_file_rejects_traversal(self, tmp_path):
        from hsync_client.sync import update_file

        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file") as mock_dl,
        ):
            update_file({"filepath": "../evil.txt", "sha256sum": "a" * 64})
            mock_dl.assert_not_called()

    def test_update_dir_rejects_traversal(self, tmp_path):
        from hsync_client.sync import update_dir

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert update_dir({"path": "../evil"}) is None
        assert not (tmp_path.parent / "evil").exists()


class TestRemotePermissionMask:
    """set_permissions strips special bits from wire-supplied values."""

    def test_setuid_bit_stripped(self, tmp_path):
        from hsync_client.utils import set_permissions

        f = tmp_path / "script.sh"
        f.write_text("#!/bin/sh\n")
        set_permissions(str(f), "4755")
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o755
        assert not (mode & stat.S_ISUID)

    def test_sticky_and_setgid_stripped(self, tmp_path):
        from hsync_client.utils import set_permissions

        d = tmp_path / "dir"
        d.mkdir()
        set_permissions(str(d), "3777")
        assert stat.S_IMODE(d.stat().st_mode) == 0o777

    def test_plain_permissions_unchanged(self, tmp_path):
        from hsync_client.utils import set_permissions

        f = tmp_path / "plain.txt"
        f.write_text("x")
        set_permissions(str(f), "640")
        assert stat.S_IMODE(f.stat().st_mode) == 0o640


# =============================================================================
# LAN master HTTP surface
# =============================================================================


class TestLANServerHardening:
    """LAN master endpoints must confine and verify peer-supplied input."""

    @pytest.fixture
    def server(self):
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_download_sibling_prefix_bypass_blocked(self, server, tmp_path):
        """Bucket /data/sync must not leak /data/sync-evil (startswith bug)."""
        import requests

        bucket = tmp_path / "sync"
        bucket.mkdir()
        evil = tmp_path / "sync-evil"
        evil.mkdir()
        (evil / "secret.txt").write_text("secret")

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            response = requests.get(url, params={"filename": "../sync-evil/secret.txt"}, timeout=5)
            assert response.status_code in (400, 403, 404)
            assert b"secret" not in response.content

    def test_delete_sibling_prefix_bypass_blocked(self, server, tmp_path):
        import requests

        bucket = tmp_path / "sync"
        bucket.mkdir()
        evil = tmp_path / "sync-evil"
        evil.mkdir()
        (evil / "keep.txt").write_text("keep")

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "../sync-evil"}, timeout=5)
            assert response.status_code == 400
            assert (evil / "keep.txt").exists()

    def test_rename_sibling_prefix_bypass_blocked(self, server, tmp_path):
        import requests

        bucket = tmp_path / "sync"
        bucket.mkdir()
        (bucket / "a.txt").write_text("a")
        evil = tmp_path / "sync-evil"
        evil.mkdir()

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "a.txt", "dst": "../sync-evil/a.txt"}, timeout=5
            )
            assert response.status_code == 400
            assert (bucket / "a.txt").exists()
            assert not (evil / "a.txt").exists()

    def test_upload_traversal_rejected(self, server, tmp_path):
        import requests

        bucket = tmp_path / "sync"
        bucket.mkdir()
        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(bucket), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "t",
                "path": "..",
                "mtime": "",
                "ctime": "",
                "sha256sum": sha256(b"x").hexdigest(),
            }
            files = {"file": ("evil.txt", b"x")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code in (400, 403)
            assert not (tmp_path / "evil.txt").exists()

    def test_upload_internal_name_rejected(self, server, tmp_path):
        import requests

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "t",
                "path": "",
                "mtime": "",
                "ctime": "",
                "sha256sum": sha256(b"*").hexdigest(),
            }
            files = {"file": (".hsyncignore", b"*")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code in (400, 403)
            assert not (tmp_path / ".hsyncignore").exists()

    def test_upload_checksum_mismatch_rejected(self, server, tmp_path):
        import requests

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "t",
                "path": "",
                "mtime": "",
                "ctime": "",
                "sha256sum": "0" * 64,
            }
            files = {"file": ("f.txt", b"real content")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 400
            assert not (tmp_path / "f.txt").exists()

    def test_upload_missing_checksum_rejected(self, server, tmp_path):
        import requests

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {"cid": "t", "path": "", "mtime": "", "ctime": ""}
            files = {"file": ("f.txt", b"real content")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 400
            assert not (tmp_path / "f.txt").exists()

    def test_upload_compressed_bomb_rejected(self, server, tmp_path):
        """A compressed upload expanding past the declared size is dropped."""
        import requests

        content = b"C" * 1_000_000
        compressed = zstd.ZstdCompressor().compress(content)

        srv, port = server
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "t",
                "path": "",
                "mtime": "",
                "ctime": "",
                "compressed": "true",
                "size": "100",  # lie: real content is 1 MB
                "sha256sum": sha256(content).hexdigest(),
            }
            files = {"file": ("bomb.txt", compressed)}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 400
            assert not (tmp_path / "bomb.txt").exists()
            assert not (tmp_path / "bomb.txt.part").exists()

    def test_tombstone_payload_size_capped(self, server, tmp_path):
        import requests

        srv, port = server
        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.lan_server.MAX_TOMBSTONE_PAYLOAD_BYTES", 1024),
            patch("hsync_client.tombstones.merge_payload", return_value=0) as mock_merge,
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            big = {f"{i:064x}": {"ts": int(time.time()), "sha256": None} for i in range(50)}
            response = requests.post(url, json=big, timeout=5)
            assert response.status_code == 400
            mock_merge.assert_not_called()

    def test_far_future_mtime_clamped(self, server, tmp_path):
        import requests

        srv, port = server
        future = str(time.time() + 10_000_000)
        with patch(
            "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "t",
                "path": "",
                "mtime": future,
                "ctime": "",
                "sha256sum": sha256(b"x").hexdigest(),
            }
            files = {"file": ("f.txt", b"x")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 201
            mtime = (tmp_path / "f.txt").stat().st_mtime
            assert mtime <= time.time() + 400
