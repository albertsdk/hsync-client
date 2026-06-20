# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the CLI module using Typer's CliRunner."""

import runpy
from unittest.mock import patch

from typer.testing import CliRunner

from hsync_client.cli import app

runner = CliRunner()


class TestEntryPoints:
    """Tests for package and console entry points."""

    def test_main_delegates_to_cli_run(self):
        """hsync_client.main.main delegates to the Typer wrapper."""
        from hsync_client.main import main

        with patch("hsync_client.main.run") as mock_run:
            main()

        mock_run.assert_called_once_with()

    def test_module_entrypoint_delegates_to_main(self):
        """python -m hsync_client invokes hsync_client.main.main."""
        with patch("hsync_client.main.main") as mock_main:
            runpy.run_module("hsync_client.__main__", run_name="__main__")

        mock_main.assert_called_once_with()


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

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_with_hyphens_and_underscores(self, mock_run_sync):
        """CLI accepts bucket names with hyphens and underscores."""
        result = runner.invoke(app, ["-b", "test-bucket", "-s", "http://test:8000"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["bucket"] == "test-bucket"
        result = runner.invoke(app, ["-b", "test_bucket", "-s", "http://test:8000"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["bucket"] == "test_bucket"

    def test_rejects_invalid_bucket_name(self):
        """CLI rejects bucket names with invalid characters or format."""
        result = runner.invoke(app, ["-b", "bucket.name"])
        assert result.exit_code != 0
        assert "alphanumeric" in result.output.lower()
        result = runner.invoke(app, ["-b", "bucket_"])
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
    def test_path_option(self, mock_run_sync):
        """CLI accepts -p/--path option."""
        result = runner.invoke(app, ["-b", "test", "-p", "/tmp/sync", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["local_path"] == "/tmp/sync"

    @patch("hsync_client.main_logic.run_sync")
    def test_ca_cert_option(self, mock_run_sync):
        """CLI accepts --ca-cert option."""
        result = runner.invoke(
            app, ["-b", "test", "--ca-cert", "/path/to/ca.crt", "-s", "http://test:8000"]
        )
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
        result = runner.invoke(
            app, ["-b", "test", "--filter-path", "subdir", "-s", "http://test:8000"]
        )
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
        assert call_kwargs["reconnect_delay"] == 5
        assert call_kwargs["ca_cert"] is None
        assert call_kwargs["filter_path"] is None
        assert call_kwargs["logfile"] is None
        assert call_kwargs["resync"] is None
        assert call_kwargs["env_file"] is None
        assert call_kwargs["discovery_timeout"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_discovery_timeout_accepts_zero(self, mock_run_sync):
        """--discovery-timeout 0 is accepted as wait-indefinitely."""
        result = runner.invoke(app, ["-b", "test", "--lan-mode", "--discovery-timeout", "0"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["discovery_timeout"] == 0

    def test_help_documents_discovery_timeout_zero(self):
        """Help output documents the 0 = wait indefinitely convention."""
        # Force a wide terminal so Rich does not word-wrap the help text and
        # split the documented sentinel across lines.
        result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "0 = wait indefinitely" in result.output

    @patch("hsync_client.main_logic.run_sync")
    def test_env_file_option(self, mock_run_sync):
        """CLI accepts --env-file option."""
        result = runner.invoke(
            app, ["-b", "test", "--env-file", "/path/to/env", "-s", "http://test:8000"]
        )
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

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_long(self, mock_run_sync):
        """CLI accepts --yes flag."""
        result = runner.invoke(app, ["-b", "test", "--yes", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_short(self, mock_run_sync):
        """CLI accepts -y shorthand flag."""
        result = runner.invoke(app, ["-b", "test", "-y", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_defaults_false(self, mock_run_sync):
        """--yes flag defaults to False when not provided."""
        result = runner.invoke(app, ["-b", "test", "-s", "http://test:8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is False


class TestCLIValidation:
    """Tests for CLI parameter validation."""

    def test_rejects_http_timeout_below_min(self):
        """CLI rejects --timeout below minimum."""
        result = runner.invoke(app, ["-b", "test", "-s", "http://test:8000", "--timeout", "0"])
        assert result.exit_code != 0
        assert "timeout" in result.output.lower()

    def test_rejects_http_timeout_above_max(self):
        """CLI rejects --timeout above maximum."""
        from hsync_client.constants import HTTP_TIMEOUT_MAX

        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "--timeout", str(HTTP_TIMEOUT_MAX + 1)]
        )
        assert result.exit_code != 0
        assert "timeout" in result.output.lower()

    def test_rejects_resync_interval_below_min(self):
        """CLI rejects --interval below minimum."""
        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "-w", "--interval", "0"]
        )
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_resync_interval_above_max(self):
        """CLI rejects --interval above maximum."""
        from hsync_client.constants import RESYNC_MAX

        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "-w", "--interval", str(RESYNC_MAX + 1)]
        )
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_retry_delay_below_min(self):
        """CLI rejects --retry-delay below minimum."""
        result = runner.invoke(app, ["-b", "test", "-s", "http://test:8000", "--retry-delay", "0"])
        assert result.exit_code != 0
        assert "retry" in result.output.lower()

    def test_rejects_retry_delay_above_max(self):
        """CLI rejects --retry-delay above maximum."""
        from hsync_client.constants import RECONNECT_DELAY_MAX

        result = runner.invoke(
            app,
            ["-b", "test", "-s", "http://test:8000", "--retry-delay", str(RECONNECT_DELAY_MAX + 1)],
        )
        assert result.exit_code != 0
        assert "retry" in result.output.lower()

    def test_rejects_stability_delay_below_min(self):
        """CLI rejects --stability-delay below minimum."""
        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "--stability-delay", "0"]
        )
        assert result.exit_code != 0
        assert "stability" in result.output.lower()

    def test_rejects_stability_delay_above_max(self):
        """CLI rejects --stability-delay above maximum."""
        from hsync_client.constants import STABILITY_DELAY_MAX

        result = runner.invoke(
            app,
            [
                "-b",
                "test",
                "-s",
                "http://test:8000",
                "--stability-delay",
                str(STABILITY_DELAY_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "stability" in result.output.lower()

    def test_rejects_max_wait_timeout_below_min(self):
        """CLI rejects --max-wait-timeout below minimum."""
        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "--max-wait-timeout", "0"]
        )
        assert result.exit_code != 0
        assert "max wait" in result.output.lower() or "timeout" in result.output.lower()

    def test_rejects_max_wait_timeout_above_max(self):
        """CLI rejects --max-wait-timeout above maximum."""
        from hsync_client.constants import MAX_WAIT_TIMEOUT_MAX

        result = runner.invoke(
            app,
            [
                "-b",
                "test",
                "-s",
                "http://test:8000",
                "--max-wait-timeout",
                str(MAX_WAIT_TIMEOUT_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "max wait" in result.output.lower() or "timeout" in result.output.lower()

    def test_rejects_negative_discovery_timeout(self):
        """CLI rejects negative --discovery-timeout values."""
        result = runner.invoke(app, ["-b", "test", "--lan-mode", "--discovery-timeout", "-1"])
        assert result.exit_code != 0
        assert "discovery timeout" in result.output.lower()

    def test_rejects_invalid_sync_direction(self):
        """CLI rejects invalid --sync-direction value."""
        result = runner.invoke(
            app, ["-b", "test", "-s", "http://test:8000", "--sync-direction", "invalid"]
        )
        assert result.exit_code != 0
        assert "sync-direction" in result.output.lower()

    def test_rejects_force_master_with_upload_only(self):
        """CLI rejects --force-master with --sync-direction=upload-only."""
        result = runner.invoke(
            app,
            [
                "-b",
                "test",
                "-s",
                "http://test:8000",
                "--force-master",
                "--sync-direction",
                "upload-only",
            ],
        )
        assert result.exit_code != 0
        assert "force-master" in result.output.lower()

    def test_rejects_force_master_with_download_only(self):
        """CLI rejects --force-master with --sync-direction=download-only."""
        result = runner.invoke(
            app,
            [
                "-b",
                "test",
                "-s",
                "http://test:8000",
                "--force-master",
                "--sync-direction",
                "download-only",
            ],
        )
        assert result.exit_code != 0
        assert "force-master" in result.output.lower()

    def test_accepts_force_master_with_bidirectional(self):
        """CLI accepts --force-master with --sync-direction=bidirectional."""
        from unittest.mock import patch as mock_patch

        with mock_patch("hsync_client.main_logic.run_sync"):
            result = runner.invoke(
                app,
                [
                    "-b",
                    "test",
                    "-s",
                    "http://test:8000",
                    "--force-master",
                    "--sync-direction",
                    "bidirectional",
                ],
            )
            assert result.exit_code == 0
