"""
Event model for HSYNC Client.

This module defines the Event class and EventName enum for representing
file system events that need to be synchronized between client and server.
Events include file additions, modifications, renames, and deletions.
"""

from enum import StrEnum
from os.path import join

from .dir import Dir
from .file import File
from .symlink import Symlink


class EventName(StrEnum):
    """
    Enumeration of file system event types.

    These event types correspond to file system changes that trigger
    synchronization between client and server.

    Values:
        added: A new file or directory was created.
        modified: An existing file was modified.
        renamed: A file or directory was renamed/moved.
        deleted: A file or directory was deleted.
    """

    added = "added"
    modified = "modified"
    renamed = "renamed"
    deleted = "deleted"
    symlink = "symlink"


class Event:
    """
    Represents a file system synchronization event.

    Events are broadcast via WebSocket to notify other clients about
    file system changes. Each event contains information about the
    source file/directory and optionally a destination (for renames).

    Attributes:
        cid: Client ID that originated the event.
        bucket: Name of the bucket where the event occurred.
        name: Type of event (added, modified, renamed, deleted).
        src: Source file or directory affected by the event.
        dst: Destination file or directory (for rename events).
    """

    def __init__(
        self,
        cid: str,
        localpath: str,
        bucket: str,
        name: EventName,
        src: File | Dir | Symlink,
        dst: File | Dir | Symlink | None = None,
        strip_localpath: bool = True,
    ):
        """
        Initialize an Event instance.

        Automatically normalizes paths based on whether the event is
        being sent (strip_localpath=True) or received (strip_localpath=False).

        Args:
            cid: Client ID that originated the event.
            localpath: Local base path for path normalization.
            bucket: Name of the bucket where the event occurred.
            name: Type of event from EventName enum.
            src: Source file, directory, or symlink affected by the event.
            dst: Destination for rename events (optional).
            strip_localpath: If True, remove localpath prefix from paths.
        """
        self.cid = cid
        self.bucket = bucket
        self.name = name.value

        def correct_path(
            entity: File | Dir | Symlink, localpath: str, strip_localpath: bool
        ) -> None:
            """
            Normalize the path of a File, Dir, or Symlink entity.

            Strips or prepends the localpath based on the strip_localpath flag.
            This allows the same Event class to be used for both sending
            (relative paths) and receiving (absolute paths) events.

            Args:
                entity: File, Dir, or Symlink object to modify.
                localpath: Base path for normalization.
                strip_localpath: If True, remove localpath prefix.
            """
            # Get path based on entity type
            path = str(entity.path) if isinstance(entity, Dir) else str(entity.filepath)

            # Normalize path
            if strip_localpath:
                path = path.removeprefix(localpath).lstrip("/")
            else:
                path = join(localpath, path.lstrip("/"))

            # Assign corrected path back to entity
            if isinstance(entity, Dir):
                entity.path = path
            else:
                entity.filepath = path

        # Correct and assign paths for source
        correct_path(src, localpath, strip_localpath)
        self.src = src

        # Correct and assign paths for destination (if present)
        if dst is not None:
            correct_path(dst, localpath, strip_localpath)
        self.dst = dst

    def __repr__(self) -> str:
        """Return string representation of the Event instance."""
        return (
            f"{self.__class__.__name__}("
            f"cid={self.cid}, "
            f"name={self.name}, "
            f"bucket={self.bucket}, "
            f"src={self.src}, "
            f"dst={self.dst})"
        )

    def to_dict(self) -> dict:
        """
        Convert the Event instance to a dictionary.

        Used for JSON serialization when sending events via WebSocket.

        Returns:
            Dictionary representation of the event.
        """
        return {
            "cid": self.cid,
            "name": self.name,
            "bucket": self.bucket,
            "src": self.src.to_dict(),
            "dst": self.dst.to_dict() if self.dst else None,
        }
