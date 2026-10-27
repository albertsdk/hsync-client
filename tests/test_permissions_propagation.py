# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests for permission-only (chmod) propagation.

A ``chmod`` changes ``st_mode`` without touching mtime/size or the content
hash; historically the watcher dedup and the reconcile engine discarded it
entirely. These tests pin the behaviour that a permission-only change
propagates as a normal ``modified`` change (no special policy).
"""

import time
from unittest.mock import MagicMock, patch

from hsync_common.models.sync_action import SyncActionName

from hsync_client.file_event_handler import BaseFileEventHandler
from hsync_client.remote_websocket import RemoteFileEventHandler


class _DummyStat:
    def __init__(self, mtime_ns: int, size: int, mode: int) -> None:
        self.st_mtime_ns = mtime_ns
        self.st_size = size
        self.st_mode = mode


class _RecordingHandler(BaseFileEventHandler):
    def _broadcast_event(self, event, src_abs_path=None, dst_abs_path=None): ...

    def _on_file_ready(self, file, src_path, from_closed=False, *, trigger=None): ...

    def _on_permissions_changed(self, file, src_path):
        self.permissions_calls.append((file, src_path))


def test_stat_key_includes_mode():
    handler = _RecordingHandler()
    assert handler._stat_key(_DummyStat(1, 100, 0o100644)) == (1, 100, 0o644)
    assert handler._stat_key(_DummyStat(1, 100, 0o100600)) != handler._stat_key(
        _DummyStat(1, 100, 0o100644)
    )


def test_process_permission_only_triggers_hook_on_mode_change():
    handler = _RecordingHandler()
    handler.permissions_calls = []
    src = "/bucket/file.txt"
    with (
        patch(
            "hsync_client.file_event_handler.Path.stat", return_value=_DummyStat(5, 10, 0o100600)
        ),
        patch(
            "hsync_client.file_event_handler.File.get",
            return_value=MagicMock(),
        ) as mock_get,
        patch("hsync_client.file_event_handler.CONFIG", {"SYNC_DIRECTION": "bidirectional"}),
        patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
    ):
        handler._last_mode[src] = 0o644  # previously 644
        handler._process_permission_only(src)
    mock_get.assert_called_once()
    assert len(handler.permissions_calls) == 1
    assert handler._last_mode[src] == 0o600


def test_process_permission_only_noop_on_same_mode():
    handler = _RecordingHandler()
    handler.permissions_calls = []
    src = "/bucket/file.txt"
    with (
        patch(
            "hsync_client.file_event_handler.Path.stat", return_value=_DummyStat(5, 10, 0o100644)
        ),
        patch("hsync_client.file_event_handler.File.get") as mock_get,
        patch("hsync_client.file_event_handler.CONFIG", {"SYNC_DIRECTION": "bidirectional"}),
        patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
    ):
        handler._last_mode[src] = 0o644  # same mode
        handler._process_permission_only(src)
    mock_get.assert_not_called()
    assert handler.permissions_calls == []


def test_process_permission_only_first_observation_records_only():
    handler = _RecordingHandler()
    handler.permissions_calls = []
    src = "/bucket/file.txt"
    with (
        patch(
            "hsync_client.file_event_handler.Path.stat", return_value=_DummyStat(5, 10, 0o100644)
        ),
        patch("hsync_client.file_event_handler.File.get") as mock_get,
        patch("hsync_client.file_event_handler.CONFIG", {"SYNC_DIRECTION": "bidirectional"}),
        patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
    ):
        handler._process_permission_only(src)
    mock_get.assert_not_called()
    assert handler.permissions_calls == []
    assert handler._last_mode[src] == 0o644


def _mock_engine(sync_mod, *, direction: str):
    """Patch CONFIG/state/File.get for one permission-only engine cycle."""
    from contextlib import ExitStack

    local_file = MagicMock()
    local_file.filepath = "/bucket/doc.txt"
    local_file.size = 4
    local_file.mtime = 1000.0
    local_file.sha256sum = "a" * 64
    local_file.permissions = "644"

    stack = ExitStack()
    stack.enter_context(
        patch.object(
            sync_mod,
            "CONFIG",
            {
                "LOCAL_BUCKET": "/bucket",
                "REMOTE_BUCKET": "demo",
                "SYNC_DIRECTION": direction,
                "CID": "TestClientX1234567890",
                "DELETION_POLICY": "propagate",
            },
        )
    )
    stack.enter_context(
        patch.object(
            sync_mod.sync_state,
            "local_version_for_compare",
            return_value={"vclock": {}, "corrected_mtime": 0},
        )
    )
    stack.enter_context(patch.object(sync_mod.sync_state, "get_base_hash", return_value="a" * 64))
    stack.enter_context(patch.object(sync_mod.File, "get", return_value=local_file))
    return stack


def _remote_local_lists():
    remote = [
        {
            "type": "file",
            "filepath": "doc.txt",
            "size": 4,
            "mtime": 1000.0,
            "sha256sum": "a" * 64,
            "permissions": "600",
        }
    ]
    local = [
        {
            "type": "file",
            "filepath": "/bucket/doc.txt",
            "size": 4,
            "mtime": 1000.0,
            "sha256sum": "a" * 64,
            "permissions": "644",
        }
    ]
    return remote, local


def test_get_sync_actions_uploads_on_permission_divergence():
    from hsync_client import sync as sync_mod

    remote, local = _remote_local_lists()
    with _mock_engine(sync_mod, direction="bidirectional"):
        actions = sync_mod.get_sync_actions(remote_files=remote, local_files=local)
    names = [a.name for a in actions]
    assert SyncActionName.upload in names
    assert SyncActionName.nothing not in names


def test_get_sync_actions_download_only_converges_locally():
    from hsync_client import sync as sync_mod

    remote, local = _remote_local_lists()
    with _mock_engine(sync_mod, direction="download-only"):
        with patch.object(sync_mod, "set_permissions") as mock_set:
            actions = sync_mod.get_sync_actions(remote_files=remote, local_files=local)
    names = [a.name for a in actions]
    assert SyncActionName.upload not in names
    mock_set.assert_called_once_with("/bucket/doc.txt", "600")


def test_get_sync_actions_preview_does_not_touch_permissions():
    """persist_state=False (preview/dry-run) must not mutate local permissions.

    The download-only permission converge writes the remote mode to disk;
    a preview must compute decisions without side effects, exactly like the
    merge-base refresh it mirrors (same purity contract).
    """
    from hsync_client import sync as sync_mod

    remote, local = _remote_local_lists()
    with _mock_engine(sync_mod, direction="download-only"):
        with patch.object(sync_mod, "set_permissions") as mock_set:
            actions = sync_mod.get_sync_actions(
                remote_files=remote, local_files=local, persist_state=False
            )
    names = [a.name for a in actions]
    assert SyncActionName.upload not in names
    mock_set.assert_not_called()


def test_get_sync_actions_preview_keeps_permission_action_silent():
    """persist_state=False must not reclassify a permission divergence as an upload."""
    from hsync_client import sync as sync_mod

    remote, local = _remote_local_lists()
    with _mock_engine(sync_mod, direction="bidirectional"):
        actions = sync_mod.get_sync_actions(
            remote_files=remote, local_files=local, persist_state=False
        )
    names = [a.name for a in actions]
    assert SyncActionName.nothing in names
    assert SyncActionName.upload not in names


def test_remote_handler_permissions_hook_uploads():
    handler = RemoteFileEventHandler(MagicMock(), MagicMock(), None)
    file_obj = MagicMock()
    with patch("hsync_client.remote_websocket.upload_file") as mock_upload:
        handler._on_permissions_changed(file_obj, "/bucket/f.txt")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not mock_upload.called:
            time.sleep(0.01)
    mock_upload.assert_called_once_with(file_obj, permissions_only=True, from_closed=False)
