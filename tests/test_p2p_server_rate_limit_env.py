# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The P2P request-rate env knobs must fail safe, never open.

A non-finite or non-positive value would either silently disable the token
bucket (``nan``/``inf`` make every comparison false, so ``allow`` returns True)
or raise in the limiter constructor and abort startup; ``_env_number`` rejects
them instead of propagating them.
"""

from __future__ import annotations

import pytest

from hsync_client.p2p_server import P2P_REQ_RATE_REFILL, _env_number


class TestEnvNumber:
    def test_absent_returns_default(self, monkeypatch):
        monkeypatch.delenv("HSYNC_TEST_KNOB", raising=False)
        assert _env_number("HSYNC_TEST_KNOB", 7.0) == 7.0

    def test_valid_value_is_used(self, monkeypatch):
        monkeypatch.setenv("HSYNC_TEST_KNOB", "123.5")
        assert _env_number("HSYNC_TEST_KNOB", 7.0) == 123.5

    @pytest.mark.parametrize("raw", ["", "abc", "nan", "inf", "-inf", "0", "-5"])
    def test_invalid_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv("HSYNC_TEST_KNOB", raw)
        assert _env_number("HSYNC_TEST_KNOB", 7.0) == 7.0

    def test_default_constant_is_positive(self):
        assert P2P_REQ_RATE_REFILL > 0
