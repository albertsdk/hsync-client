# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for the CLI module using Typer's CliRunner."""

import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner

from hsync_client.cli import app


runner = CliRunner()


class TestTyperCli:
    """Tests for the Typer CLI application."""

    def test_help_shows_description(self):
        """Help output shows application description."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "HTTP-based file synchronization client" in result.output

    def test_requires_bucket_argument(self):
        """CLI requires -b/--bucket argument."""
        result = runner.invoke(app, [])
        assert result.exit_code != 0
        assert "bucket" in result.output.lower() or "missing" in result.output.lower()

    def test_version_option(self):
        """CLI shows version with --version."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "hsync" in result.output

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_short_option(self, mock_run_sync):
        """CLI accepts -b bucket option."""
        result = runner.invoke(app, ["-b", "testbucket", "-s", "http://test:8000"])
        assert result.exit_code == 0
        mock_run_sync.assert_called_once()
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "testbucket"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_long_option(self, mock_run_sync):
        """CLI accepts --bucket option."""
        result = runner.invoke(app, ["--bucket", "mybucket123", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "mybucket123"

    def test_rejects_invalid_bucket_name(self):
        """CLI rejects bucket names with non-alphanumeric chars."""
        result = runner.invoke(app, ["-b", "test-bucket"])
        assert result.exit_code != 0
        assert "alphanumeric" in result.output.lower()

    @patch("hsync_client.main_logic.run_sync")
    def test_server_can_be_provided(self, mock_run_sync):
        """Server argument can be provided via CLI."""
        result = runner.invoke(app, ["-b", "test", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "test"
        assert call_kwargs["server_url"] == "http://test:8000"

    @patch("hsync_client.main_logic.run_sync")
    def test_watch_flag(self, mock_run_sync):
        """CLI accepts -w/--watch flag."""
        result = runner.invoke(app, ["-b", "test", "-w", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_verbose_flag(self, mock_run_sync):
        """CLI accepts -v/--verbose flag."""
        result = runner.invoke(app, ["-b", "test", "-v", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["debug"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_quiet_flag(self, mock_run_sync):
        """CLI accepts -q/--quiet flag."""
        result = runner.invoke(app, ["-b", "test", "-q", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["quiet"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_dry_run_flag(self, mock_run_sync):
        """CLI accepts -n/--dry-run flag."""
        result = runner.invoke(app, ["-b", "test", "-n", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["dryrun"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_insecure_flag(self, mock_run_sync):
        """CLI accepts --insecure flag."""
        result = runner.invoke(app, ["-b", "test", "--insecure", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["verify_ssl"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_no_color_flag(self, mock_run_sync):
        """CLI accepts --no-color flag."""
        result = runner.invoke(app, ["-b", "test", "--no-color", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["no_color"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_help_symbols_flag(self, mock_run_sync):
        """CLI accepts --help-symbols flag."""
        result = runner.invoke(app, ["-b", "test", "--help-symbols", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["legend"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_compare_by_mtime(self, mock_run_sync):
        """CLI accepts --compare-by mtime."""
        result = runner.invoke(app, ["-b", "test", "--compare-by", "mtime", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["sync_order"] == "mtime"

    @patch("hsync_client.main_logic.run_sync")
    def test_compare_by_ctime(self, mock_run_sync):
        """CLI accepts --compare-by ctime."""
        result = runner.invoke(app, ["-b", "test", "--compare-by", "ctime", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["sync_order"] == "ctime"

    @patch("hsync_client.main_logic.run_sync")
    def test_path_option(self, mock_run_sync):
        """CLI accepts -p/--path option."""
        result = runner.invoke(app, ["-b", "test", "-p", "/tmp/sync", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["local_path"] == "/tmp/sync"

    @patch("hsync_client.main_logic.run_sync")
    def test_ca_cert_option(self, mock_run_sync):
        """CLI accepts --ca-cert option."""
        result = runner.invoke(app, ["-b", "test", "--ca-cert", "/path/to/ca.crt", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["ca_cert"] == "/path/to/ca.crt"

    @patch("hsync_client.main_logic.run_sync")
    def test_output_option(self, mock_run_sync):
        """CLI accepts -o/--output option."""
        result = runner.invoke(app, ["-b", "test", "-o", "/tmp/log.txt", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["logfile"] == "/tmp/log.txt"

    @patch("hsync_client.main_logic.run_sync")
    def test_filter_path_option(self, mock_run_sync):
        """CLI accepts --filter-path option."""
        result = runner.invoke(app, ["-b", "test", "--filter-path", "subdir", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["filter_path"] == "subdir"

    @patch("hsync_client.main_logic.run_sync")
    def test_retry_delay_option(self, mock_run_sync):
        """CLI accepts --retry-delay option."""
        result = runner.invoke(app, ["-b", "test", "--retry-delay", "10", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["reconnect_delay"] == 10

    @patch("hsync_client.main_logic.run_sync")
    def test_default_values(self, mock_run_sync):
        """CLI sets correct default values."""
        result = runner.invoke(app, ["-b", "test", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is False
        assert call_kwargs["debug"] is False
        assert call_kwargs["quiet"] is False
        assert call_kwargs["dryrun"] is False
        assert call_kwargs["verify_ssl"] is True
        assert call_kwargs["sync_order"] == "mtime"
        assert call_kwargs["reconnect_delay"] == 5
        assert call_kwargs["ca_cert"] is None
        assert call_kwargs["filter_path"] is None
        assert call_kwargs["logfile"] is None
        assert call_kwargs["resync"] is None
        assert call_kwargs["env_file"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_env_file_option(self, mock_run_sync):
        """CLI accepts --env-file option."""
        result = runner.invoke(app, ["-b", "test", "--env-file", "/path/to/env", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["env_file"] == "/path/to/env"

    @patch("hsync_client.main_logic.run_sync")
    def test_lan_mode_option(self, mock_run_sync):
        """CLI accepts --lan-mode option."""
        result = runner.invoke(app, ["-b", "test", "--lan-mode"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["lan_mode"] is True

