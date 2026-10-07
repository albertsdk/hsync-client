# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for P2P utility functions."""

from hsync_client.p2p_peers import HSYNC_P2P_LISTEN_PORT, get_free_port


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

    def test_default_p2p_listen_port_is_zero(self):
        """Test that HSYNC_P2P_LISTEN_PORT default is 0 (random)."""
        assert HSYNC_P2P_LISTEN_PORT == 0
