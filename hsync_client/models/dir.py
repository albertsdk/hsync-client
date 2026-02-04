"""
Directory model for HSYNC Client.

This module defines the Dir class representing a directory with its
path and permissions, used for directory synchronization events.
"""

import stat

from os import stat as os_stat
from os.path import basename, dirname, exists, join
from typing import Optional


def is_hidden_dir(directory: str) -> bool:
    """
    Check if a directory is hidden.

    A directory is considered hidden if:
    - Its name starts with a dot (.)
    - Any parent directory in its path starts with a dot

    Args:
        directory: Path to the directory.

    Returns:
        True if the directory is hidden, False otherwise.
    """
    # Check if the directory name itself starts with a dot
    dir_name = basename(directory.rstrip("/"))
    if dir_name.startswith("."):
        return True
    # Check if any parent directory is hidden
    parent = dirname(directory)
    if parent and parent != directory:
        parent_name = basename(parent)
        if parent_name.startswith("."):
            return True
    return False


class Dir:
    """
    Represents a directory with path and permissions.

    This class is used to track directory metadata for synchronization
    events, including creation, modification, and deletion of directories.

    Attributes:
        path: Absolute or relative path to the directory.
        permissions: Symbolic permission string (e.g., 'drwxr-xr-x').
    """

    def __init__(
        self, path: Optional[str] = None, permissions: Optional[str] = None
    ):
        """
        Initialize a Dir instance.

        Args:
            path: Path to the directory.
            permissions: Symbolic permission string.
        """
        self.path = path
        self.permissions = permissions

    def __repr__(self) -> str:
        """Return string representation of the Dir instance."""
        return (
            f"{self.__class__.__name__}("
            f"path={self.path}, "
            f"permissions={self.permissions})"
        )

    @staticmethod
    def _get_permissions(obj_path: str) -> str:
        """
        Get permissions for a path in symbolic notation.

        Args:
            obj_path: Path to the file or directory.

        Returns:
            Permission string in symbolic format.
        """
        obj_stat = os_stat(obj_path)
        permissions = obj_stat.st_mode
        return stat.filemode(permissions)

    def to_dict(self) -> dict:
        """
        Convert the Dir instance to a dictionary.

        Returns:
            Dictionary with path and permissions keys.
        """
        return {
            "path": self.path,
            "permissions": self.permissions,
        }

    @classmethod
    def from_dict(
        cls, data: dict, localpath: str, strip_localpath: bool = False
    ) -> "Dir":
        """
        Create a Dir instance from a dictionary.

        Args:
            data: Dictionary containing 'path' and optionally 'permissions'.
            localpath: Base path for resolving relative paths.
            strip_localpath: If True, remove localpath prefix from path.

        Returns:
            New Dir instance.
        """
        data_path = data.get("path", "")
        path = (
            str(data_path).removeprefix(localpath).lstrip("/")
            if strip_localpath
            else join(localpath, str(data_path).lstrip("/"))
        )
        return cls(
            path=path,
            permissions=data.get("permissions"),
        )

    @classmethod
    def get(cls, path: str, buckets_dir: Optional[str] = None) -> Optional["Dir"]:
        """
        Get a Dir instance for an existing directory.

        Args:
            path: Path to the directory.
            buckets_dir: Optional base directory to prepend.

        Returns:
            Dir instance if directory exists, None otherwise.
        """
        path = join(buckets_dir, path.lstrip("/")) if buckets_dir else path
        if exists(path):
            return cls(
                path=path,
                permissions=cls._get_permissions(path),
            )
        else:
            return None
