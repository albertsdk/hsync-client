"""
Tests for hsync_client.main_logic module - Deletion risk and stdin functionality.

Contains TestCheckDeletionRisk, TestWithStdinCommands, TestRunStdinWait classes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hsync_client.main_logic import _check_deletion_risk, _run_stdin_wait, _with_stdin_commands


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

    # ── Risky scenarios ──────────────────────────────────────────────

    def test_lan_no_force_master_warns(self, tmp_path):
        """Warns when LAN mode without force-master and non-empty dir."""
        (tmp_path / "file.txt").write_text("content")
        mock_logger = MagicMock()

        with patch("builtins.input", return_value="y"):
            _check_deletion_risk(
                local_path=str(tmp_path),
                lan_mode=True,
                force_master=False,
                sync_direction="bidirectional",
                auto_confirm=False,
                logger=mock_logger,
            )

        assert mock_logger.warning.call_count >= 2
        warning_texts = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "DELETE" in warning_texts
        assert "LAN" in warning_texts

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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
                sync_direction="bidirectional",
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
