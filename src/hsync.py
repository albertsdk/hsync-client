#!/usr/bin/python3

import stat

from model.event import Event, EventName
from model.dir import Dir, is_hidden_dir
from model.file import File, is_hidden_file
from model.sync_action import SyncAction, SyncActionName
from argparse import ArgumentParser, ArgumentTypeError, HelpFormatter
from asyncio import create_task, get_running_loop, run, run_coroutine_threadsafe, sleep
from copy import deepcopy
from datetime import datetime
from hashlib import md5
from json import dumps, loads
from os import (
    chmod,
    environ,
    getcwd,
    getpid,
    makedirs,
    remove,
    rename,
    stat as os_stat,
    utime,
    walk,
)
from os.path import (
    abspath,
    basename,
    dirname,
    exists,
    getmtime,
    getsize,
    islink,
    join,
    normpath,
)
from pathlib import Path
from random import choices
from re import sub
from requests import get, post
from requests.exceptions import RequestException
from shutil import move, rmtree
from ssl import CERT_NONE, CERT_REQUIRED, create_default_context, Purpose
from string import ascii_letters, digits
from sys import exc_info
from tempfile import NamedTemporaryFile
from time import sleep as time_sleep
from traceback import print_tb
from websockets import connect
from websockets.exceptions import ConnectionClosed
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


HSYNC_VERSION = "0.1.0"


def log(message: str = "", data: dict = None) -> None:
    """Logs a message with a timestamp, process ID, and log level.

    Args:
        message (str): The message to be logged.
    """
    if CONFIG and CONFIG["QUIET"]:
        return
    time = datetime.now()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S") + f"Z.{time.microsecond // 1000:03d}"
    level = "DEBUG" if DEBUG else "INFO"
    string = f"{timestamp} - [{getpid()}] {level}: {message}"
    if data:
        string = string + "\n" + dumps(data, indent=4, sort_keys=True)
    if CONFIG and CONFIG["LOGFILE"]:
        with open(CONFIG["LOGFILE"], "a") as file:
            file.write(f"{string}\n")
    else:
        print(string)


def is_valid_logfile(path_string: str) -> bool:
    try:
        path = Path(path_string)

        # If there's no parent directory, assume it's just a filename
        if path.parent == Path("."):
            # Basic check for forbidden characters in the filename
            forbidden_chars = {"<", ">", ":", '"', "/", "\\", "|", "?", "*"}
            if any(char in path.name for char in forbidden_chars):
                return False

            # Verify the filename doesn't use reserved names (Windows check)
            reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}  # etc.
            if path.name.upper() in reserved_names:
                return False

        else:
            # Check for absolute path if a directory is present
            if not path.is_absolute():
                return False

            # Check each part of the path for forbidden characters
            forbidden_chars = {"<", ">", ":", '"', "\\", "|", "?", "*"}
            for part in path.parts:
                if any(char in part for char in forbidden_chars):
                    return False

        return True
    except Exception:
        return False


def is_same_paths(path1, path2):
    return normpath(abspath(path1)) == normpath(abspath(path2))


def get_cid(length=10):
    characters = ascii_letters + digits
    random_string = "".join(choices(characters, k=length))
    return random_string


def bytes_to_human_readable(bytes):
    units = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    index = 0
    while bytes >= 1024 and index < len(units) - 1:
        bytes /= 1024.0
        index += 1
    return f"{bytes:.2f}{units[index]}"


def compute_md5(filepath):
    """Compute the MD5 checksum of a file."""
    with open(filepath, "rb") as f:
        return md5(f.read()).hexdigest()


def get_permissions(obj_path) -> str:
    obj_stat = os_stat(obj_path)
    permissions = obj_stat.st_mode
    return stat.filemode(permissions)


def set_permissions(file_path, permissions: str):
    if exists(file_path) and permissions == get_permissions(file_path):
        return

    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}
    for i, section in enumerate(
        [permissions[1:4], permissions[4:7], permissions[7:10]]
    ):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))
    if DEBUG:
        log(f'[P] Set permission "{permissions}" to {file_path}')
    chmod(file_path, mode)


def get_files_local() -> dict[File]:
    file_list = []
    for root, dirs, files in walk(CONFIG["LOCAL_BUCKET"]):
        # Skip hidden dirs
        dirs[:] = [d for d in dirs if not is_hidden_dir(d)]

        for file in files:
            if is_hidden_file(file):
                continue

            file_list.append(File().get(join(root, file)).to_dict())

    # Order
    file_list.sort(key=lambda x: x.get(CONFIG["SYNC_ORDER"]))

    return file_list


