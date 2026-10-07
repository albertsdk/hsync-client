# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Focused integration tests for explicit inventory observer hooks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from hsync_common.models.file import File
from requests import RequestException

from hsync_client.config import CONFIG
from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.p2p_inventory import refresh_active_inventory, set_active_inventory_publisher
from hsync_client.sync import sync
from hsync_client.transfer import download_file, upload_file


class RecordingObserver:
    def __init__(self) -> None:
        self.snapshots: list[list] = []
        self.upserts: list[object] = []
        self.removed_files: list[str] = []
        self.removed_prefixes: list[str] = []
        self.renamed_files: list[tuple[str, str]] = []
        self.renamed_prefixes: list[tuple[str, str]] = []

    def replace_snapshot(self, entries) -> None:
        self.snapshots.append(list(entries))

    def upsert(self, entry) -> None:
        self.upserts.append(entry)

    def remove_file(self, path: str) -> None:
        self.removed_files.append(path)

    def remove_prefix(self, prefix: str) -> None:
        self.removed_prefixes.append(prefix)

    def rename_file(self, src: str, dst: str) -> None:
        self.renamed_files.append((src, dst))

    def rename_prefix(self, src: str, dst: str) -> None:
        self.renamed_prefixes.append((src, dst))


def test_ignore_reload_refreshes_active_seeder_snapshot() -> None:
    """The ignore watcher can replace the role-scoped inventory from disk."""
    observer = RecordingObserver()
    local = [{"filepath": "visible.txt", "sha256sum": "a" * 64, "size": 1}]
    set_active_inventory_publisher(observer)  # type: ignore[arg-type]
    try:
        with patch("hsync_client.sync.get_files_local", return_value=local):
            assert refresh_active_inventory() is True
    finally:
        set_active_inventory_publisher(None)

    assert observer.snapshots == [local]


@pytest.mark.asyncio
async def test_sync_replaces_inventory_before_remote_lookup(tmp_path: Path) -> None:
    observer = RecordingObserver()
    local = [{"filepath": "local.txt", "sha256sum": "a" * 64, "size": 1}]
    with (
        patch.dict(CONFIG, {"LOCAL_BUCKET": str(tmp_path)}, clear=False),
        patch("hsync_client.sync._cleanup_partial_files"),
        patch("hsync_client.sync.ensure_hsynckeep_files"),
        patch("hsync_client.sync.get_files_local", return_value=local),
        patch("hsync_client.sync.get_files_remote", side_effect=RuntimeError("stop")),
        pytest.raises(RuntimeError, match="stop"),
    ):
        await sync(p2p_mode=True, inventory_observer=observer)

    assert observer.snapshots == [local]


def test_download_noop_notifies_inventory_after_content_verification(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("content")
    local_file = File.get(str(path))
    assert local_file is not None
    file = File(
        filepath="file.txt",
        ctime=local_file.ctime,
        mtime=local_file.mtime,
        size=local_file.size,
        sha256sum=local_file.sha256sum,
        permissions=local_file.permissions,
    )
    observer = RecordingObserver()

    with patch.dict(
        CONFIG,
        {
            "LOCAL_BUCKET": str(tmp_path),
            "TRANSFER_RETRY_ATTEMPTS": 1,
            "TRANSFER_RETRY_MIN_WAIT": 0,
            "TRANSFER_RETRY_MAX_WAIT": 0,
        },
        clear=False,
    ):
        download_file(file, inventory_observer=observer)

    assert observer.upserts == [file]


def test_stable_upload_notifies_inventory_before_transport_failure(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("content")
    file = File.get(str(path))
    assert file is not None
    observer = RecordingObserver()

    with (
        patch.dict(
            CONFIG,
            {
                "LOCAL_BUCKET": str(tmp_path),
                "TRANSFER_RETRY_ATTEMPTS": 1,
                "TRANSFER_RETRY_MIN_WAIT": 0,
                "TRANSFER_RETRY_MAX_WAIT": 0,
            },
            clear=False,
        ),
        patch("hsync_client.transfer._wait_for_stable_file", return_value=True),
        patch(
            "hsync_client.transfer._upload_file_attempt",
            side_effect=RequestException("offline"),
        ),
    ):
        upload_file(file, inventory_observer=observer)

    assert observer.upserts == [file]


@pytest.mark.asyncio
async def test_inbound_rename_and_delete_update_explicit_observer(tmp_path: Path) -> None:
    observer = RecordingObserver()
    context = EventDispatchContext(inventory_observer=observer)
    source = tmp_path / "source.txt"
    source.write_text("content")
    attrs = {
        "sha256sum": "a" * 64,
        "mtime": 1,
        "ctime": 1,
        "size": 7,
        "permissions": "644",
    }
    rename_event = {
        "name": "renamed",
        "src": {"filepath": "source.txt", **attrs},
        "dst": {"filepath": "dest.txt", **attrs},
    }
    delete_event = {"name": "deleted", "src": {"filepath": "dest.txt", **attrs}}

    with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
        await apply_inbound_event(rename_event, context)
        await apply_inbound_event(delete_event, context)

    destination = str(tmp_path / "dest.txt")
    assert observer.renamed_files == [(str(source), destination)]
    assert observer.removed_files == [destination]
    assert not Path(destination).exists()
