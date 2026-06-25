# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Shared pytest fixtures for the hsync-client test suite.
"""

import pytest


@pytest.fixture(autouse=True)
def deterministic_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Force plain, uncolored CLI output for every test.

    CI environments (e.g. GitHub Actions) make Rich emit ANSI styling, which
    highlights option names like ``--force-master`` and injects escape codes
    between the dashes and words. That breaks substring assertions on
    ``result.output``. Setting ``TERM=dumb`` disables Rich's color system
    entirely, so error messages stay literal regardless of the host terminal.
    """
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("PY_COLORS", raising=False)
