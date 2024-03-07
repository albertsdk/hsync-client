"""
File model - re-exports from hsync_common.

Backward-compatible shim: all symbols are canonical in hsync_common.models.file.
"""

from hsync_common.models.file import (  # noqa: F401
    File,
    FileDict,
    clear_md5_cache,
    invalidate_md5_cache,
    is_hidden_file,
)
