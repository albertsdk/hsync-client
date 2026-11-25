# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: the ``hsync trash`` verbs a front-end drives (ADR-0056).

Only the public CLI is exercised — the JSON listing, the restore verdict, the
exit code and the refusal messages — so the suite still passes if the store is
rewritten inside. The WD OS5 package (``trash.php``) and the desktop's trash
dialog make these same calls, which is what makes this the contract they rely on.

Requires HSYNC_BB_SERVER=1 to run.
"""

import json
import os
import uuid

import pytest
from typer.testing import CliRunner

from hsync_client.cli import app

_BB_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_REASON = "Black-box client tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_ENABLED, reason=_BB_REASON)

runner = CliRunner()


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A synced folder with its marker, plus an isolated store, as a daemon has."""
    path = tmp_path / "shared"
    path.mkdir()
    (path / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
    monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
    return path


def _run(*args):
    return runner.invoke(app, ["trash", *args])


class TestListAndAdd:
    def test_a_parked_file_is_listed_with_its_size(self, folder):
        (folder / "report.txt").write_bytes(b"hello")

        added = _run("add", "--folder", str(folder), "report.txt")
        listed = _run("list", "--folder", str(folder), "--json")

        assert added.exit_code == 0, added.output
        assert not (folder / "report.txt").exists()
        [entry] = json.loads(listed.output)
        assert (entry["path"], entry["size"], entry["payload_present"]) == ("report.txt", 5, True)

    def test_a_path_outside_the_folder_is_refused(self, folder, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"x")

        result = _run("add", "--folder", str(folder), str(outside))

        assert result.exit_code == 1
        assert "not inside the folder" in result.output
        assert outside.exists()

    def test_an_empty_store_lists_nothing(self, folder):
        listed = _run("list", "--folder", str(folder), "--json")

        assert listed.exit_code == 0, listed.output
        assert json.loads(listed.output) == []


class TestRestore:
    def test_the_bytes_come_back_and_the_entry_leaves(self, folder):
        (folder / "report.txt").write_bytes(b"hello")
        _run("add", "--folder", str(folder), "report.txt")

        restored = _run("restore", "--folder", str(folder), "report.txt", "--json")
        listed = _run("list", "--folder", str(folder), "--json")

        assert restored.exit_code == 0, restored.output
        assert json.loads(restored.output)["status"] == "restored"
        assert (folder / "report.txt").read_bytes() == b"hello"
        assert json.loads(listed.output) == []

    def test_an_existing_destination_keeps_the_entry(self, folder):
        (folder / "report.txt").write_bytes(b"hello")
        _run("add", "--folder", str(folder), "report.txt")
        (folder / "report.txt").write_bytes(b"back again")

        restored = _run("restore", "--folder", str(folder), "report.txt", "--json")

        assert restored.exit_code == 1
        assert json.loads(restored.output)["status"] == "destination-exists"
        assert (folder / "report.txt").read_bytes() == b"back again"

    def test_force_replaces_the_file_and_keeps_the_previous_copy(self, folder):
        (folder / "report.txt").write_bytes(b"old")
        _run("add", "--folder", str(folder), "report.txt")
        (folder / "report.txt").write_bytes(b"new")

        restored = _run("restore", "--folder", str(folder), "report.txt", "--force", "--json")
        listed = _run("list", "--folder", str(folder), "--json")

        assert restored.exit_code == 0, restored.output
        report = json.loads(restored.output)
        assert (report["status"], report["replaced"]) == ("restored", True)
        assert (folder / "report.txt").read_bytes() == b"old"
        [kept] = json.loads(listed.output)
        assert kept["path"] == "report.txt"

    def test_a_download_only_folder_refuses(self, folder):
        (folder / "report.txt").write_bytes(b"hello")
        _run("add", "--folder", str(folder), "report.txt")

        restored = _run(
            "restore",
            "--folder",
            str(folder),
            "report.txt",
            "--direction",
            "download-only",
            "--json",
        )

        assert restored.exit_code == 1
        assert json.loads(restored.output)["status"] == "download-only"

    def test_an_unknown_path_reports_no_entry(self, folder):
        result = _run("restore", "--folder", str(folder), "missing.txt", "--json")

        assert result.exit_code == 1
        assert json.loads(result.output)["status"] == "no-entry"


class TestEmptyAndPrune:
    def test_empty_removes_the_entries_and_reports_the_bytes(self, folder):
        (folder / "a.txt").write_bytes(b"aa")
        (folder / "b.txt").write_bytes(b"bbb")
        _run("add", "--folder", str(folder), "a.txt")
        _run("add", "--folder", str(folder), "b.txt")

        emptied = _run("empty", "--folder", str(folder), "--yes", "--json")
        listed = _run("list", "--folder", str(folder), "--json")

        assert emptied.exit_code == 0, emptied.output
        assert json.loads(emptied.output) == {"freed": 5, "removed": 2}
        assert json.loads(listed.output) == []

    def test_prune_keeps_a_fresh_entry(self, folder):
        (folder / "report.txt").write_bytes(b"hello")
        _run("add", "--folder", str(folder), "report.txt")

        swept = _run("prune", "--json")

        assert swept.exit_code == 0, swept.output
        payload = json.loads(swept.output)
        assert (payload["expired"], payload["kept"]) == (0, 1)


class TestStoreLocationGuard:
    def test_a_store_inside_the_folder_stops_the_run(self, folder, monkeypatch):
        """The store is device-local: the client refuses to start with it inside."""
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(folder / ".trash"))

        result = runner.invoke(app, ["p2p", "-b", "shared", "-p", str(folder)])

        assert result.exit_code == 2
        assert "must not live inside the synced folder" in result.output
