import stat

from os import stat as os_stat
from os.path import basename, dirname, exists, join


def is_hidden_dir(directory: str):
    return basename(dirname(directory)).startswith(".")


class Dir:
    def __init__(self, path: str = None, permissions: str = None):
        self.path = path
        self.permissions = permissions

    def __repr__(self):
        return f"Dir(path={self.path}, permissions={self.permissions})"

    def _get_permissions(obj_path) -> str:
        obj_stat = os_stat(obj_path)
        permissions = obj_stat.st_mode
        return stat.filemode(permissions)

    def to_dict(self):
        return {
            "path": self.path,
            "permissions": self.permissions,
        }

    @classmethod
    def from_dict(cls, data: dict, localpath: str, strip_localpath: bool = False):
        path = (
            str(data.get("path")).removeprefix(localpath).lstrip("/")
            if strip_localpath
            else join(localpath, data.get("path").lstrip("/"))
        )
        return cls(
            path=path,
            permissions=data.get("permissions", None),
        )

    @classmethod
    def get(cls, path: str, buckets_dir: str = None):
        path = join(buckets_dir, path.lstrip("/")) if buckets_dir else path
        if exists(path):
            return cls(
                path=path,
                permissions=cls._get_permissions(path),
            )
        else:
            return None
