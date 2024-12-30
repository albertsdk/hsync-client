"""
Tests for LAN embedded HTTP server HTTPS functionality.
"""

import pytest

from hsync_client.lan_server import LANServer


class TestLANServerHTTPS:
    """Tests for LANServer HTTPS functionality."""

    def test_server_https_requires_cert(self):
        """Test that HTTPS mode requires certificate files."""
        with pytest.raises(ValueError, match="cert_file and key_file are required"):
            LANServer(port=9999, use_https=True)

    def test_server_https_property(self):
        """Test use_https property."""
        server = LANServer(port=9999, use_https=False)
        assert server.use_https is False
