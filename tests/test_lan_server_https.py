"""
Tests for LAN embedded HTTP server HTTPS functionality.
"""

import time

import pytest

from hsync_client.lan_server import LANServer


class TestSSLUtils:
    """Tests for SSL certificate generation utilities."""

    def test_generate_self_signed_cert(self):
        """Test generating a self-signed certificate."""
        from hsync_client.ssl_utils import CertificateInfo, generate_self_signed_cert

        cert_info = generate_self_signed_cert()

        assert isinstance(cert_info, CertificateInfo)
        assert cert_info.cert_pem is not None
        assert cert_info.key_pem is not None
        assert cert_info.fingerprint is not None
        # Fingerprint should be a hex string (SHA-256 = 64 characters)
        assert len(cert_info.fingerprint) == 64
        assert cert_info.cert_file is None  # Not saved yet
        assert cert_info.key_file is None

    def test_generate_cert_with_custom_cn(self):
        """Test generating a certificate with custom common name."""
        from hsync_client.ssl_utils import generate_self_signed_cert

        cert_info = generate_self_signed_cert(common_name="test-hsync")
        assert cert_info.cert_pem is not None
        assert b"BEGIN CERTIFICATE" in cert_info.cert_pem

    def test_save_cert_to_temp_files(self):
        """Test saving certificate to temporary files."""
        import os

        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files

        cert_info = generate_self_signed_cert()
        saved_info = save_cert_to_temp_files(cert_info)

        assert saved_info.cert_file is not None
        assert saved_info.key_file is not None
        assert os.path.exists(saved_info.cert_file)
        assert os.path.exists(saved_info.key_file)

        # Verify file contents
        with open(saved_info.cert_file, "rb") as f:
            assert f.read() == cert_info.cert_pem
        with open(saved_info.key_file, "rb") as f:
            assert f.read() == cert_info.key_pem

        # Cleanup
        os.unlink(saved_info.cert_file)
        os.unlink(saved_info.key_file)

    def test_fingerprint_is_deterministic(self):
        """Test that same cert produces same fingerprint."""
        import hashlib

        from cryptography import x509

        # Generate a cert and verify fingerprint calculation
        from hsync_client.ssl_utils import generate_self_signed_cert

        cert_info = generate_self_signed_cert()

        # Parse the cert and recalculate fingerprint
        cert = x509.load_pem_x509_certificate(cert_info.cert_pem)
        from cryptography.hazmat.primitives import serialization

        expected_fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

        assert cert_info.fingerprint == expected_fp


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

    def test_server_https_with_cert(self):
        """Test creating HTTPS server with certificate."""
        import os

        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files

        cert_info = generate_self_signed_cert()
        saved_info = save_cert_to_temp_files(cert_info)

        try:
            server = LANServer(
                port=0,
                use_https=True,
                cert_file=saved_info.cert_file,
                key_file=saved_info.key_file,
            )
            assert server.use_https is True
            server.start()
            time.sleep(0.1)
            assert server.is_running() is True
            server.stop()
        finally:
            if saved_info.cert_file:
                os.unlink(saved_info.cert_file)
            if saved_info.key_file:
                os.unlink(saved_info.key_file)
