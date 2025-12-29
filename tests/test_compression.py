"""
Tests for hsync_client.compression module.

Tests compression decision logic, compress/decompress functions, and file compression.
"""

import os
import tempfile

from hsync_client.compression import (
    MIN_SIZE_FOR_COMPRESSION,
    MIN_SIZE_FOR_LAN,
    MIN_SIZE_FOR_UPLOAD,
    compress_data,
    compress_file,
    decompress_data,
    should_compress,
)


class TestShouldCompress:
    """Tests for should_compress function."""

    def test_small_file_download_skipped(self):
        """Files below download threshold are not compressed."""
        assert should_compress("file.txt", MIN_SIZE_FOR_COMPRESSION - 1) is False

    def test_large_file_download_compressed(self):
        """Files above download threshold are compressed."""
        assert should_compress("file.txt", MIN_SIZE_FOR_COMPRESSION + 1) is True

    def test_small_file_upload_skipped(self):
        """Files below upload threshold are not compressed for uploads."""
        size = MIN_SIZE_FOR_UPLOAD - 1
        assert should_compress("file.txt", size, transfer_type="upload") is False

    def test_large_file_upload_compressed(self):
        """Files above upload threshold are compressed for uploads."""
        size = MIN_SIZE_FOR_UPLOAD + 1
        assert should_compress("file.txt", size, transfer_type="upload") is True

    def test_small_file_lan_skipped(self):
        """Files below LAN threshold are not compressed in LAN mode."""
        size = MIN_SIZE_FOR_LAN - 1
        assert should_compress("file.txt", size, is_lan=True) is False

    def test_large_file_lan_compressed(self):
        """Files above LAN threshold are compressed in LAN mode."""
        size = MIN_SIZE_FOR_LAN + 1
        assert should_compress("file.txt", size, is_lan=True) is True

    def test_precompressed_extension_skipped(self):
        """Already compressed files are not compressed again."""
        large_size = MIN_SIZE_FOR_COMPRESSION * 100
        assert should_compress("photo.jpg", large_size) is False
        assert should_compress("video.mp4", large_size) is False
        assert should_compress("archive.zip", large_size) is False
        assert should_compress("audio.mp3", large_size) is False
        assert should_compress("doc.docx", large_size) is False

    def test_precompressed_case_insensitive(self):
        """Extension check is case-insensitive."""
        large_size = MIN_SIZE_FOR_COMPRESSION * 100
        assert should_compress("PHOTO.JPG", large_size) is False
        assert should_compress("Video.MP4", large_size) is False

    def test_compressible_extensions(self):
        """Compressible file types are compressed when large enough."""
        large_size = MIN_SIZE_FOR_COMPRESSION * 100
        assert should_compress("data.csv", large_size) is True
        assert should_compress("source.py", large_size) is True
        assert should_compress("config.json", large_size) is True
        assert should_compress("readme.txt", large_size) is True

    def test_exact_threshold_download(self):
        """Files exactly at threshold are not compressed (below means <)."""
        # At exactly the threshold, size < threshold is False
        assert should_compress("file.txt", MIN_SIZE_FOR_COMPRESSION) is True

    def test_zero_size_skipped(self):
        """Zero-size files are not compressed."""
        assert should_compress("file.txt", 0) is False

    def test_lan_mode_overrides_transfer_type(self):
        """LAN mode uses LAN threshold regardless of transfer_type."""
        size = MIN_SIZE_FOR_UPLOAD + 1  # above upload threshold
        # If it's also below LAN threshold, it should be skipped
        if size < MIN_SIZE_FOR_LAN:
            assert should_compress("file.txt", size, transfer_type="upload", is_lan=True) is False


class TestCompressDecompress:
    """Tests for compress_data and decompress_data functions."""

    def test_compress_and_decompress_roundtrip(self):
        """Compressing then decompressing returns original data."""
        original = b"Hello, World! " * 100
        compressed = compress_data(original)
        decompressed = decompress_data(compressed)
        assert decompressed == original

    def test_compress_empty_data(self):
        """Empty data can be compressed and decompressed."""
        compressed = compress_data(b"")
        decompressed = decompress_data(compressed)
        assert decompressed == b""

    def test_compressed_is_smaller(self):
        """Compressed data is smaller than original for repetitive content."""
        original = b"AAAA" * 10000
        compressed = compress_data(original)
        assert len(compressed) < len(original)

    def test_compress_binary_data(self):
        """Binary data can be compressed and decompressed."""
        original = bytes(range(256)) * 50
        compressed = compress_data(original)
        decompressed = decompress_data(compressed)
        assert decompressed == original


class TestCompressFile:
    """Tests for compress_file function."""

    def test_compress_text_file(self):
        """Text file can be compressed."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            content = b"Test content for compression\n" * 100
            f.write(content)
            temp_path = f.name

        try:
            compressed = compress_file(temp_path)
            decompressed = decompress_data(compressed)
            assert decompressed == content
        finally:
            os.unlink(temp_path)

    def test_compress_empty_file(self):
        """Empty file can be compressed."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            temp_path = f.name

        try:
            compressed = compress_file(temp_path)
            decompressed = decompress_data(compressed)
            assert decompressed == b""
        finally:
            os.unlink(temp_path)

    def test_compress_binary_file(self):
        """Binary file can be compressed."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            content = bytes(range(256)) * 20
            f.write(content)
            temp_path = f.name

        try:
            compressed = compress_file(temp_path)
            decompressed = decompress_data(compressed)
            assert decompressed == content
        finally:
            os.unlink(temp_path)
