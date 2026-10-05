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

    for locale in ("en", "it", "de", "es", "fr"):
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

    for locale in ("en", "it", "de", "es", "fr"):
        data = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        assert data.get("field.server"), locale
