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
import subprocess
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


# ── desktop parity: keys, labels, defaults ─────────────────────────────
def _form_names() -> set[str]:
    html = INDEX_HTML.read_text(encoding="utf-8")
    return set(re.findall(r'name="(HSYNC_[A-Z0-9_]+)"', html))


def _php_keys(name: str) -> set[str]:
    text = (WEB / name).read_text(encoding="utf-8")
    block = text.split("const KEYS = [", 1)[1].split("];", 1)[0]
    return set(re.findall(r"'(HSYNC_[A-Z0-9_]+)'", block))


def test_php_whitelists_match_the_form() -> None:
    """config_get.php, config_set.php and the form expose the same key set.

    The per-file retry waits are the only keys kept in the whitelist without a
    form field: the desktop does not expose them either, and keeping them means
    a hand-edited value survives a save.
    """
    get_keys = _php_keys("config_get.php")
    set_keys = _php_keys("config_set.php")
    form = _form_names()
    assert get_keys == set_keys
    assert form <= get_keys, sorted(form - get_keys)
    assert get_keys - form == {
        "HSYNC_TRANSFER_RETRY_MIN_WAIT",
        "HSYNC_TRANSFER_RETRY_MAX_WAIT",
    }, sorted(get_keys - form)


def test_every_boolean_key_is_normalized() -> None:
    """Every boolean the server validates is a checkbox the UI collects."""
    app = APP_JS.read_text(encoding="utf-8")
    block = re.search(r"const CHECKBOX_KEYS = \[(.*?)\];", app, re.DOTALL)
    assert block is not None, "CHECKBOX_KEYS not found in app.js"
    checkboxes = set(re.findall(r"'(HSYNC_[A-Z0-9_]+)'", block.group(1)))

    php = (WEB / "config_set.php").read_text(encoding="utf-8")
    tail = php.split("case 'HSYNC_PREFER_MASTER':", 1)[1]
    tail = tail.split("if (!in_array($val, ['true', 'false'], true))", 1)[0]
    booleans = {"HSYNC_PREFER_MASTER"} | set(re.findall(r"case '(HSYNC_[A-Z0-9_]+)':", tail))
    assert checkboxes == booleans, (sorted(checkboxes - booleans), sorted(booleans - checkboxes))


def test_inline_labels_match_the_dictionary() -> None:
    """The English fallback inside index.html must equal en.json, key by key."""
    import html as html_mod
    import json

    data = json.loads((I18N / "en.json").read_text(encoding="utf-8"))
    src = INDEX_HTML.read_text(encoding="utf-8")
    mismatches = {}
    for key, text in re.findall(r'data-i18n="([^"]+)"[^>]*>([^<]*)<', src):
        inline = html_mod.unescape(text).strip()
        if inline and data.get(key) != inline:
            mismatches[key] = (inline, data.get(key))
    assert not mismatches, mismatches


def test_no_numeric_flag_receives_an_empty_value() -> None:
    """The launcher guards every numeric flag and disables the resync explicitly."""
    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    for var in (
        "HSYNC_DISCOVERY_TIMEOUT",
        "HSYNC_HTTP_TIMEOUT",
        "HSYNC_RECONNECT_DELAY",
        "HSYNC_STABILITY_DELAY",
        "HSYNC_MAX_WAIT_TIMEOUT",
        "HSYNC_MAX_INBOUND_SIZE_MB",
    ):
        assert f'is_uint "${{{var}}}"' in start or f'is_uint_gt0 "${{{var}}}"' in start, var
    assert "export HSYNC_RESYNC=0" in start


