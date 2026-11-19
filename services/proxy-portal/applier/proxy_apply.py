#!/usr/bin/env python3
"""
Applies proxy account change requests from a spool directory to the accounts file and the running proxies.

    proxy_apply.py init      create the spool folders with their owners and modes (run as root)
    proxy_apply.py run       apply the pending requests, one batch at a time, then write status.json
    proxy_apply.py status    print the sanitized accounts status (proxy_accounts.py status --json)

The spool (default /var/lib/proxy-portal/spool):
  requests/<id>.json   written by the requester (the portal), owned by its uid; consumed by run
  status/<id>.json     one per request: applying, then applied, failed or unconfirmed; no passwords
  results/<id>.json    the new password of an add or rotate, mode 0400, owned by the requester's uid;
                       the requester deletes it after reading, run deletes it after RESULT_TTL seconds
  status.json          the accounts without passwords, for the portal

A request is {"id": ..., "op": "add|rotate|enable|disable|remove|admin-on|admin-off", "username": ...}
and nothing else, or {"id": ..., "op": "grant|revoke", "username": ..., "proxy": ...}: the private proxy to grant
to the account or to take back. Passwords are generated here, never taken from a request. svc-ha and svc-monitor are
managed with proxy_accounts.py only; service accounts are never admins and never granted a proxy. Settings come from
the environment (PROXY_APPLY_*) or the options below, never from a request. Python 3.8+, standard library only.
"""

import argparse
import base64
import datetime
import json
import os
import pwd
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

SERVICES_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICES_DIR / "xray" / "scripts"))
import proxy_accounts as pa  # noqa: E402

DEFAULT_SPOOL = Path("/var/lib/proxy-portal/spool")
GRANT_OPS = ("grant", "revoke")  # carry the private proxy as "proxy"
OPS = ("add", "rotate", "enable", "disable", "remove", "admin-on", "admin-off") + GRANT_OPS
REQUEST_KEYS = {"id", "op", "username"}
MAX_PROXY_NAME = 128
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
REQUEST_FILE_RE = re.compile(r"([a-z0-9][a-z0-9-]{7,63})\.json")
MAX_REQUEST_BYTES = 4096
MAX_BATCH = 64
RESULT_TTL = 600       # seconds a one-time result is kept
STALE_TEMP = 3600      # seconds before a requester's leftover temp file is removed

HA_SERVICE = "xray-ha"
BLACKBOX_SERVICE = "blackbox-exporter"
BACKUP_DIR = Path("proxy-accounts/.apply-backup")
MANIFEST = "manifest.json"
SEQUENCE_FILE = "sequence.json"  # in the spool: the last apply sequence number

Run = Callable[[Sequence[str], Optional[str], float], Tuple[int, str, str]]
# (target, user, password): the target is ha-general, ha-private or a proxy's own entry, by its Compose service
# (xray-<proxy>), whose address the applier finds in the Compose file
Probe = Callable[[str, str, str], Tuple[str, Optional[int]]]
ENTRIES = (pa.GENERAL, pa.HA_PRIVATE)
Login = Tuple[str, str, str, str]  # (label, entry, user, password)


class RequestError(Exception):
    pass


class ApplyError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- requests

def parse_request(raw: bytes) -> Dict[str, str]:
    """Validate one request file's bytes; raises RequestError."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError(f"larger than {MAX_REQUEST_BYTES} bytes")

    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise RequestError("duplicate keys")
        return dict(pairs)

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:  # deep nesting raises RecursionError on 3.8
        raise RequestError(f"not UTF-8 JSON: {type(e).__name__}")
    keys = REQUEST_KEYS | {"proxy"} if isinstance(data, dict) and data.get("op") in GRANT_OPS else REQUEST_KEYS
    if not isinstance(data, dict) or set(data) != keys:
        raise RequestError(f"must be an object with exactly the keys {sorted(REQUEST_KEYS)}, and proxy for "
                           f"{' and '.join(GRANT_OPS)}")
    if not all(isinstance(v, str) for v in data.values()):
        raise RequestError("every value must be a string")
    if not ID_RE.fullmatch(data["id"]):
        raise RequestError("invalid id")
    if data["op"] not in OPS:
        raise RequestError(f"op must be one of {', '.join(OPS)}")
    if "proxy" in data and (len(data["proxy"]) > MAX_PROXY_NAME or not pa.PROXY_NAME_RE.fullmatch(data["proxy"])):
        raise RequestError("invalid proxy name")
    try:
        pa.validate_name(data["username"])
    except pa.AccountsError as e:
        raise RequestError(str(e))
    if data["username"] in pa.PROTECTED:
        raise RequestError(f"{data['username']} is managed with proxy_accounts.py only")
    request = {"id": data["id"], "op": data["op"], "username": data["username"]}
    if "proxy" in data:
        request["proxy"] = data["proxy"]
    return request


def apply_op(accounts: pa.Accounts, request: Dict[str, str],
             private: Optional[Set[str]] = None) -> Tuple[bool, Optional[str]]:
    """
    Apply one request to the accounts in memory: (changed, new password or None). Raises AccountsError. private:
    the private proxies that have a config, the only ones a grant may name (a revoke takes back any granted name).
    """
    op, name = request["op"], request["username"]
    if name in pa.PROTECTED:  # parse_request refuses them already
        raise pa.AccountsError(f"{name} is managed with proxy_accounts.py only")
    if op == "add":
        if name in accounts.accounts:
            raise pa.AccountsError(f"account {name!r} exists")
        password = pa.new_password()
        accounts.accounts[name] = password
        return True, password
    accounts.require(name)
    if op == "rotate":
        password = pa.new_password()
        accounts.accounts[name] = password
        return True, password
    if op == "remove":
        accounts.remove(name)
        return True, None
    if op in ("admin-on", "admin-off"):
        return accounts.set_admin(name, op == "admin-on"), None
    if op == "grant":
        return accounts.grant(name, request["proxy"], private or set()), None
    if op == "revoke":
        return accounts.revoke(name, request["proxy"]), None
    return accounts.set_enabled(name, op == "enable"), None


# ---------------------------------------------------------------- spool

def _dir_fd(path: Path) -> int:
    return os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def write_public(path: Path, text: str) -> None:
    """Replace path atomically with a world-readable file (status files hold no secrets)."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


