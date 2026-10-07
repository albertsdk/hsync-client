# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Compression utilities for HSYNC Client.

Re-exports from hsync_common.compression. All compression logic and
thresholds (download/upload/P2P) live in hsync-common.
"""

# Re-export the compression API used by client modules.
from hsync_common.compression import (  # noqa: F401
    CONTENT_ENCODING_ZSTD,
    compress_data,
    compress_file_to_file,
    compression_level,
    decompress_stream,
    should_compress,
)
