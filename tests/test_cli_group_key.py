# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The ``hsync derive-group-key`` operator helper."""

from hsync_common.protocol.rdv_tokens import derive_group_key
from typer.testing import CliRunner

from hsync_client.cli import app

runner = CliRunner()


def _combined(result) -> str:
    return result.output + getattr(result, "stderr", "")


def test_prints_the_derived_key():
    result = runner.invoke(app, ["derive-group-key"], input="correct horse battery\n")

    assert result.exit_code == 0
    assert derive_group_key("correct horse battery") in result.output


def test_rejects_a_short_password():
    result = runner.invoke(app, ["derive-group-key"], input="short\n")

    assert result.exit_code == 1
    assert "at least 12" in _combined(result)
