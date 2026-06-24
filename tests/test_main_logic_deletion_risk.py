"""
Tests for hsync_client.main_logic module - Deletion risk and stdin functionality.

Contains TestCheckDeletionRisk, TestWithStdinCommands, TestRunStdinWait classes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hsync_client.main_logic import (
    _check_deletion_risk,
    _check_lan_write_policy,
    _run_stdin_wait,
    _with_stdin_commands,
)


class TestCheckDeletionRisk:
    """Tests for _check_deletion_risk function."""

    # ── Safe scenarios (no warning expected) ─────────────────────────

    def test_empty_directory_no_warning(self, tmp_path):
        """No warning when directory is empty."""
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=False,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_upload_only_no_warning(self, tmp_path):
        """No warning for upload-only mode (never deletes local files)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=False,
            sync_direction="upload-only",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_lan_force_master_no_warning(self, tmp_path):
        """No warning when LAN mode with force-master (this client is authoritative)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=True,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_bidirectional_server_mode_no_warning(self, tmp_path):
        """No warning for bidirectional server mode (normal behaviour)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=False,
            force_master=False,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_nonexistent_directory_no_warning(self, tmp_path):
        """No warning when directory does not exist (OSError on listdir)."""
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path / "nonexistent"),
            lan_mode=True,
            force_master=False,
            sync_direction="download-only",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_hsynckeep_only_no_warning(self, tmp_path):
        """No warning when directory contains only the .hsynckeep sentinel."""
        from hsync_client.constants import HSYNCKEEP_FILENAME

        (tmp_path / HSYNCKEEP_FILENAME).write_text("")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=False,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_hidden_files_only_no_warning(self, tmp_path):
        """No warning when directory contains only hidden/temp files."""
        (tmp_path / ".hidden").write_text("x")
        (tmp_path / "file.tmp").write_text("x")
        (tmp_path / "backup.bak").write_text("x")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=False,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_user_file_in_subdir_warns(self, tmp_path):
        """Warns when a user file exists deeper in the tree, not at top level."""
        from hsync_client.constants import HSYNCKEEP_FILENAME

        (tmp_path / HSYNCKEEP_FILENAME).write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "real.txt").write_text("user data")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="y"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )
        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "DELETE" in warning_texts

    def test_lan_bidirectional_no_warning(self, tmp_path):
        """No warning for bidirectional LAN: local-only files are uploaded, not deleted."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()
        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=True,
            force_master=False,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    # ── Risky scenarios ──────────────────────────────────────────────

    def test_lan_download_only_warns(self, tmp_path):
        """Warns when LAN mode is download-only and dir is non-empty."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="y"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert mock_logger.warning.call_count >= 2
        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "DELETE" in warning_texts
        assert "download-only" in warning_texts

    def test_download_only_warns(self, tmp_path):
        """Warns when download-only mode and non-empty dir."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="y"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=False,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert mock_logger.warning.call_count >= 2
        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "DELETE" in warning_texts
        assert "download-only" in warning_texts

    # ── auto_confirm behaviour ───────────────────────────────────────

    def test_auto_confirm_skips_prompt(self, tmp_path):
        """auto_confirm=True logs warning but doesn't prompt."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        # Should NOT call input() at all
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=True,
                logger=mock_logger,
            )

        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "--yes flag set" in warning_texts

    # ── User response handling ───────────────────────────────────────

    def test_user_accepts_with_y(self, tmp_path):
        """User typing 'y' allows sync to proceed (no SystemExit)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="y"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )
        # No exception raised – test passes

    def test_user_accepts_with_yes(self, tmp_path):
        """User typing 'yes' allows sync to proceed."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="yes"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

    def test_user_declines_with_n_raises_system_exit(self, tmp_path):
        """User typing 'n' raises SystemExit(0)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="n"), pytest.raises(SystemExit) as exc_info:
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert exc_info.value.code == 0
        mock_logger.info.assert_called()

    def test_user_declines_with_empty_raises_system_exit(self, tmp_path):
        """Empty input (just Enter) raises SystemExit(0)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value=""), pytest.raises(SystemExit) as exc_info:
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert exc_info.value.code == 0

    def test_eof_raises_system_exit(self, tmp_path):
        """EOFError (piped input) raises SystemExit(0)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", side_effect=EOFError), pytest.raises(SystemExit) as exc_info:
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert exc_info.value.code == 0

    def test_keyboard_interrupt_raises_system_exit(self, tmp_path):
        """KeyboardInterrupt (Ctrl+C) raises SystemExit(0)."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with (
            patch("builtins.input", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc_info,
        ):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="download-only",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert exc_info.value.code == 0

    def test_download_only_with_auto_confirm(self, tmp_path):
        """download-only + auto_confirm logs warning and proceeds."""
        (tmp_path / "data.bin").write_bytes(b"\x00" * 100)
        mock_logger = MagicMock()

        _check_deletion_risk(
            local_path=str(tmp_path),
            lan_mode=False,
            force_master=False,
            sync_direction="download-only",
            auto_confirm=True,
            logger=mock_logger,
        )

        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "--yes flag set" in warning_texts
        assert "download-only" in warning_texts


