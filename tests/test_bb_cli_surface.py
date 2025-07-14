# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the client CLI surface.

Drives the public Typer entry point only (help/version/argument parsing).
No internal modules are imported or mocked.
"""

from typer.testing import CliRunner

from hsync_client.cli import app

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
        result = runner.invoke(app, [])

        # Then the CLI errors
        assert result.exit_code != 0
        assert "bucket" in result.output.lower() or "missing" in result.output.lower()

    def test_invalid_bucket_name_rejected(self):
        # Given a bucket name with forbidden characters
        # When invoking
        result = runner.invoke(app, ["-b", "bucket.name", "-s", "http://test:8000"])

        # Then the CLI rejects it
        assert result.exit_code != 0

    def test_bucket_with_hyphens_and_underscores_accepted(self):
        # Given a valid bucket name
        # When invoking (run_sync is not actually executed: server is unreachable
        # so the call returns early, but argument parsing must succeed)
        result = runner.invoke(
            app, ["-b", "valid-bucket_name", "-s", "http://127.0.0.1:1", "--yes"]
        )

        # Then the name is accepted (parsing passes; exit 0 since the
        # unreachable server makes run_sync return without error)
        assert "Invalid" not in result.output

    def test_capabilities_json_outputs_valid_json(self):
        """--capabilities-json produces valid JSON with expected keys."""
        import json

        result = runner.invoke(app, ["--capabilities-json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "app_version" in data
        assert "cli_options" in data

    def test_quiet_with_version_shows_version(self):
        """--quiet does not interfere with --version (eager callback)."""
        result = runner.invoke(app, ["--quiet", "--version"])
        assert result.exit_code == 0
        # --version is eager and always prints; --quiet affects sync logs only
        assert "hsync" in result.output
