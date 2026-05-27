"""
Tests for hsync_client.ssl_utils module.

Tests SSL certificate generation, saving, fingerprint verification.
"""

import hashlib
import os
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from hsync_client.ssl_utils import (
    CertificateInfo,
    create_fingerprint_ssl_context,
    generate_self_signed_cert,
    save_cert_to_temp_files,
    verify_cert_fingerprint,
)


class TestCertificateInfo:
    """Tests for CertificateInfo dataclass."""

    def test_default_fields(self):
        """cert_file, key_file and key_password default to empty/None."""
        info = CertificateInfo(cert_pem=b"cert", key_pem=b"key", fingerprint="abc")
        assert info.cert_file is None
        assert info.key_file is None
        assert info.key_password == b""

    def test_all_fields(self):
        """All fields can be set."""
        info = CertificateInfo(
            cert_pem=b"cert",
            key_pem=b"key",
            fingerprint="abc",
            key_password=b"secret",
            cert_file="/tmp/cert.pem",
            key_file="/tmp/key.pem",
        )
        assert info.cert_file == "/tmp/cert.pem"
        assert info.key_file == "/tmp/key.pem"
        assert info.key_password == b"secret"


class TestGenerateSelfSignedCert:
    """Tests for generate_self_signed_cert function."""

    def test_generates_cert_and_key(self):
        """Certificate and encrypted PKCS#8 key are generated as PEM bytes."""
        info = generate_self_signed_cert()
        assert isinstance(info.cert_pem, bytes)
        assert isinstance(info.key_pem, bytes)
        assert b"BEGIN CERTIFICATE" in info.cert_pem
        assert b"BEGIN ENCRYPTED PRIVATE KEY" in info.key_pem

    def test_key_password_is_generated(self):
        """A non-empty session password is generated."""
        info = generate_self_signed_cert()
        assert isinstance(info.key_password, bytes)
        assert len(info.key_password) == 32

    def test_key_password_decrypts_key(self):
        """The generated password can decrypt the key."""
        info = generate_self_signed_cert()
        key = serialization.load_pem_private_key(info.key_pem, password=info.key_password)
        assert key is not None

    def test_key_password_is_unique_per_call(self):
        """Each call generates a fresh session password."""
        info1 = generate_self_signed_cert()
        info2 = generate_self_signed_cert()
        assert info1.key_password != info2.key_password

    def test_fingerprint_is_hex_sha256(self):
        """Fingerprint is a 64-char hex SHA-256 digest."""
        info = generate_self_signed_cert()
        assert len(info.fingerprint) == 64
        # All hex chars
        int(info.fingerprint, 16)

    def test_fingerprint_matches_cert(self):
        """Fingerprint matches SHA-256 of the DER-encoded certificate."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        der = cert.public_bytes(serialization.Encoding.DER)
        expected = hashlib.sha256(der).hexdigest()
        assert info.fingerprint == expected

    def test_custom_common_name(self):
        """Custom common name is set in the certificate."""
        info = generate_self_signed_cert(common_name="test-node")
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "test-node"

    def test_default_common_name(self):
        """Default common name is 'hsync-lan'."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "hsync-lan"

    def test_cert_has_san_extension(self):
        """Certificate includes Subject Alternative Name extension."""
        info = generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_each_call_produces_unique_cert(self):
        """Each call produces a unique certificate."""
        info1 = generate_self_signed_cert()
        info2 = generate_self_signed_cert()
        assert info1.fingerprint != info2.fingerprint


