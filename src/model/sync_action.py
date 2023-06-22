"""
Sync action model for HSYNC Client.

This module defines the SyncAction class and SyncActionName enum for
representing synchronization decisions made by the client when comparing
local and remote file states.
"""

from datetime import timedelta as td
from enum import Enum
from typing import Optional

from .file import File


class SyncActionName(str, Enum):
    """
    Enumeration of possible synchronization actions.

    These actions are determined by comparing local and remote file
    states to decide what operation is needed.

    Values:
        download: File should be downloaded from server (remote is newer).
        upload: File should be uploaded to server (local is newer).
        nothing: No action needed (files are in sync).
    """

    download = "download"
    upload = "upload"
    nothing = "nothing"


class SyncAction:
    """
    Represents a synchronization action for a file.

    Encapsulates the decision about what to do with a file during
    synchronization, including the action type and time difference
    between local and remote versions.

    Attributes:
        file: The File object this action applies to.
        name: The type of action (download, upload, nothing).
        timedelta: Human-readable time difference string.
    """

    def __init__(
        self,
        file: File,
        name: Optional[SyncActionName] = None,
        timedelta: int = 0,
    ):
        """
        Initialize a SyncAction instance.

        Args:
            file: The File object this action applies to.
            name: The type of sync action.
            timedelta: Time difference in seconds between versions.
        """
        self.file = file
        self.name = name
        self.timedelta = self._format_timedelta(timedelta)

    def __repr__(self) -> str:
        """Return string representation of the SyncAction instance."""
        return (
            f"{self.__class__.__name__}("
            f"file={self.file}, "
            f"name={self.name}, "
            f"timedelta={self.timedelta})"
        )

    def to_dict(self) -> dict:
        """
        Convert the SyncAction instance to a dictionary.

        Returns:
            Dictionary with file, name, and timedelta keys.
        """
        return {
            "file": self.file.to_dict(),
            "name": self.name,
            "timedelta": self.timedelta,
        }

    def _format_timedelta(self, delta_seconds: int) -> str:
        """
        Format a time delta in seconds to a human-readable string.

        Converts seconds to a compact format showing days, hours,
        minutes, and seconds as needed (e.g., "2d 3h 15m 30s").

        Args:
            delta_seconds: Time difference in seconds.

        Returns:
            Formatted string like "2d 3h 15m 30s" or "0s" if zero.
        """
        delta = td(seconds=delta_seconds)
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0:
            parts.append(f"{seconds}s")

        return " ".join(parts) if parts else "0s"
