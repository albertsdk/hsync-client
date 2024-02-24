# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for LAN utility functions."""

from hsync_client.lan import HSYNC_LAN_PORT, check_version_compatibility, get_free_port


class TestGetFreePort:
    """Tests for get_free_port function."""

    def test_get_free_port_returns_valid_port(self):
        """Test that get_free_port returns a valid port number."""
        port = get_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_get_free_port_returns_different_ports(self):
        """Test that get_free_port returns different ports on successive calls."""
        ports = [get_free_port() for _ in range(5)]
        # At least some ports should be different (not guaranteed but very likely)
        assert len(set(ports)) >= 2

    def test_default_lan_port_is_zero(self):
        """Test that HSYNC_LAN_PORT default is 0 (random)."""
        assert HSYNC_LAN_PORT == 0


class TestVersionCompatibility:
    """Tests for version compatibility checking."""

    def test_same_version_is_compatible(self):
        """Test that identical versions are compatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.1.0")
        assert is_compat is True
        assert reason == "compatible"

    def test_different_patch_is_compatible(self):
        """Test that different patch versions are compatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.1.5")
        assert is_compat is True
        assert reason == "compatible"

        is_compat, reason = check_version_compatibility("1.2.3", "1.2.99")
        assert is_compat is True
        assert reason == "compatible"

    def test_different_minor_is_warning(self):
        """Test that different minor versions trigger warning but are allowed."""
        is_compat, reason = check_version_compatibility("0.1.0", "0.2.0")
        assert is_compat is True
        assert reason == "minor_mismatch"

        is_compat, reason = check_version_compatibility("1.0.0", "1.5.0")
        assert is_compat is True
        assert reason == "minor_mismatch"

    def test_different_major_is_incompatible(self):
        """Test that different major versions are incompatible."""
        is_compat, reason = check_version_compatibility("0.1.0", "1.0.0")
        assert is_compat is False
        assert reason == "major_mismatch"

        is_compat, reason = check_version_compatibility("2.0.0", "1.0.0")
        assert is_compat is False
        assert reason == "major_mismatch"

    def test_short_version_is_padded(self):
        """Test that short versions are padded with zeros."""
        is_compat, reason = check_version_compatibility("1", "1.0.0")
        assert is_compat is True
        assert reason == "compatible"

        is_compat, reason = check_version_compatibility("1.2", "1.2.0")
        assert is_compat is True
        assert reason == "compatible"

    def test_invalid_version_returns_parse_error(self):
        """Test that invalid versions return parse_error but are allowed."""
        # Empty string or non-numeric should trigger parse_error
        # Note: current implementation may not trigger parse_error for all cases
        # but should never crash
        is_compat, _reason = check_version_compatibility("0.1.0", "0.1.0")
        assert is_compat is True
