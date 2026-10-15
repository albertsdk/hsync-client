# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the CLI module using Typer's CliRunner."""

import runpy
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from hsync_client.cli import app
from hsync_client.config import get_env_settings
from hsync_client.constants import BOOTNODE_DEFAULT_PORT

runner = CliRunner()


class TestEntryPoints:
    """Tests for package and console entry points."""

    def test_main_diverts_multiprocessing_before_running_cli(self):
        """main diverts multiprocessing helpers, then delegates to the CLI.

        Regression guard for the PyInstaller build: loguru's ``enqueue=True``
        sinks start the stdlib multiprocessing resource tracker, which re-runs
        ``sys.executable`` (the frozen ``hsync``) with ``-B -c ...``. The entry
        point must call ``freeze_support()`` first so the PyInstaller runtime
        hook diverts that helper instead of booting the Typer app, which failed
        with ``No such option: -B``.
        """
        from hsync_client.main import main

        events: list[str] = []
        with (
            patch(
                "multiprocessing.freeze_support",
                side_effect=lambda: events.append("freeze_support"),
            ),
            patch("hsync_client.cli.run", side_effect=lambda: events.append("run")),
        ):
            main()

        assert events == ["freeze_support", "run"]

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
    def test_once_in_p2p_is_one_shot(self, mock_run_sync):
        """--once in P2P mode selects a one-shot slave run."""
        result = runner.invoke(app, ["p2p", "--once", "-b", "test"])
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
        result = runner.invoke(app, ["p2p", "-b", "test", "--discovery-timeout", "0"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["discovery_timeout"] == 0

    @patch("hsync_client.main_logic.run_sync")
    def test_bootnode_fingerprint_and_tofu_flow(self, mock_run_sync):
        hex64 = "0" * 64
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "boot.example.net",
                "--bootnode-fingerprint",
                hex64,
                "--bootnode-tofu",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_run_sync.call_args.kwargs
        assert kwargs["bootnode_fingerprints"] == (hex64,)
        assert kwargs["bootnode_tofu"] is True

    def test_bootnode_ca_cert_is_refused(self):
        """--ca-cert has no bootnode-channel meaning: refuse it, not silently ignore."""
        result = runner.invoke(
            app, ["p2p", "-b", "test", "boot.example.net", "--ca-cert", "/tmp/ca.pem"]
        )
        assert result.exit_code != 0
        assert "ca-cert" in result.output

    def test_insecure_conflicts_with_bootnode_fingerprint(self):
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "boot.example.net",
                "--insecure",
                "--bootnode-fingerprint",
                "0" * 64,
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_bootnode_fingerprint_requires_bootnode(self):
        result = runner.invoke(app, ["p2p", "-b", "test", "--bootnode-fingerprint", "0" * 64])
        assert result.exit_code != 0
        assert "require a bootnode" in result.output

    def test_bootnode_fingerprint_rejects_invalid(self):
        result = runner.invoke(
            app, ["p2p", "-b", "test", "boot.example.net", "--bootnode-fingerprint", "zzz"]
        )
        assert result.exit_code != 0
        assert "invalid --bootnode-fingerprint" in result.output

    def test_help_documents_discovery_timeout_zero(self):
        """Help output documents the 0 = wait indefinitely convention."""
        # Force a wide terminal so Rich does not word-wrap the help text and
        # split the documented sentinel across lines.
        result = runner.invoke(app, ["p2p", "--help"], env={"COLUMNS": "200"})
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
    def test_p2p_mode_option(self, mock_run_sync):
        """CLI accepts the lan subcommand."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_mode"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_require_pairing_default_false(self, mock_run_sync):
        """Without --require-pairing, p2p_require_pairing defaults to False."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_require_pairing"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_require_pairing_option(self, mock_run_sync):
        """CLI accepts --require-pairing and forwards p2p_require_pairing=True."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--require-pairing"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_require_pairing"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_peer_download_default_and_opt_out(self, mock_run_sync):
        """Peer downloading is on by default and --no-peer-download is final."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["p2p_peer_download_enabled"] is True

        result = runner.invoke(app, ["p2p", "-b", "test", "--no-peer-download"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["p2p_peer_download_enabled"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_allow_unauthenticated_writes_option(self, mock_run_sync):
        """The dangerous no-key mutation opt-in is explicit and defaults off."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["p2p_allow_unauthenticated_writes"] is False

        result = runner.invoke(
            app,
            ["p2p", "-b", "test", "--allow-unauthenticated-writes"],
        )
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["p2p_allow_unauthenticated_writes"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_allow_deletions_flag(self, mock_run_sync):
        """CLI accepts --allow-deletions flag."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--allow-deletions"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["allow_deletions"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_allow_unauthenticated_p2p_flag(self, mock_run_sync):
        """CLI accepts --allow-unauthenticated-p2p flag."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--allow-unauthenticated-p2p"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_allow_unauthenticated"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_allow_deletions_defaults_false(self, mock_run_sync):
        """--allow-deletions flag defaults to False when not provided."""
        result = runner.invoke(app, ["server", "-b", "test", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["allow_deletions"] is False


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
        result = runner.invoke(app, ["p2p", "-b", "test", "--discovery-timeout", "-1"])
        assert result.exit_code != 0
        assert "discovery timeout" in result.output.lower()

    def test_rejects_invalid_sync_direction(self):
        """CLI rejects invalid --sync-direction value."""
        result = runner.invoke(
            app, ["server", "-b", "test", "test.example.com", "--sync-direction", "invalid"]
        )
        assert result.exit_code != 0
        assert "sync-direction" in result.output.lower()

    def test_rejects_prefer_master_with_upload_only(self):
        """CLI rejects --prefer-master with --sync-direction=upload-only."""
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--prefer-master",
                "--sync-direction",
                "upload-only",
            ],
        )
        assert result.exit_code != 0
        assert "prefer-master" in result.output.lower()

    def test_rejects_prefer_master_with_download_only(self):
        """CLI rejects --prefer-master with --sync-direction=download-only."""
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--prefer-master",
                "--sync-direction",
                "download-only",
            ],
        )
        assert result.exit_code != 0
        assert "prefer-master" in result.output.lower()

    def test_accepts_prefer_master_with_bidirectional(self):
        """CLI accepts --prefer-master with --sync-direction=bidirectional."""
        from unittest.mock import patch as mock_patch

        with mock_patch("hsync_client.main_logic.run_sync"):
            result = runner.invoke(
                app,
                [
                    "p2p",
                    "-b",
                    "test",
                    "--prefer-master",
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
    def test_missing_hostname_resolves_from_env(self, mock_run_sync, monkeypatch):
        """No HOSTNAME resolves the full URL from HSYNC_SERVER."""
        monkeypatch.setenv("HSYNC_SERVER", "https://env.example.com:9753")
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test"])
            assert result.exit_code == 0
            assert mock_run_sync.call_args.kwargs["server_url"] == "https://env.example.com:9753"
        finally:
            get_env_settings.cache_clear()

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_resolves_from_env_file(self, mock_run_sync, monkeypatch, tmp_path):
        """A --env-file supplying HSYNC_SERVER satisfies the server requirement."""
        monkeypatch.delenv("HSYNC_SERVER", raising=False)
        env_file = tmp_path / "prod.env"
        env_file.write_text("HSYNC_SERVER=https://file.example.com:9753\n", encoding="utf-8")
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test", "--env-file", str(env_file)])
            assert result.exit_code == 0
            assert mock_run_sync.call_args.kwargs["server_url"] == "https://file.example.com:9753"
        finally:
            get_env_settings.cache_clear()

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_without_server_fails_fast(self, mock_run_sync, monkeypatch):
        """No HOSTNAME and no HSYNC_SERVER is a usage error before run_sync."""
        monkeypatch.delenv("HSYNC_SERVER", raising=False)
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test"])
            assert result.exit_code != 0
            assert "hostname" in result.output.lower()
            mock_run_sync.assert_not_called()
        finally:
            get_env_settings.cache_clear()

    def test_rejects_full_url_as_hostname(self):
        """A URL in the hostname slot is a usage error (the scheme is implied)."""
        result = runner.invoke(app, ["server", "-b", "test", "https://hsync.example.com"])
        assert result.exit_code != 0
        assert "not a URL" in result.output

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_host_port_shorthand(self, mock_run_sync):
        """HOST:PORT assembles the https:// URL (mutually exclusive with --port)."""
        result = runner.invoke(app, ["server", "-b", "test", "hsync.example.com:9753"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:9753"

    def test_rejects_port_supplied_twice(self):
        """A port in HOST:PORT and in --port is a usage error, not a precedence."""
        result = runner.invoke(
            app, ["server", "-b", "test", "hsync.example.com:9753", "--port", "9753"]
        )
        assert result.exit_code != 0
        assert "not both" in result.output

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
        for flag in (
            "--never-master",
            "--prefer-master",
            "--require-pairing",
            "--no-peer-download",
            "--allow-unauthenticated-writes",
        ):
            assert flag in doc["cli_options"]["p2p"]
        for flag in ("--port", "--insecure", "--ca-cert"):
            assert flag in doc["cli_options"]["server"]
        assert "--listen-port" not in doc["cli_options"]["server"]
        # C2: the p2p subcommand owns --port (the bootnode UDP port) and
        # --upnp; --listen-port is the master TCP port and stays p2p-only.
        for flag in ("--port", "--upnp", "--insecure", "--ca-cert", "--listen-port"):
            assert flag in doc["cli_options"]["p2p"]

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
        assert set(options) == {"p2p", "server", "trust"}
        for flags in options.values():
            assert flags == sorted(flags)
            assert all(f.startswith("--") for f in flags)
        assert "--limit-upload" in options["p2p"]
        assert "--port" in options["server"]
        assert "--listen-port" not in options["server"]
        assert "--port" in options["p2p"]
        assert "--upnp" in options["p2p"]


class TestBuildBootnode:
    """The bootnode CLI resolution rules."""

    def test_no_bootnode_selects_the_mdns_plan(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode(None, None, upnp=False) == (None, BOOTNODE_DEFAULT_PORT, False)

    def test_default_bootnode_port_is_443_and_upnp_defaults_off(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode("boot.example.net", None, upnp=False) == (
            "boot.example.net",
            443,
            False,
        )

    def test_explicit_port_wins_and_upnp_enables_mapping(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode("boot.example.net", 8443, upnp=True) == (
            "boot.example.net",
            8443,
            True,
        )

    def test_port_without_bootnode_is_refused(self):
        from hsync_client.cli import _build_bootnode

        with pytest.raises(typer.BadParameter):
            _build_bootnode(None, 8443, upnp=False)

    def test_url_and_path_are_refused(self):
        from hsync_client.cli import _build_bootnode

        for bad in ("https://boot.example.net", "boot.example.net/path"):
            with pytest.raises(typer.BadParameter):
                _build_bootnode(bad, None, upnp=False)

    def test_host_port_shorthand_is_accepted(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode("boot.example.net:8443", None, upnp=False) == (
            "boot.example.net",
            8443,
            False,
        )

    def test_host_port_and_port_flag_are_mutually_exclusive(self):
        from hsync_client.cli import _build_bootnode

        with pytest.raises(typer.BadParameter):
            _build_bootnode("boot.example.net:8443", 8443, upnp=False)

    def test_out_of_range_port_is_refused(self):
        from hsync_client.cli import _build_bootnode

        with pytest.raises(typer.BadParameter):
            _build_bootnode("boot.example.net", 70000, upnp=False)

    def test_ipv6_bracketed_is_returned_unbracketed(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode("[::1]", None, upnp=False) == ("::1", 443, False)

    def test_ipv6_bracketed_with_port_is_returned_unbracketed(self):
        from hsync_client.cli import _build_bootnode

        assert _build_bootnode("[::1]:8443", None, upnp=False) == ("::1", 8443, False)

    def test_env_bootnode_is_resolved_when_use_env(self, monkeypatch):
        from hsync_client.cli import _build_bootnode
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_BOOTNODE", "boot.example.net")
        monkeypatch.delenv("HSYNC_UPNP_ENABLED", raising=False)
        get_env_settings.cache_clear()
        assert _build_bootnode(None, None, upnp=False, use_env=True) == (
            "boot.example.net",
            443,
            False,
        )

    def test_env_upnp_enabled_opts_in(self, monkeypatch):
        from hsync_client.cli import _build_bootnode
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_BOOTNODE", "boot.example.net")
        monkeypatch.setenv("HSYNC_UPNP_ENABLED", "true")
        get_env_settings.cache_clear()
        assert _build_bootnode(None, None, upnp=False, use_env=True) == (
            "boot.example.net",
            443,
            True,
        )

    def test_env_bootnode_port_and_flag_port(self, monkeypatch):
        """An explicit --port overrides a port embedded in HSYNC_BOOTNODE (CLI > env)."""
        from hsync_client.cli import _build_bootnode
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_BOOTNODE", "boot.example.net:8443")
        monkeypatch.delenv("HSYNC_UPNP_ENABLED", raising=False)
        get_env_settings.cache_clear()
        assert _build_bootnode(None, 9000, upnp=False, use_env=True) == (
            "boot.example.net",
            9000,
            False,
        )

    def test_env_bootnode_port_without_flag(self, monkeypatch):
        from hsync_client.cli import _build_bootnode
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_BOOTNODE", "boot.example.net:8443")
        monkeypatch.delenv("HSYNC_UPNP_ENABLED", raising=False)
        get_env_settings.cache_clear()
        assert _build_bootnode(None, None, upnp=False, use_env=True) == (
            "boot.example.net",
            8443,
            False,
        )

    def test_env_is_not_used_without_use_env(self, monkeypatch):
        from hsync_client.cli import _build_bootnode
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_BOOTNODE", "boot.example.net")
        get_env_settings.cache_clear()
        assert _build_bootnode(None, None, upnp=False) == (None, BOOTNODE_DEFAULT_PORT, False)


class TestBootnodeTrustCommands:
    """``hsync trust bootnode`` manages the known-hosts store (ADR-0035)."""

    def test_add_list_show_remove(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_BOOTNODE_TRUST_FILE", str(tmp_path / "trust.json"))
        fingerprint = "a" * 64

        empty = runner.invoke(app, ["trust", "bootnode", "list"])
        assert empty.exit_code == 0
        assert "No bootnodes" in empty.output

        added = runner.invoke(
            app, ["trust", "bootnode", "add", "boot.example.net", fingerprint, "--port", "1443"]
        )
        assert added.exit_code == 0, added.output
        assert str(tmp_path / "trust.json") in added.output

        listed = runner.invoke(app, ["trust", "bootnode", "list"])
        assert fingerprint in listed.output
        assert "boot.example.net:1443" in listed.output

        shown = runner.invoke(
            app, ["trust", "bootnode", "show", "boot.example.net", "--port", "1443"]
        )
        assert shown.exit_code == 0
        assert fingerprint in shown.output

        removed = runner.invoke(
            app, ["trust", "bootnode", "remove", "boot.example.net", "--port", "1443"]
        )
        assert removed.exit_code == 0

        missing = runner.invoke(
            app, ["trust", "bootnode", "show", "boot.example.net", "--port", "1443"]
        )
        assert missing.exit_code == 1

    def test_add_rejects_an_invalid_fingerprint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_BOOTNODE_TRUST_FILE", str(tmp_path / "trust.json"))
        result = runner.invoke(app, ["trust", "bootnode", "add", "boot.example.net", "nope"])
        assert result.exit_code == 1

    def test_list_json(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setenv("HSYNC_BOOTNODE_TRUST_FILE", str(tmp_path / "trust.json"))
        result = runner.invoke(
            app,
            ["trust", "bootnode", "add", "boot.example.net", "a" * 64, "--port", "1443"],
        )
        assert result.exit_code == 0
        listed = runner.invoke(app, ["trust", "bootnode", "list", "--json"])
        assert listed.exit_code == 0
        payload = json.loads(listed.output)
        assert payload == [
            {
                "host": "boot.example.net",
                "port": 1443,
                "fingerprint": "a" * 64,
                "updated_at": payload[0]["updated_at"],
                "first_seen": payload[0]["first_seen"],
                "source": "cli",
            }
        ]


class TestPeerTrustCommands:
    """``hsync trust peer`` manages the known-peers store (mirrors bootnode trust)."""

    def test_list_add_remove(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(tmp_path / "pins.json"))
        fingerprint = "b" * 64

        empty = runner.invoke(app, ["trust", "peer", "list"])
        assert empty.exit_code == 0
        assert "No peers" in empty.output

        added = runner.invoke(
            app,
            ["trust", "peer", "add", "--bucket", "photos", "master-1", f"sha256:{fingerprint}"],
        )
        assert added.exit_code == 0, added.output
        assert str(tmp_path / "pins.json") in added.output

        listed = runner.invoke(app, ["trust", "peer", "list"])
        assert listed.exit_code == 0
        assert "master-1|photos" in listed.output
        assert fingerprint in listed.output

        removed = runner.invoke(app, ["trust", "peer", "remove", "--bucket", "photos", "master-1"])
        assert removed.exit_code == 0

        missing = runner.invoke(app, ["trust", "peer", "remove", "--bucket", "photos", "master-1"])
        assert missing.exit_code == 0
        assert "was not in the known-peers store" in missing.output

    def test_add_rejects_an_invalid_fingerprint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(tmp_path / "pins.json"))
        result = runner.invoke(
            app, ["trust", "peer", "add", "--bucket", "photos", "master-1", "nope"]
        )
        assert result.exit_code == 1

    def test_list_json(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setenv("HSYNC_P2P_PIN_FILE", str(tmp_path / "pins.json"))
        runner.invoke(app, ["trust", "peer", "add", "--bucket", "photos", "master-1", "a" * 64])
        listed = runner.invoke(app, ["trust", "peer", "list", "--json"])
        assert listed.exit_code == 0
        assert json.loads(listed.output) == [
            {"cid": "master-1", "bucket": "photos", "fingerprint": "a" * 64}
        ]


class TestServerTrustCommands:
    """``hsync trust server`` manages the sync-server known-hosts store (ADR-0036)."""

    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_SERVER_TRUST_FILE", str(tmp_path / "servers.json"))

    def test_add_list_show_remove(self):
        fingerprint = "b" * 64

        empty = runner.invoke(app, ["trust", "server", "list"])
        assert empty.exit_code == 0
        assert "No servers" in empty.output

        added = runner.invoke(
            app,
            [
                "trust",
                "server",
                "add",
                "server.example.com",
                f"sha256:{fingerprint}",
                "--port",
                "9753",
            ],
        )
        assert added.exit_code == 0, added.output
        assert "server.example.com:9753" in added.output

        listed = runner.invoke(app, ["trust", "server", "list"])
        assert listed.exit_code == 0
        assert "server.example.com:9753" in listed.output
        assert fingerprint in listed.output

        shown = runner.invoke(
            app, ["trust", "server", "show", "server.example.com", "--port", "9753"]
        )
        assert shown.exit_code == 0
        assert fingerprint in shown.output

        removed = runner.invoke(
            app, ["trust", "server", "remove", "server.example.com", "--port", "9753"]
        )
        assert removed.exit_code == 0
        missing = runner.invoke(
            app, ["trust", "server", "remove", "server.example.com", "--port", "9753"]
        )
        assert "was not in the known-hosts store" in missing.output

    def test_show_unknown_server_exits_nonzero(self):
        result = runner.invoke(app, ["trust", "server", "show", "unknown.example.com"])
        assert result.exit_code == 1

    def test_add_rejects_an_invalid_fingerprint(self):
        result = runner.invoke(app, ["trust", "server", "add", "server.example.com", "nope"])
        assert result.exit_code == 1

    def test_list_json(self):
        import json

        runner.invoke(app, ["trust", "server", "add", "server.example.com", "a" * 64])
        listed = runner.invoke(app, ["trust", "server", "list", "--json"])
        assert listed.exit_code == 0
        payload = json.loads(listed.output)
        assert payload[0]["host"] == "server.example.com"
        assert payload[0]["fingerprint"] == "a" * 64
