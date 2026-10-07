# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests for the black-box polling helper ``conftest._wait_for``.

The helper is what every black-box wait is built on, so its contract is pinned
here (it runs in the plain unit suite, not behind ``HSYNC_BB_*``):

* a predicate that eventually turns truthy wins within the timeout;
* a predicate that keeps raising an *ignored* transient error keeps polling and
  can still succeed (the case that made a server-mode wait abort the whole test
  instead of using its own retry window);
* an empty ``ignore`` (the default) lets exceptions propagate unchanged;
* an exception type outside ``ignore`` propagates unchanged.
"""

import pytest
import requests

from tests.conftest import _wait_for


def test_returns_true_when_the_predicate_settles():
    state = {"n": 0}

    def predicate():
        state["n"] += 1
        return state["n"] >= 3

    assert _wait_for(predicate, timeout=5.0, interval=0.01) is True
    assert state["n"] == 3


def test_returns_false_on_timeout():
    assert _wait_for(lambda: False, timeout=0.1, interval=0.01) is False


def test_ignored_transient_error_keeps_polling_and_can_still_succeed():
    """A truncated download mid-replace is "not yet", not a test failure."""
    state = {"n": 0}

    def predicate():
        state["n"] += 1
        if state["n"] < 3:
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
        return True

    assert (
        _wait_for(
            predicate,
            timeout=5.0,
            interval=0.01,
            ignore=(requests.exceptions.RequestException,),
        )
        is True
    )
    assert state["n"] == 3


def test_ignored_transient_error_times_out_instead_of_aborting():
    def predicate():
        raise requests.exceptions.ConnectionError("server still restarting")

    assert (
        _wait_for(
            predicate,
            timeout=0.1,
            interval=0.01,
            ignore=(requests.exceptions.RequestException,),
        )
        is False
    )


def test_without_ignore_the_exception_propagates():
    def predicate():
        raise requests.exceptions.ChunkedEncodingError("boom")

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        _wait_for(predicate, timeout=5.0, interval=0.01)


def test_a_non_ignored_exception_propagates():
    """A programming error inside the predicate is never mistaken for "not yet"."""

    def predicate():
        raise ValueError("bug in the predicate")

    with pytest.raises(ValueError):
        _wait_for(
            predicate,
            timeout=5.0,
            interval=0.01,
            ignore=(requests.exceptions.RequestException,),
        )
