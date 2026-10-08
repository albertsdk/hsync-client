# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the terminal trust-prompt selection (ADR-0035)."""

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

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


def test_ask_prompt_is_not_auto_accepted(tty):
    """Policy ``ask`` always uses the SSH-style prompt; there is no auto-accept."""
    prompt = trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": TrustPolicy.ask})
    assert prompt is trust_prompt.terminal_trust_prompt
    tty["isatty"] = False
    assert trust_prompt.build_trust_prompt({"BOOTNODE_TRUST": TrustPolicy.ask}) is None


def test_server_policy_key_and_prompt(tty):
    """The server policy uses its own key and the server subject prompt."""
    prompt = trust_prompt.build_trust_prompt(
        {"SERVER_TRUST": TrustPolicy.ask},
        policy_key="SERVER_TRUST",
        prompt=trust_prompt.server_terminal_trust_prompt,
    )
    assert prompt is trust_prompt.server_terminal_trust_prompt
    # The bootnode policy key is not consulted when the server key is selected.
    assert (
        trust_prompt.build_trust_prompt(
            {"BOOTNODE_TRUST": TrustPolicy.ask},
            policy_key="SERVER_TRUST",
            prompt=trust_prompt.server_terminal_trust_prompt,
        )
        is None
    )
