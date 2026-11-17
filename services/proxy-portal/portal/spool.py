"""
The portal's side of the applier's spool (applier/README.md#spool):

    requests/<id>.json   written here: a temp name not ending in .json, then renamed, so it is never read half-written
    status/<id>.json     read here: applying, applied, failed or unconfirmed
    results/<id>.json    the one-time password of an add or rotate, owned by the portal's uid, mode 0400: read once
                         and deleted at once

The portal never keeps a password: take_result returns it to the one response that shows it.
"""

import json
import os
import re
import stat
import uuid

from django.conf import settings
from django.views.decorators.debug import sensitive_variables

ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
MAX_STATUS_BYTES = 64 * 1024
MAX_RESULT_BYTES = 4096


class ResultError(Exception):
    """The result file is missing (expired, or already taken) or not one the applier wrote."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason  # "missing" or "unreadable"


def new_id():
    return str(uuid.uuid4())


def _dir(name):
    return os.open(str(settings.SPOOL_DIR / name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def write_request(request_id, op, username):
    """Write requests/<id>.json atomically: exclusive create of .<id>.tmp, fsync, rename. Raises OSError."""
    assert ID_RE.fullmatch(request_id)
    data = json.dumps({"id": request_id, "op": op, "username": username}).encode("utf-8")
    dfd = _dir("requests")
    tmp = f".{request_id}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, f"{request_id}.json", src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


def read_status(request_id):
    """status/<id>.json as a dict, or None while there is none (or it cannot be read)."""
    if not ID_RE.fullmatch(request_id):
        return None
    try:
        dfd = _dir("status")
    except OSError:
        return None
    try:
        fd = os.open(f"{request_id}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, MAX_STATUS_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_STATUS_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("id") != request_id or not isinstance(data.get("status"), str):
        return None
    return data


def result_exists(request_id):
    try:
        dfd = _dir("results")
    except OSError:
        return False
    try:
        os.lstat(f"{request_id}.json", dir_fd=dfd)
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


@sensitive_variables("raw", "data", "password")
def take_result(request_id, username):
    """
    Read results/<id>.json and delete it at once; return its password. The file must be a regular file with one
    link, owned by this process's uid, as the applier writes it. Raises ResultError.
    """
    assert ID_RE.fullmatch(request_id)
    name = f"{request_id}.json"
    try:
        dfd = _dir("results")
    except OSError:
        raise ResultError("unreadable")
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        except FileNotFoundError:
            raise ResultError("missing")
        except OSError:
            _unlink(dfd, name)
            raise ResultError("unreadable")
        try:
            st = os.fstat(fd)
            ok = stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid()
            raw = os.read(fd, MAX_RESULT_BYTES + 1) if ok else b""
        finally:
            os.close(fd)
        _unlink(dfd, name)  # right after reading: a result is shown once
    finally:
        os.close(dfd)
    if not ok or len(raw) > MAX_RESULT_BYTES:
        raise ResultError("unreadable")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ResultError("unreadable")
    if not isinstance(data, dict):
        raise ResultError("unreadable")
    password = data.get("password")
    if data.get("id") != request_id or data.get("username") != username or not isinstance(password, str) \
            or not password:
        raise ResultError("unreadable")
    return password


def drop_result(request_id):
    """Delete results/<id>.json, if any: a result a later request made invalid."""
    assert ID_RE.fullmatch(request_id)
    try:
        dfd = _dir("results")
    except OSError:
        return
    try:
        _unlink(dfd, f"{request_id}.json")
    finally:
        os.close(dfd)


def _unlink(dfd, name):
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass
