"""`keep` must silence the deletion batch before the mass-deletion guard sees it.

A regression guard for the ordering inside ``sync()``: the guard used to run
first, so a `keep` device could emit ``sync_paused`` and exit with the
deletion-guard code for a batch the policy was never going to apply.
"""

import asyncio
from unittest.mock import patch

from hsync_common.models.file import File
from hsync_common.protocol.events import UiEventName

from hsync_client import sync as sync_module
from hsync_client.sync import SyncAction, SyncActionName

NAMES = ("a.txt", "b.txt", "c.txt")


def _run_cycle(tmp_path, policy: str, threshold: int):
    """Run one cycle over three unconfirmed local-only deletions."""
    actions = []
    for name in NAMES:
        path = tmp_path / name
        path.write_text("x\n")
        file = File.get(str(path))
        assert file is not None
        actions.append(SyncAction(file, SyncActionName.delete))

    events: list[tuple[UiEventName, dict]] = []
    config = {
        "LOCAL_BUCKET": str(tmp_path),
        "SYNC_DIRECTION": "bidirectional",
        "DELETION_POLICY": policy,
        "MASS_DELETE_THRESHOLD": threshold,
        "MASS_DELETE_PERCENT": 0,
    }

    with (
        patch("hsync_client.sync.CONFIG", config),
        patch("hsync_client.sync.get_files_local", return_value=[]),
        patch("hsync_client.sync.get_files_remote", return_value=[]),
        patch("hsync_client.sync.get_sync_actions", return_value=actions),
        patch("hsync_client.sync.get_tombstones_remote", return_value={}),
        patch("hsync_client.sync.push_tombstones_remote"),
        patch("hsync_client.sync.emit_folder_size"),
        patch("hsync_client.sync._cleanup_partial_files"),
        patch("hsync_client.sync.ensure_hsynckeep_files"),
        patch("hsync_client.sync.sync_state"),
        patch(
            "hsync_client.sync.emit_event",
            side_effect=lambda name, **kw: events.append((name, kw)),
        ),
    ):
        result = asyncio.run(sync_module.sync())
    return result, events


def test_keep_never_trips_the_guard(tmp_path):
    # One deletion is enough to trip the guard: without the policy short-circuit
    # this cycle would pause and exit with the deletion-guard code.
    result, events = _run_cycle(tmp_path, "keep", threshold=1)

    assert result.guard_tripped is False
    assert result.deleted == 0
    assert [name for name, _ in events if name == UiEventName.sync_paused] == []
    assert all((tmp_path / name).exists() for name in NAMES)
    # The cycle still reports its outcome to the front-end.
    assert [name for name, _ in events if name == UiEventName.sync_complete]


def test_propagate_still_applies_the_deletions(tmp_path):
    result, events = _run_cycle(tmp_path, "propagate", threshold=0)

    assert result.deleted == len(NAMES)
    assert result.guard_tripped is False
    assert all(not (tmp_path / name).exists() for name in NAMES)
    assert [name for name, _ in events if name == UiEventName.sync_paused] == []


def test_the_guard_still_trips_for_the_same_batch(tmp_path):
    """Sanity check: the threshold these tests use is really reachable."""
    result, events = _run_cycle(tmp_path, "propagate", threshold=1)

    assert result.guard_tripped is True
    assert result.deleted == 0
    assert [name for name, _ in events if name == UiEventName.sync_paused]
    assert all((tmp_path / name).exists() for name in NAMES)
