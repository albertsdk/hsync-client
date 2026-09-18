"""Tests for content-unchanged metadata (mtime) propagation.

Covers the reconcile-side divergence rule and the metadata-only PATCH payload
used to push the newer mtime to the peer (a plain ``touch``).
"""

from unittest.mock import MagicMock, patch

from hsync_client import sync as sync_mod
from hsync_client import sync_state as sync_state_mod
from hsync_client import transfer as transfer_mod


class TestMtimeDivergence:
    def test_unknown_values_never_diverge(self):
        assert not sync_state_mod.mtime_diverges(0, 1_700_000_000)
        assert not sync_state_mod.mtime_diverges(1_700_000_000, 0)
        assert not sync_state_mod.mtime_diverges(0, 0)

    def test_within_tolerance_does_not_diverge(self):
        base = 1_700_000_000
        assert not sync_state_mod.mtime_diverges(base, base)
        assert not sync_state_mod.mtime_diverges(base, base + 1)
        assert not sync_state_mod.mtime_diverges(base, base - 1)

    def test_beyond_tolerance_diverges(self):
        base = 1_700_000_000
        assert sync_state_mod.mtime_diverges(base, base + 2)
        assert sync_state_mod.mtime_diverges(base, base - 2)


class TestApplyLocalMtime:
    def test_applies_the_peer_mtime(self, tmp_path):
        target = tmp_path / "file.bin"
        target.write_bytes(b"content")
        new_mtime = 1_700_000_000

        sync_mod._apply_local_mtime(str(target), new_mtime)

        assert int(target.stat().st_mtime) == new_mtime
        assert target.read_bytes() == b"content"

    def test_missing_file_is_ignored(self, tmp_path):
        sync_mod._apply_local_mtime(str(tmp_path / "missing.bin"), 1_700_000_000)


def _config() -> dict:
    return {
        "API": {"buckets": "http://srv/api/v1/buckets"},
        "REMOTE_BUCKET": "demo",
        "REQUEST_HEADERS": {},
        "CID": "TestClientX1234567890",
        "HTTP_TIMEOUT": 30,
    }


class TestPatchPermissionsRemoteMtime:
    @staticmethod
    def _call(mtime: int):
        response = MagicMock()
        response.status_code = 200
        with (
            patch.object(transfer_mod, "patch", return_value=response) as mock_patch,
            patch.object(transfer_mod, "get_ssl_verify", return_value=False),
            patch.object(transfer_mod, "CONFIG", _config()),
        ):
            ok = transfer_mod.patch_permissions_remote("doc.bin", "644", mtime)
        return ok, mock_patch.call_args.kwargs["params"]

    def test_mtime_is_sent_when_positive(self):
        ok, params = self._call(1_700_000_000)
        assert ok is True
        assert params["mtime"] == "1700000000"
        assert params["permissions"] == "644"

    def test_mtime_absent_when_zero(self):
        ok, params = self._call(0)
        assert ok is True
        assert "mtime" not in params