def get_files_remote() -> dict[File]:
    try:
        uri = f"{CONFIG["API"]["files"]}/{CONFIG["REMOTE_BUCKET"]}"
        response = get(
            uri,
            headers=CONFIG["REQUEST_HEADERS"],
            params={
                # TOFO: implement path
                # "path":
                "sync_order": CONFIG["SYNC_ORDER"]
            },
            verify=CONFIG["NO_VERIFY_SSL"],
        )

        if response.status_code != 200:
            raise Exception(response.text)

        return response.json()
    except Exception as e:
        raise Exception(f"Error downloading files list: {e}")


def download_file(file: File):
    uri = f"{CONFIG["API"]["download"]}/{CONFIG["REMOTE_BUCKET"]}"
    response = get(
        uri,
        headers=CONFIG["REQUEST_HEADERS"],
        params={"filename": file.filepath},
        stream=True,
        verify=CONFIG["NO_VERIFY_SSL"],
    )
    if DEBUG:
        log(f"[I] Request URI: {uri}")
        log(f"[I] Request Header:", CONFIG["REQUEST_HEADERS"])

    if response.status_code == 200:
        with NamedTemporaryFile(delete=False) as temp_file:
            temp_file_path = temp_file.name

        try:
            with open(temp_file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if not chunk:
                        break
                    f.write(chunk)

            md5sum = compute_md5(temp_file_path)
            if md5sum != file.md5sum:
                log(
                    f'[E] "{file.filepath}" '
                    f"(Invalid checksum: expected {file.md5sum}, "
                    f"{md5sum} given)"
                )
                return

            relative_dir = dirname(file.filepath)
            dest_dir = join(CONFIG["LOCAL_BUCKET"], relative_dir.lstrip("/"))
            makedirs(dest_dir, exist_ok=True)

            full_save_path = join(dest_dir, file.name.lstrip("/"))
            if exists(full_save_path):
                local_mtime = getmtime(full_save_path)
                if local_mtime >= file.mtime:
                    nothing_to_do(file)
                    return

            move(temp_file_path, full_save_path)
            log(f'[D] "{full_save_path}" {bytes_to_human_readable(file.size)}')

            utime(full_save_path, (file.ctime, file.mtime))

            if file.permissions:
                set_permissions(full_save_path, file.permissions)

        except Exception as e:
            log(f"[E] Error saving file: {e}")
        finally:
            if exists(temp_file_path):
                remove(temp_file_path)
    else:
        log(f"[E] Error downloading file: {response.status_code}")


def is_file_stable(file: File, interval: int = 2):
    last_size = getsize(file.filepath)
    time_sleep(interval)
    current_size = getsize(file.filepath)
    if DEBUG:
        log(
            f'[I] Writing file: "{file.filepath}" '
            f"({bytes_to_human_readable(last_size)} >"
            f" {bytes_to_human_readable(current_size)})"
        )
    return last_size == current_size


def upload_file(file: File, check_size: bool = True):
    if check_size:
        while not is_file_stable(file):
            time_sleep(1)
        # No more file chages, ready to upload!
        file.size = getsize(file.filepath)
        file.md5sum = compute_md5(file.filepath)
        if DEBUG:
            log(
                f'[I] Written file: "{file.filepath}" '
                f"({bytes_to_human_readable(file.size )})"
            )
        upload_file(file, False)
        return

    try:
        uri = f"{CONFIG["API"]["upload"]}/{CONFIG["REMOTE_BUCKET"]}"
        prefix = f"{CONFIG["LOCAL_BUCKET"]}".rstrip("/")
        path = dirname(file.filepath).rstrip("/").removeprefix(prefix).lstrip("/")
        data = {
            "cid": CONFIG["CID"],
            "path": path,
            "ctime": file.ctime,
            "mtime": file.mtime,
            "size": file.size,
            "md5sum": file.md5sum,
            "permissions": file.permissions,
        }

        if DEBUG:
            log(f"[I] Upload file: {file.filepath}")
            log(f"[I]         URI: {uri}")
            log(f"[I]      Header:", CONFIG["REQUEST_HEADERS"])
            log(f"[I]        data:", data)

        with open(file.filepath, "rb") as upload:
            files = {"file": (basename(file.filepath), upload)}
            response = post(
                uri,
                headers=CONFIG["REQUEST_HEADERS"],
                data=data,
                files=files,
                verify=CONFIG["NO_VERIFY_SSL"],
            )
            if response.status_code == 201:
                log(f'[U] "{file.filepath}" {bytes_to_human_readable(file.size)}')
            elif response.status_code == 409:
                if DEBUG:
                    log(f'[I] "{file.filepath}" (already present)')
            else:
                response.raise_for_status()
    except RequestException as e:
        log(f'[E] Error uploading: "{file.filepath}" {e}')
    except Exception as e:
        log(f"[E] Exception: {e}")


def nothing_to_do(file: File):
    if DEBUG:
        log(f'[-] "{file.filepath}"')


def get_sync_actions(
    remote_files: dict[File] = [], local_files: dict[File] = []
) -> list[SyncAction]:
    sync_actions = []
    remote_filepaths = set()

    for remote_file_info in remote_files:
        remote_file = File().from_dict(remote_file_info, CONFIG["LOCAL_BUCKET"])
        local_file = File().get(remote_file_info["filepath"], CONFIG["LOCAL_BUCKET"])
        # ...to compare same local and remote files paths
        remote_filepaths.add(remote_file.filepath)

        if local_file:
            # Determine the action based on modified date comparison
            if remote_file.mtime > local_file.mtime:
                action = SyncActionName.download
                timedelta = remote_file.mtime - local_file.mtime
            elif remote_file.mtime < local_file.mtime:
                action = SyncActionName.upload
                timedelta = local_file.mtime - remote_file.mtime
            else:
                action = SyncActionName.nothing  # No action needed
                timedelta = 0
            sync_actions.append(
                SyncAction(
                    remote_file if action == SyncActionName.download else local_file,
                    action,
                    timedelta,
                )
            )
        else:
            # If file does not exist locally, we need to download it
            sync_actions.append(SyncAction(remote_file, SyncActionName.download))

    # Identify local files not present in remote
    for local_file in local_files:
        local_file_info = File().from_dict(local_file, CONFIG["LOCAL_BUCKET"])
        if local_file_info.filepath not in remote_filepaths:
            # Schedule an upload for files not present in remote
            file = File().get(local_file["filepath"])
            sync_actions.append(SyncAction(file, SyncActionName.upload))

    return sync_actions


def valid_reconnect_delay(value):
    try:
        ivalue = int(value)
    except ValueError:
        raise ArgumentTypeError(f"The value '{value}' must be integer.")

    if ivalue <= 1 or ivalue >= 60:
        raise ArgumentTypeError("The value must be between 1 and 60 sec.")

    return ivalue


def valid_resync(value):
    try:
        ivalue = int(value)
    except ValueError:
        raise ArgumentTypeError(f"The value '{value}' must be integer.")

    if ivalue <= 20 or ivalue >= 3600:
        raise ArgumentTypeError("The value must be between 20 and 3600 sec.")

    return ivalue


def valid_bucket(value):
    if not value.isalnum():
        raise ArgumentTypeError(f"The bucket name must be alphanumeric.")
    return value


async def sync():
    if not CONFIG["DRYRUN"]:
        log("[I] Sync starting…")

    sync_actions = []
    try:
        remote_files = get_files_remote()
        local_files = get_files_local()
        sync_actions = get_sync_actions(remote_files, local_files)
        actions = {
            SyncActionName.download: lambda file: download_file(file),
            SyncActionName.upload: lambda file: upload_file(file, False),
            SyncActionName.nothing: lambda file: nothing_to_do(file),
        }
        if DEBUG:
            log(f"[I] Local files:", local_files)
            log(f"[I] Remote files:", remote_files)
            log(f"[I] Sync actions: {sync_actions}")
    except Exception as e:
        if DEBUG:
            exc_type, exc_value, exc_traceback = exc_info()
            log(f"[E] Exception type: {exc_type}")
            log(f"[E] Exception message: {exc_value}")
            log(f"[E] Traceback details: {print_tb(exc_traceback)}")
        else:
            log(f"[E] Exception: {e}")

    todo = 0

    if CONFIG["DRYRUN"]:
        for sync_action in sync_actions:
            if DEBUG:
                log(f"[I] Sync action:", sync_action.to_dict())
            else:
                if sync_action.name == SyncActionName.download:
                    action = "D"
                elif sync_action.name == SyncActionName.upload:
                    action = "U"
                else:
                    action = "-"

                # TODO: simplify using File class
                filepath = sync_action.file.filepath.removeprefix(
                    CONFIG["LOCAL_BUCKET"]
                ).lstrip("/")
                log(
                    f'[{action}] "{filepath}" '
                    f"{bytes_to_human_readable(sync_action.file.size)} "
                    f"({sync_action.timedelta})"
                )
    else:
        for sync_action in sync_actions:
            if sync_action.name in {SyncActionName.download, SyncActionName.upload}:
                actions[sync_action.name](sync_action.file)
                todo += 1

    if not CONFIG["DRYRUN"]:
        if todo == 0:
            log("[I] Nothing to sync")
        log("[I] Sync completed.")


def update_dir(data: dict) -> Dir:
    # Get local dir object with buckets path if exists
    dir = Dir().get(data["path"], CONFIG["LOCAL_BUCKET"])
    # ...exists?
    if dir:
        # ...same permissions?
        dir_data = Dir().from_dict(data, CONFIG["LOCAL_BUCKET"])
        if dir_data.permissions != dir.permissions:
            dir.permissions = dir_data.permissions
            set_permissions(dir.path, dir.permissions)
    else:
        dir = Dir().from_dict(data, CONFIG["LOCAL_BUCKET"])
        makedirs(dir.path, exist_ok=True)
        set_permissions(dir.path, dir.permissions)

    return dir


def update_file(data: dict):
    file_data = File().from_dict(data, CONFIG["LOCAL_BUCKET"], False)
    if exists(file_data.filepath):
        file = File().get(file_data.filepath)
        # ...need dowmnload?
        if file.md5sum != file_data.md5sum:
            file_data = File().from_dict(data, CONFIG["LOCAL_BUCKET"])
            download_file(file_data)
        elif file.permissions != file_data.permissions:
            set_permissions(file_data.filepath, file_data.permissions)
        else:
            if DEBUG:
                log(f'[I] File not changed: "{file_data.filepath}"')
    else:
        file = File().from_dict(data, CONFIG["LOCAL_BUCKET"])
        download_file(file)


async def remote_events():
    log("[I] Continuous sync starting…")
    # Construct the URI by formatting each part explicitly with single quotes
    # to avoid any conflicts or errors with double quotes within the f-string.
    api_events = CONFIG["API"]["events"]
    cid = CONFIG["CID"]
    bucket = CONFIG["REMOTE_BUCKET"]
    uri = f"{api_events}/{bucket}/{cid}?token={HSYNC_API_KEY}"

    ssl_context = None
    if str(api_events).startswith("wss"):
        ssl_context = create_default_context(Purpose.SERVER_AUTH)
        if CONFIG["NO_VERIFY_SSL"]:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = CERT_NONE
        else:
            ssl_context.check_hostname = True
            ssl_context.verify_mode = CERT_REQUIRED

    async with connect(
        uri,
        ssl=ssl_context,
    ) as websocket:
        uri_log = uri.replace(f"?token={HSYNC_API_KEY}", "")
        log(f"[I] WebSocket connected to {uri_log}")
        if DEBUG:
            log(f"[I] Sync reconnetion delay: {CONFIG["RECONNECT_DELAY"]} sec.")
            log(f"[I] Force resync: {CONFIG["RESYNC"]} sec.")

        log(f"[I] Client ID (CID): {CONFIG["CID"]}")

        loop = get_running_loop()
        event_handler = FileEventHandler(websocket, loop)
        observer = Observer()
        observer.schedule(event_handler, CONFIG["LOCAL_BUCKET"], recursive=True)
        observer.start()

        try:
            while True:
                data = await websocket.recv()
                message = loads(data)
                if DEBUG:
                    log(f"[I] Received message for CID {CONFIG["CID"]}: {message}")

                if message.get("name") not in {
                    EventName.added.value,
                    EventName.modified.value,
                    EventName.renamed.value,
                    EventName.deleted.value,
                }:
                    log(f"[E] Invalid event: {message.get("name")}")
                    continue

                if message.get("cid") == CONFIG["CID"]:
                    if DEBUG:
                        log(f"[W] Same CID, dropped.")
                    continue
                if message.get("bucket") != bucket:
                    if DEBUG:
                        log(f"[W] Different bucket, dropped.")
                    continue

                src = message.get("src", None)
                dst = message.get("dst", None)
                event_name = EventName(message.get("name"))

                # Added / Modified
                if event_name in {EventName.added, EventName.modified}:
                    # Dir
                    if src and "path" in src:
                        update_dir(src)
                    # File
                    elif src and "filepath" in src:
                        update_file(src)
                # Renamed
                elif event_name == EventName.renamed:
                    # Dir
                    if src and "path" in src:
                        src_dir = Dir().from_dict(src, CONFIG["LOCAL_BUCKET"])
                        dst_dir = Dir().from_dict(dst, CONFIG["LOCAL_BUCKET"])
                        if exists(src_dir.path):
                            rename(src_dir.path, dst_dir.path)
                        else:
                            log(f"[W] Cannot rename: {src_dir.path} -> {dst_dir.path}")
                            log(f"[W] Not found: {src_dir.path}")
                            update_dir(dst)
                    # File
                    elif src and "filepath" in src:
                        src_file = File().from_dict(src, CONFIG["LOCAL_BUCKET"], False)
                        dst_file = File().from_dict(dst, CONFIG["LOCAL_BUCKET"], False)
                        if exists(src_file.filepath):
                            rename(src_file.filepath, dst_file.filepath)
                        else:
                            log(
                                f'[W] Cannot rename: "{src_file.filepath}" -> "{dst_file.filepath}"'
                            )
                            log(f'[W] Not found: "{src_file.filepath}"')
                            download_file(dst_file)
                # Deleted
                elif event_name == EventName.deleted:
                    # Dir
                    if src and "path" in src:
                        dir = Dir().from_dict(src, CONFIG["LOCAL_BUCKET"])
                        if exists(dir.path):
                            rmtree(dir.path, ignore_errors=False)
                        else:
                            log(f'[W] Cannot delete: "{dir.path}"')
                            log(f'[W] Not found: "{dir.path}"')
                    # File
                    elif src and "filepath" in src:
                        file = File().from_dict(src, CONFIG["LOCAL_BUCKET"], False)
                        if exists(file.filepath):
                            remove(file.filepath)
                        else:
                            log(f'[W] Cannot delete: "{file.filepath}"')
                            log(f'[W] Not found: "{file.filepath}"')

        except ConnectionClosed as e:
            if e.reason:
                log(f"[W] Connection closed: {e.reason}")
            else:
                log(f"[W] Connection closed.")

        except Exception as e:
            log(f"[E] Exception: {e}")
        finally:
            observer.stop()
            observer.join()


async def resync():
    while True:
        log(f"[I] Force sync ({CONFIG["RESYNC"]} sec.)")
        await sync()
        await sleep(CONFIG["RESYNC"])


async def remote_connect():
    while True:
        try:
            await sync()
            if CONFIG["RESYNC"]:
                create_task(resync())
            await remote_events()
                
        except ConnectionClosed:
            await sleep(CONFIG["RECONNECT_DELAY"])
        except Exception as e:
            log(f"[E] An unexpected error occurred: {e}.")
            await sleep(CONFIG["RECONNECT_DELAY"])
        finally:
            log(f"[I] Reconnecting in {CONFIG["RECONNECT_DELAY"]} sec.…")


class FileEventHandler(FileSystemEventHandler):
    """Event handler to send websocket messages to other HSYNC clients
       on filesystem updates

    Args:
        FileSystemEventHandler (_type_):
            Base file system event handler that you can override methods from
    """

    def __init__(self, websocket, loop):
        self.websocket = websocket
        self.loop = loop

    def on_any_event(self, event):
        if DEBUG:
            hidden_prefix = "dir" if event.is_directory else "file"
            paths = {"src": event.src_path, "dest": event.dest_path}

            for kind, path in paths.items():
                if path and (
                    (event.is_directory and is_hidden_dir(path))
                    or (not event.is_directory and is_hidden_file(path))
                ):
                    log(f"[I] Skip hidden {kind} {hidden_prefix}: {path}")

    def on_created(self, event):
        # Dir
        if event.is_directory and not is_hidden_dir(event.src_path):
            log(f'[C] "{event.src_path}"')
            dir = Dir().get(event.src_path)
            message = Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.added,
                dir,
            )
            if DEBUG:
                log(f"[I] Send message: {message}")
            run_coroutine_threadsafe(
                self.websocket.send(dumps(message.to_dict())), self.loop
            )
        else:
            # Symlink
            if not is_hidden_file(event.src_path) and islink(event.src_path):
                # TODO: implement symlink event
                upload_file(File().get(event.src_path))

    def on_modified(self, event):
        # Dir
        if (
            event.is_directory
            and not is_hidden_dir(event.src_path)
            and not is_same_paths(CONFIG["LOCAL_BUCKET"], event.src_path)
        ):
            log(f'[M] "{event.src_path}"')
            dir = Dir().get(event.src_path)
            message = Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.modified,
                dir,
            )
            if DEBUG:
                log(f"[I] Send message: {message}")
            run_coroutine_threadsafe(
                self.websocket.send(dumps(message.to_dict())), self.loop
            )

    def on_closed(self, event):
        # Ensure the event pertains to a file, not a hidden one.
        if not event.is_directory and not is_hidden_file(event.src_path):
            try:
                if exists(event.src_path):
                    upload_file(File().get(event.src_path))
                else:
                    log(
                        f'[W] File "{event.src_path}" not found. '
                        f"It might have been altered by an external tool."
                    )
            except Exception as e:
                log(f'[E] Error handling closed event for "{event.src_path}": {e}')

    # def on_closed_no_write(self, event):
    #    return self.on_closed(event)

    def on_moved(self, event):
        if DEBUG:
            if event.dest_path:
                log(f'[M] "{event.src_path}" -> "{event.dest_path}"')
            else:
                log(f'[M] "{event.src_path}"')

        if event.is_directory:
            if is_hidden_dir(event.src_path) and not event.dest_path:
                return
            if is_hidden_dir(event.src_path) and is_hidden_dir(event.dest_path):
                return
            if is_hidden_dir(event.src_path) and not is_hidden_dir(event.dest_path):
                # TODO ???
                log("[W] TODO ???")
                return
            if is_hidden_dir(event.dest_path):
                log(f"[W] Switch event to deleted: {event.dest_path}")
                self.on_deleted(event.dest_path)
                return

            # Destination path already exist, source path is the old path
            # source object assume destination permissions (the event
            # object doesn't have it)
            dst = Dir().get(event.dest_path)
            src = deepcopy(dst)
            src.path = event.src_path

            if event.dest_path:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.renamed,
                    src,
                    dst,
                )
            else:
                message = Event(
                    CONFIG["CID"],
                    CONFIG["LOCAL_BUCKET"],
                    CONFIG["REMOTE_BUCKET"],
                    EventName.modified,
                    src,
                )
        else:
            if is_hidden_file(event.src_path):
                return

            # Destination file already exist, source file is the old file
            # source object assume destination data (the event
            # object doesn't have it)
            dst = File().get(event.dest_path)
            src = deepcopy(dst)
            src.filepath = event.src_path
            message = Event(
                CONFIG["CID"],
                CONFIG["LOCAL_BUCKET"],
                CONFIG["REMOTE_BUCKET"],
                EventName.renamed,
                src,
                dst,
            )

        if DEBUG:
            log(f"[I] Send message: {message}")
        run_coroutine_threadsafe(
            self.websocket.send(dumps(message.to_dict())), self.loop
        )

    def on_deleted(self, event):
        if event.is_directory and is_hidden_dir(event.src_path):
            return
        else:
            if is_hidden_file(event.src_path):
                return

        log(f'[R] "{event.src_path}"')
        obj = Dir(event.src_path) if event.is_directory else File(event.src_path)
        message = Event(
            CONFIG["CID"],
            CONFIG["LOCAL_BUCKET"],
            CONFIG["REMOTE_BUCKET"],
            EventName.deleted,
            obj,
        )

        if DEBUG:
            log(f"[I] Send message: {message}")
        run_coroutine_threadsafe(
            self.websocket.send(dumps(message.to_dict())), self.loop
        )


