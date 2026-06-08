"""
File model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.file.

``is_hidden_file`` is matcher-aware in hsync_common itself: it transparently
consults the process-wide :class:`hsync_common.ignore.IgnoreMatcher` when one
is installed, and falls back to the legacy pattern check otherwise. The wrapper
that previously lived here has been folded into hsync_common so that the
behaviour is identical regardless of the import path.
"""

from hsync_common.models.file import (  # noqa: F401
    File,
    FileDict,
    clear_sha256_cache,
    invalidate_sha256_cache,
    is_hidden_file,
)
