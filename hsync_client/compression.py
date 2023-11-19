# SPDX-FileCopyrightText: 2024 Alberto Basso
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Copyright (C) 2024 Alberto Basso
#
# This file is part of HSYNC Client.
#
# HSYNC Client is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Compression utilities for HSYNC Client.

This module provides file compression/decompression using Zstandard (zstd),
with intelligent decision-making on when compression is beneficial.
"""

import logging
from os.path import splitext

import zstandard as zstd


# Logger instance for this module
LOG = logging.getLogger(__name__)

# Compression level (1-22, default 3 is a good balance of speed/ratio)
COMPRESSION_LEVEL = 3

# Content-Encoding header value for zstd compression
CONTENT_ENCODING_ZSTD = "zstd"

# =============================================================================
# Compression Thresholds
# =============================================================================
# Different thresholds based on transfer direction and network type.
# These are based on the trade-off between compression CPU overhead and
# network transfer time savings.
#
# Download (Server→Client): Lower threshold, server CPU is acceptable
# Upload (Client→Server): Higher threshold, client CPU is more precious
# LAN mode: Much higher threshold, local network is fast
# =============================================================================

# Download threshold: 10KB
# - Below 10KB, compression overhead often exceeds transfer time savings
# - Above 10KB, compression benefits clearly outweigh overhead
MIN_SIZE_FOR_DOWNLOAD = 10 * 1024  # 10 KB

# Upload threshold: 32KB (typical production threshold)
# - Client CPU is more precious than server CPU
# - Only compress medium-large files where benefit is clear
MIN_SIZE_FOR_UPLOAD = 32 * 1024  # 32 KB

# LAN mode threshold: 100KB
# - Local network is very fast (1Gbps+), compression rarely helps
# - Only compress large files where transfer time is significant
# - Below 100KB, compression time > transfer time on fast LAN
MIN_SIZE_FOR_LAN = 100 * 1024  # 100 KB

# Legacy constant for backward compatibility
MIN_SIZE_FOR_COMPRESSION = MIN_SIZE_FOR_DOWNLOAD

# File extensions that are already compressed (compression won't help)
PRECOMPRESSED_EXTENSIONS = frozenset({
    # Archives
    ".zip", ".gz", ".gzip", ".bz2", ".xz", ".7z", ".rar", ".tar.gz", ".tgz",
    ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".zst", ".zstd", ".lz4", ".lzma",
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic", ".heif",
    ".jp2", ".j2k", ".jpf", ".jpm", ".jpx",
    # Audio
    ".mp3", ".aac", ".ogg", ".opus", ".flac", ".m4a", ".wma",
    # Video
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpeg", ".mpg", ".3gp",
    # Documents (already compressed internally)
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub",
    # Other
    ".woff", ".woff2", ".eot",  # Fonts
})

# Create reusable compressor/decompressor for efficiency
_compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)
_decompressor = zstd.ZstdDecompressor()


def should_compress(
    filepath: str,
    size: int,
    transfer_type: str = "download",
    is_lan: bool = False,
) -> bool:
    """
    Determine if a file should be compressed before transfer.

    Uses different thresholds based on transfer direction and network type:
    - Download (server→client): 10KB threshold
    - Upload (client→server): 32KB threshold
    - LAN mode: 100KB threshold (local network is fast)

    Args:
        filepath: Path to the file.
        size: Size of the file in bytes.
        transfer_type: "download" or "upload" (default: "download").
        is_lan: True if operating in LAN mode (default: False).

    Returns:
        True if file should be compressed, False otherwise.
    """
    # Determine threshold based on context
    if is_lan:
        threshold = MIN_SIZE_FOR_LAN
        context = "LAN"
    elif transfer_type == "upload":
        threshold = MIN_SIZE_FOR_UPLOAD
        context = "upload"
    else:
        threshold = MIN_SIZE_FOR_DOWNLOAD
        context = "download"

    # Too small to benefit from compression
    if size < threshold:
        LOG.debug(
            f"Skip compression ({context}): {filepath} "
            f"below threshold ({size} < {threshold} bytes)"
        )
        return False

    # Check if file extension indicates pre-compressed format
    ext = splitext(filepath.lower())[1]
    if ext in PRECOMPRESSED_EXTENSIONS:
        LOG.debug(f"Skip compression: {filepath} already compressed ({ext})")
        return False

    return True


def compress_data(data: bytes) -> bytes:
    """
    Compress data using Zstandard.

    Args:
        data: Raw bytes to compress.

    Returns:
        Compressed bytes.
    """
    return _compressor.compress(data)


def decompress_data(data: bytes) -> bytes:
    """
    Decompress Zstandard-compressed data.

    Args:
        data: Compressed bytes.

    Returns:
        Decompressed bytes.
    """
    return _decompressor.decompress(data)


def compress_file(filepath: str) -> bytes:
    """
    Read and compress a file.

    Args:
        filepath: Path to file to compress.

    Returns:
        Compressed file content.
    """
    with open(filepath, "rb") as f:
        return compress_data(f.read())


LOG.debug(f"Module {__name__} loaded")

