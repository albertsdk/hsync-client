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
        result = runner.invoke(app, ["server"])
        assert result.exit_code != 0
        assert "bucket" in result.output.lower() or "missing" in result.output.lower()

    def test_version_option(self):
        """CLI shows version with --version."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "hsync" in result.output

    def test_help_symbols_prints_legend_without_other_options(self):
        """--help-symbols is eager: prints the legend and exits 0 without -b/-s."""
        result = runner.invoke(app, ["--help-symbols"])
        assert result.exit_code == 0
        assert "Log legend:" in result.output
        assert "Upload file to server/master" in result.output

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_short_option(self, mock_run_sync):
        """CLI accepts -b bucket option."""
        result = runner.invoke(app, ["server", "-b", "testbucket", "test.example.com"])
        assert result.exit_code == 0
        mock_run_sync.assert_called_once()
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "testbucket"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_long_option(self, mock_run_sync):
        """CLI accepts --bucket option."""
        result = runner.invoke(app, ["server", "--bucket", "mybucket123", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "mybucket123"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_with_hyphens_and_underscores(self, mock_run_sync):
        """CLI accepts bucket names with hyphens and underscores."""
        result = runner.invoke(app, ["server", "-b", "test-bucket", "test.example.com"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["bucket"] == "test-bucket"
        result = runner.invoke(app, ["server", "-b", "test_bucket", "test.example.com"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["bucket"] == "test_bucket"

    def test_rejects_invalid_bucket_name(self):
        """CLI rejects bucket names with invalid characters or format."""
        result = runner.invoke(app, ["server", "-b", "bucket.name"])
        assert result.exit_code != 0
        assert "alphanumeric" in result.output.lower()
        result = runner.invoke(app, ["server", "-b", "bucket_"])
        assert result.exit_code != 0
        assert "alphanumeric" in result.output.lower()

    @patch("hsync_client.main_logic.run_sync")
    def test_server_url_is_assembled_from_hostname(self, mock_run_sync):
        """The bare hostname form assembles the implied https:// server URL."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "--port", "8000"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "test"
        assert call_kwargs["server_url"] == "https://test.example.com:8000"

    @patch("hsync_client.main_logic.run_sync")
    def test_default_is_continuous(self, mock_run_sync):
        """Without --once the client runs continuously (infinite retry)."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_once_flag(self, mock_run_sync):
        """CLI accepts --once as the sole one-shot form."""
        result = runner.invoke(app, ["server", "-b", "test", "--once", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_once_in_lan_is_one_shot(self, mock_run_sync):
        """--once in LAN mode selects a one-shot slave run."""
        result = runner.invoke(app, ["lan", "--once", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_verbose_flag(self, mock_run_sync):
        """CLI accepts -v/--verbose flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "-v"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["debug"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_quiet_flag(self, mock_run_sync):
        """CLI accepts -q/--quiet flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "-q"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["quiet"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_dry_run_flag(self, mock_run_sync):
        """CLI accepts -n/--dry-run flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "-n"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["dryrun"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_insecure_flag(self, mock_run_sync):
        """CLI accepts --insecure flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "--insecure"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["verify_ssl"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_no_color_flag(self, mock_run_sync):
        """CLI accepts --no-color flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "--no-color"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["no_color"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_help_symbols_flag(self, mock_run_sync):
        """--help-symbols is eager: prints the legend and exits before run_sync."""
        result = runner.invoke(app, ["--help-symbols"])
        assert result.exit_code == 0
        assert "Log legend:" in result.output
        mock_run_sync.assert_not_called()

    @patch("hsync_client.main_logic.run_sync")
    def test_path_option(self, mock_run_sync):
        """CLI accepts -p/--path option."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "-p", "/tmp/sync"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["local_path"] == "/tmp/sync"

    @patch("hsync_client.main_logic.run_sync")
    def test_ca_cert_option(self, mock_run_sync):
        """CLI accepts --ca-cert option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--ca-cert", "/path/to/ca.crt"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["ca_cert"] == "/path/to/ca.crt"

    @patch("hsync_client.main_logic.run_sync")
    def test_output_option(self, mock_run_sync):
        """CLI accepts --log-file option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--log-file", "/tmp/log.txt"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["logfile"] == "/tmp/log.txt"

    @patch("hsync_client.main_logic.run_sync")
    def test_subdir_option(self, mock_run_sync):
        """CLI accepts --subdir option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--subdir", "subdir"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["filter_path"] == "subdir"

    @patch("hsync_client.main_logic.run_sync")
    def test_retry_delay_option(self, mock_run_sync):
        """CLI accepts --retry-delay option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--retry-delay", "10"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["reconnect_delay"] == 10

    @patch("hsync_client.main_logic.run_sync")
    def test_default_values(self, mock_run_sync):
        """CLI sets correct default values."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is True
        assert call_kwargs["debug"] is False
        assert call_kwargs["quiet"] is False
        assert call_kwargs["dryrun"] is False
        assert call_kwargs["verify_ssl"] is True
        assert call_kwargs["reconnect_delay"] is None
        assert call_kwargs["ca_cert"] is None
        assert call_kwargs["filter_path"] is None
        assert call_kwargs["logfile"] is None
        assert call_kwargs["resync"] is None
        assert call_kwargs["env_file"] is None
        assert call_kwargs["discovery_timeout"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_discovery_timeout_accepts_zero(self, mock_run_sync):
        """--discovery-timeout 0 is accepted as wait-indefinitely."""
        result = runner.invoke(app, ["lan", "-b", "test", "--discovery-timeout", "0"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["discovery_timeout"] == 0

    def test_help_documents_discovery_timeout_zero(self):
        """Help output documents the 0 = wait indefinitely convention."""
        # Force a wide terminal so Rich does not word-wrap the help text and
        # split the documented sentinel across lines.
        result = runner.invoke(app, ["lan", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "0 = wait indefinitely" in result.output

    @patch("hsync_client.main_logic.run_sync")
    def test_env_file_option(self, mock_run_sync):
        """CLI accepts --env-file option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--env-file", "/path/to/env"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["env_file"] == "/path/to/env"

    @patch("hsync_client.main_logic.run_sync")
    def test_lan_mode_option(self, mock_run_sync):
        """CLI accepts the lan subcommand."""
        result = runner.invoke(app, ["lan", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["lan_mode"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_require_pairing_default_false(self, mock_run_sync):
        """Without --require-pairing, lan_require_pairing defaults to False."""
        result = runner.invoke(app, ["lan", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["lan_require_pairing"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_require_pairing_option(self, mock_run_sync):
        """CLI accepts --require-pairing and forwards lan_require_pairing=True."""
        result = runner.invoke(app, ["lan", "-b", "test", "--require-pairing"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["lan_require_pairing"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_long(self, mock_run_sync):
        """CLI accepts --yes flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "--yes"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_short(self, mock_run_sync):
        """CLI accepts -y shorthand flag."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "-y"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_yes_flag_defaults_false(self, mock_run_sync):
        """--yes flag defaults to False when not provided."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["auto_confirm"] is False


class TestCLIValidation:
    """Tests for CLI parameter validation."""

    def test_rejects_http_timeout_below_min(self):
        """CLI rejects --http-timeout below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--http-timeout", "0"]
        )
        assert result.exit_code != 0
        assert "timeout" in result.output.lower()

    def test_rejects_http_timeout_above_max(self):
        """CLI rejects --http-timeout above maximum."""
        from hsync_client.constants import HTTP_TIMEOUT_MAX

        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "test.example.com",
                "--http-timeout",
                str(HTTP_TIMEOUT_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "timeout" in result.output.lower()

    def test_rejects_resync_interval_below_min(self):
        """CLI rejects --interval below minimum."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com", "--interval", "0"])
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_resync_interval_above_max(self):
        """CLI rejects --interval above maximum."""
        from hsync_client.constants import RESYNC_MAX

        result = runner.invoke(
            app,
            ["server", "-b", "test", "test.example.com", "--interval", str(RESYNC_MAX + 1)],
        )
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_retry_delay_below_min(self):
        """CLI rejects --retry-delay below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--retry-delay", "0"]
        )
        assert result.exit_code != 0
        assert "retry" in result.output.lower()

    def test_rejects_retry_delay_above_max(self):
        """CLI rejects --retry-delay above maximum."""
        from hsync_client.constants import RECONNECT_DELAY_MAX

        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "test.example.com",
                "--retry-delay",
                str(RECONNECT_DELAY_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "retry" in result.output.lower()

    def test_rejects_stability_delay_below_min(self):
        """CLI rejects --stability-delay below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--stability-delay", "0"]
        )
        assert result.exit_code != 0
        assert "stability" in result.output.lower()

    def test_rejects_stability_delay_above_max(self):
        """CLI rejects --stability-delay above maximum."""
        from hsync_client.constants import STABILITY_DELAY_MAX

        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "test.example.com",
                "--stability-delay",
                str(STABILITY_DELAY_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "stability" in result.output.lower()

    def test_rejects_max_wait_timeout_below_min(self):
        """CLI rejects --max-wait-timeout below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--max-wait-timeout", "0"]
        )
        assert result.exit_code != 0
        assert "max wait" in result.output.lower() or "timeout" in result.output.lower()

    def test_rejects_max_wait_timeout_above_max(self):
        """CLI rejects --max-wait-timeout above maximum."""
        from hsync_client.constants import MAX_WAIT_TIMEOUT_MAX

        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "test.example.com",
                "--max-wait-timeout",
                str(MAX_WAIT_TIMEOUT_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "max wait" in result.output.lower() or "timeout" in result.output.lower()

    def test_rejects_negative_discovery_timeout(self):
        """CLI rejects negative --discovery-timeout values."""
        result = runner.invoke(app, ["lan", "-b", "test", "--discovery-timeout", "-1"])
        assert result.exit_code != 0
        assert "discovery timeout" in result.output.lower()

    def test_rejects_invalid_sync_direction(self):
        """CLI rejects invalid --sync-direction value."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--sync-direction", "invalid"]
        )
        assert result.exit_code != 0
        assert "sync-direction" in result.output.lower()

    def test_rejects_force_master_with_upload_only(self):
        """CLI rejects --force-master with --sync-direction=upload-only."""
        result = runner.invoke(
            app,
            [
                "lan",
                "-b",
                "test",
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
                "lan",
                "-b",
                "test",
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
                    "lan",
                    "-b",
                    "test",
                    "--force-master",
                    "--sync-direction",
                    "bidirectional",
                ],
            )
            assert result.exit_code == 0


class TestServerHostnameArgument:
    """The bare-hostname form replaces the removed `-s URL`: https:// and the
    default port are implied, and URL-like or otherwise hostile inputs are
    rejected as usage errors before any sync starts."""

    @patch("hsync_client.main_logic.run_sync")
    def test_explicit_port_assembles_https_url(self, mock_run_sync):
        """HOSTNAME --port PORT assembles the https:// URL."""
        result = runner.invoke(app, ["server", "-b", "test", "hsync.example.com", "--port", "443"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:443"

    @patch("hsync_client.main_logic.run_sync")
    def test_default_port_is_implied(self, mock_run_sync):
        """HOSTNAME without --port uses the hsync-server default port."""
        result = runner.invoke(app, ["server", "-b", "test", "hsync.example.com"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:9753"

    @patch("hsync_client.main_logic.run_sync")
    def test_bare_ipv6_literal_is_bracketed(self, mock_run_sync):
        """A bare IPv6 literal becomes the bracketed https:// host part."""
        result = runner.invoke(app, ["server", "-b", "test", "::1", "--port", "9753"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://[::1]:9753"

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_falls_back_to_env(self, mock_run_sync):
        """No HOSTNAME leaves server_url None (the HSYNC_SERVER env fallback)."""
        result = runner.invoke(app, ["server", "-b", "test"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] is None

    def test_rejects_full_url_as_hostname(self):
        """A URL in the hostname slot is a usage error (the scheme is implied)."""
        result = runner.invoke(app, ["server", "-b", "test", "https://hsync.example.com"])
        assert result.exit_code != 0
        assert "bare hostname" in result.output

    def test_rejects_host_port_shorthand(self):
        """host:port is rejected: the port belongs to --port."""
        result = runner.invoke(app, ["server", "-b", "test", "hsync.example.com:9753"])
        assert result.exit_code != 0
        assert "--port" in result.output

    def test_rejects_port_without_hostname(self):
        """--port alone cannot express the server."""
        result = runner.invoke(app, ["server", "-b", "test", "--port", "9753"])
        assert result.exit_code != 0
        assert "requires a hostname" in result.output

    def test_rejects_port_out_of_range(self):
        """--port accepts only 1-65535."""
        result = runner.invoke(app, ["server", "-b", "test", "hsync.example.com", "--port", "0"])
        assert result.exit_code != 0
        assert "between 1 and 65535" in result.output

    def test_rejects_removed_server_flag(self):
        """`-s/--server` is gone: using it is a plain usage error."""
        result = runner.invoke(app, ["server", "-b", "test", "-s", "https://hsync.example.com"])
        assert result.exit_code != 0
        assert "No such option" in result.output


class TestCapabilitiesJson:
    """Tests for the --capabilities-json eager option."""

    def test_prints_valid_capabilities_document(self):
        """--capabilities-json emits a parseable document and exits cleanly."""
        from hsync_common.protocol.capabilities import (
            CAPABILITIES_SCHEMA_VERSION,
            parse_capabilities,
        )

        result = runner.invoke(app, ["--capabilities-json"])
        assert result.exit_code == 0
        doc = parse_capabilities(result.output)
        assert doc is not None
        assert doc["schema_version"] == CAPABILITIES_SCHEMA_VERSION

    def test_advertises_real_cli_options(self):
        """The document reports the actual long-form flags of the command."""
        from hsync_common.protocol.capabilities import parse_capabilities

        result = runner.invoke(app, ["--capabilities-json"])
        doc = parse_capabilities(result.output)
        assert doc is not None
        for flag in ("--never-master", "--force-master", "--require-pairing"):
            assert flag in doc["cli_options"]["lan"]
        for flag in ("--port", "--insecure", "--ca-cert"):
            assert flag in doc["cli_options"]["server"]
        assert "--listen-port" not in doc["cli_options"]["server"]
        # -s/--server is gone (replaced by the HOSTNAME argument): the
        # advertised surface must not keep advertising it.
        assert "--server" not in doc["cli_options"]["server"]
        assert "--port" not in doc["cli_options"]["lan"]

    def test_does_not_require_bucket(self):
        """The eager option short-circuits before bucket validation."""
        result = runner.invoke(app, ["--capabilities-json"])
        assert result.exit_code == 0
        assert "missing option" not in result.output.lower()

    def test_collect_subcommand_options_introspects_commands(self):
        """collect_subcommand_options returns sorted long-form flags per subcommand."""
        from hsync_client.cli import app as typer_app
        from hsync_client.cli import collect_subcommand_options

        options = collect_subcommand_options(typer_app)
        assert set(options) == {"lan", "server"}
        for flags in options.values():
            assert flags == sorted(flags)
            assert all(f.startswith("--") for f in flags)
        assert "--limit-upload" in options["lan"]
        assert "--port" in options["server"]
        assert "--server" not in options["server"]
        assert "--listen-port" not in options["server"]
        assert "--port" not in options["lan"]
