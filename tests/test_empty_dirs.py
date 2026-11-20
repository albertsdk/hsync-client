# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the shared empty-directory status rule (``empty_dirs``)."""

from __future__ import annotations

from hsync_client.empty_dirs import empty_dir_states, file_identities

BUCKET = "/share"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _local(rel: str, size: int = 0, *, bucket: str = BUCKET) -> dict:
    return {"filepath": f"{bucket}/{rel}", "sha256sum": EMPTY_SHA, "size": size}


def _peer(rel: str, size: int = 0) -> dict:
    return {"filepath": rel, "sha256sum": EMPTY_SHA, "size": size}


def _held(*entries: dict) -> dict[tuple[str, str], int]:
    return file_identities(entries, BUCKET)


class TestFileIdentities:
    def test_keeps_content_and_markers(self):
        identities = _held(
            _peer("a/.hsynckeep"),
            _peer("a/file.txt", size=3),
            {"filepath": "a/link", "type": "symlink", "sha256sum": "ignored", "size": 0},
        )

        assert set(identities) == {
            ("a/.hsynckeep", EMPTY_SHA),
            ("a/file.txt", EMPTY_SHA),
        }


class TestEmptyDirStates:
    """Only a directory that is empty on both sides has a verdict."""

    def test_a_local_only_marker_is_to_upload(self):
        states = empty_dir_states({}, [_local("empty/.hsynckeep")], BUCKET)

        assert states == [("to_upload", "empty")]

    def test_a_marker_held_by_the_peer_is_synced(self):
        local = [_local("empty/.hsynckeep")]

        assert empty_dir_states(_held(_peer("empty/.hsynckeep")), local, BUCKET) == [
            ("synced", "empty")
        ]

    def test_a_peer_only_marker_is_to_download(self):
        assert empty_dir_states(_held(_peer("empty/.hsynckeep")), [], BUCKET) == [
            ("to_download", "empty")
        ]

    def test_a_stale_marker_in_a_populated_local_dir_is_ignored(self):
        """A marker left behind by a momentary emptiness is not emptiness."""
        local = [_local("album/.hsynckeep"), _local("album/track.txt", size=10)]

        assert empty_dir_states(_held(_peer("album/.hsynckeep")), local, BUCKET) == []

    def test_a_marker_of_a_dir_the_peer_filled_is_ignored(self):
        local = [_local("album/.hsynckeep")]
        held = _held(_peer("album/.hsynckeep"), _peer("album/track.txt", size=10))

        assert empty_dir_states(held, local, BUCKET) == []

    def test_a_peer_marker_for_a_peer_populated_dir_is_not_a_download(self):
        held = _held(_peer("album/.hsynckeep"), _peer("album/track.txt", size=10))

        assert empty_dir_states(held, [], BUCKET) == []

    def test_content_in_a_sibling_dir_does_not_suppress_the_verdict(self):
        local = [
            _local("parent/empty/.hsynckeep"),
            _local("parent/full/file.txt", size=4),
        ]

        assert empty_dir_states({}, local, BUCKET) == [("to_upload", "parent/empty")]

    def test_the_bucket_root_marker_has_no_row(self):
        """A root ``.hsynckeep`` names no directory: it must never become one."""
        local = [_local(".hsynckeep")]

        assert empty_dir_states({}, local, BUCKET) == []
        assert empty_dir_states(_held(_peer(".hsynckeep")), [], BUCKET) == []

    def test_a_directory_holding_only_subdirectories_is_not_empty(self, tmp_path):
        """A subdirectory is content, exactly as it is for the marker's rule."""
        (tmp_path / "album" / "child").mkdir(parents=True)
        (tmp_path / "album" / ".hsynckeep").touch()
        local = [_local("album/.hsynckeep", bucket=str(tmp_path))]

        assert empty_dir_states({}, local, str(tmp_path)) == []

    def test_an_on_disk_empty_directory_is_still_reported(self, tmp_path):
        (tmp_path / "album").mkdir()
        (tmp_path / "album" / ".hsynckeep").touch()
        local = [_local("album/.hsynckeep", bucket=str(tmp_path))]

        assert empty_dir_states({}, local, str(tmp_path)) == [("to_upload", "album")]
