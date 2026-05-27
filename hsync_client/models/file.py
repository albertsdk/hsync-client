"""
File model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.file.

``is_hidden_file`` is wrapped here so it transparently consults the
process-wide :class:`hsync_common.ignore.IgnoreMatcher` when one is
installed.  When no matcher is active, the function behaves exactly
like the original ``hsync_common`` implementation.
"""

from hsync_common.ignore import get_active_matcher
from hsync_common.models.file import (  # noqa: F401
    File,
    FileDict,
    clear_sha256_cache,
    invalidate_sha256_cache,
)
from hsync_common.models.file import is_hidden_file as _orig_is_hidden_file


def is_hidden_file(file_path: str) -> bool:
    """Return ``True`` when *file_path* must be excluded from sync.

    Delegates to the active :class:`IgnoreMatcher` (which combines
    ``.hsyncignore``, extra patterns and the default hidden-file
    detection); falls back to the legacy regex when no matcher is set.
    """
    matcher = get_active_matcher()
    if matcher is not None:
        return matcher.matches(file_path, is_dir=False)
    return _orig_is_hidden_file(file_path)
