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
    FingerprintHTTPAdapter,
    create_fingerprint_ssl_context,
    default_master_cert_dir,
    load_or_create_master_cert,
    verify_cert_fingerprint,
    verify_peer_cert_fingerprint,
)


@pytest.fixture
def cert_dir(tmp_path, monkeypatch):
    """Point the persistent master cert dir at an isolated temp directory.

    The keypair lives in the device identity directory (``HSYNC_DEVICE_DIR``);
    the conftest isolation fixture already points it at the per-test
    ``*-isolation/hsync_device`` sibling of *tmp_path* — redirect it to
    *tmp_path* itself for these tests.
    """
    monkeypatch.setenv("HSYNC_DEVICE_DIR", str(tmp_path))
    return tmp_path


class TestCertificateInfo:
    """Tests for CertificateInfo dataclass."""

    def test_default_fields(self):
        """cert_file and key_file default to None."""
        info = CertificateInfo(cert_pem=b"cert", key_pem=b"key", fingerprint="abc")
        assert info.cert_file is None
        assert info.key_file is None

    def test_all_fields(self):
        """All fields can be set."""
        info = CertificateInfo(
            cert_pem=b"cert",
            key_pem=b"key",
            fingerprint="abc",
            cert_file="/tmp/cert.pem",
            key_file="/tmp/key.pem",
        )
        assert info.cert_file == "/tmp/cert.pem"
        assert info.key_file == "/tmp/key.pem"