class TestCheckLanWritePolicy:
    """Tests for _check_lan_write_policy (LAN-key requirement)."""

    # ── Allowed scenarios (no SystemExit) ────────────────────────────

    def test_non_lan_mode_is_skipped(self):
        """Outside LAN mode the policy never applies, even without a key."""
        _check_lan_write_policy(
            lan_mode=False,
            lan_key=None,
            sync_direction="bidirectional",
            auto_confirm=True,
            logger=MagicMock(),
        )

    def test_interactive_write_without_key_is_allowed(self):
        """Interactive runs (no --yes) may write without a key (operator vouches)."""
        _check_lan_write_policy(
            lan_mode=True,
            lan_key=None,
            sync_direction="bidirectional",
            auto_confirm=False,
            logger=MagicMock(),
        )

    def test_non_interactive_download_only_is_allowed(self):
        """download-only never writes to the master, so no key is required."""
        _check_lan_write_policy(
            lan_mode=True,
            lan_key=None,
            sync_direction="download-only",
            auto_confirm=True,
            logger=MagicMock(),
        )

    def test_non_interactive_write_with_key_is_allowed(self):
        """A key satisfies the policy for unattended writes over the secure LAN."""
        _check_lan_write_policy(
            lan_mode=True,
            lan_key="secret",
            sync_direction="bidirectional",
            auto_confirm=True,
            logger=MagicMock(),
        )

    # ── Refused scenarios (SystemExit + auth_required event) ─────────

    def test_non_interactive_write_without_key_exits(self):
        """Unattended write without a key is refused with an AUTH_REQUIRED event."""
        mock_logger = MagicMock()
        with (
            patch("hsync_client.main_logic.emit_event") as mock_emit,
            pytest.raises(SystemExit) as exc_info,
        ):
            _check_lan_write_policy(
                lan_mode=True,
                lan_key=None,
                sync_direction="bidirectional",
                auto_confirm=True,
                logger=mock_logger,
            )
        assert exc_info.value.code == 1
        error_texts = " ".join(str(c) for c in mock_logger.error.call_args_list)
        assert "AUTH_REQUIRED" in error_texts
        mock_emit.assert_called_once()

    def test_non_interactive_upload_only_without_key_exits(self):
        """upload-only writes to the master and is refused unattended without a key."""
        with (
            patch("hsync_client.main_logic.emit_event"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _check_lan_write_policy(
                lan_mode=True,
                lan_key=None,
                sync_direction="upload-only",
                auto_confirm=True,
                logger=MagicMock(),
            )
        assert exc_info.value.code == 1


class TestWithStdinCommands:
    """Tests for _with_stdin_commands async function."""

    @pytest.mark.asyncio
    async def test_cancels_stdin_task_when_coro_completes(self):
        """stdin reader task is cancelled after the main coroutine finishes."""
        mock_log = MagicMock()

        async def quick_coro():
            pass

        with patch("hsync_client.main_logic.StdinCommandHandler") as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _with_stdin_commands(mock_log, quick_coro())

            # Handler was instantiated and commands registered
            MockHandler.assert_called_once_with(mock_log)
            assert (
                handler_inst.register.call_count == 4
            )  # PREVIEW, SYNC, SET_SYNC_DIRECTION, SET_BANDWIDTH

    @pytest.mark.asyncio
    async def test_cancels_stdin_task_when_coro_raises(self):
        """stdin reader task is cancelled even when the coroutine raises."""
        mock_log = MagicMock()

        async def failing_coro():
            raise RuntimeError("boom")

        with patch("hsync_client.main_logic.StdinCommandHandler") as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            with pytest.raises(RuntimeError, match="boom"):
                await _with_stdin_commands(mock_log, failing_coro())

    @pytest.mark.asyncio
    async def test_registers_preview_sync_and_direction_commands(self):
        """All three commands are registered: PREVIEW, SYNC, SET_SYNC_DIRECTION."""
        mock_log = MagicMock()

        async def noop():
            pass

        with patch("hsync_client.main_logic.StdinCommandHandler") as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _with_stdin_commands(mock_log, noop())

            registered_cmds = [c[0][0] for c in handler_inst.register.call_args_list]
            assert "PREVIEW" in registered_cmds
            assert "SYNC" in registered_cmds
            assert "SET_SYNC_DIRECTION" in registered_cmds


class TestStdinCommandHandlers:
    """Invoke the registered stdin handler closures to cover their bodies."""

    async def _register_and_capture(self):
        """Run _with_stdin_commands with mocks and return the handler map."""
        registered: dict = {}
        handler_inst = MagicMock()
        handler_inst.register.side_effect = lambda name, fn: registered.__setitem__(name, fn)
        handler_inst.run = AsyncMock()

        async def noop():
            pass

        with patch("hsync_client.main_logic.StdinCommandHandler", return_value=handler_inst):
            await _with_stdin_commands(MagicMock(), noop())
        return registered

    @pytest.mark.asyncio
    async def test_sync_handler_uses_lan_mode(self):
        """The SYNC handler syncs with the configured LAN_MODE flag."""
        registered = await self._register_and_capture()
        with (
            patch("hsync_client.main_logic.CONFIG", {"LAN_MODE": True}),
            patch("hsync_client.main_logic.sync", new=AsyncMock()) as mock_sync,
        ):
            await registered["SYNC"]()
        mock_sync.assert_awaited_once_with(lan_mode=True)

    @pytest.mark.asyncio
    async def test_set_sync_direction_valid_invalid_and_noop(self):
        """SET_SYNC_DIRECTION applies valid changes, rejects invalid, skips no-ops."""
        registered = await self._register_and_capture()
        cfg = {"LAN_MODE": False, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.main_logic.CONFIG", cfg),
            patch("hsync_client.main_logic.sync", new=AsyncMock()) as mock_sync,
        ):
            await registered["SET_SYNC_DIRECTION"](direction="upload-only")
            assert cfg["SYNC_DIRECTION"] == "upload-only"
            # No-op: same direction does not resync again.
            await registered["SET_SYNC_DIRECTION"](direction="upload-only")
            # Invalid direction is rejected.
            await registered["SET_SYNC_DIRECTION"](direction="sideways")
        assert cfg["SYNC_DIRECTION"] == "upload-only"
        mock_sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_bandwidth_updates_and_clears_limits(self):
        """SET_BANDWIDTH parses ints/strings, treats 0 as unlimited, rejects junk."""
        registered = await self._register_and_capture()
        cfg = {"MAX_BANDWIDTH_UPLOAD": None, "MAX_BANDWIDTH_DOWNLOAD": None}
        with (
            patch("hsync_client.main_logic.CONFIG", cfg),
            patch("hsync_client.main_logic.reset_bandwidth_throttle") as mock_reset,
        ):
            # int upload, string download
            await registered["SET_BANDWIDTH"](upload=2048, download="4096")
            assert cfg["MAX_BANDWIDTH_UPLOAD"] == 2048
            assert cfg["MAX_BANDWIDTH_DOWNLOAD"] == 4096
            # zero means unlimited (None)
            await registered["SET_BANDWIDTH"](upload="0", download=0)
            assert cfg["MAX_BANDWIDTH_UPLOAD"] is None
            assert cfg["MAX_BANDWIDTH_DOWNLOAD"] is None
            # invalid values are rejected without raising
            await registered["SET_BANDWIDTH"](upload="abc")
            await registered["SET_BANDWIDTH"](download="xyz")
        assert mock_reset.call_count >= 1

    @pytest.mark.asyncio
    async def test_run_stdin_wait_sync_handler(self):
        """_run_stdin_wait's SYNC handler clears dry-run, syncs and stops the reader."""
        registered: dict = {}
        handler_inst = MagicMock()
        handler_inst.register.side_effect = lambda name, fn: registered.__setitem__(name, fn)
        handler_inst.run = AsyncMock()

        cfg = {"LAN_MODE": False, "DRYRUN": True}
        with (
            patch("hsync_client.main_logic.StdinCommandHandler", return_value=handler_inst),
            patch("hsync_client.main_logic.CONFIG", cfg),
            patch("hsync_client.main_logic.sync", new=AsyncMock()) as mock_sync,
        ):
            await _run_stdin_wait(MagicMock())
            await registered["SYNC"]()

        assert cfg["DRYRUN"] is False
        mock_sync.assert_awaited_once_with(lan_mode=False)
        handler_inst.stop.assert_called_once()


class TestRunStdinWait:
    """Tests for _run_stdin_wait async function."""

    @pytest.mark.asyncio
    async def test_registers_preview_and_sync(self):
        """_run_stdin_wait registers PREVIEW and SYNC commands."""
        mock_log = MagicMock()

        with patch("hsync_client.main_logic.StdinCommandHandler") as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _run_stdin_wait(mock_log)

            registered_cmds = [c[0][0] for c in handler_inst.register.call_args_list]
            assert "PREVIEW" in registered_cmds
            assert "SYNC" in registered_cmds

    @pytest.mark.asyncio
    async def test_calls_handler_run(self):
        """_run_stdin_wait calls handler.run() to read stdin."""
        mock_log = MagicMock()

        with patch("hsync_client.main_logic.StdinCommandHandler") as MockHandler:
            handler_inst = MagicMock()
            handler_inst.run = AsyncMock()
            MockHandler.return_value = handler_inst

            await _run_stdin_wait(mock_log)

            handler_inst.run.assert_awaited_once()
