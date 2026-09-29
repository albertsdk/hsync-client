# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the terminal trust-prompt selection (ADR-0035)."""

import pytest
from hsync_common.protocol.bootnode_trust import TrustPolicy

from hsync_client import trust_prompt


@pytest.fixture
def tty(monkeypatch):
    """Force the TTY probe so the selection logic can be exercised."""
    state = {"isatty": True}
    monkeypatch.setattr(trust_prompt, "is_tty_interactive", lambda: state["isatty"])
    return state


@pytest.mark.parametrize("policy", [TrustPolicy.accept_new, TrustPolicy.strict, TrustPolicy.none])
def test_non_ask_policies_never_prompt(policy, tty):
    assert trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": policy}) is None


def test_ask_without_a_terminal_fails_closed(tty):
    tty["isatty"] = False
    assert trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": TrustPolicy.ask}) is None


def test_ask_on_a_terminal_uses_the_ssh_style_prompt(tty):
    prompt = trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": TrustPolicy.ask})
    assert prompt is trust_prompt.terminal_trust_prompt


def test_yes_auto_accepts_only_with_a_terminal(tty):
    prompt = trust_prompt.build_trust_prompt(
        {"BOOTNODE_TRUST": TrustPolicy.ask, "AUTO_CONFIRM": True}
    )
    assert prompt is not None and prompt("h", 443, "f" * 64) is True
    tty["isatty"] = False
    assert (
        trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": TrustPolicy.ask, "AUTO_CONFIRM": True})
        is None
    )
