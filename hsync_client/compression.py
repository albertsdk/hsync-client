# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Client.

Re-exports from hsync_common.compression. All compression logic and
thresholds (download/upload/LAN) live in hsync-common.
"""

# Re-export core compression from hsync-common
from hsync_common.compression import (  # noqa: F401
    COMPRESSION_LEVEL,
    CONTENT_ENCODING_ZSTD,
    MIN_SIZE_FOR_COMPRESSION,
    MIN_SIZE_FOR_DOWNLOAD,
    MIN_SIZE_FOR_LAN,
    MIN_SIZE_FOR_UPLOAD,
    PRECOMPRESSED_EXTENSIONS,
    compress_data,
    compress_file,
    decompress_data,
    decompress_stream,
    should_compress,
)
