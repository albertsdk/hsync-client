from datetime import timedelta
from enum import Enum
from .file import File


class SyncActionName(str, Enum):
    download = "download"
    upload = "upload"
    nothing = "nothing"


class SyncAction:
    def __init__(self, file: File, name: SyncActionName = None, timedelta: int = 0):
        self.file = file
        self.name = name
        self.timedelta = self._format_timedelta(timedelta)

    def __repr__(self):
        return (
            f"SyncAction(file={self.file}, "
            f"name={self.name}, "
            f"timedelta={self.timedelta})"
        )

    def to_dict(self):
        return {
            "file": self.file.to_dict(),
            "name": self.name,
            "timedelta": self.timedelta,
        }

    def _format_timedelta(self, delta_seconds):
        delta = timedelta(seconds=delta_seconds)
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