#
# Main
#
HSYNC_ENDPOINT_URL = environ.get("HSYNC_ENDPOINT_URL", None)
HSYNC_API_KEY = environ.get("HSYNC_API_KEY", "")
DEBUG = False
CONFIG = {}


def main():
    global CONFIG, DEBUG

    class SortedHelpFormatter(HelpFormatter):
        def add_arguments(self, actions):
            actions = sorted(actions, key=lambda action: action.option_strings)
            super(SortedHelpFormatter, self).add_arguments(actions)

    parser = ArgumentParser(
        description="HSYNC Client - HTTP Files synchronization client",
        formatter_class=SortedHelpFormatter,
    )
    parser.add_argument(
        "-u",
        "--endpoint-url",
        type=str,
        required=False if HSYNC_ENDPOINT_URL else True,
        help="HSYNC endpoint server URI.",
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="Displays the operations that would be performed without actually running them.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug messages.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not output any message.",
    )
    parser.add_argument(
        "-d",
        "--dir",
        type=str,
        required=False,
        default=getcwd(),
        help="Local path for syncing.",
    )
    parser.add_argument(
        "-l",
        "--logfile",
        type=str,
        required=False,
        help="Filename to log messages.",
    )
    parser.add_argument(
        "-b",
        "--bucket",
        type=valid_bucket,
        required=True,
        help="Bucket name. Where the files are stored.",
    )
    parser.add_argument(
        "--sync-order",
        type=str,
        required=False,
        default="mtime",
        choices=["mtime", "ctime"],
        help="Sync priority order.",
    )
    parser.add_argument(
        "-c",
        "--continuous",
        action="store_true",
        help="Continuous synchronization.",
    )
    parser.add_argument(
        "--resync",
        type=valid_resync,
        required=False,
        help=(
            "Automatic reconnection after the given seconds "
            "(only on continuous synchronization)."
        ),
    )
    parser.add_argument(
        "--reconnect-delay",
        type=valid_reconnect_delay,
        required=False,
        default=5,
        help=(
            "Try automatic reconnection again after the given seconds "
            "(only on continuous synchronization)."
        ),
    )
    parser.add_argument(
        "--legend",
        action="store_true",
        help="Show log legend description.",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_false",
        help=(
            "By default, the HSYNC client uses SSL when communicating with AWS services. "
            "For each SSL connection, the HSYNC client will verify SSL certificates. "
            "This option overrides the default behavior of verifying SSL certificates."
        ),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {HSYNC_VERSION}",
        help="Display the version of the tool.",
    )

    args = parser.parse_args()

    if not exists(args.dir):
        log(f"[E] Directory '{args.dir}' does not exist")
        return

    if args.logfile and not is_valid_logfile(args.logfile):
        log(f"[E] Invalid logfile '{args.logfile}'")
        return

    endpoint_url = args.endpoint_url or HSYNC_ENDPOINT_URL
    ws_url = sub(r"^http:", "ws:", endpoint_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    CONFIG = {
        "API": {
            "download": f"{endpoint_url}/download/v1",
            "events": f"{ws_url}/events/v1",
            "files": f"{endpoint_url}/files/v1",
            "upload": f"{endpoint_url}/upload/v1",
        },
        "CID": get_cid(),
        "DRYRUN": args.dryrun,
        "LOCAL_BUCKET": args.dir,
        "LOGFILE": args.logfile,
        "NO_VERIFY_SSL": args.no_verify_ssl,
        "QUIET": args.quiet,
        "REMOTE_BUCKET": args.bucket,
        "REQUEST_HEADERS": {
            "accept": "application/json",
            "Authorization": f"Bearer {HSYNC_API_KEY}",
            "X-Client-Name": basename(__file__),
            "X-Client-Version": HSYNC_VERSION,
        },
        "RECONNECT_DELAY": args.reconnect_delay,
        "RESYNC": args.resync,
        "SYNC_ORDER": args.sync_order,
    }
    DEBUG = args.debug

    if DEBUG:
        log(f"[I] Local configuration:", CONFIG)

    if args.legend:
        log("[I] Log legend")
        log_levels = {
            "-": "File not changed (no action required)",
            "D": "Download file",
            "E": "Error",
            "I": "Info",
            "M": "Move/Rename file/dir",
            "P": "Set permission",
            "R": "Remove file/dir",
            "U": "Upload file",
            "W": "Warning",
        }
        for key, description in log_levels.items():
            log(f"    {key}: {description}")

    # Continuous sync
    if CONFIG["DRYRUN"] and args.continuous:
        log("[W] Contiunous sync is disabled due to dryrun option set")
    elif not CONFIG["DRYRUN"] and args.continuous:
        # Start continuous directory watching
        run(remote_connect())
    else:
        # Just single sync
        run(sync())


if __name__ == "__main__":
    main()