class Spool:
    def __init__(self, root: Path, requester_uid: int):
        self.root = Path(root)
        self.requests = self.root / "requests"
        self.status = self.root / "status"
        self.results = self.root / "results"
        self.requester_uid = requester_uid

    def init(self, requester_gid: int) -> None:
        """Create the folders: the requester owns requests/ and results/ (0700); root owns the rest (0755)."""
        root = os.geteuid() == 0
        for path, owned, mode in ((self.root, False, 0o755), (self.status, False, 0o755),
                                  (self.requests, True, 0o700), (self.results, True, 0o700)):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise OSError(f"{path} is not a folder")
            if root:
                os.chown(str(path), *((self.requester_uid, requester_gid) if owned else (0, 0)))
            os.chmod(str(path), mode)

    def check(self) -> None:
        for path in (self.root, self.requests, self.status, self.results):
            st = os.lstat(str(path))
            if not stat.S_ISDIR(st.st_mode):
                raise OSError(f"{path} is not a folder: run init")

    def pending(self) -> List[str]:
        """Names in requests/ that end in .json, oldest first; removes the requester's stale temp files."""
        dfd = _dir_fd(self.requests)
        try:
            entries = []
            for name in os.listdir(dfd):
                try:
                    st = os.lstat(name, dir_fd=dfd)
                except FileNotFoundError:
                    continue
                if name.endswith(".json"):
                    entries.append((st.st_mtime_ns, name))
                elif time.time() - st.st_mtime > STALE_TEMP:
                    try:
                        self._discard(dfd, name)
                    except OSError as e:
                        log(f"temp file {name!r}: not removed: {type(e).__name__}")
            return [name for _, name in sorted(entries)]
        finally:
            os.close(dfd)

    def _discard(self, dfd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=dfd)
        except IsADirectoryError:
            shutil.rmtree(str(self.requests / name))
        except FileNotFoundError:
            pass
        except PermissionError:  # a folder, on systems where unlink says EPERM
            shutil.rmtree(str(self.requests / name))

    def discard(self, name: str) -> None:
        """Remove requests/<name>, whatever it is. Raises OSError when it cannot."""
        dfd = _dir_fd(self.requests)
        try:
            self._discard(dfd, name)
        finally:
            os.close(dfd)

    def read(self, name: str) -> Tuple[Optional[Dict[str, str]], Optional[str], Optional[int]]:
        """Read requests/<name> without removing it: (request, error, owner uid)."""
        dfd = _dir_fd(self.requests)
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
            except OSError as e:
                return None, f"cannot open: {e.strerror}", None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    return None, "not a regular file", st.st_uid
                if st.st_nlink != 1:
                    return None, "has more than one link", st.st_uid
                if st.st_uid != self.requester_uid:
                    return None, f"owned by uid {st.st_uid}, not the requester", st.st_uid
                chunks, size = [], 0
                while size <= MAX_REQUEST_BYTES:
                    chunk = os.read(fd, MAX_REQUEST_BYTES + 1 - size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
            finally:
                os.close(fd)
            try:
                request = parse_request(b"".join(chunks))
            except RequestError as e:
                return None, str(e), st.st_uid
            if f"{request['id']}.json" != name:
                return None, "id does not match the file name", st.st_uid
            return request, None, st.st_uid
        finally:
            os.close(dfd)

    def status_path(self, request_id: str) -> Path:
        assert ID_RE.fullmatch(request_id)
        return self.status / f"{request_id}.json"

    def max_sequence(self) -> int:
        """The highest applied_seq in the status files, 0 when there is none."""
        last = 0
        for path in self.status.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("applied_seq")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                last = max(last, value)
        return last

    def write_status(self, request_id: str, record: dict) -> None:
        write_public(self.status_path(request_id), json.dumps(dict(record, updated_at=now_iso()), indent=2) + "\n")

    def write_result(self, request_id: str, uid: int, username: str, password: str) -> None:
        """Write the one-time result: created exclusively, without following links, then renamed into place."""
        assert ID_RE.fullmatch(request_id)
        dfd = _dir_fd(self.results)
        tmp = f".{request_id}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400,
                         dir_fd=dfd)
            try:
                if os.geteuid() == 0:
                    os.fchown(fd, uid, -1)
                os.fchmod(fd, 0o400)
                text = json.dumps({"id": request_id, "username": username, "password": password}) + "\n"
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, f"{request_id}.json", src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        finally:
            os.close(dfd)

    def drop_result(self, request_id: str) -> None:
        dfd = _dir_fd(self.results)
        try:
            os.unlink(f"{request_id}.json", dir_fd=dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    def expire_results(self, ttl: float = RESULT_TTL) -> None:
        dfd = _dir_fd(self.results)
        try:
            for name in os.listdir(dfd):
                try:
                    st = os.lstat(name, dir_fd=dfd)
                    if not stat.S_ISDIR(st.st_mode) and time.time() - st.st_mtime > ttl:
                        os.unlink(name, dir_fd=dfd)
                except OSError:
                    pass
        finally:
            os.close(dfd)


# ---------------------------------------------------------------- docker and the HA entry

def subprocess_run(argv: Sequence[str], input_text: Optional[str], timeout: float) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


class Compose:
    def __init__(self, compose_file: Path, project: Optional[str], run: Run = subprocess_run):
        self.base = ["docker", "compose", "-f", str(compose_file)] + (["-p", project] if project else [])
        self.run = run

    def _ok(self, stage: str, *args: str, timeout: float = 120) -> str:
        rc, out, err = self.run(self.base + list(args), None, timeout)
        if rc != 0:
            raise ApplyError(stage, f"docker compose {args[0]} exited {rc}: {err.strip()[-300:]}")
        return out

    def services(self) -> Set[str]:
        return set(self._ok("precheck", "config", "--services").split())

    def running(self) -> Set[str]:
        return set(self._ok("precheck", "ps", "--services", "--status", "running").split())

    def image(self, stage: str, service: str) -> str:
        """The image the Compose file gives service."""
        try:
            image = json.loads(self._ok(stage, "config", "--format", "json"))["services"][service]["image"]
        except (ValueError, KeyError, TypeError):
            raise ApplyError(stage, f"no image for {service} in the compose config")
        if not isinstance(image, str):
            raise ApplyError(stage, f"no image for {service} in the compose config")
        return image

    def published(self, service: str, container_port: int) -> Optional[Tuple[Optional[str], int]]:
        """
        (host IP or None for every address, host port) that the Compose file publishes service's container_port on,
        or None when it publishes none or the Compose file cannot be read.
        """
        try:
            ports = json.loads(self._ok("verify", "config", "--format", "json"))["services"][service].get("ports")
        except (ApplyError, ValueError, KeyError, TypeError, AttributeError):
            return None
        for entry in ports or []:
            try:
                if int(entry.get("target")) != container_port or entry.get("protocol", "tcp") != "tcp":
                    continue
                port = int(str(entry.get("published")).split("-")[0])
            except (TypeError, ValueError, AttributeError):
                continue
            host = entry.get("host_ip") or None
            return (None if host in ("0.0.0.0", "::") else host), port
        return None

    def pinned_image(self, service: str) -> str:
        image = self.image("test", service)
        if "@sha256:" not in image:
            raise ApplyError("test", f"the image of {service} is not pinned by digest: {image}")
        return image

    def image_id(self, image: str) -> str:
        """The local image ID of an image reference."""
        rc, out, err = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], None, 60)
        if rc != 0 or not out.strip():
            raise ApplyError("precheck", f"image {image} is not on this host: {err.strip()[-200:]}")
        return out.strip()

    def container_images(self, service: str) -> List[str]:
        """The image IDs that service's containers were created from."""
        ids = self._ok("precheck", "ps", "-q", service).split()
        if not ids:
            return []
        rc, out, err = self.run(["docker", "inspect", "--format", "{{.Image}}"] + ids, None, 60)
        if rc != 0:
            raise ApplyError("precheck", f"docker inspect exited {rc}: {err.strip()[-300:]}")
        return out.split()

    def restart(self, services: Sequence[str]) -> None:
        if not services:  # docker compose restart without services restarts every service of the project
            raise ApplyError("restart", "no services to restart")
        self._ok("restart", "restart", *services, timeout=300)

    def xray_test(self, image: str, config_text: str) -> Tuple[bool, str]:
        """xray run -test on a config given on stdin: no file, no network."""
        rc, out, err = self.run(["docker", "run", "-i", "--rm", "--network", "none", image,
                                 "xray", "run", "-test", "-format", "json", "-c", "stdin:"], config_text, 120)
        return rc == 0, (out + err)


