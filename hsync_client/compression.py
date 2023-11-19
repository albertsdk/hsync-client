# SPDX-FileCopyrightText: 2024 Alberto Basso <alberto.basso.dev@gmail.com>
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

# Minimum file size to consider compression (1KB)
MIN_SIZE_FOR_COMPRESSION = 1024

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


def should_compress(filepath: str, size: int) -> bool:
    """
    Determine if a file should be compressed before transfer.

    Args:
        filepath: Path to the file.
        size: Size of the file in bytes.

    Returns:
        True if file should be compressed, False otherwise.
    """
    # Too small to benefit from compression
    if size < MIN_SIZE_FOR_COMPRESSION:
        LOG.debug(f"Skip compression: {filepath} too small ({size} bytes)")
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

