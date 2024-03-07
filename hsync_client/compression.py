# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Client.

Extends hsync_common.compression with client-specific thresholds
for upload and LAN transfer modes.
"""

from os.path import splitext

# Re-export core compression from hsync-common
from hsync_common.compression import (  # noqa: F401
    COMPRESSION_LEVEL,
    CONTENT_ENCODING_ZSTD,
    MIN_SIZE_FOR_COMPRESSION,
    MIN_SIZE_FOR_DOWNLOAD,
    PRECOMPRESSED_EXTENSIONS,
    compress_data,
    compress_file,
    decompress_data,
)
from loguru import logger as log

# =============================================================================
# Client-Specific Compression Thresholds
# =============================================================================

# Upload threshold: 32KB (typical production threshold)
# - Client CPU is more precious than server CPU
# - Only compress medium-large files where benefit is clear
MIN_SIZE_FOR_UPLOAD = 32 * 1024  # 32 KB

# LAN mode threshold: 100KB
# - Local network is very fast (1Gbps+), compression rarely helps
# - Only compress large files where transfer time is significant
# - Below 100KB, compression time > transfer time on fast LAN
MIN_SIZE_FOR_LAN = 100 * 1024  # 100 KB


def should_compress(
    filepath: str,
    size: int,
    transfer_type: str = "download",
    is_lan: bool = False,
) -> bool:
    """
    Determine if a file should be compressed before transfer.

    Uses different thresholds based on transfer direction and network type:
    - Download (server to client): 10KB threshold
    - Upload (client to server): 32KB threshold
    - LAN mode: 100KB threshold (local network is fast)

    Args:
        filepath: Path to the file.
        size: Size of the file in bytes.
        transfer_type: "download" or "upload" (default: "download").
        is_lan: True if operating in LAN mode (default: False).

    Returns:
        True if file should be compressed, False otherwise.
    """
    if is_lan:
        threshold = MIN_SIZE_FOR_LAN
        context = "LAN"
    elif transfer_type == "upload":
        threshold = MIN_SIZE_FOR_UPLOAD
        context = "upload"
    else:
        threshold = MIN_SIZE_FOR_DOWNLOAD
        context = "download"

    if size < threshold:
        log.debug(
            f"Skip compression ({context}): {filepath} below threshold ({size} < {threshold} bytes)"
        )
        return False

    ext = splitext(filepath.lower())[1]
    if ext in PRECOMPRESSED_EXTENSIONS:
        log.debug(f"Skip compression: {filepath} already compressed ({ext})")
        return False

    return True


log.debug(f"Module {__name__} loaded")