def _read_status(sock: socket.socket) -> Optional[int]:
    data = b""
    while b"\r\n" not in data and len(data) < 16384:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    match = re.match(rb"HTTP/1\.[01] (\d{3})[ \r]", data)
    return int(match.group(1)) if match else None


def proxy_probe(proxy: Tuple[str, int], url: str, user: str, password: str, timeout: float = 8.0,
                context: Optional[ssl.SSLContext] = None) -> Tuple[str, Optional[int]]:
    """
    One request for url through an HTTP proxy, logging in as user: ("status", code), ("noresponse", None)
    when the proxy took the connection but sent no status, or ("unreachable", None).
    An https url goes through CONNECT: a refused login is the CONNECT's 407.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    auth = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    try:
        sock = socket.create_connection(proxy, timeout=timeout)
    except OSError:
        return "unreachable", None
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if parts.scheme == "http":
            sock.sendall((f"GET {url} HTTP/1.1\r\nHost: {parts.netloc}\r\nProxy-Authorization: Basic {auth}\r\n"
                          "User-Agent: proxy-apply\r\nConnection: close\r\n\r\n").encode("ascii"))
            code = _read_status(sock)
        else:
            sock.sendall((f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                          f"Proxy-Authorization: Basic {auth}\r\nUser-Agent: proxy-apply\r\n\r\n").encode("ascii"))
            code = _read_status(sock)
            if code == 200:
                tls = (context or ssl.create_default_context()).wrap_socket(sock, server_hostname=host)
                sock = tls
                tls.sendall((f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUser-Agent: proxy-apply\r\n"
                             "Connection: close\r\n\r\n").encode("ascii"))
                code = _read_status(tls)
        return ("status", code) if code is not None else ("noresponse", None)
    except (OSError, ValueError):
        return "noresponse", None
    finally:
        sock.close()


# ---------------------------------------------------------------- applying

class Applier:
    def __init__(self, base_dir: Path, spool: Spool, compose: Compose, probe: Probe,
                 verify_timeout: float = 60, verify_interval: float = 2,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 addresses: Optional[Dict[str, Tuple[str, int]]] = None, proxy_host: str = "127.0.0.1"):
        self.base = Path(base_dir)
        # the probe's targets: the HA entries, and each private proxy's own entry once a check needs it, at its
        # published HTTP port on proxy_host (or the host address the Compose file publishes it on)
        self.addresses = addresses if addresses is not None else {}
        self.proxy_host = proxy_host
        self.spool = spool
        self.compose = compose
        self.probe = probe
        self.verify_timeout = verify_timeout
        self.verify_interval = verify_interval
        self.sleep = sleep
        self.clock = clock
        self.keep_backup = False  # set when a rollback could not restore the files: the next run retries
        self.delivered = {}  # type: Dict[str, str]  # username -> id of the last result written in this run

    # -- entry point

    def run(self) -> int:
        """
        Apply every pending request, one batch at a time, holding the accounts lock throughout.
        Exit status 1 when a rollback did not finish (a status with restored: false), 2 when a request entry
        could not be removed, else 0: refused and rolled-back requests are reported in their status files,
        not as a failure of the service.
        """
        self.spool.check()
        failures = 0
        stuck = set()  # type: Set[str]
        with pa.accounts_lock(self.base):
            self.recover()
            self.spool.expire_results()
            while True:
                names = [name for name in self.spool.pending() if name not in stuck][:MAX_BATCH]
                if not names:
                    break
                batch = self.take(names, stuck)
                if batch:
                    failures += self.apply_batch(batch)
                if (self.base / BACKUP_DIR / MANIFEST).exists():  # files not restored: the next run retries first
                    break
            self.write_portal_status()
        if stuck:
            log(f"{len(stuck)} request entries could not be removed")
        return 1 if failures else 2 if stuck else 0

    def take(self, names: List[str], stuck: Set[str]) -> List[Tuple[Dict[str, str], int]]:
        """
        Take the next batch, oldest first. An entry is removed only once its status is written (applying, or
        failed when refused), so no request disappears without one. A batch ends before a second request
        for the same account; that one waits for the next batch, so every request has its own outcome.
        """
        batch = []  # type: List[Tuple[Dict[str, str], int]]
        usernames = set()  # type: Set[str]
        for name in names:
            match = REQUEST_FILE_RE.fullmatch(name)
            request_id = match.group(1) if match else None
            if request_id and self.spool.status_path(request_id).exists():
                log(f"request {request_id}: id already used, dropped")
            else:
                try:
                    request, error, uid = self.spool.read(name)
                except Exception as e:  # an unexpected error refuses the request, it does not lose it
                    request, error, uid = None, f"cannot read: {type(e).__name__}", None
                if request is not None and request["username"] in usernames:
                    break
                if request is None:
                    log(f"request {name!r}: refused: {error}")
                    if request_id:
                        self.spool.write_status(request_id, {"id": request_id, "status": "failed",
                                                             "stage": "request", "error": error, "restored": True})
                else:
                    self.spool.write_status(request["id"], dict(request, status="applying"))
                    usernames.add(request["username"])
                    batch.append((request, uid))
            try:
                self.spool.discard(name)
            except OSError as e:
                log(f"request entry {name!r}: not removed: {type(e).__name__}")
                stuck.add(name)
        return batch

    def write_portal_status(self) -> None:
        view = pa.status(pa.Accounts(self.base))
        view["generated_at"] = now_iso()
        write_public(self.spool.root / "status.json", json.dumps(view, indent=2) + "\n")

    # -- one batch

    def apply_batch(self, batch: List[Tuple[Dict[str, str], int]]) -> int:
        records = {request["id"]: dict(request, status="applying") for request, _ in batch}  # type: Dict[str, dict]
        log("batch: " + ", ".join(f"{r['id']} {r['op']} {r['username']}" + (f" {r['proxy']}" if "proxy" in r else "")
                                  for r, _ in batch))

        try:
            accounts = pa.Accounts(self.base)
            before = pa.Accounts(self.base)
            private = pa.private_proxies(self.base) if any("proxy" in r for r, _ in batch) else set()
        except (pa.AccountsError, OSError, ValueError) as e:
            for record in records.values():
                record.update(status="failed", stage="accounts", error=str(e), restored=True)
            return self.finish(records, {}, [])
        old_passwords = dict(accounts.accounts)
        old_enabled = {name for name in accounts.accounts if accounts.enabled(name)}
        old_admins = set(accounts.admins)
        old_grants = {name: set(proxies) for name, proxies in accounts.grants.items()}
        passwords = {}  # type: Dict[str, str]
        active, changed_any = [], False
        for request, uid in batch:
            try:
                changed, password = apply_op(accounts, request, private)
            except pa.AccountsError as e:
                records[request["id"]].update(status="failed", stage="request", error=str(e), restored=True)
                continue
            changed_any |= changed
            records[request["id"]]["changed"] = changed
            if password is not None:
                passwords[request["id"]] = password
            active.append((request, uid))
        secrets_seen = list(old_passwords.values()) + list(accounts.accounts.values())

        if not active:
            return self.finish(records, {}, [])
        snapshot = None
        restart = []  # type: List[str]
        try:
            accounts_text = accounts.text()
            try:
                files = pa.render_files(accounts)
                known = self.running_known(before)
            except (pa.AccountsError, OSError, ValueError, KeyError) as e:
                raise ApplyError("render", str(e))
            outputs = [(path, text) for path, text in files
                       if not (path.exists() and path.read_text(encoding="utf-8") == text)]
            requests = [r for r, _ in active]
            if not changed_any and not outputs and known:
                # Nothing to change, by the record: still check each request's own account at the entries
                accept, refuse, unverified = self.request_checks(requests, old_passwords, old_grants, accounts)
                outcome, detail = unverified_outcome(self.verify(accept, refuse), unverified)
                if outcome != "failed":
                    seq = self.next_sequence()
                    for request, _ in active:
                        records[request["id"]].update(status="applied" if outcome == "ok" else "unconfirmed",
                                                      restarted=[], applied_seq=seq)
                        if outcome == "unconfirmed":
                            records[request["id"]].update(stage="verify", error=detail)
                    return self.finish(records, {}, active)
                log(f"the running services do not hold the files ({scrub(detail, secrets_seen)}): applying all")
                known = False
            # Unless the running services are known to hold the files, every rendered file counts as changed
            changed = outputs if known else files
            image = self.compose.pinned_image(HA_SERVICE)
            for path, text in changed:
                if path.name == "config.json":
                    ok, output = self.compose.xray_test(image, text)
                    if not ok:
                        last = ([line for line in output.splitlines() if line.strip()] or ["no output"])[-1]
                        raise ApplyError("test", f"{path.relative_to(self.base)}: {scrub(last, secrets_seen)}")
            # a batch of grants and revokes only restarts what it changes: no more than the proxies they name
            restart = self.restart_set(accounts, changed, grants_only=all(r["op"] in GRANT_OPS for r in requests))
            self.check_images(image, restart)
            try:
                snapshot = self.backup(outputs, accounts_text, restart, [r["id"] for r, _ in active])
            except OSError as e:
                raise ApplyError("backup", f"{type(e).__name__}: {e.strerror or e}")
            try:
                for path, text in outputs:  # the configs first, the accounts file last
                    pa.write_private(path, text)
                pa.write_private(accounts.path, accounts_text)
            except OSError as e:
                raise ApplyError("swap", f"{type(e).__name__}: {e.strerror or e}")
            if restart:
                log(f"files replaced; restarting {' '.join(restart)}")
                self.compose.restart(restart)
            else:
                log("files replaced; no config changed, nothing to restart")
            accept, refuse = self.expectations(old_passwords, old_enabled, old_admins, accounts)
            checks = self.request_checks(requests, old_passwords, old_grants, accounts)
            accept, refuse = merge_logins((accept, refuse), checks[:2])
            outcome, detail = unverified_outcome(self.verify(accept, refuse), checks[2])
            if outcome == "failed":
                raise ApplyError("verify", detail)
            status = "applied" if outcome == "ok" else "unconfirmed"
            render_sha256 = pa.render_sha256(self.base, files)
            try:
                seq = self.next_sequence()
                for request, _ in active:
                    records[request["id"]].update(status=status, restarted=restart, applied_seq=seq)
                    if status == "unconfirmed":
                        records[request["id"]].update(stage="verify", error=detail)
                self.commit(records, list(passwords), accounts_text, render_sha256)
            except OSError as e:
                raise ApplyError("commit", f"{type(e).__name__}: {e.strerror or e}")
        except ApplyError as e:
            error = scrub(str(e), secrets_seen)
            log(f"failed at {e.stage}: {error}")
            restored = True
            if snapshot is not None:
                restored = self.rollback(snapshot, restart)
                if not restored:
                    self.forget_applied()
            for request, _ in active:
                records[request["id"]].pop("restarted", None)
                records[request["id"]].pop("applied_seq", None)
                records[request["id"]].update(status="failed", stage=e.stage, error=error, restored=restored)
            return self.finish(records, {}, active)

        self.write_applied(pa.sha256_text(accounts_text), render_sha256, now_iso())
        log(f"{status}{': ' + detail if detail else ''}")
        return self.finish(records, passwords, active)

    def commit(self, records: Dict[str, dict], result_ids: List[str], accounts_text: str, render_sha256: str) -> None:
        """Mark the backup as committed: from here on, an interrupted apply is finished, not rolled back."""
        manifest = self.base / BACKUP_DIR / MANIFEST
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.update(committed=True, records=list(records.values()), results=result_ids,
                    accounts_sha256=pa.sha256_text(accounts_text), render_sha256=render_sha256, applied_at=now_iso())
        pa.write_private(manifest, json.dumps(data) + "\n")

    def write_applied(self, accounts_sha256: str, render_sha256: Optional[str], applied_at: str) -> None:
        """What the running services hold: the accounts file and the rendered files of the last verified apply."""
        pa.write_private(self.base / pa.APPLIED_FILE, json.dumps(
            {"accounts_sha256": accounts_sha256, "render_sha256": render_sha256, "applied_at": applied_at},
            indent=2) + "\n")

    def forget_applied(self) -> None:
        """After a rollback that did not finish, nothing is known to be running: the next batch restarts all."""
        try:
            (self.base / pa.APPLIED_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f"{pa.APPLIED_FILE}: not removed: {type(e).__name__}")

    def running_known(self, accounts: pa.Accounts) -> bool:
        """
        True when the running services are known to hold the accounts file as it is: the last apply recorded this
        file and these rendered files (applied.json), and the files on disk equal a render of it. A change made
        with proxy_accounts.py and rendered without the restart, or a missing applied.json, makes it False.
        """
        try:
            applied = json.loads((self.base / pa.APPLIED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(applied, dict) or applied.get("accounts_sha256") != pa.sha256_text(accounts.text()):
            return False
        try:
            files = pa.render_files(accounts)
        except pa.AccountsError:  # e.g. a grant of a proxy since removed, which this batch may revoke
            return False
        if any(not (path.exists() and path.read_text(encoding="utf-8") == text) for path, text in files):
            return False
        return applied.get("render_sha256") == pa.render_sha256(self.base, files)

    def next_sequence(self) -> int:
        """
        The next apply sequence number: increases with every batch that goes live, across runs. Kept in
        <spool>/sequence.json; without it, it continues from the highest in the status files.
        """
        path = self.spool.root / SEQUENCE_FILE
        try:
            last = int(json.loads(path.read_text(encoding="utf-8"))["last"])
        except (OSError, ValueError, KeyError, TypeError):
            last = self.spool.max_sequence()
        seq = last + 1
        write_public(path, json.dumps({"last": seq}) + "\n")
        return seq

    def check_images(self, image: str, restart: List[str]) -> None:
        """
        Every Xray service about to restart must be configured with the image the configs were tested with and run
        a container created from it: a restart keeps a container's image, so an older Xray that ignores the
        inbounds' users would accept anyone.
        """
        expected = None
        for service in restart:
            if not service.startswith("xray-"):
                continue
            configured = self.compose.image("precheck", service)
            if configured != image:
                raise ApplyError("precheck", f"{service} is configured with {configured}, not {image}")
            expected = expected or self.compose.image_id(image)
            if any(running != expected for running in self.compose.container_images(service)):
                raise ApplyError("precheck", f"{service} runs another image than {image}: recreate it with "
                                             f"docker compose up -d {service}")

    def finish(self, records: Dict[str, dict], passwords: Dict[str, str],
               active: List[Tuple[Dict[str, str], int]]) -> int:
        """Write the one-time results, then the final statuses, then drop the backup."""
        uids = {request["id"]: uid for request, uid in active}
        for request_id, record in records.items():
            if record["status"] in ("applied", "unconfirmed") and request_id in passwords:
                try:
                    self.spool.write_result(request_id, uids[request_id], record["username"], passwords[request_id])
                    record["result"] = True
                except OSError as e:
                    record["result"] = False
                    log(f"request {request_id}: result not written: {e.strerror}")
        for request_id, record in records.items():
            self.spool.write_status(request_id, record)
            log(f"request {request_id}: {record['status']}")
        if not self.keep_backup:
            shutil.rmtree(str(self.base / BACKUP_DIR), ignore_errors=True)
        self.keep_backup = False
        self.supersede(records)
        return sum(record["status"] == "failed" and not record.get("restored") for record in records.values())

    def supersede(self, records: Dict[str, dict]) -> None:
        """A live add, rotate or remove replaces the password of an earlier result of this run: drop that result."""
        for request_id, record in records.items():
            if record["status"] not in ("applied", "unconfirmed") or not record.get("changed"):
                continue
            if record["op"] in ("add", "rotate", "remove"):
                earlier = self.delivered.pop(record["username"], None)
                if earlier is not None:
                    self.spool.drop_result(earlier)
                    try:
                        previous = json.loads(self.spool.status_path(earlier).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        previous = {"id": earlier}
                    self.spool.write_status(earlier, dict(previous, result=False, superseded_by=request_id))
                    log(f"request {earlier}: result superseded by {request_id}")
            if record.get("result"):
                self.delivered[record["username"]] = request_id

    def restart_set(self, accounts: pa.Accounts, outputs: List[Tuple[Path, str]],
                    grants_only: bool = False) -> List[str]:
        """
        xray-ha; with raw auth every proxy but the open ones, which hold no accounts; a proxy whose config changed (a
        private proxy whenever the admins it accepts change, an open proxy when its logins are removed); and
        blackbox-exporter when its config did. grants_only, for a batch of grants and revokes only: just the
        services whose config changed, which may be none (a grant to a disabled account changes no config).
        xray-ha must run either way: the checks go through it.
        """
        running = self.compose.running()
        if HA_SERVICE not in running:
            raise ApplyError("precheck", f"{HA_SERVICE} is not running")
        known = self.compose.services()
        changed = {path for path, _ in outputs}
        if grants_only:
            restart = [HA_SERVICE] if self.base / pa.HA_CONFIG in changed else []
            for path in sorted(self.base.glob(pa.RAW_CONFIG_GLOB)):
                service = pa.PROXY_HOST_PREFIX + path.parent.parent.name
                if path in changed and service in known and service in running:
                    restart.append(service)
            if self.base / pa.BLACKBOX_CONFIG in changed and BLACKBOX_SERVICE in running:
                restart.append(BLACKBOX_SERVICE)
            return restart
        try:
            open_ = pa.open_proxies(self.base, pa.public_proxies(self.base))
        except (pa.AccountsError, OSError) as e:
            raise ApplyError("precheck", str(e))
        restart = [HA_SERVICE]
        for path in sorted(self.base.glob(pa.RAW_CONFIG_GLOB)):
            proxy = path.parent.parent.name
            service = "xray-" + proxy
            if ((accounts.raw_auth and proxy not in open_) or path in changed) and service in known \
                    and service in running:
                restart.append(service)
        if self.base / pa.BLACKBOX_CONFIG in changed and BLACKBOX_SERVICE in running:
            restart.append(BLACKBOX_SERVICE)
        return restart

    def request_checks(self, requests: List[Dict[str, str]], old_passwords: Dict[str, str],
                       old_grants: Dict[str, Set[str]], accounts: pa.Accounts) -> Tuple[List[Login], List[Login],
                                                                                         List[str]]:
        """(accept, refuse, unverified) for each request's own account: on both HA entries and at private proxies."""
        accept, refuse = self.target_checks(requests, old_passwords, accounts)
        proxy_accept, proxy_refuse, unverified = self.proxy_checks(requests, old_passwords, old_grants, accounts)
        accept, refuse = merge_logins((accept, refuse), (proxy_accept, proxy_refuse))
        return accept, refuse, unverified

    def proxy_target(self, proxy: str, running: Set[str]) -> Optional[str]:
        """The probe target of a private proxy's own entry (its Compose service), None when it is not running or
        publishes no HTTP port."""
        service = pa.PROXY_HOST_PREFIX + proxy
        if service not in running:
            return None
        if service not in self.addresses:
            published = self.compose.published(service, pa.PROXY_CONTAINER_PORTS["http"])
            if published is None:
                return None
            host, port = published
            self.addresses[service] = (host or self.proxy_host, port)
        return service

    def proxy_checks(self, requests: List[Dict[str, str]], old_passwords: Dict[str, str],
                     old_grants: Dict[str, Set[str]], accounts: pa.Accounts) -> Tuple[List[Login], List[Login],
                                                                                       List[str]]:
        """
        (accept, refuse, unverified) at the private proxies' own entries, where the grants count: for each request's
        account, at every private proxy it was or is granted and at the one a grant or revoke names, its login passes
        while it is enabled and an admin or granted that proxy and is refused otherwise, and a replaced password is
        refused. The proxy a grant or revoke names also lets svc-monitor in and refuses a wrong password and one
        enabled non-admin account not granted it. A private proxy that is not running or publishes no HTTP port
        cannot be checked: unverified lists it, and the outcome is unconfirmed at best.
        """
        accept, refuse, unverified = [], [], []  # type: Tuple[List[Login], List[Login], List[str]]
        wanted = []  # type: List[Tuple[Dict[str, str], str]]
        for request in requests:
            name, named = request["username"], request.get("proxy")
            for proxy in sorted(old_grants.get(name, set()) | accounts.grants.get(name, set())
                                | ({named} if named else set())):
                wanted.append((request, proxy))
        if not wanted:
            return accept, refuse, unverified
        private = pa.private_proxies(self.base)
        try:
            running = self.compose.running()
        except ApplyError:  # then no proxy can be checked
            running = set()
        for request, proxy in wanted:
            if proxy not in private:  # a revoke of a proxy that is no longer private: nothing there to check
                continue
            target = self.proxy_target(proxy, running)
            if target is None:
                if proxy not in unverified:
                    unverified.append(proxy)
                continue
            name = request["username"]
            new, old = accounts.accounts.get(name), old_passwords.get(name)
            allowed = accounts.enabled(name) and (accounts.is_admin(name) or proxy in accounts.grants.get(name, ()))
            if new is not None:
                (accept if allowed else refuse).append((f"{name} on {proxy}", target, name, new))
            if old is not None and old != new:
                label = name if new is None else f"{name} (previous password)"
                refuse.append((f"{label} on {proxy}", target, name, old))
            if proxy == request.get("proxy"):
                accept.append((f"{pa.MONITOR} on {proxy}", target, pa.MONITOR, accounts.accounts[pa.MONITOR]))
                refuse.append((f"a wrong password on {proxy}", target, pa.MONITOR, "wrong-" + pa.new_password()))
                other = next((n for n in sorted(accounts.accounts) if n not in pa.PROTECTED and n != name
                              and accounts.enabled(n) and not accounts.is_admin(n)
                              and proxy not in accounts.grants.get(n, ())), None)
                if other is not None:
                    refuse.append((f"{other} (not granted) on {proxy}", target, other, accounts.accounts[other]))
        return accept, refuse, unverified

    @staticmethod
    def target_checks(requests: List[Dict[str, str]], old_passwords: Dict[str, str],
                      accounts: pa.Accounts) -> Tuple[List[Login], List[Login]]:
        """
        (accept, refuse) for each request's own account on both entries, from the state it asks for rather than
        from what the batch changed, so a request that found its account already in that state is still checked:
        ha-general accepts an enabled account and ha-private an enabled admin, each refuses the account otherwise,
        and both refuse a removed account's or replaced password.
        """
        accept, refuse = [], []  # type: List[Login]
        for request in requests:
            name = request["username"]
            new, old = accounts.accounts.get(name), old_passwords.get(name)
            for entry, allowed in ((pa.GENERAL, accounts.enabled(name)), (pa.HA_PRIVATE, accounts.is_admin(name))):
                if new is not None:
                    (accept if allowed else refuse).append((f"{name} on {entry}", entry, name, new))
                if old is not None and old != new:
                    label = name if new is None else f"{name} (previous password)"
                    refuse.append((f"{label} on {entry}", entry, name, old))
        return accept, refuse

    @staticmethod
    def expectations(old_passwords: Dict[str, str], old_enabled: Set[str], old_admins: Set[str],
                     accounts: pa.Accounts) -> Tuple[List[Login], List[Login]]:
        """
        (accept, refuse): logins through the HA entries that must get 204 or 407. On ha-general and on
        ha-private, as on each the accounts it accepts: svc-monitor passes; a new, enabled or rotated login
        passes; a disabled or removed one, or a replaced password, is refused. ha-private also refuses every
        non-admin this batch changed, and one non-admin it did not, so each apply shows that ha-private keeps
        non-admins out.
        """
        pw = accounts.accounts
        accept = [(f"{pa.MONITOR} on {entry}", entry, pa.MONITOR, pw[pa.MONITOR]) for entry in ENTRIES]
        refuse = []  # type: List[Login]
        non_admin_refused = False
        for name in sorted(set(old_passwords) | set(pw)):
            if name in pa.PROTECTED:
                continue
            old, new = old_passwords.get(name), pw.get(name)
            rotated = old is not None and new is not None and old != new
            states = {pa.GENERAL: (name in old_enabled, accounts.enabled(name)),
                      pa.HA_PRIVATE: (name in old_enabled and name in old_admins, accounts.is_admin(name))}
            for entry, (was, now) in states.items():
                if was and (not now or rotated):
                    label = name if not now else f"{name} (previous password)"
                    refuse.append((f"{label} on {entry}", entry, name, old))
                if now and (not was or rotated):
                    accept.append((f"{name} on {entry}", entry, name, new))
            changed = old is None or rotated or (name in old_enabled) != accounts.enabled(name) \
                or (name in old_admins) != (name in accounts.admins)
            if new is not None and accounts.enabled(name) and not accounts.is_admin(name):
                if changed and (pa.HA_PRIVATE, name, new) not in [(e, u, p) for _, e, u, p in refuse]:
                    refuse.append((f"{name} (not an admin) on {pa.HA_PRIVATE}", pa.HA_PRIVATE, name, new))
                non_admin_refused |= changed
        if not non_admin_refused:
            other = next((name for name in sorted(pw) if name not in pa.PROTECTED and accounts.enabled(name)
                          and not accounts.is_admin(name)), None)
            if other is not None:
                refuse.append((f"{other} (not an admin) on {pa.HA_PRIVATE}", pa.HA_PRIVATE, other, pw[other]))
        return accept, refuse

    def verify(self, accept: List[Login], refuse: List[Login]) -> Tuple[str, str]:
        """
        ("ok"|"failed"|"unconfirmed", detail). A login that must be refused gets 407 from a running entry
        whatever the proxies do, and a wrong password always must (the canary, on each entry): no 407 by the
        deadline is a failure. A login that must pass needs a working proxy for its 204: no answer by then
        is unconfirmed.
        """
        deadline = self.clock() + self.verify_timeout
        accept = list(accept)
        refuse = list(refuse) + [(f"a wrong password on {entry}", entry, pa.MONITOR, "wrong-" + pa.new_password())
                                 for entry in ENTRIES]
        last = {}
        while True:
            for item in list(refuse):
                kind, code = self.probe(item[1], item[2], item[3])
                if kind == "status" and code == 407:
                    refuse.remove(item)
                elif kind == "status":
                    return "failed", f"{item[0]}: expected 407, got {code}"
                else:
                    last[item[0]] = kind
            for item in list(accept):
                kind, code = self.probe(item[1], item[2], item[3])
                if kind == "status" and code == 204:
                    accept.remove(item)
                elif kind == "status" and code == 407:
                    return "failed", f"{item[0]}: refused with 407"
                else:
                    last[item[0]] = str(code) if kind == "status" else kind
            if not accept and not refuse:
                return "ok", ""
            if self.clock() >= deadline:
                if refuse:
                    services = sorted({HA_SERVICE if target in ENTRIES else target for _, target, _, _ in refuse})
                    return "failed", f"no 407 from {', '.join(services)} in {self.verify_timeout:.0f} s for " + \
                        ", ".join(f"{label} ({last.get(label, '-')})" for label, _, _, _ in refuse)
                return "unconfirmed", "no 204 for " + ", ".join(f"{p} ({last.get(p, '-')})" for p, _, _, _ in accept)
            self.sleep(self.verify_interval)

    # -- backup, rollback, recovery

    def backup(self, outputs, accounts_text, restart, ids) -> List[Tuple[Path, Optional[bytes], int]]:
        """Keep the files about to be replaced, in memory and in BACKUP_DIR with a manifest (for recovery)."""
        snapshot = []
        for path in [p for p, _ in outputs] + [self.base / pa.ACCOUNTS_FILE]:
            try:
                st = os.lstat(str(path))
                snapshot.append((path, path.read_bytes(), stat.S_IMODE(st.st_mode)))
            except FileNotFoundError:
                snapshot.append((path, None, 0o600))
        folder = self.base / BACKUP_DIR
        shutil.rmtree(str(folder), ignore_errors=True)
        folder.mkdir(mode=0o700)
        files = []
        for i, (path, data, mode) in enumerate(snapshot):
            if data is not None:
                pa.write_private(folder / str(i), data.decode("utf-8"))
            files.append({"path": str(path.relative_to(self.base)), "backup": str(i) if data is not None else None,
                          "mode": mode})
        pa.write_private(folder / MANIFEST, json.dumps({"ids": ids, "services": restart, "files": files}) + "\n")
        return snapshot

    def restore_files(self, snapshot) -> None:
        for path, data, mode in snapshot:
            if data is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                pa.write_private(path, data.decode("utf-8"))
                os.chmod(str(path), mode)

    def rollback(self, snapshot, restart: List[str]) -> bool:
        """Restore the files and restart the same services; True when both HA entries answer svc-monitor again."""
        try:
            self.restore_files(snapshot)
        except OSError as e:
            log(f"rollback: files not restored ({type(e).__name__}); the backup is kept for the next run")
            self.keep_backup = True
            return False
        try:
            if restart:
                self.compose.restart(restart)
                accounts = pa.Accounts(self.base)
                monitor = accounts.accounts[pa.MONITOR]
                # the HA entries, and each restarted private proxy whose address a check of this run found
                targets = list(ENTRIES) + [service for service in restart if service in self.addresses]
                outcome, detail = self.verify([(f"{pa.MONITOR} on {target}", target, pa.MONITOR, monitor)
                                               for target in targets], [])
                log(f"rollback: {outcome} {detail}".rstrip())
                return outcome != "failed"
            return True
        except (ApplyError, OSError, pa.AccountsError, ValueError) as e:
            log(f"rollback: files restored, restart or check failed: {type(e).__name__}")
            return False

    def recover(self) -> None:
        """After an interrupted apply: restore the backup, restart its services, mark its requests failed."""
        folder = self.base / BACKUP_DIR
        manifest = folder / MANIFEST
        if not manifest.exists():
            shutil.rmtree(str(folder), ignore_errors=True)
            self.fail_stale_applying()
            return
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("committed"):
            self.finish_committed(data)
            shutil.rmtree(str(folder), ignore_errors=True)
            self.fail_stale_applying()
            return
        snapshot = []
        for entry in data["files"]:
            path = (self.base / entry["path"]).resolve()
            if self.base.resolve() not in path.parents:
                raise OSError(f"{manifest}: path outside the services folder")
            backup = (folder / entry["backup"]).read_bytes() if entry["backup"] is not None else None
            snapshot.append((path, backup, int(entry["mode"])))
        log("recovering an interrupted apply: restoring its files")
        restored = self.rollback(snapshot, [s for s in data["services"] if isinstance(s, str)])
        if not restored:
            self.forget_applied()
        if self.keep_backup:
            raise OSError("cannot restore the files of an interrupted apply")
        for request_id in data["ids"]:
            if isinstance(request_id, str) and ID_RE.fullmatch(request_id):
                self.spool.drop_result(request_id)
                self.spool.write_status(request_id, {"id": request_id, "status": "failed", "stage": "interrupted",
                                                     "error": "the apply was interrupted", "restored": restored})
        shutil.rmtree(str(folder), ignore_errors=True)
        self.fail_stale_applying()

    def finish_committed(self, data: dict) -> None:
        """
        The interrupted apply was verified and live: write what it had not written yet, restart nothing. Changes
        with proxy_accounts.py may have followed it, so applied.json is removed rather than written: the next
        batch restarts and verifies every service.
        """
        log("finishing an interrupted apply that was already live")
        self.forget_applied()
        results = {i for i in data.get("results", []) if isinstance(i, str)}
        for record in data["records"]:
            if not isinstance(record, dict) or not ID_RE.fullmatch(str(record.get("id", ""))):
                continue
            if record["id"] in results:  # a result not written by then is lost: result false
                record["result"] = (self.spool.results / f"{record['id']}.json").exists()
            self.spool.write_status(record["id"], record)

    def fail_stale_applying(self) -> None:
        """Requests left at applying without a backup never reached the file swap: nothing changed."""
        for path in self.spool.status.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if record.get("status") == "applying":
                record.update(status="failed", stage="interrupted", error="the apply was interrupted", restored=True)
                self.spool.write_status(path.stem, record)


