# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the CLI module using Typer's CliRunner."""

import runpy
import signal
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from hsync_client.cli import app
from hsync_client.config import get_env_settings
from hsync_client.constants import RENDEZVOUS_DEFAULT_PORT

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

    def test_sigterm_becomes_a_keyboard_interrupt(self):
        """SIGTERM must unwind like Ctrl+C, not kill the process outright.

        Regression: the default SIGTERM action terminates immediately, so the
        ``finally`` blocks that stop the P2P/server tasks never ran, the last log
        records stayed queued and the sinks kept the semaphores the resource
        tracker then reported as leaked — the ``8 leaked semaphore objects``
        warning printed on every stop (the desktop's Stop button sends SIGTERM).
        """
        from hsync_client.cli import _raise_keyboard_interrupt

        with pytest.raises(KeyboardInterrupt):
            _raise_keyboard_interrupt(signal.SIGTERM, None)

    def test_run_installs_the_sigterm_handler(self):
        """The process entry point wires the handler before running the CLI."""
        from hsync_client.cli import _install_sigterm_handler, _raise_keyboard_interrupt

        previous = signal.getsignal(signal.SIGTERM)
        try:
            _install_sigterm_handler()
            assert signal.getsignal(signal.SIGTERM) is _raise_keyboard_interrupt
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_run_closes_the_log_sinks_on_every_exit_path(self):
        """``run`` drains and closes the sinks after the CLI returns or raises."""
        from hsync_client.cli import run

        with (
            patch("hsync_client.cli._install_sigterm_handler"),
            patch("hsync_client.cli.app", side_effect=SystemExit(3)),
            patch("hsync_client.logger.shutdown_logging") as mock_shutdown,
        ):
            with pytest.raises(SystemExit):
                run()

        mock_shutdown.assert_called_once_with()

    def test_run_turns_typers_interrupted_exit_into_a_clean_stop(self):
        """Typer's ``Exit(130)`` after an interruption must not fail the run.

        Typer converts ``KeyboardInterrupt`` into ``Exit(130)`` inside its own
        runner (``typer/core.py::main``), before the exception can reach a
        handler: without this remap a service stop exited 130, which a service
        manager records as a failed stop.
        """
        from hsync_client.cli import _INTERRUPTED_EXIT_CODE, run

        with (
            patch("hsync_client.cli._install_sigterm_handler"),
            patch("hsync_client.cli.app", side_effect=SystemExit(_INTERRUPTED_EXIT_CODE)),
            patch("hsync_client.logger.shutdown_logging"),
        ):
            run()  # returns normally, so the process exits 0

    def test_run_keeps_every_other_exit_code(self):
        """Only the interruption code is remapped: real failures still exit."""
        from hsync_client.cli import run

        with (
            patch("hsync_client.cli._install_sigterm_handler"),
            patch("hsync_client.cli.app", side_effect=SystemExit(1)),
            patch("hsync_client.logger.shutdown_logging"),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run()

        assert excinfo.value.code == 1


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
        result = runner.invoke(
            app, ["server", "-b", "testbucket", "--server-url", "test.example.com"]
        )
        assert result.exit_code == 0
        mock_run_sync.assert_called_once()
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "testbucket"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_long_option(self, mock_run_sync):
        """CLI accepts --bucket option."""
        result = runner.invoke(
            app, ["server", "--bucket", "mybucket123", "--server-url", "test.example.com"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "mybucket123"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_bucket_with_hyphens_and_underscores(self, mock_run_sync):
        """CLI accepts bucket names with hyphens and underscores."""
        result = runner.invoke(
            app, ["server", "-b", "test-bucket", "--server-url", "test.example.com"]
        )
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["bucket"] == "test-bucket"
        result = runner.invoke(
            app, ["server", "-b", "test_bucket", "--server-url", "test.example.com"]
        )
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
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com:8000"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["bucket"] == "test"
        assert call_kwargs["server_url"] == "https://test.example.com:8000"

    @patch("hsync_client.main_logic.run_sync")
    def test_default_is_continuous(self, mock_run_sync):
        """Without --once the client runs continuously (infinite retry)."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["continuous"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_once_flag(self, mock_run_sync):
        """CLI accepts --once as the sole one-shot form."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--once", "--server-url", "test.example.com"]
        )
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
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "-v"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["debug"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_quiet_flag(self, mock_run_sync):
        """CLI accepts -q/--quiet flag."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "-q"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["quiet"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_dry_run_flag(self, mock_run_sync):
        """CLI accepts -n/--dry-run flag."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "-n"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["dryrun"] is True

    @patch("hsync_client.main_logic.run_sync")
    def test_insecure_flag(self, mock_run_sync):
        """CLI accepts --insecure flag."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--insecure"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["verify_ssl"] is False

    @patch("hsync_client.main_logic.run_sync")
    def test_color_flag(self, mock_run_sync):
        """CLI accepts --color=never (and the other color modes)."""
        from hsync_common.runtime import ColorMode

        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--color", "never"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["color"] is ColorMode.NEVER

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
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "-p", "/tmp/sync"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["local_path"] == "/tmp/sync"

    @patch("hsync_client.main_logic.run_sync")
    def test_ca_cert_option(self, mock_run_sync):
        """CLI accepts --ca-cert option."""
        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "--server-url",
                "test.example.com",
                "--ca-cert",
                "/path/to/ca.crt",
            ],
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["ca_cert"] == "/path/to/ca.crt"

    @patch("hsync_client.main_logic.run_sync")
    def test_output_option(self, mock_run_sync):
        """CLI accepts --log-file option."""
        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "--server-url",
                "test.example.com",
                "--log-file",
                "/tmp/log.txt",
            ],
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["logfile"] == "/tmp/log.txt"

    @patch("hsync_client.main_logic.run_sync")
    def test_subdir_option(self, mock_run_sync):
        """CLI accepts --subdir option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--subdir", "subdir"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["filter_path"] == "subdir"

    @patch("hsync_client.main_logic.run_sync")
    def test_retry_delay_option(self, mock_run_sync):
        """CLI accepts --retry-delay option."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--retry-delay", "10"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["reconnect_delay"] == 10

    @patch("hsync_client.main_logic.run_sync")
    def test_default_values(self, mock_run_sync):
        """CLI sets correct default values."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "test.example.com"])
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
        assert call_kwargs["peer_rendezvous_timeout"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_rendezvous_timeout_accepts_zero(self, mock_run_sync):
        """--peer-rendezvous-timeout 0 is accepted as wait-indefinitely."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--peer-rendezvous-timeout", "0"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["peer_rendezvous_timeout"] == 0

    @patch("hsync_client.main_logic.run_sync")
    def test_rendezvous_fingerprint_and_tofu_flow(self, mock_run_sync):
        hex64 = "0" * 64
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "boot.example.net",
                "--rendezvous-server-fingerprint",
                f"boot.example.net:443={hex64}",
                "--rendezvous-server-tofu",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_run_sync.call_args.kwargs
        assert kwargs["rendezvous_server_pins"] == {"boot.example.net:443": (hex64,)}
        assert kwargs["rendezvous_server_tofu"] is True

    def test_rendezvous_ca_cert_is_refused(self):
        """--ca-cert has no rendezvous-channel meaning: refuse it, not silently ignore."""
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "boot.example.net",
                "--ca-cert",
                "/tmp/ca.pem",
            ],
        )
        assert result.exit_code != 0
        assert "ca-cert" in result.output

    def test_insecure_conflicts_with_rendezvous_fingerprint(self):
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "boot.example.net",
                "--insecure",
                "--rendezvous-server-fingerprint",
                "0" * 64,
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_rendezvous_fingerprint_requires_rendezvous(self):
        result = runner.invoke(
            app, ["p2p", "-b", "test", "--rendezvous-server-fingerprint", "0" * 64]
        )
        assert result.exit_code != 0
        assert "at least one" in result.output

    def test_a_pin_for_an_unconfigured_server_is_refused(self):
        """A pin can only describe a server the client actually dials."""
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "a.example",
                "--rendezvous-server-fingerprint",
                f"b.example={'0' * 64}",
            ],
        )
        assert result.exit_code != 0
        assert "not a configured" in result.output

    def test_a_bare_pin_is_refused_with_several_servers(self):
        """The shorthand is unambiguous only with exactly one endpoint."""
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "a.example,b.example",
                "--rendezvous-server-fingerprint",
                "0" * 64,
            ],
        )
        assert result.exit_code != 0
        assert "ambiguous" in result.output

    def test_each_server_keeps_its_own_pins(self, mock_run_sync=None):
        """Two endpoints, two identities: every pin lands on its own server."""
        from unittest.mock import patch

        fp_a, fp_b = "a" * 64, "b" * 64
        with patch("hsync_client.main_logic.run_sync") as run_sync:
            result = runner.invoke(
                app,
                [
                    "p2p",
                    "-b",
                    "test",
                    "--rendezvous-server",
                    "a.example,b.example:8443",
                    "--rendezvous-server-fingerprint",
                    f"a.example:443={fp_a}",
                    "--rendezvous-server-fingerprint",
                    f"b.example:8443={fp_b}",
                ],
            )
        assert result.exit_code == 0, result.output
        pins = run_sync.call_args.kwargs["rendezvous_server_pins"]
        assert pins["a.example:443"] == (fp_a,)
        assert pins["b.example:8443"] == (fp_b,)

    def test_rendezvous_fingerprint_rejects_invalid(self):
        result = runner.invoke(
            app,
            [
                "p2p",
                "-b",
                "test",
                "--rendezvous-server",
                "boot.example.net",
                "--rendezvous-server-fingerprint",
                "zzz",
            ],
        )
        assert result.exit_code != 0
        assert "invalid --rendezvous-server-fingerprint" in result.output

    def test_help_documents_rendezvous_timeout_zero(self):
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
            app,
            [
                "server",
                "-b",
                "test",
                "--server-url",
                "test.example.com",
                "--env-file",
                "/path/to/env",
            ],
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
    def test_p2p_trust_default_omitted(self, mock_run_sync):
        """Without --p2p-trust nothing is forwarded (the client default applies)."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_trust"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_p2p_trust_option(self, mock_run_sync):
        """CLI accepts --p2p-trust POLICY and forwards it unchanged."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--p2p-trust", "strict"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["p2p_trust"] == "strict"

    @patch("hsync_client.main_logic.run_sync")
    def test_deletion_policy_default_omitted(self, mock_run_sync):
        """Without --deletion-policy nothing is forwarded (the default applies)."""
        result = runner.invoke(app, ["p2p", "-b", "test"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["deletion_policy"] is None

    @patch("hsync_client.main_logic.run_sync")
    def test_deletion_policy_keep_is_forwarded(self, mock_run_sync):
        """--deletion-policy keep reaches run_sync unchanged."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--deletion-policy", "keep"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["deletion_policy"] == "keep"

    def test_deletion_policy_rejects_unknown_values(self):
        """A policy outside the contract is a usage error, not a silent default."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--deletion-policy", "nope"])
        assert result.exit_code != 0
        assert "invalid --deletion-policy" in result.output

    def test_p2p_trust_rejects_non_pinning_policy(self):
        """chain/none never pin, so they are rejected as a usage error."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--p2p-trust", "none"])
        assert result.exit_code != 0
        assert "invalid --p2p-trust" in result.output

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
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--allow-deletions"]
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
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "test.example.com"])
        assert result.exit_code == 0
        call_kwargs = mock_run_sync.call_args.kwargs
        assert call_kwargs["allow_deletions"] is False


