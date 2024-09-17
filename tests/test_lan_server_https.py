"""
Tests for LAN embedded HTTP server HTTPS functionality.
"""

import time

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

    def test_server_https_with_cert(self, tmp_path, monkeypatch):
        """Test creating an HTTPS server with the persistent master certificate."""
        from hsync_client.ssl_utils import MASTER_CERT_DIR_ENV, load_or_create_master_cert

        monkeypatch.setenv(MASTER_CERT_DIR_ENV, str(tmp_path))
        cert_info = load_or_create_master_cert()

        server = LANServer(
            port=0,
            use_https=True,
            cert_file=cert_info.cert_file,
            key_file=cert_info.key_file,
        )
        assert server.use_https is True
        server.start()
        time.sleep(0.1)
        assert server.is_running() is True
        server.stop()
