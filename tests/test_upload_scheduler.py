# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Per-path upload scheduler: serialization and coalescing."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from hsync_client.upload_scheduler import PerPathUploadScheduler


@dataclass(slots=True)
class _FakeFile:
    filepath: str
    tag: str


def test_serializes_per_path_and_coalesces_to_the_latest() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, bool, bool]] = []

    def upload(file, *, permissions_only: bool, from_closed: bool) -> None:
        calls.append((file.tag, permissions_only, from_closed))
        started.set()
        release.wait(5)

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = PerPathUploadScheduler(executor, upload)
        first = scheduler.submit(_FakeFile("/bucket/a.txt", "first"))
        assert first is not None
        assert started.wait(2), "first upload did not start"
        assert scheduler.submit(_FakeFile("/bucket/a.txt", "second")) is None
        assert scheduler.submit(_FakeFile("/bucket/a.txt", "third")) is None
        release.set()
        first.result(5)

    # One attempt in flight; only the newest pending event runs afterwards.
    assert calls == [("first", False, False), ("third", False, False)]


def test_content_event_supersedes_pending_permission_only() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def upload(file, *, permissions_only: bool, from_closed: bool) -> None:
        calls.append(permissions_only)
        if len(calls) == 1:
            started.set()
            release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = PerPathUploadScheduler(executor, upload)
        first = scheduler.submit(_FakeFile("/bucket/a.txt", "first"))
        assert first is not None and started.wait(2)
        scheduler.submit(_FakeFile("/bucket/a.txt", "chmod"), permissions_only=True)
        scheduler.submit(_FakeFile("/bucket/a.txt", "content"))
        release.set()
        first.result(5)

    assert calls == [False, False], "a content event must force a content upload"


def test_permission_only_events_stay_permission_only() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def upload(file, *, permissions_only: bool, from_closed: bool) -> None:
        calls.append(permissions_only)
        if len(calls) == 1:
            started.set()
            release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = PerPathUploadScheduler(executor, upload)
        first = scheduler.submit(_FakeFile("/bucket/a.txt", "first"))
        assert first is not None and started.wait(2)
        scheduler.submit(_FakeFile("/bucket/a.txt", "chmod1"), permissions_only=True)
        scheduler.submit(_FakeFile("/bucket/a.txt", "chmod2"), permissions_only=True)
        release.set()
        first.result(5)

    assert calls == [False, True]


def test_different_paths_upload_concurrently() -> None:
    both_started = threading.Event()
    release = threading.Event()
    started = 0
    lock = threading.Lock()

    def upload(file, *, permissions_only: bool, from_closed: bool) -> None:
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        release.wait(5)

    with ThreadPoolExecutor(max_workers=4) as executor:
        scheduler = PerPathUploadScheduler(executor, upload)
        a = scheduler.submit(_FakeFile("/bucket/a.txt", "a"))
        b = scheduler.submit(_FakeFile("/bucket/b.txt", "b"))
        assert a is not None and b is not None
        assert both_started.wait(2), "distinct paths must not serialize each other"
        release.set()
        a.result(5)
        b.result(5)