class TestLoadOrCreateMasterCert:
    """Tests for load_or_create_master_cert function."""

    def test_generates_cert_and_key_on_first_use(self, cert_dir):
        """Certificate and unencrypted PKCS#8 key are generated as PEM bytes."""
        info = load_or_create_master_cert()
        assert isinstance(info.cert_pem, bytes)
        assert isinstance(info.key_pem, bytes)
        assert b"BEGIN CERTIFICATE" in info.cert_pem
        assert b"BEGIN PRIVATE KEY" in info.key_pem

    def test_writes_persistent_files(self, cert_dir):
        """The keypair is persisted under the configured cert dir."""
        info = load_or_create_master_cert()
        assert info.cert_file is not None
        assert info.key_file is not None
        assert os.path.exists(info.cert_file)
        assert os.path.exists(info.key_file)

    def test_fingerprint_stable_across_calls(self, cert_dir):
        """A second call loads the same identity (TOFU stability)."""
        first = load_or_create_master_cert()
        second = load_or_create_master_cert()
        assert first.fingerprint == second.fingerprint
        assert first.cert_pem == second.cert_pem
        assert first.key_pem == second.key_pem

    def test_key_is_unencrypted_pkcs8(self, cert_dir):
        """The persisted key is loadable without a password."""
        info = load_or_create_master_cert()
        key = serialization.load_pem_private_key(info.key_pem, password=None)
        assert key is not None

    def test_generates_ecdsa_p256_identity(self, cert_dir):
        """The identity is ECDSA P-256 (shared service-identity model)."""
        from cryptography.hazmat.primitives.asymmetric import ec

        info = load_or_create_master_cert()
        key = serialization.load_pem_private_key(info.key_pem, password=None)
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)
        assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)

    def test_validity_is_twenty_years(self, cert_dir):
        """Validity is the shared 20-year service-identity lifetime + backdate."""
        from datetime import timedelta

        info = load_or_create_master_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        span = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert span >= timedelta(days=365 * 19)

    def test_rejects_non_ecdsa_identity(self, cert_dir):
        """A pre-existing non-ECDSA keypair is refused, not silently reused."""
        from hsync_common.ssl_utils import (
            build_self_signed_certificate,
            generate_rsa_private_key,
            serialize_cert_pem,
            serialize_private_key_unencrypted,
        )

        rsa_key = generate_rsa_private_key()
        cert = build_self_signed_certificate(
            rsa_key,
            subject_attrs=[x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "hsync-lan")],
            san_list=[x509.DNSName("localhost")],
            validity_days=365,
        )
        (cert_dir / "p2p_master_cert.pem").write_bytes(serialize_cert_pem(cert))
        (cert_dir / "p2p_master_key.pem").write_bytes(serialize_private_key_unencrypted(rsa_key))

        with pytest.raises(ValueError, match="ECDSA P-256"):
            load_or_create_master_cert()

    def test_fingerprint_is_hex_sha256(self, cert_dir):
        """Fingerprint is a 64-char hex SHA-256 digest."""
        info = load_or_create_master_cert()
        assert len(info.fingerprint) == 64
        # All hex chars
        int(info.fingerprint, 16)

    def test_fingerprint_matches_cert(self, cert_dir):
        """Fingerprint matches SHA-256 of the DER-encoded certificate."""
        info = load_or_create_master_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        der = cert.public_bytes(serialization.Encoding.DER)
        assert info.fingerprint == hashlib.sha256(der).hexdigest()

    def test_custom_common_name(self, cert_dir):
        """Custom common name is set in the certificate."""
        info = load_or_create_master_cert(common_name="test-node")
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "test-node"

    def test_default_common_name(self, cert_dir):
        """Default common name is 'hsync-lan'."""
        info = load_or_create_master_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        assert cn.value == "hsync-lan"

    def test_cert_has_san_extension(self, cert_dir):
        """Certificate includes Subject Alternative Name extension."""
        info = load_or_create_master_cert()
        cert = x509.load_pem_x509_certificate(info.cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_key_file_has_restrictive_permissions(self, cert_dir):
        """The key file is created with 0o600 permissions."""
        info = load_or_create_master_cert()
        assert (os.stat(info.key_file).st_mode & 0o777) == 0o600  # type: ignore[arg-type]

    def test_cert_dir_has_restrictive_permissions(self, cert_dir):
        """The cert directory is created with 0o700 permissions."""
        load_or_create_master_cert()
        assert (os.stat(cert_dir).st_mode & 0o777) == 0o700


class TestDefaultMasterCertDir:
    """Tests for default_master_cert_dir function."""

    def test_env_override(self, monkeypatch):
        """HSYNC_DEVICE_DIR overrides the resolved directory."""
        monkeypatch.setenv("HSYNC_DEVICE_DIR", "/custom/cert/dir")
        assert str(default_master_cert_dir()) == "/custom/cert/dir"

    def test_xdg_state_home(self, monkeypatch):
        """Falls back to $XDG_STATE_HOME when no override is set."""
        monkeypatch.delenv("HSYNC_DEVICE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
        result = default_master_cert_dir()
        assert str(result) == os.path.join("/xdg/state", "hsync", "device")

    def test_default_local_state(self, monkeypatch):
        """Falls back to ~/.local/state when nothing else is set."""
        monkeypatch.delenv("HSYNC_DEVICE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        result = default_master_cert_dir()
        assert result.name == "device"
        assert "hsync" in result.parts


class TestFingerprintHTTPAdapter:
    """Tests for FingerprintHTTPAdapter."""

    def test_normalises_fingerprint_to_lowercase(self):
        """The asserted fingerprint is stored lowercased."""
        adapter = FingerprintHTTPAdapter("ABCDEF")
        assert adapter._assert_fingerprint == "abcdef"

    def test_pin_kwargs_sets_fingerprint_and_disables_chain(self):
        """_pin_kwargs injects assert_fingerprint and a CERT_NONE context."""
        adapter = FingerprintHTTPAdapter("abc")
        kwargs = adapter._pin_kwargs({})
        assert kwargs["assert_fingerprint"] == "abc"
        assert kwargs["assert_hostname"] is False
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_NONE
        assert kwargs["ssl_context"].check_hostname is False


class TestVerifyPeerCertFingerprint:
    """Tests for verify_peer_cert_fingerprint function."""

    def test_none_ssl_object_returns_false(self):
        """A missing TLS object never verifies."""
        assert verify_peer_cert_fingerprint(None, "abc") is False

    def test_matching_peer_cert_returns_true(self):
        """A peer cert whose DER matches the fingerprint verifies."""
        der = b"peer certificate der"
        fingerprint = hashlib.sha256(der).hexdigest()

        class _FakeSSL:
            def getpeercert(self, binary_form=False):
                return der

        assert verify_peer_cert_fingerprint(_FakeSSL(), fingerprint) is True

    def test_mismatching_peer_cert_returns_false(self):
        """A peer cert whose DER does not match the fingerprint fails."""

        class _FakeSSL:
            def getpeercert(self, binary_form=False):
                return b"peer certificate der"

        assert verify_peer_cert_fingerprint(_FakeSSL(), "0" * 64) is False

    def test_no_peer_cert_returns_false(self):
        """When the peer presents no certificate, verification fails."""

        class _FakeSSL:
            def getpeercert(self, binary_form=False):
                return None

        assert verify_peer_cert_fingerprint(_FakeSSL(), "abc") is False


class TestCreateFingerprintSslContext:
    """Tests for create_fingerprint_ssl_context function."""

    def test_returns_ssl_context(self):
        """Returns an ssl.SSLContext."""
        ctx = create_fingerprint_ssl_context("a" * 64)
        assert isinstance(ctx, ssl.SSLContext)

    def test_no_hostname_check(self):
        """Context does not check hostname."""
        ctx = create_fingerprint_ssl_context("a" * 64)
        assert ctx.check_hostname is False

    def test_no_cert_verification(self):
        """Context uses CERT_NONE for fingerprint-based verification."""
        ctx = create_fingerprint_ssl_context("a" * 64)
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