def test_defaults_match_the_desktop_reference() -> None:
    """The shipped NAS defaults equal hsync-desktop's (see hsync_desktop/config.py)."""
    install = (PKG / "Apkg" / "install.sh").read_text(encoding="utf-8")
    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    expected = {
        "HSYNC_BUCKET": "my-bucket",
        "HSYNC_P2P_GROUP": "my-group",
        "HSYNC_DISCOVERY_TIMEOUT": "10",
        "HSYNC_UPNP_ENABLED": "true",
        "HSYNC_HTTP_TIMEOUT": "30",
        "HSYNC_RECONNECT_DELAY": "5",
        "HSYNC_STABILITY_DELAY": "5",
        "HSYNC_MAX_WAIT_TIMEOUT": "120",
        "HSYNC_MIN_FREE_DISK": "500MB",
        "HSYNC_MAX_INBOUND_SIZE_MB": "10240",
        "HSYNC_DELTA_MIN_FILE_SIZE": "256",
        "HSYNC_DELTA_MIN_FILE_SIZE_P2P": "1024",
        "HSYNC_SERVER_TRUST": "chain",
        "HSYNC_BOOTNODE_TRUST": "ask",
        "HSYNC_COMPRESSION_ENABLED": "true",
        "HSYNC_PREFER_MASTER": "false",
        "HSYNC_NEVER_MASTER": "false",
        "HSYNC_AUTOTUNE": "true",
        "HSYNC_MASS_DELETE_GUARD": "true",
        "HSYNC_MASS_DELETE_THRESHOLD": "50",
        "HSYNC_MASS_DELETE_PERCENT": "50",
        "HSYNC_MAX_CONCURRENT_TRANSFERS": "0",
        # Deliberate NAS deviation: this device is a backup target.
        "HSYNC_IGNORE_DELETE": "true",
    }
    for key, value in expected.items():
        assert f'{key}="{value}"' in install, f"install.sh seed for {key}"
        assert f': "${{{key}:={value}}}"' in start, f"start.sh fallback for {key}"


def test_config_set_clamps_ranges(tmp_path: Path) -> None:
    """A behavioural check: out-of-range and empty values become valid ones."""
    env_file = tmp_path / "hsync.env"
    prepend = tmp_path / "prepend.php"
    prepend.write_text(
        "<?php\n"
        f"define('HSYNC_ENV', '{env_file}');\n"
        f"define('HSYNC_CONFIG', '{tmp_path}');\n"
        "$_SERVER['REQUEST_METHOD'] = 'POST';\n"
        "$_SERVER['CONTENT_TYPE'] = 'application/x-www-form-urlencoded';\n"
        "$_POST = [\n"
        "    'HSYNC_MODE' => 'p2p',\n"
        "    'HSYNC_BUCKET' => 'bad bucket',\n"
        "    'HSYNC_HTTP_TIMEOUT' => '',\n"
        "    'HSYNC_DISCOVERY_TIMEOUT' => '999',\n"
        "    'HSYNC_RECONNECT_DELAY' => '0',\n"
        "    'HSYNC_P2P_PEER_READ_CAP' => '100',\n"
        "    'HSYNC_SYNC_DIRECTION' => 'sideways',\n"
        "    'HSYNC_COMPRESSION_ENABLED' => 'true',\n"
        "    'HSYNC_INTERVAL' => '5',\n"
        "    'HSYNC_MASS_DELETE_PERCENT' => '250',\n"
        "    'HSYNC_P2P_GROUP' => \"grp\\nHSYNC_VERBOSE\",\n"
        "];\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "php",
            "-d",
            f"auto_prepend_file={prepend}",
            "-d",
            "display_errors=0",
            str(WEB / "config_set.php"),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    written = dict(re.findall(r"^(HSYNC_[A-Z0-9_]+)='([^']*)'$", env_file.read_text(), re.M))
    assert written["HSYNC_BUCKET"] == "my-bucket"
    assert written["HSYNC_HTTP_TIMEOUT"] == "30"
    assert written["HSYNC_DISCOVERY_TIMEOUT"] == "60"
    assert written["HSYNC_RECONNECT_DELAY"] == "1"
    assert written["HSYNC_P2P_PEER_READ_CAP"] == "32"
    assert written["HSYNC_SYNC_DIRECTION"] == "bidirectional"
    assert written["HSYNC_INTERVAL"] == "20"
    assert written["HSYNC_MASS_DELETE_PERCENT"] == "100"

    # A newline in a free-text field must not break the line-based .env file.
    lines = [ln for ln in env_file.read_text().splitlines() if ln and not ln.startswith("#")]
    assert all(re.fullmatch(r"HSYNC_[A-Z0-9_]+='.*'", ln) for ln in lines), lines
    group_line = next(ln for ln in lines if ln.startswith("HSYNC_P2P_GROUP="))
    assert "HSYNC_VERBOSE" in group_line and group_line.count("=") == 1


# ── shared password (desktop parity) ───────────────────────────────────
def test_p2p_password_is_wired_end_to_end() -> None:
    """The NAS manages HSYNC_P2P_PASSPHRASE (12+), not a 32-char raw key."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r'<input[^>]*id="HSYNC_P2P_PASSPHRASE"[^>]*minlength="(\d+)"', html)
    assert match is not None, "HSYNC_P2P_PASSPHRASE input not found"
    assert int(match.group(1)) == 12
    assert 'name="HSYNC_P2P_PASSPHRASE"' in html
    assert 'name="HSYNC_P2P_KEY"' not in html

    for name in ("config_get.php", "config_set.php"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert "HSYNC_P2P_PASSPHRASE" in text, name
        assert "HSYNC_P2P_KEY" not in text, name

    start = (PKG / "Apkg" / "start.sh").read_text(encoding="utf-8")
    assert "export HSYNC_P2P_PASSPHRASE" in start
    assert "HSYNC_P2P_KEY" not in start

    install = (PKG / "Apkg" / "install.sh").read_text(encoding="utf-8")
    assert 'HSYNC_P2P_PASSPHRASE="${P2P_PASSPHRASE}"' in install
    assert "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789" in install
    assert "HSYNC_P2P_KEY" not in install

    app = APP_JS.read_text(encoding="utf-8")
    assert "'HSYNC_P2P_PASSPHRASE'" in app
    assert "HSYNC_P2P_KEY" not in app

    env_lib = (WEB / "env_lib.php").read_text(encoding="utf-8")
    assert "HSYNC_P2P_PASSPHRASE" in env_lib
    assert "HSYNC_P2P_KEY" not in env_lib

    import json

    for locale in _supported_locales():
        data = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        assert data.get("field.p2p_password"), locale
        assert data.get("note.p2p_password"), locale
        assert data.get("toast.password_too_short"), locale
        assert "field.p2p_key" not in data, locale


def test_config_set_enforces_the_password_floor(tmp_path: Path) -> None:
    """An 11-char password is dropped; a 12-char one is persisted."""
    for value, expected in (("abcdefghijk", ""), ("abcdwxyz1234", "abcdwxyz1234")):
        env_file = tmp_path / "hsync.env"
        prepend = tmp_path / "prepend.php"
        prepend.write_text(
            "<?php\n"
            f"define('HSYNC_ENV', '{env_file}');\n"
            f"define('HSYNC_CONFIG', '{tmp_path}');\n"
            "$_SERVER['REQUEST_METHOD'] = 'POST';\n"
            "$_SERVER['CONTENT_TYPE'] = 'application/x-www-form-urlencoded';\n"
            "$_POST = ['HSYNC_MODE' => 'p2p', 'HSYNC_P2P_PASSPHRASE' => " + repr(value) + "];\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "php",
                "-d",
                f"auto_prepend_file={prepend}",
                "-d",
                "display_errors=0",
                str(WEB / "config_set.php"),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        written = dict(re.findall(r"^(HSYNC_[A-Z0-9_]+)='([^']*)'$", env_file.read_text(), re.M))
        assert written["HSYNC_P2P_PASSPHRASE"] == expected
        assert "HSYNC_P2P_KEY" not in written
