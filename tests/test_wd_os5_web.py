"""Regression tests for the WD OS5 web UI assets.

The web UI (PHP + vanilla JS under ``packaging/wd-os5/Apkg/web``) has no JS test
harness, so we guard the static assets with a small Python check. This catches
the bucket ``pattern`` regression where an unescaped ``-`` inside a character
class made the regex invalid under the JS ``v`` (unicodeSets) flag that modern
browsers apply to HTML ``pattern`` attributes (see commit history: ``37de4eb``
escaped the hyphen, ``fe37d97`` re-broke it).
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "packaging" / "wd-os5" / "Apkg" / "web" / "index.html"

# Expected, v-flag-safe bucket pattern. The hyphen is escaped (``\-``) so the
# inner character class compiles under both the ``u`` and ``v`` flags; this
# mirrors the authoritative server-side grammar in config_set.php.
EXPECTED_BUCKET_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9_\-]*[A-Za-z0-9])?$"

_BUCKET_PATTERN_RE = re.compile(
    r'<input[^>]*\bid="HSYNC_BUCKET"[^>]*\bpattern="([^"]*)"', re.DOTALL
)


def test_bucket_pattern_is_v_flag_safe() -> None:
    """The bucket input pattern must escape the hyphen (valid under the v flag)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = _BUCKET_PATTERN_RE.search(html)
    assert match is not None, "HSYNC_BUCKET input with a pattern= not found in index.html"
    pattern = match.group(1)
    assert pattern == EXPECTED_BUCKET_PATTERN, (
        f"bucket pattern is {pattern!r}, expected {EXPECTED_BUCKET_PATTERN!r} "
        "(unescaped '-' in a character class is invalid under the JS v flag)"
    )


# ── bootnode / mode consistency ────────────────────────────────────────
PKG = PROJECT_ROOT / "packaging" / "wd-os5"
WEB = PKG / "Apkg" / "web"
I18N = WEB / "i18n"
APP_JS = WEB / "app.js"
_SUPPORTED_RE = re.compile(r"const SUPPORTED = \[([^\]]*)\]")


def _supported_locales() -> list[str]:
    """Read the language list from app.js so the tests track the single source."""
    match = _SUPPORTED_RE.search(APP_JS.read_text(encoding="utf-8"))
    assert match is not None, "SUPPORTED not found in app.js"
    return re.findall(r"'([^']+)'", match.group(1))


def test_bootnode_fields_present_in_ui() -> None:
    """The p2p section exposes the optional bootnode host and the UPnP toggle."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="HSYNC_BOOTNODE"' in html
    assert 'id="HSYNC_UPNP_ENABLED"' in html


def test_php_config_whitelists_bootnode_keys() -> None:
    """Both PHP endpoints whitelist the two new environment keys."""
    for name in ("config_get.php", "config_set.php"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert "HSYNC_BOOTNODE" in text, name
        assert "HSYNC_UPNP_ENABLED" in text, name


def test_i18n_has_bootnode_keys_in_all_locales() -> None:
    import json

    for locale in _supported_locales():
        data = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        assert data.get("field.bootnode"), locale
        assert data.get("field.upnp"), locale


def test_start_sh_uses_p2p_subcommand_and_exports_bootnode() -> None:
    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    assert 'SUBCOMMAND="p2p"' in start
    assert 'SUBCOMMAND="lan"' not in start
    assert "export HSYNC_BOOTNODE" in start
    assert "export HSYNC_UPNP_ENABLED" in start


def test_mode_value_is_p2p_everywhere() -> None:
    """The mode value ``lan`` is retired: UI, validation and service agree on p2p."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<option value="p2p"' in html
    assert '<option value="lan"' not in html
    php = (WEB / "config_set.php").read_text(encoding="utf-8")
    assert "['p2p', 'server']" in php
    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    assert "p2p)    SUBCOMMAND=" in start


def test_config_set_accepts_endpoint_grammar() -> None:
    """``config_set.php`` validates the HOST[:PORT] grammar, not a bare hostname."""
    php = (WEB / "config_set.php").read_text(encoding="utf-8")
    assert "$endpoint_re" in php
    assert r"\[" in php  # bracketed IPv6 literal in the endpoint regex


def test_config_set_whitelists_server() -> None:
    """``config_set.php`` validates ``HSYNC_SERVER`` (bare host or https:// URL)."""
    php = (WEB / "config_set.php").read_text(encoding="utf-8")
    assert "case 'HSYNC_SERVER':" in php


def test_server_input_is_text_not_url() -> None:
    """A bare ``HOST[:PORT]`` must be accepted by the browser (type=url blocks it)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="HSYNC_SERVER" type="text"' in html or 'type="text" name="HSYNC_SERVER"' in html
    assert 'type="url" name="HSYNC_SERVER"' not in html


def test_i18n_has_server_field_in_all_locales() -> None:
    import json

    for locale in _supported_locales():
        data = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        assert data.get("field.server"), locale


def test_bootnode_ports_field_is_wired_end_to_end() -> None:
    """``HSYNC_BOOTNODE_PORTS`` is exposed, whitelisted, validated and exported."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="HSYNC_BOOTNODE_PORTS"' in html

    for name in ("config_get.php", "config_set.php"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert "HSYNC_BOOTNODE_PORTS" in text, name

    php = (WEB / "config_set.php").read_text(encoding="utf-8")
    assert "case 'HSYNC_BOOTNODE_PORTS':" in php

    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    assert "export HSYNC_BOOTNODE_PORTS" in start

    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "'HSYNC_BOOTNODE_PORTS'" in app  # CONNECT_FIELDS + generated env prefix

    import json

    for locale in _supported_locales():
        data = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        assert data.get("field.bootnode_ports"), locale


def test_i18n_dictionaries_cover_every_supported_locale() -> None:
    """Every language in app.js SUPPORTED has a complete i18n dictionary.

    Guards against adding a language to the selector without shipping its
    translations (the UI would fall back to the English ``data-i18n`` defaults)
    and against a dictionary drifting from the English key set.
    """
    import json

    locales = _supported_locales()
    assert locales, "app.js exposes no supported languages"
    assert "en" in locales, "English must remain a supported language"

    reference = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
    assert reference, "en.json is empty"

    for locale in locales:
        path = I18N / f"{locale}.json"
        assert path.exists(), f"missing i18n dictionary for {locale!r}"
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(set(reference) - set(data))
        extra = sorted(set(data) - set(reference))
        assert not missing and not extra, (
            f"{locale}.json keys differ from en.json: missing={missing} extra={extra}"
        )
        empty = [
            key for key, value in data.items() if not isinstance(value, str) or not value.strip()
        ]
        assert not empty, f"{locale}.json has empty values: {empty}"


def test_language_selector_has_native_names_for_every_locale() -> None:
    """The selector shows the desktop-style native names, one per language."""
    app = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"const LANGUAGE_NAMES = \{(.*?)\n    \};", app, re.DOTALL)
    assert match is not None, "LANGUAGE_NAMES not found in app.js"
    names = dict(re.findall(r"(\w+):\s*'([^']*)'", match.group(1)))
    for locale in _supported_locales():
        assert names.get(locale), f"missing native name for {locale!r}"
