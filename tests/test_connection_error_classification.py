"""Stable transport-failure codes for the desktop connectivity badge."""

from __future__ import annotations

from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
)
from requests.exceptions import (
    ProxyError,
    SSLError,
    Timeout,
)

from hsync_client import sync as sync_mod
from hsync_client.exceptions import NetworkError, ServerError
from hsync_client.utils.http import classify_connection_error


class TestClassifyConnectionError:
    def test_tls(self):
        assert classify_connection_error(SSLError("CERTIFICATE_VERIFY_FAILED")) == "tls"

    def test_proxy(self):
        assert classify_connection_error(ProxyError("proxy boom")) == "proxy"

    def test_timeout(self):
        assert classify_connection_error(Timeout("timed out")) == "timeout"

    def test_dns(self):
        error = RequestsConnectionError("Name or service not known")
        assert classify_connection_error(error) == "dns"

    def test_refused(self):
        error = RequestsConnectionError("Connection refused")
        assert classify_connection_error(error) == "refused"

    def test_unreachable(self):
        error = RequestsConnectionError("No route to host")
        assert classify_connection_error(error) == "unreachable"

    def test_unknown(self):
        assert classify_connection_error(ValueError("nope")) == "unknown"

    def test_a_wrapped_network_error_uses_the_chained_cause(self):
        cause = SSLError("hostname mismatch")
        try:
            raise NetworkError(message="Sync failed") from cause
        except NetworkError as exc:
            assert classify_connection_error(exc) == "tls"

    def test_a_wrapped_network_error_falls_back_to_its_message(self):
        # Most sync paths wrap the requests error without chaining it; the
        # message still carries the original text.
        error = NetworkError(message="Error downloading files list: Connection refused")
        assert classify_connection_error(error) == "refused"


class TestReportSyncFailure:
    def _capture(self, monkeypatch) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            sync_mod, "emit_event", lambda event, **payload: events.append((str(event), payload))
        )
        return events

    def test_a_network_error_carries_reason_and_code(self, monkeypatch):
        events = self._capture(monkeypatch)

        sync_mod._report_sync_failure(
            NetworkError(message="Error downloading files list: Connection refused")
        )

        assert events
        event, payload = events[0]
        assert event == "error"
        assert payload["reason"] == "connection"
        assert payload["code"] == "refused"

    def test_a_server_error_is_not_a_connection_reason(self, monkeypatch):
        events = self._capture(monkeypatch)

        sync_mod._report_sync_failure(ServerError(message="500 Internal Server Error"))

        event, payload = events[0]
        assert event == "error"
        assert payload["reason"] == ""
        assert "code" not in payload

    def test_a_missing_bucket_carries_the_share_missing_reason(self, monkeypatch):
        """The GUI localizes this reason and offers to create the share."""
        from hsync_client.exceptions import BucketMissingError

        events = self._capture(monkeypatch)

        sync_mod._report_sync_failure(BucketMissingError(message="Bucket 'barbara' does not exist"))

        event, payload = events[0]
        assert event == "error"
        assert payload["reason"] == "share_missing"