def unverified_outcome(result: Tuple[str, str], unverified: List[str]) -> Tuple[str, str]:
    """An ok verification that left proxies unchecked is unconfirmed."""
    outcome, detail = result
    if outcome == "ok" and unverified:
        return "unconfirmed", f"not checked: {', '.join(unverified)} (not running, or no published HTTP port)"
    return outcome, detail


def merge_logins(*pairs):
    """Join (accept, refuse) lists, each login (user and password) once, the first label kept."""
    accept, refuse, seen = [], [], set()
    for which, target in ((0, accept), (1, refuse)):
        for pair in pairs:
            for login in pair[which]:
                if (which,) + tuple(login[1:]) not in seen:
                    seen.add((which,) + tuple(login[1:]))
                    target.append(login)
    return accept, refuse


def scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text.strip()[-500:]


# ---------------------------------------------------------------- command line

def requester_ids(value: str) -> Tuple[int, int]:
    if value.isdigit():
        return int(value), int(value)
    try:
        entry = pwd.getpwnam(value)
    except KeyError:
        raise ValueError(f"no user {value!r} (PROXY_APPLY_REQUESTER)")
    return entry.pw_uid, entry.pw_gid


def host_port(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Apply proxy account change requests from the spool.")
    parser.add_argument("--services-dir", type=Path, default=Path(env("PROXY_APPLY_SERVICES_DIR") or SERVICES_DIR))
    parser.add_argument("--spool", type=Path, default=Path(env("PROXY_APPLY_SPOOL") or DEFAULT_SPOOL))
    parser.add_argument("--requester", default=env("PROXY_APPLY_REQUESTER"),
                        help="user name or uid that writes the requests (the portal)")
    parser.add_argument("--compose-file", type=Path, default=env("PROXY_APPLY_COMPOSE_FILE"),
                        help="default: <services-dir>/docker-compose.yml")
    parser.add_argument("--compose-project", default=env("PROXY_APPLY_COMPOSE_PROJECT") or None)
    parser.add_argument("--ha-http", type=host_port, default=env("PROXY_APPLY_HA_HTTP") or "127.0.0.1:30889",
                        help="ha-general's HTTP port")
    parser.add_argument("--ha-private-http", type=host_port,
                        default=env("PROXY_APPLY_HA_PRIVATE_HTTP") or "127.0.0.1:31889", help="ha-private's HTTP port")
    parser.add_argument("--proxy-host", default=env("PROXY_APPLY_PROXY_HOST") or "127.0.0.1",
                        help="where the proxies' own entries are reached, at their published HTTP ports")
    parser.add_argument("--probe-url", default=env("PROXY_APPLY_PROBE_URL") or "https://www.google.com/generate_204")
    parser.add_argument("--verify-timeout", type=float, default=float(env("PROXY_APPLY_VERIFY_TIMEOUT") or 60))
    parser.add_argument("command", choices=("init", "run", "status"))
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(pa.status(pa.Accounts(args.services_dir)), indent=2))
        return 0
    if not args.requester:
        parser.error("--requester (PROXY_APPLY_REQUESTER) is required")
    try:
        uid, gid = requester_ids(args.requester)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    spool = Spool(args.spool, uid)
    if args.command == "init":
        spool.init(gid)
        return 0
    compose = Compose(args.compose_file or args.services_dir / "docker-compose.yml", args.compose_project)

    addresses = {pa.GENERAL: args.ha_http, pa.HA_PRIVATE: args.ha_private_http}  # the applier adds the proxies'

    def probe(target, user, password):
        address = addresses.get(target)
        if address is None:
            return "unreachable", None
        return proxy_probe(address, args.probe_url, user, password)

    try:
        return Applier(args.services_dir, spool, compose, probe, verify_timeout=args.verify_timeout,
                       addresses=addresses, proxy_host=args.proxy_host).run()
    except (OSError, pa.AccountsError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except Exception:  # exit 1 stays reserved for an unfinished rollback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
