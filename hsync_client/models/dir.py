"""
Directory model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.dir.

``is_hidden_dir`` is matcher-aware in hsync_common itself: it transparently
consults the process-wide :class:`hsync_common.ignore.IgnoreMatcher` when one
is installed, and falls back to the legacy dot-prefix check otherwise. The
wrapper that previously lived here has been folded into hsync_common so that
the behaviour is identical regardless of the import path.
"""

from hsync_common.models.dir import Dir, is_hidden_dir  # noqa: F401
