"""
Tests for hsync_client.retry module.

Tests retry decorators with exponential backoff for network operations.
"""

import pytest
from requests import RequestException

from hsync_client.exceptions import NetworkError, ServerError
from hsync_client.retry import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_WAIT,
    DEFAULT_MIN_WAIT,
    retry_on_connection_error,
    retry_on_network_error,
    retry_on_transient_transfer_error,
)


class TestRetryOnNetworkError:
    """Tests for retry_on_network_error decorator."""

    def test_success_no_retry(self):
        """Successful function is not retried."""
        call_count = 0

        @retry_on_network_error(max_attempts=3)
        def successful():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = successful()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_request_exception(self):
        """Function is retried on RequestException."""
        call_count = 0

        @retry_on_network_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise RequestException("connection refused")

        with pytest.raises(RequestException):
            failing()

        assert call_count == 3

    def test_retries_on_network_error(self):
        """Function is retried on NetworkError."""
        call_count = 0

        @retry_on_network_error(max_attempts=2, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise NetworkError("timeout")

        with pytest.raises(NetworkError):
            failing()

        assert call_count == 2

    def test_succeeds_after_retry(self):
        """Function succeeds after transient failure."""
        call_count = 0

        @retry_on_network_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RequestException("temporary failure")
            return "recovered"

        result = flaky()
        assert result == "recovered"
        assert call_count == 3

    def test_does_not_retry_on_value_error(self):
        """Non-network exceptions are not retried."""
        call_count = 0

        @retry_on_network_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def bad_input():
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid input")

        with pytest.raises(ValueError):
            bad_input()

        assert call_count == 1


class TestRetryOnConnectionError:
    """Tests for retry_on_connection_error decorator."""

    def test_success_no_retry(self):
        """Successful function is not retried."""
        call_count = 0

        @retry_on_connection_error(max_attempts=3)
        def successful():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = successful()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_request_exception(self):
        """Function is retried on RequestException."""
        call_count = 0

        @retry_on_connection_error(max_attempts=2, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise RequestException("connection refused")

        with pytest.raises(RequestException):
            failing()

        assert call_count == 2

    def test_does_not_retry_on_network_error(self):
        """NetworkError is NOT retried by connection error decorator."""
        call_count = 0

        @retry_on_connection_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise NetworkError("custom error")

        with pytest.raises(NetworkError):
            failing()

        assert call_count == 1


class TestRetryOnTransientTransferError:
    """Tests for retry_on_transient_transfer_error decorator."""

    def test_success_no_retry(self):
        """Successful function is not retried."""
        call_count = 0

        @retry_on_transient_transfer_error(max_attempts=3)
        def successful():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert successful() == "ok"
        assert call_count == 1

    def test_retries_on_request_exception(self):
        """Connection-level errors are retried."""
        call_count = 0

        @retry_on_transient_transfer_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise RequestException("connection refused")

        with pytest.raises(RequestException):
            failing()

        assert call_count == 3

    def test_retries_on_server_error(self):
        """Server-side 5xx (ServerError) is retried."""
        call_count = 0

        @retry_on_transient_transfer_error(max_attempts=2, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise ServerError("500")

        with pytest.raises(ServerError):
            failing()

        assert call_count == 2

    def test_does_not_retry_on_network_error(self):
        """A plain NetworkError (e.g. permanent) is NOT retried."""
        call_count = 0

        @retry_on_transient_transfer_error(max_attempts=3, min_wait=0, max_wait=0.01)
        def failing():
            nonlocal call_count
            call_count += 1
            raise NetworkError("auth")

        with pytest.raises(NetworkError):
            failing()

        assert call_count == 1


class TestRetryDefaults:
    """Tests for retry default constants."""

    def test_default_max_attempts(self):
        """Default max attempts is 3."""
        assert DEFAULT_MAX_ATTEMPTS == 3

    def test_default_min_wait(self):
        """Default min wait is 1 second."""
        assert DEFAULT_MIN_WAIT == 1

    def test_default_max_wait(self):
        """Default max wait is 10 seconds."""
        assert DEFAULT_MAX_WAIT == 10
