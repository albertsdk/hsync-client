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

    Typer forces a colored terminal whenever ``GITHUB_ACTIONS`` (or
    ``FORCE_COLOR``/``PY_COLORS``) is set, so on CI its Rich highlighter wraps
    option names like ``--force-master`` in ANSI codes, splitting the substring
    and breaking assertions on ``result.output``. That decision is baked into
    ``typer.rich_utils.FORCE_TERMINAL`` at import time, so clearing the env vars
    later is useless; instead we override the module global, which
    ``_get_rich_console`` re-reads on every call. ``False`` disables color/style
    entirely while still honouring an explicit ``COLUMNS`` (unlike ``TERM=dumb``,
    which would clamp width to 80 and wrap width-sensitive help tests).
    """
    import typer.rich_utils as rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False, raising=False)
