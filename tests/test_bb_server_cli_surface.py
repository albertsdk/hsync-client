# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for the client CLI surface.

Drives the public Typer entry point only (help/version/argument parsing).
No internal modules are imported or mocked.

Requires HSYNC_BB_SERVER=1 to run.
"""

import os

import pytest
from typer.testing import CliRunner

from hsync_client.cli import app

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)

runner = CliRunner()


class TestCliSurface:
    def test_help_shows_description(self):
        # Given the CLI
        # When invoking --help
        result = runner.invoke(app, ["--help"])

        # Then the description is shown and the command succeeds
        assert result.exit_code == 0
        assert "HTTP-based file synchronization client" in result.output

    def test_version_option(self):
        # Given the CLI
        # When invoking --version
        result = runner.invoke(app, ["--version"])

        # Then the version is shown
        assert result.exit_code == 0
        assert "hsync" in result.output

    def test_missing_bucket_argument_errors(self):
        # Given no bucket argument
        # When invoking with no args
        result = runner.invoke(app, ["server"])

        # Then the CLI errors
        assert result.exit_code != 0
        assert "bucket" in result.output.lower() or "missing" in result.output.lower()

    def test_invalid_bucket_name_rejected(self):
        # Given a bucket name with forbidden characters
        # When invoking
        result = runner.invoke(
            app, ["server", "-b", "bucket.name", "--server-url", "test.example.com"]
        )

        # Then the CLI rejects it
        assert result.exit_code != 0

    def test_bucket_with_hyphens_and_underscores_accepted(self):
        # Given a valid bucket name and a valid full URL
        # When invoking with a plain-HTTP URL (the only scheme the CLI refuses)
        result = runner.invoke(
            app,
            [
                "server",
                "-b",
                "valid-bucket_name",
                "--server-url",
                "http://server.example.com",
                "--allow-deletions",
            ],
        )

        # Then the bucket name parses (no bucket validation error) and the
        # rejected value is the scheme, not the bucket
        assert result.exit_code != 0
        assert "must be alphanumeric" not in result.output
        assert "https" in result.output.lower()

    def test_plain_http_url_rejected(self):
        # Given a http:// URL (server mode is HTTPS-only)
        # When invoking
        result = runner.invoke(
            app, ["server", "-b", "demo", "--server-url", "http://server.example.com"]
        )

        # Then the CLI reports a usage error and no sync starts
        assert result.exit_code != 0
        assert "https" in result.output.lower()

    def test_missing_server_url_fails_fast(self):
        # Given no --server-url and no HSYNC_SERVER_URL
        # When invoking
        result = runner.invoke(app, ["server", "-b", "demo"])

        # Then the CLI reports a usage error and no sync starts
        assert result.exit_code != 0
        assert "sync-server url" in result.output.lower()

    def test_capabilities_json_outputs_valid_json(self):
        """--capabilities-json produces valid JSON with expected keys."""
        import json

        result = runner.invoke(app, ["--capabilities-json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "app_version" in data
        assert "cli_options" in data

    def test_version_is_eager_global_option(self):
        """--version is a global parent option: it exits 0 before the subcommand
        (and its required options) is parsed."""
        result = runner.invoke(app, ["--version", "server"])
        assert result.exit_code == 0
        # The eager parent callback prints and exits before subcommand dispatch,
        # so the required -b/--bucket of ``server`` is never demanded.
        assert "hsync" in result.output
        assert "Missing option" not in result.output