class TestSaveCertToTempFiles:
    """Tests for save_cert_to_temp_files function."""

    @staticmethod
    def _cleanup(saved):
        import shutil

        if saved.cert_file:
            shutil.rmtree(os.path.dirname(saved.cert_file), ignore_errors=True)

    def test_creates_temp_files(self):
        """Temporary files are created with cert and (encrypted) key content."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)

        try:
            assert saved.cert_file is not None
            assert saved.key_file is not None
            assert os.path.exists(saved.cert_file)
            assert os.path.exists(saved.key_file)

            with open(saved.cert_file, "rb") as f:
                assert f.read() == info.cert_pem
            with open(saved.key_file, "rb") as f:
                assert f.read() == info.key_pem
        finally:
            self._cleanup(saved)

    def test_preserves_fingerprint_and_password(self):
        """Saved CertificateInfo preserves fingerprint and key_password."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)
        try:
            assert saved.fingerprint == info.fingerprint
            assert saved.key_password == info.key_password
        finally:
            self._cleanup(saved)

    def test_temp_dir_has_restrictive_permissions(self):
        """The temp directory is created with 0o700 permissions."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)
        try:
            tmpdir = os.path.dirname(saved.cert_file)  # type: ignore[arg-type]
            assert (os.stat(tmpdir).st_mode & 0o777) == 0o700
        finally:
            self._cleanup(saved)

    def test_key_file_has_restrictive_permissions(self):
        """The key file is created with 0o600 permissions."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)
        try:
            assert (os.stat(saved.key_file).st_mode & 0o777) == 0o600  # type: ignore[arg-type]
        finally:
            self._cleanup(saved)

    def test_key_on_disk_is_encrypted(self):
        """The key written on disk is always PKCS#8 encrypted."""
        info = generate_self_signed_cert()
        saved = save_cert_to_temp_files(info)
        try:
            with open(saved.key_file, "rb") as f:  # type: ignore[arg-type]
                pem = f.read()
            assert pem.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
            # Wrong password cannot decrypt
            with pytest.raises(ValueError):
                serialization.load_pem_private_key(pem, password=b"wrong")
            # Correct session password can decrypt
            key = serialization.load_pem_private_key(pem, password=saved.key_password)
            assert key is not None
        finally:
            self._cleanup(saved)

    def test_refuses_without_password(self):
        """save_cert_to_temp_files refuses a CertificateInfo without password."""
        info = generate_self_signed_cert()
        # Strip the password to simulate a misuse
        bad = CertificateInfo(
            cert_pem=info.cert_pem,
            key_pem=info.key_pem,
            fingerprint=info.fingerprint,
            key_password=b"",
        )
        with pytest.raises(ValueError, match="key_password is required"):
            save_cert_to_temp_files(bad)


class TestCreateFingerprintSslContext:
    """Tests for create_fingerprint_ssl_context function."""

    def test_returns_ssl_context(self):
        """Returns an ssl.SSLContext."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert isinstance(ctx, ssl.SSLContext)

    def test_no_hostname_check(self):
        """Context does not check hostname."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert ctx.check_hostname is False

    def test_no_cert_verification(self):
        """Context uses CERT_NONE for fingerprint-based verification."""
        ctx = create_fingerprint_ssl_context("abc123")
        assert ctx.verify_mode == ssl.CERT_NONE


class TestVerifyCertFingerprint:
    """Tests for verify_cert_fingerprint function."""

    def test_matching_fingerprint(self):
        """Matching fingerprint returns True."""
        data = b"test certificate data"
        fingerprint = hashlib.sha256(data).hexdigest()
        assert verify_cert_fingerprint(data, fingerprint) is True

    def test_mismatching_fingerprint(self):
        """Mismatching fingerprint returns False."""
        data = b"test certificate data"
        assert verify_cert_fingerprint(data, "0" * 64) is False

    def test_case_insensitive(self):
        """Fingerprint comparison is case-insensitive."""
        data = b"test certificate data"
        fingerprint = hashlib.sha256(data).hexdigest()
        assert verify_cert_fingerprint(data, fingerprint.upper()) is True


class TestGetServerCertFingerprint:
    """Tests for get_server_cert_fingerprint function."""

    def test_get_fingerprint_connection_error(self):
        """Returns None on connection error."""
        from hsync_client.ssl_utils import get_server_cert_fingerprint

        # Non-routable IP to cause connection timeout
        result = get_server_cert_fingerprint("10.255.255.1", 9999)
        assert result is None

    def test_get_fingerprint_invalid_host(self):
        """Returns None for invalid host."""
        from hsync_client.ssl_utils import get_server_cert_fingerprint

        result = get_server_cert_fingerprint("invalid.hostname.that.does.not.exist", 443)
        assert result is None
