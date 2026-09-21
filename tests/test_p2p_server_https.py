"""
Tests for P2P embedded HTTP server HTTPS functionality.
"""

import pytest

from hsync_client.p2p_server import P2PServer


class TestLANServerHTTPS:
    """Tests for P2PServer HTTPS functionality."""

    def test_server_https_requires_cert(self):
        """Test that HTTPS mode requires certificate files."""
        with pytest.raises(ValueError, match="cert_file and key_file are required"):
            P2PServer(port=9999, use_https=True)

    def test_server_https_property(self):
        """Test use_https property."""
        server = P2PServer(port=9999, use_https=False)
        assert server.use_https is False
