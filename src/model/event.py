from enum import Enum
from os.path import join
from typing import Union
from .dir import Dir
from .file import File


class EventName(str, Enum):
    added = "added"
    modified = "modified"
    renamed = "renamed"
    deleted = "deleted"


class Event:
    def __init__(
        self,
        cid: str,
        localpath: str,
        bucket: str,
        name: EventName,
        src: Union[File, Dir],
        dst: Union[File, Dir] = None,
        strip_localpath: bool = True,
    ):
        self.cid = cid
        self.bucket = bucket
        self.name = name.value

        def correct_path(entity, localpath, strip_localpath):
            # Correct the path based on the type of entity (File or Dir)
            path = str(entity.path) if isinstance(entity, Dir) else str(entity.filepath)
            if strip_localpath:
                path = path.removeprefix(localpath).lstrip("/")
            else:
                path = join(localpath, path.lstrip("/"))

            # Assign the corrected path to the entity
            if isinstance(entity, Dir):
                entity.path = path
            else:
                entity.filepath = path

        # Correct and assign the paths for src and dst
        correct_path(src, localpath, strip_localpath)
        self.src = src

        if dst is not None:
            correct_path(dst, localpath, strip_localpath)
        self.dst = dst

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"cid={self.cid}, "
            f"name={self.name}, "
            f"bucket={self.bucket}, "
            f"src={self.src}, "
            f"dst={self.dst})"
        )

    def to_dict(self):
        return {
            "cid": self.cid,
            "name": self.name,
            "bucket": self.bucket,
            "src": self.src.to_dict(),
            "dst": self.dst.to_dict() if self.dst else None,
        }
