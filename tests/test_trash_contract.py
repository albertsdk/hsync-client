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
