"""Startup server-port resolution: standard port first, then the 443 proxy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hsync_client import main_logic


def _log() -> MagicMock:
    return MagicMock()


class TestResolveServerPort:
    """A bare address implies 9753; a 443 reverse proxy is the fallback."""

    def test_keeps_the_default_port_when_it_answers(self, monkeypatch):
        calls: list[str] = []

        def probe(url: str, **_kwargs) -> bool:
            calls.append(url)
            return True

        monkeypatch.setattr(main_logic, "_probe_server_health", probe)

        resolved = main_logic._resolve_server_port(
            "https://h.example.com:9753", verify_ssl=True, log=_log()
        )

        assert resolved == "https://h.example.com:9753"
        assert calls == ["https://h.example.com:9753"]

    def test_falls_back_to_443_when_the_default_port_is_unreachable(self, monkeypatch):
        calls: list[str] = []

        def probe(url: str, **_kwargs) -> bool:
            calls.append(url)
            return url.endswith(":443")

        monkeypatch.setattr(main_logic, "_probe_server_health", probe)
        log = _log()

        resolved = main_logic._resolve_server_port(
            "https://h.example.com:9753", verify_ssl=True, log=log
        )

        assert resolved == "https://h.example.com:443"
        assert calls == ["https://h.example.com:9753", "https://h.example.com:443"]
        log.info.assert_called_once()

    def test_keeps_the_default_port_when_neither_answers(self, monkeypatch):
        monkeypatch.setattr(main_logic, "_probe_server_health", lambda *_a, **_k: False)

        resolved = main_logic._resolve_server_port(
            "https://h.example.com:9753", verify_ssl=True, log=_log()
        )

        assert resolved == "https://h.example.com:9753"

    def test_an_explicit_non_default_port_is_never_second_guessed(self, monkeypatch):
        monkeypatch.setattr(
            main_logic,
            "_probe_server_health",
            lambda *_a, **_k: pytest.fail("an explicit port must not be probed"),
        )

        resolved = main_logic._resolve_server_port(
            "https://h.example.com:9000", verify_ssl=True, log=_log()
        )

        assert resolved == "https://h.example.com:9000"
