# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The store's policy values are the shared contract's, not local copies.

``hsync-common`` publishes the trash contract (``models/trash.py::build_trash_contract``,
fixture ``trash-contract``) and the Android store asserts it too, so the client
must not re-declare a number or a refusal the other side also enforces.
"""

from __future__ import annotations

import inspect
import json

import pytest
from hsync_common.models.trash import (
    TRASH_RETENTION_DAYS_DEFAULT,
    TRASH_SWEEP_INTERVAL_SECONDS,
    TRASH_UNRECOVERABLE_RESTORE,
    TREE_TRASH_MAX_BYTES,
    TREE_TRASH_MAX_FILES,
    build_trash_contract,
)

from hsync_client import trash as trash_mod

_FOLDER_ID = "5f2a1c3e-1234-4abc-9def-0123456789ab"


def _store(tmp_path, folder):
    settings = trash_mod.trash_settings({"TRASH_DIR": str(tmp_path / "trash")})
    return trash_mod.TrashStore(folder, settings=settings)


def test_the_tree_caps_are_the_shared_ones():
    assert trash_mod.TREE_TRASH_MAX_FILES == TREE_TRASH_MAX_FILES
    assert trash_mod.TREE_TRASH_MAX_BYTES == TREE_TRASH_MAX_BYTES


def test_the_unrecoverable_refusals_are_the_shared_ones():
    contract = build_trash_contract()

    assert trash_mod.TRASH_UNRECOVERABLE_RESTORE == TRASH_UNRECOVERABLE_RESTORE
    assert set(trash_mod.TRASH_UNRECOVERABLE_RESTORE) <= set(contract["restore_statuses"])


def test_the_sweep_interval_default_is_the_shared_one():
    parameter = inspect.signature(trash_mod.TrashStore.prune_if_due).parameters["interval_seconds"]

    assert parameter.default == TRASH_SWEEP_INTERVAL_SECONDS


def test_the_configured_retention_is_the_shared_default():
    assert trash_mod.trash_settings({}).retention_days == TRASH_RETENTION_DAYS_DEFAULT


def test_the_written_record_carries_exactly_the_contract_keys(tmp_path):
    folder = tmp_path / "share"
    (folder / "docs").mkdir(parents=True)
    (folder / ".hsyncfolder").write_text(_FOLDER_ID, encoding="utf-8")
    target = folder / "docs/report.txt"
    target.write_text("payload", encoding="utf-8")
    store = _store(tmp_path, folder)

    assert store.move(target).trashed is True

    record = json.loads((store.entries()[0].directory / "entry.json").read_text(encoding="utf-8"))
    assert sorted(record) == build_trash_contract()["entry"]["keys"]


def _seed_sweep_tree(root, folder_id, case):
    """Write one sweep case onto disk: entry.json plus a payload of its size."""
    for entry in case["entries"]:
        directory = root / folder_id / entry["entry_id"]
        payload = directory / "payload" / "docs" / "file.bin"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"x" * entry["size"])
        record = {
            "relpath": "docs/file.bin",
            "sha256": "a" * 64,
            "size": entry["size"],
            "deleted_at": entry["deleted_at"],
            "origin": "sync",
        }
        (directory / "entry.json").write_text(json.dumps(record), encoding="utf-8")
    for broken in case["broken"]:
        _seed_broken(root, folder_id, broken, case["now"])


def _seed_broken(root, folder_id, broken, now):
    """Write one unusable entry the two stores must both discard.

    The record's ``deleted_at`` is irrelevant: the sweep drops the directory
    because it cannot be restored, not because of its age.
    """
    directory = root / folder_id / broken["entry_id"]
    payload = directory / "payload" / "docs" / "file.bin"
    payload.parent.mkdir(parents=True)
    kind = broken["kind"]
    if kind == "no-record":
        payload.write_bytes(b"x")
        return
    if kind == "payload-mismatch":
        # A payload that no longer matches the recorded size is as unusable as a
        # missing one: the restore could only refuse it.
        payload.write_bytes(b"x" * 4)
    elif kind != "payload-missing":
        # An unknown kind must fail here, never be seeded as something else.
        raise ValueError(f"unknown broken entry kind: {kind!r}")
    # "payload-missing" leaves the bytes absent on purpose: the record is there.
    (directory / "entry.json").write_text(
        json.dumps(
            {
                "relpath": "docs/file.bin",
                "sha256": "a" * 64,
                "size": 3,
                "deleted_at": now,
                "origin": "sync",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("case", build_trash_contract()["prune_cases"], ids=lambda c: c["why"])
def test_every_sweep_case_produces_the_shared_plan(tmp_path, case):
    root = tmp_path / "trash"
    _seed_sweep_tree(root, _FOLDER_ID, case)
    settings = trash_mod.TrashSettings(
        root=root,
        retention_days=case["retention_days"],
        max_size=case["budget"],
    )

    report = trash_mod.prune_store(settings, now=case["now"])

    assert report.expired == len(case["expired"])
    assert report.evicted == len(case["evicted"])
    assert report.discarded == case["discarded"]
    assert report.kept == case["kept"]
    assert report.freed == case["freed"]
    remaining = sorted(p.name for p in (root / _FOLDER_ID).iterdir() if p.is_dir())
    assert remaining == sorted(
        entry["entry_id"]
        for entry in case["entries"]
        if entry["entry_id"] not in set(case["expired"]) | set(case["evicted"])
    )
