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

    CI environments (e.g. GitHub Actions) export ``FORCE_COLOR``, which makes
    Rich emit ANSI styling. The highlighter then wraps option names like
    ``--force-master`` in escape codes, splitting the substring and breaking
    assertions on ``result.output``. Clearing the color-forcing variables lets
    Rich fall back to plain output for the captured (non-terminal) stream while
    still honouring an explicit ``COLUMNS`` (so width-sensitive help tests can
    widen the terminal without being clamped, as ``TERM=dumb`` would).
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("PY_COLORS", raising=False)
