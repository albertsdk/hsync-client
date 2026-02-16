# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Compression utilities for HSYNC Client.

This module provides file compression/decompression using Zstandard (zstd),
with intelligent decision-making on when compression is beneficial.
"""

from os.path import splitext

import zstandard as zstd
from loguru import logger as log

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
PRECOMPRESSED_EXTENSIONS = frozenset(
    {
        # Archives
        ".zip",
        ".gz",
        ".gzip",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
        ".zst",
        ".zstd",
        ".lz4",
        ".lzma",
        ".cab",
        ".dmg",
        ".pkg",
        ".msi",
        ".deb",
        ".rpm",
        ".apk",
        ".ipa",
        ".jar",
        ".war",
        ".ear",
        ".nupkg",
        # Images
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".heic",
        ".heif",
        ".jp2",
        ".j2k",
        ".jpf",
        ".jpm",
        ".jpx",
        ".tiff",
        ".tif",
        ".psd",
        ".ico",
        ".icns",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
        ".orf",
        ".rw2",
        # Audio
        ".mp3",
        ".aac",
        ".ogg",
        ".opus",
        ".flac",
        ".m4a",
        ".wma",
        ".alac",
        # Video
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".3gp",
        ".m2ts",
        ".mts",
        ".vob",
        ".ogv",
        # Documents (already compressed internally)
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".pages",
        ".numbers",
        ".keynote",
        ".xps",
        ".cbz",
        ".cbr",
        # Fonts
        ".woff",
        ".woff2",
        ".eot",
        ".ttc",
        # Other
        ".sqlite",
        ".db",
    }
)

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
        log.debug(
            f"Skip compression ({context}): {filepath} below threshold ({size} < {threshold} bytes)"
        )
        return False

    # Check if file extension indicates pre-compressed format
    ext = splitext(filepath.lower())[1]
    if ext in PRECOMPRESSED_EXTENSIONS:
        log.debug(f"Skip compression: {filepath} already compressed ({ext})")
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


log.debug(f"Module {__name__} loaded")
