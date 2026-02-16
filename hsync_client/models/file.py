"""
File model for HSYNC Client.

This module defines the File class representing a file with its metadata,
and utility functions for file handling during synchronization.
"""

import stat
from datetime import UTC, datetime
from hashlib import md5
from os import stat as os_stat
from os.path import (
    basename,
    exists,
    getctime,
    getmtime,
    getsize,
    join,
)
from re import match
from typing import Optional


def is_hidden_file(file_path: str) -> bool:
    """
    Check if a file should be considered hidden and excluded from sync.

    Matches various patterns for hidden, temporary, and backup files:
    - ^\\..* : Files starting with a dot (Unix hidden files)
    - .*~$ : Emacs/editor backup files ending with tilde
    - ^4913$ : Vim temporary file identifier
    - ^\\..*\\.sw[px]$ : Vim swap files (.swp, .swo)
    - ^\\.~lock\\..*#$ : LibreOffice lock files
    - .*\\.bak$ : Common backup files ending with .bak
    - .*\\.tmp$ : Temporary files ending with .tmp
    - .*\\.temp$ : Temporary files ending with .temp
    - .*\\.swp$ : Swap files ending with .swp
    - .*\\.swo$ : Swap files ending with .swo
    - .*-\\d{4}-\\d{2}-\\d{2}-\\d{6}$ : Date-timestamped backups (YYYY-MM-DD-HHmmss)

    Args:
        file_path: Path to the file to check.

    Returns:
        True if the file matches any hidden file pattern, False otherwise.
    """
    from hsync_client.constants import HSYNCKEEP_FILENAME

    name = basename(file_path)

    # Sentinel file for empty-directory sync — never hidden
    if name == HSYNCKEEP_FILENAME:
        return False

    hidden_file_regex = (
        r"(^\..*|.*~$|^4913$|^\..*\.sw[px]$|^\.~lock\..*#$|"
        r".*\.bak$|.*\.tmp$|.*\.temp$|.*\.swp$|.*\.swo$|"
        r".*-\d{4}-\d{2}-\d{2}-\d{6}$)"
    )
    return bool(match(hidden_file_regex, name))


class File:
    """
    Represents a file with its metadata for synchronization.

    This class stores file information including path, timestamps, size,
    MD5 checksum, and permissions. It's used to track file state and
    determine sync actions.

    Attributes:
        filepath: Full path to the file.
        name: Base name of the file (extracted from filepath).
        ctime: Creation time as Unix timestamp.
        mtime: Modification time as Unix timestamp.
        size: File size in bytes.
        md5sum: MD5 checksum of file content.
        permissions: Symbolic permission string (e.g., '-rw-r--r--').
    """

    def __init__(
        self,
        filepath: str | None = None,
        ctime: str | int | float | datetime | None = None,
        mtime: str | int | float | datetime | None = None,
        size: int = 0,
        md5sum: str | None = None,
        permissions: str | None = None,
    ):
        """
        Initialize a File instance.

        Args:
            filepath: Full path to the file.
            ctime: Creation time (datetime, ISO string, or timestamp).
            mtime: Modification time (datetime, ISO string, or timestamp).
            size: File size in bytes.
            md5sum: MD5 checksum of file content.
            permissions: Symbolic permission string.
        """
        self.filepath = filepath
        self.name = basename(filepath) if filepath else None
        self.size = size
        self.md5sum = md5sum
        self.permissions = permissions
        self.ctime = self._parse_time(ctime)
        self.mtime = self._parse_time(mtime)

    def __repr__(self) -> str:
        """Return string representation of the File instance."""
        return (
            f"{self.__class__.__name__}("
            f"filepath={self.filepath}, "
            f"name={self.name}, "
            f"ctime={self.ctime}, "
            f"mtime={self.mtime}, "
            f"size={self.size}, "
            f"md5sum={self.md5sum}, "
            f"permissions={self.permissions})"
        )

    def _parse_time(self, time: str | int | float | datetime | None) -> int | None:
        """
        Parse various time formats to Unix timestamp.

        Handles datetime objects, ISO format strings, and numeric
        timestamps (int or float).

        Args:
            time: Time value in various formats.

        Returns:
            Unix timestamp as integer, or None if input is None.
        """
        if isinstance(time, datetime):
            return int(time.timestamp())
        elif isinstance(time, str):
            return int(datetime.fromisoformat(time).timestamp())
        elif isinstance(time, (int, float)):
            return int(datetime.fromtimestamp(time, UTC).timestamp())
        return None

    @staticmethod
    def _compute_md5(filepath: str) -> str:
        """
        Compute the MD5 checksum of a file.

        Reads the entire file into memory for hashing.

        Args:
            filepath: Path to the file.

        Returns:
            MD5 hash as hexadecimal string.
        """
        with open(filepath, "rb") as f:
            return md5(f.read()).hexdigest()

    @staticmethod
    def _get_permissions(obj_path: str) -> str:
        """
        Get file permissions in symbolic notation.

        Args:
            obj_path: Path to the file.

        Returns:
            Permission string in symbolic format (e.g., '-rw-r--r--').
        """
        obj_stat = os_stat(obj_path)
        permissions = obj_stat.st_mode
        return stat.filemode(permissions)

    def to_dict(self) -> dict:
        """
        Convert the File instance to a dictionary.

        Returns:
            Dictionary with all file metadata.
        """
        return {
            "filepath": self.filepath,
            "name": self.name,
            "ctime": self.ctime,
            "mtime": self.mtime,
            "size": self.size,
            "md5sum": self.md5sum,
            "permissions": self.permissions,
        }

    @classmethod
    def get(cls, file_path: str, buckets_dir: str | None = None) -> Optional["File"]:
        """
        Get a File instance for an existing file.

        Reads file metadata from the filesystem including timestamps,
        size, MD5 hash, and permissions.

        Args:
            file_path: Path to the file.
            buckets_dir: Optional base directory to prepend.

        Returns:
            File instance if file exists, None otherwise.
        """
        file_path = join(buckets_dir, file_path.lstrip("/")) if buckets_dir else file_path
        if exists(file_path):
            return cls(
                filepath=file_path,
                ctime=int(getctime(file_path)),
                mtime=int(getmtime(file_path)),
                size=getsize(file_path),
                md5sum=cls._compute_md5(file_path),
                permissions=cls._get_permissions(file_path),
            )
        else:
            return None

    @classmethod
    def from_dict(
        cls,
        data: dict,
        dir_prefix: str | None = None,
        strip_dir_prefix: bool = True,
    ) -> "File":
        """
        Create a File instance from a dictionary.

        Args:
            data: Dictionary containing file metadata.
            dir_prefix: Base directory for path manipulation.
            strip_dir_prefix: If True, remove dir_prefix from filepath.

        Returns:
            New File instance.

        Raises:
            ValueError: If strip_dir_prefix is True but dir_prefix is not set.
        """
        if strip_dir_prefix and not dir_prefix:
            raise ValueError("Set dir_prefix to strip it.")

        data_filepath = data.get("filepath", "")
        if strip_dir_prefix and dir_prefix:
            filepath = str(data_filepath).removeprefix(dir_prefix).lstrip("/")
        elif dir_prefix:
            filepath = join(dir_prefix, str(data_filepath).lstrip("/"))
        else:
            filepath = str(data_filepath)

        return cls(
            filepath=filepath,
            ctime=data.get("ctime"),
            mtime=data.get("mtime"),
            size=data.get("size", 0),
            md5sum=data.get("md5sum"),
            permissions=data.get("permissions"),
        )