class TestCLIValidation:
    """Tests for CLI parameter validation."""

    def test_rejects_http_timeout_below_min(self):
        """CLI rejects --http-timeout below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--http-timeout", "0"]
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
                "--server-url",
                "test.example.com",
                "--http-timeout",
                str(HTTP_TIMEOUT_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "timeout" in result.output.lower()

    def test_rejects_resync_interval_below_min(self):
        """CLI rejects --interval below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--interval", "0"]
        )
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_resync_interval_above_max(self):
        """CLI rejects --interval above maximum."""
        from hsync_client.constants import RESYNC_MAX

        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "--server-url",
                "test.example.com",
                "--interval",
                str(RESYNC_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "interval" in result.output.lower()

    def test_rejects_retry_delay_below_min(self):
        """CLI rejects --retry-delay below minimum."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "test.example.com", "--retry-delay", "0"]
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
                "--server-url",
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
            app,
            ["server", "-b", "test", "--server-url", "test.example.com", "--stability-delay", "0"],
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
                "--server-url",
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
            app,
            ["server", "-b", "test", "--server-url", "test.example.com", "--max-wait-timeout", "0"],
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
                "--server-url",
                "test.example.com",
                "--max-wait-timeout",
                str(MAX_WAIT_TIMEOUT_MAX + 1),
            ],
        )
        assert result.exit_code != 0
        assert "max wait" in result.output.lower() or "timeout" in result.output.lower()

    def test_rejects_negative_rendezvous_timeout(self):
        """CLI rejects negative --peer-rendezvous-timeout values."""
        result = runner.invoke(app, ["p2p", "-b", "test", "--peer-rendezvous-timeout", "-1"])
        assert result.exit_code != 0
        assert "rendezvous timeout" in result.output.lower()

    def test_rejects_invalid_sync_direction(self):
        """CLI rejects invalid --sync-direction value."""
        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "test",
                "--server-url",
                "test.example.com",
                "--sync-direction",
                "invalid",
            ],
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
        """HOST[:PORT] assembles the https:// URL."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "hsync.example.com:443"]
        )
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:443"

    @patch("hsync_client.main_logic.run_sync")
    def test_default_port_is_implied(self, mock_run_sync):
        """A bare host uses the hsync-server default port."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "hsync.example.com"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:9753"

    @patch("hsync_client.main_logic.run_sync")
    def test_bare_ipv6_literal_is_bracketed(self, mock_run_sync):
        """A bare IPv6 literal becomes the bracketed https:// host part."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "[::1]:9753"])
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://[::1]:9753"

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_resolves_from_env(self, mock_run_sync, monkeypatch):
        """No HOSTNAME resolves the full URL from HSYNC_SERVER_URL."""
        monkeypatch.setenv("HSYNC_SERVER_URL", "https://env.example.com:9753")
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test"])
            assert result.exit_code == 0
            assert mock_run_sync.call_args.kwargs["server_url"] == "https://env.example.com:9753"
        finally:
            get_env_settings.cache_clear()

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_resolves_from_env_file(self, mock_run_sync, monkeypatch, tmp_path):
        """A --env-file supplying HSYNC_SERVER_URL satisfies the server requirement."""
        monkeypatch.delenv("HSYNC_SERVER_URL", raising=False)
        env_file = tmp_path / "prod.env"
        env_file.write_text("HSYNC_SERVER_URL=https://file.example.com:9753\n", encoding="utf-8")
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test", "--env-file", str(env_file)])
            assert result.exit_code == 0
            assert mock_run_sync.call_args.kwargs["server_url"] == "https://file.example.com:9753"
        finally:
            get_env_settings.cache_clear()

    @patch("hsync_client.main_logic.run_sync")
    def test_missing_hostname_without_server_fails_fast(self, mock_run_sync, monkeypatch):
        """No HOSTNAME and no HSYNC_SERVER_URL is a usage error before run_sync."""
        monkeypatch.delenv("HSYNC_SERVER_URL", raising=False)
        get_env_settings.cache_clear()
        try:
            result = runner.invoke(app, ["server", "-b", "test"])
            assert result.exit_code != 0
            assert "sync-server url" in result.output.lower()
            mock_run_sync.assert_not_called()
        finally:
            get_env_settings.cache_clear()

    def test_accepts_a_full_https_url(self):
        """--server-url takes the full URL form, path included."""
        with patch("hsync_client.main_logic.run_sync") as mock_run_sync:
            result = runner.invoke(
                app, ["server", "-b", "test", "--server-url", "https://hsync.example.com/hsync"]
            )
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com/hsync"

    @patch("hsync_client.main_logic.run_sync")
    def test_accepts_host_port_shorthand(self, mock_run_sync):
        """HOST:PORT assembles the https:// URL."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "hsync.example.com:9753"]
        )
        assert result.exit_code == 0
        assert mock_run_sync.call_args.kwargs["server_url"] == "https://hsync.example.com:9753"

    def test_rejects_plain_http_scheme(self):
        """Server mode always uses HTTPS: an http:// URL is a usage error."""
        result = runner.invoke(
            app, ["server", "-b", "test", "--server-url", "http://hsync.example.com"]
        )
        assert result.exit_code != 0
        assert "https" in result.output.lower()

    def test_rejects_an_empty_server_url(self):
        """An empty --server-url cannot express the server."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", ""])
        assert result.exit_code != 0
        assert "missing sync-server url" in result.output.lower()

    def test_rejects_port_out_of_range(self):
        """The endpoint port accepts only 1-65535."""
        result = runner.invoke(app, ["server", "-b", "test", "--server-url", "hsync.example.com:0"])
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
            "--p2p-trust",
            "--no-peer-download",
            "--allow-unauthenticated-writes",
        ):
            assert flag in doc["cli_options"]["p2p"]
        for flag in ("--server-url", "--insecure", "--ca-cert"):
            assert flag in doc["cli_options"]["server"]
        assert "--listen-port" not in doc["cli_options"]["server"]
        # C2: the p2p subcommand owns --port (the rendezvous UDP port) and
        # --map-data-port; --listen-port is the master TCP port and stays p2p-only.
        for flag in (
            "--rendezvous-server",
            "--map-data-port",
            "--insecure",
            "--ca-cert",
            "--listen-port",
        ):
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
        assert set(options) == {"p2p", "server", "trust", "state", "derive-group-key"}
        for flags in options.values():
            assert flags == sorted(flags)
            assert all(f.startswith("--") for f in flags)
        assert "--limit-upload" in options["p2p"]
        assert "--server-url" in options["server"]
        assert "--listen-port" not in options["server"]
        assert "--rendezvous-server" in options["p2p"]
        assert "--map-data-port" in options["p2p"]


class TestBuildRendezvousServers:
    """The rendezvous-server resolution rules: one option, one grammar."""

    def test_no_server_selects_the_mdns_plan(self):
        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(None) == ()

    def test_default_port_is_the_channel_default(self):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["boot.example.net"]) == (
            HostPort("boot.example.net", RENDEZVOUS_DEFAULT_PORT),
        )

    def test_endpoint_port_is_preserved(self):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["boot.example.net:8443"]) == (
            HostPort("boot.example.net", 8443),
        )

    def test_repeatable_option_lists_every_server_in_order(self):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["a.example", "b.example:8443"]) == (
            HostPort("a.example", RENDEZVOUS_DEFAULT_PORT),
            HostPort("b.example", 8443),
        )

    def test_a_value_may_carry_the_env_comma_list(self):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["a.example,b.example:53"]) == (
            HostPort("a.example", RENDEZVOUS_DEFAULT_PORT),
            HostPort("b.example", 53),
        )

    def test_listing_the_same_endpoint_twice_is_refused(self):
        """One destination listed twice is a config error, not two leases."""
        from hsync_client.cli import _build_rendezvous_servers

        with pytest.raises(typer.BadParameter):
            _build_rendezvous_servers(["a.example", "a.example:443"])

    def test_blank_value_is_ignored(self):
        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["   "]) == ()

    @pytest.mark.parametrize(
        "bad",
        [
            "https://boot.example.net",
            "boot.example.net/path",
            "boot.example.net:0",
            "a.example,,b.example",
        ],
    )
    def test_invalid_input_is_refused(self, bad):
        from hsync_client.cli import _build_rendezvous_servers

        with pytest.raises(typer.BadParameter):
            _build_rendezvous_servers([bad])

    def test_ipv6_bracketed_is_returned_unbracketed(self):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers

        assert _build_rendezvous_servers(["[::1]"]) == (HostPort("::1", RENDEZVOUS_DEFAULT_PORT),)

    def test_env_is_resolved_when_use_env(self, monkeypatch):
        from hsync_common.endpoint import HostPort

        from hsync_client.cli import _build_rendezvous_servers
        from hsync_client.config import get_env_settings

        monkeypatch.setenv("HSYNC_RENDEZVOUS_SERVER_ENDPOINTS", "a.example,b.example:8443")
        get_env_settings.cache_clear()
        try:
            assert _build_rendezvous_servers(None, use_env=True) == (
                HostPort("a.example", RENDEZVOUS_DEFAULT_PORT),
                HostPort("b.example", 8443),
            )
        finally:
            get_env_settings.cache_clear()

    def test_env_is_not_used_without_use_env(self, monkeypatch):
        from hsync_client.cli import _build_rendezvous_servers
        from hsync_client.config import get_env_settings

        monkeypatch.setenv(
            "HSYNC_RENDEZVOUS_SERVER_ENDPOINTS", "--rendezvous-server", "boot.example.net"
        )
        get_env_settings.cache_clear()
        try:
            assert _build_rendezvous_servers(None) == ()
        finally:
            get_env_settings.cache_clear()


class TestRendezvousTrustCommands:
    """``hsync trust rendezvous-server`` manages the known-hosts store (ADR-0035)."""

    def test_add_list_show_remove(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_RENDEZVOUS_SERVER_TRUST_FILE", str(tmp_path / "trust.json"))
        fingerprint = "a" * 64

        empty = runner.invoke(app, ["trust", "rendezvous-server", "list"])
        assert empty.exit_code == 0
        assert "No rendezvous servers" in empty.output

        added = runner.invoke(
            app, ["trust", "rendezvous-server", "add", "boot.example.net:1443", fingerprint]
        )
        assert added.exit_code == 0, added.output
        assert str(tmp_path / "trust.json") in added.output

        listed = runner.invoke(app, ["trust", "rendezvous-server", "list"])
        assert fingerprint in listed.output
        assert "boot.example.net:1443" in listed.output

        shown = runner.invoke(app, ["trust", "rendezvous-server", "show", "boot.example.net:1443"])
        assert shown.exit_code == 0
        assert fingerprint in shown.output

        removed = runner.invoke(
            app, ["trust", "rendezvous-server", "remove", "boot.example.net:1443"]
        )
        assert removed.exit_code == 0

        missing = runner.invoke(
            app, ["trust", "rendezvous-server", "show", "boot.example.net:1443"]
        )
        assert missing.exit_code == 1

    def test_add_rejects_an_invalid_fingerprint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_RENDEZVOUS_SERVER_TRUST_FILE", str(tmp_path / "trust.json"))
        result = runner.invoke(
            app, ["trust", "rendezvous-server", "add", "boot.example.net", "nope"]
        )
        assert result.exit_code == 1

    def test_list_json(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setenv("HSYNC_RENDEZVOUS_SERVER_TRUST_FILE", str(tmp_path / "trust.json"))
        result = runner.invoke(
            app,
            ["trust", "rendezvous-server", "add", "boot.example.net:1443", "a" * 64],
        )
        assert result.exit_code == 0
        listed = runner.invoke(app, ["trust", "rendezvous-server", "list", "--json"])
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
    """``hsync trust peer`` manages the known-peers store (mirrors rendezvous trust)."""

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
        entries = json.loads(listed.output)
        assert [(e["cid"], e["bucket"], e["fingerprint"]) for e in entries] == [
            ("master-1", "photos", "a" * 64)
        ]
        assert entries[0]["first_seen"]


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
                "server.example.com:9753",
                f"sha256:{fingerprint}",
            ],
        )
        assert added.exit_code == 0, added.output
        assert "server.example.com:9753" in added.output

        listed = runner.invoke(app, ["trust", "server", "list"])
        assert listed.exit_code == 0
        assert "server.example.com:9753" in listed.output
        assert fingerprint in listed.output

        shown = runner.invoke(app, ["trust", "server", "show", "server.example.com:9753"])
        assert shown.exit_code == 0
        assert fingerprint in shown.output

        removed = runner.invoke(app, ["trust", "server", "remove", "server.example.com:9753"])
        assert removed.exit_code == 0
        missing = runner.invoke(app, ["trust", "server", "remove", "server.example.com:9753"])
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


def test_common_run_sync_kwargs_maps_and_normalizes_shared_options():
    """The shared CLI adapter keeps the two command keyword sets identical."""
    from hsync_client import cli

    values = {
        "bucket": "bucket",
        "env_file": None,
        "insecure": False,
        "ca_cert": None,
        "http_timeout": 30,
        "events_json_fd": None,
        "local_path": "/tmp/local",
        "once": False,
        "resync": 0,
        "reconnect_delay": 5,
        "stability_delay": 5,
        "max_wait_timeout": 120,
        "filter_path": "",
        "dryrun": False,
        "deletion_policy": "propagate",
        "sync_direction": "bidirectional",
        "no_compression": False,
        "no_delta": True,
        "delta_min_file_size": 1024,
        "delta_min_file_size_p2p": 1024,
        "parsed_bw_upload": None,
        "parsed_bw_download": None,
        "verbose": False,
        "quiet": False,
        "logfile": "",
        "color": "auto",
        "allow_deletions": True,
        "parsed_min_free_disk_space": 0,
        "max_inbound_size": 0,
        "ignore_patterns": [" a ", ""],
        "max_concurrent_transfers": 4,
        "no_autotune": False,
        "mass_delete_guard": True,
        "mass_delete_threshold": None,
        "mass_delete_percent": None,
    }

    kwargs = cli._common_run_sync_kwargs(values)

    assert kwargs["bucket"] == "bucket"
    assert kwargs["verify_ssl"] is True
    assert kwargs["continuous"] is True
    assert kwargs["compression_enabled"] is True
    assert kwargs["delta_enabled"] is False
    assert kwargs["ignore_patterns"] == ["a"]
    assert kwargs["debug"] is False
