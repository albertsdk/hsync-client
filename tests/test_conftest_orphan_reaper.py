# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests for the autouse orphan reaper (``conftest``).

``_kill_orphan_hsync_processes`` calls ``_terminate_child_hsync_processes``
after every test. The earlier implementation always slept 0.3 s even when no
child existed, which dominated the wall time of the unit suite. These tests pin
the two properties that keep it both correct and cheap:

* a real direct child whose command line matches the pattern is terminated;
* the common path (no matching child) does not sleep at all.

The child command line carries a unique marker so the test never targets
unrelated processes: ``pgrep -P <pytest_pid> -f <pattern>`` is scoped to the
direct children of *this* pytest process.
"""

import subprocess
import sys
import time
import uuid

from tests.conftest import _terminate_child_hsync_processes


def test_terminates_a_matching_child():
    """A direct child whose argv matches the pattern is terminated and reaped."""
    marker = f"pytest_reaper_marker_{uuid.uuid4().hex}"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", marker],
    )
    try:
        _terminate_child_hsync_processes(patterns=(marker,))
        assert child.wait(timeout=5) is not None
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_common_path_does_not_sleep(monkeypatch):
    """With no matching child the helper returns without any wait."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))

    _terminate_child_hsync_processes(patterns=(f"pytest_reaper_none_{uuid.uuid4().hex}",))

    assert calls == []
