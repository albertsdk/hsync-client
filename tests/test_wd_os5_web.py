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
