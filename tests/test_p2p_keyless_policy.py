# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the keyless P2P confirmation gate and the bypass warnings."""

from unittest.mock import MagicMock, patch

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.main_logic import _check_p2p_keyless_policy
from hsync_client.security_warnings import BYPASS_WARNINGS, log_active_bypasses


@pytest.fixture(autouse=True)
def _interactive_tty():
    with patch("hsync_client.main_logic.is_tty_interactive", return_value=True):
        yield


class TestCheckKeylessPolicy:
    def test_not_p2p_skips(self):
        mock_logger = MagicMock()
        _check_p2p_keyless_policy(
            p2p_mode=False,
            p2p_key=None,
            allow_unauthenticated_p2p=False,
            allow_unauthenticated_writes=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_key_present_skips(self):
        mock_logger = MagicMock()
        _check_p2p_keyless_policy(
            p2p_mode=True,
            p2p_key="secret",
            allow_unauthenticated_p2p=False,
            allow_unauthenticated_writes=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_explicit_acknowledgement_skips(self):
        mock_logger = MagicMock()
        _check_p2p_keyless_policy(
            p2p_mode=True,
            p2p_key=None,
            allow_unauthenticated_p2p=True,
            allow_unauthenticated_writes=False,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_writes_opt_in_implies_acknowledgement(self):
        mock_logger = MagicMock()
        _check_p2p_keyless_policy(
            p2p_mode=True,
            p2p_key=None,
            allow_unauthenticated_p2p=False,
            allow_unauthenticated_writes=True,
            logger=mock_logger,
        )
        mock_logger.warning.assert_not_called()

    def test_keyless_prompts_and_accepts(self):
        mock_logger = MagicMock()
        with patch("builtins.input", return_value="y"):
            _check_p2p_keyless_policy(
                p2p_mode=True,
                p2p_key=None,
                allow_unauthenticated_p2p=False,
                allow_unauthenticated_writes=False,
                logger=mock_logger,
            )
        mock_logger.warning.assert_called()

    def test_keyless_decline_exits(self):
        mock_logger = MagicMock()
        with (
            patch("builtins.input", return_value="n"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _check_p2p_keyless_policy(
                p2p_mode=True,
                p2p_key=None,
                allow_unauthenticated_p2p=False,
                allow_unauthenticated_writes=False,
                logger=mock_logger,
            )
        assert exc_info.value.code == 0

    def test_non_interactive_fails_closed(self):
        mock_logger = MagicMock()
        with (
            patch("hsync_client.main_logic.is_tty_interactive", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            _check_p2p_keyless_policy(
                p2p_mode=True,
                p2p_key=None,
                allow_unauthenticated_p2p=False,
                allow_unauthenticated_writes=False,
                logger=mock_logger,
            )
        assert exc_info.value.code == 1
        error_texts = " ".join(str(c) for c in mock_logger.error.call_args_list)
        assert "--allow-unauthenticated-p2p" in error_texts


class TestBypassWarnings:
    def test_all_warnings_are_present(self):
        for key in (
            "allow_deletions",
            "allow_unauthenticated_writes",
            "allow_unauthenticated_p2p",
            "accept_new",
        ):
            assert key in BYPASS_WARNINGS

    def test_no_bypass_logs_nothing(self):
        logger = MagicMock()
        log_active_bypasses(logger)
        logger.warning.assert_not_called()

    def test_bypasses_log_one_warning_each(self):
        logger = MagicMock()
        log_active_bypasses(
            logger,
            allow_deletions=True,
            allow_unauthenticated_p2p=True,
            discovery_server_trust=TrustPolicy.accept_new,
        )
        assert logger.warning.call_count == 3
        texts = " ".join(str(c) for c in logger.warning.call_args_list)
        assert "Deletion-risk" in texts
        assert "without a shared key" in texts
        assert "accept-new" in texts

    def test_accepts_the_production_loguru_logger(self):
        """The CLI passes a loguru logger; the warnings must reach a real sink.

        The original defect was a type mismatch (a ``logging.Logger`` annotation
        while ``main`` passes a ``loguru.Logger``), so the regression guard is
        mypy; this test locks the production logger through the real loguru sink
        path so the integration cannot silently drift.
        """
        from loguru import logger as loguru_logger

        records: list[str] = []
        sink_id = loguru_logger.add(lambda message: records.append(str(message)), level="WARNING")
        try:
            log_active_bypasses(
                loguru_logger,
                allow_deletions=True,
                allow_unauthenticated_p2p=True,
                discovery_server_trust=TrustPolicy.accept_new,
            )
        finally:
            loguru_logger.remove(sink_id)
        assert len(records) == 3
        texts = " ".join(records)
        assert "Deletion-risk" in texts
        assert "without a shared key" in texts
        assert "accept-new" in texts
