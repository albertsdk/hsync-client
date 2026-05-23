"""
Directory model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.dir.

``is_hidden_dir`` is wrapped here so it transparently consults the
process-wide :class:`hsync_common.ignore.IgnoreMatcher` when one is
installed.  When no matcher is active, the function behaves exactly
like the original ``hsync_common`` implementation.
"""

from hsync_common.ignore import get_active_matcher
from hsync_common.models.dir import Dir  # noqa: F401
from hsync_common.models.dir import is_hidden_dir as _orig_is_hidden_dir


def is_hidden_dir(directory: str) -> bool:
    """Return ``True`` when *directory* must be excluded from sync.

    Delegates to the active :class:`IgnoreMatcher` (which combines
    ``.hsyncignore``, extra patterns and the default hidden-dir
    detection); falls back to the legacy check when no matcher is set.
    """
    matcher = get_active_matcher()
    if matcher is not None:
        return matcher.matches(directory, is_dir=True)
    return _orig_is_hidden_dir(directory)
