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

# Re-export the compression API used by client modules.
from hsync_common.compression import (  # noqa: F401
    CONTENT_ENCODING_ZSTD,
    compress_data,
    compress_file_to_file,
    compression_level,
    decompress_stream,
    should_compress,
)
