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

A request is {"id": ..., "op": "add|rotate|enable|disable|remove", "username": ...} and nothing else.
Passwords are generated here, never taken from a request. svc-ha and svc-monitor are managed with
proxy_accounts.py only. Settings come from the environment (PROXY_APPLY_*) or the options below, never
from a request. Python 3.8+, standard library only.
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
OPS = ("add", "rotate", "enable", "disable", "remove")
REQUEST_KEYS = {"id", "op", "username"}
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

Run = Callable[[Sequence[str], Optional[str], float], Tuple[int, str, str]]
Probe = Callable[[str, str], Tuple[str, Optional[int]]]


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
    if not isinstance(data, dict) or set(data) != REQUEST_KEYS:
        raise RequestError(f"must be an object with exactly the keys {sorted(REQUEST_KEYS)}")
    if not all(isinstance(v, str) for v in data.values()):
        raise RequestError("every value must be a string")
    if not ID_RE.fullmatch(data["id"]):
        raise RequestError("invalid id")
    if data["op"] not in OPS:
        raise RequestError(f"op must be one of {', '.join(OPS)}")
    try:
        pa.validate_name(data["username"])
    except pa.AccountsError as e:
        raise RequestError(str(e))
    if data["username"] in pa.PROTECTED:
        raise RequestError(f"{data['username']} is managed with proxy_accounts.py only")
    return {"id": data["id"], "op": data["op"], "username": data["username"]}


def apply_op(accounts: pa.Accounts, request: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    """Apply one request to the accounts in memory: (changed, new password or None). Raises AccountsError."""
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

    def pinned_image(self, service: str) -> str:
        try:
            image = json.loads(self._ok("test", "config", "--format", "json"))["services"][service]["image"]
        except (ValueError, KeyError, TypeError):
            raise ApplyError("test", f"no image for {service} in the compose config")
        if "@sha256:" not in image:
            raise ApplyError("test", f"the image of {service} is not pinned by digest: {image}")
        return image

    def restart(self, services: Sequence[str]) -> None:
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
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.base = Path(base_dir)
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
        log("batch: " + ", ".join(f"{r['id']} {r['op']} {r['username']}" for r, _ in batch))

        try:
            accounts = pa.Accounts(self.base)
        except (pa.AccountsError, OSError, ValueError) as e:
            for record in records.values():
                record.update(status="failed", stage="accounts", error=str(e), restored=True)
            return self.finish(records, {}, [])
        old_passwords = dict(accounts.accounts)
        old_enabled = {name for name in accounts.accounts if accounts.enabled(name)}
        passwords = {}  # type: Dict[str, str]
        active, changed_any = [], False
        for request, uid in batch:
            try:
                changed, password = apply_op(accounts, request)
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
                outputs = pa.render_outputs(accounts)
            except (pa.AccountsError, OSError, ValueError, KeyError) as e:
                raise ApplyError("render", str(e))
            if not changed_any and not outputs:
                for request, _ in active:
                    records[request["id"]].update(status="applied", restarted=[])
                return self.finish(records, {}, active)
            image = self.compose.pinned_image(HA_SERVICE)
            for path, text in outputs:
                if path.name == "config.json":
                    ok, output = self.compose.xray_test(image, text)
                    if not ok:
                        last = ([line for line in output.splitlines() if line.strip()] or ["no output"])[-1]
                        raise ApplyError("test", f"{path.relative_to(self.base)}: {scrub(last, secrets_seen)}")
            restart = self.restart_set(accounts, outputs)
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
            log(f"files replaced; restarting {' '.join(restart)}")
            self.compose.restart(restart)
            accept, refuse = self.expectations(old_passwords, old_enabled, accounts)
            outcome, detail = self.verify(accept, refuse)
            if outcome == "failed":
                raise ApplyError("verify", detail)
            status = "applied" if outcome == "ok" else "unconfirmed"
            for request, _ in active:
                records[request["id"]].update(status=status, restarted=restart)
                if status == "unconfirmed":
                    records[request["id"]].update(stage="verify", error=detail)
            try:
                self.commit(records, list(passwords), accounts_text)
            except OSError as e:
                raise ApplyError("commit", f"{type(e).__name__}: {e.strerror or e}")
        except ApplyError as e:
            error = scrub(str(e), secrets_seen)
            log(f"failed at {e.stage}: {error}")
            restored = True
            if snapshot is not None:
                restored = self.rollback(snapshot, restart)
            for request, _ in active:
                records[request["id"]].pop("restarted", None)
                records[request["id"]].update(status="failed", stage=e.stage, error=error, restored=restored)
            return self.finish(records, {}, active)

        self.write_applied(pa.sha256_text(accounts_text), now_iso())
        log(f"{status}{': ' + detail if detail else ''}")
        return self.finish(records, passwords, active)

    def commit(self, records: Dict[str, dict], result_ids: List[str], accounts_text: str) -> None:
        """Mark the backup as committed: from here on, an interrupted apply is finished, not rolled back."""
        manifest = self.base / BACKUP_DIR / MANIFEST
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.update(committed=True, records=list(records.values()), results=result_ids,
                    accounts_sha256=pa.sha256_text(accounts_text), applied_at=now_iso())
        pa.write_private(manifest, json.dumps(data) + "\n")

    def write_applied(self, accounts_sha256: str, applied_at: str) -> None:
        pa.write_private(self.base / pa.APPLIED_FILE, json.dumps(
            {"accounts_sha256": accounts_sha256, "applied_at": applied_at}, indent=2) + "\n")

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

    def restart_set(self, accounts: pa.Accounts, outputs: List[Tuple[Path, str]]) -> List[str]:
        """xray-ha; the lines, with raw auth or when their config changed; blackbox-exporter when its config did."""
        running = self.compose.running()
        if HA_SERVICE not in running:
            raise ApplyError("precheck", f"{HA_SERVICE} is not running")
        known = self.compose.services()
        changed = {path for path, _ in outputs}
        restart = [HA_SERVICE]
        for path in sorted(self.base.glob(pa.RAW_CONFIG_GLOB)):
            service = "xray-" + path.parent.parent.name
            if (accounts.raw_auth or path in changed) and service in known and service in running:
                restart.append(service)
        if self.base / pa.BLACKBOX_CONFIG in changed and BLACKBOX_SERVICE in running:
            restart.append(BLACKBOX_SERVICE)
        return restart

    @staticmethod
    def expectations(old_passwords: Dict[str, str], old_enabled: Set[str], accounts: pa.Accounts):
        """(accept, refuse): (label, user, password) logins through the HA entry that must get 204 or 407."""
        def on_ha(name):
            return name != pa.RELAY
        accept = [(pa.MONITOR, pa.MONITOR, accounts.accounts[pa.MONITOR])]
        refuse = []
        for name in sorted(set(old_passwords) | set(accounts.accounts)):
            if not on_ha(name) or name == pa.MONITOR:
                continue
            was = name in old_enabled
            now = accounts.enabled(name)
            if was and (not now or accounts.accounts[name] != old_passwords[name]):
                label = name if not now else f"{name} (previous password)"
                refuse.append((label, name, old_passwords[name]))
            if now and (not was or accounts.accounts[name] != old_passwords[name]):
                accept.append((name, name, accounts.accounts[name]))
        return accept, refuse

    def verify(self, accept, refuse) -> Tuple[str, str]:
        """
        ("ok"|"failed"|"unconfirmed", detail). A login that must be refused gets 407 from a running entry
        whatever the lines do, and a wrong password always must (the canary): no 407 by the deadline is a
        failure. A login that must pass needs a working line for its 204: no answer by then is unconfirmed.
        """
        deadline = self.clock() + self.verify_timeout
        accept = list(accept)
        refuse = list(refuse) + [("a wrong password", pa.MONITOR, "wrong-" + pa.new_password())]
        last = {}
        while True:
            for item in list(refuse):
                kind, code = self.probe(item[1], item[2])
                if kind == "status" and code == 407:
                    refuse.remove(item)
                elif kind == "status":
                    return "failed", f"{item[0]}: expected 407, got {code}"
                else:
                    last[item[0]] = kind
            for item in list(accept):
                kind, code = self.probe(item[1], item[2])
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
                    return "failed", f"no 407 from {HA_SERVICE} in {self.verify_timeout:.0f} s for " + \
                        ", ".join(f"{label} ({last.get(label, '-')})" for label, _, _ in refuse)
                return "unconfirmed", "no 204 for " + ", ".join(f"{p} ({last.get(p, '-')})" for p, _, _ in accept)
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
        """Restore the files and restart the same services; True when the HA entry answers svc-monitor again."""
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
                outcome, detail = self.verify([(pa.MONITOR, pa.MONITOR, accounts.accounts[pa.MONITOR])], [])
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
        """The interrupted apply was verified and live: write what it had not written yet, restart nothing."""
        log("finishing an interrupted apply that was already live")
        self.write_applied(str(data["accounts_sha256"]), str(data["applied_at"]))
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
    parser.add_argument("--ha-http", type=host_port, default=env("PROXY_APPLY_HA_HTTP") or "127.0.0.1:30889")
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

    def probe(user, password):
        return proxy_probe(args.ha_http, args.probe_url, user, password)

    try:
        return Applier(args.services_dir, spool, compose, probe, verify_timeout=args.verify_timeout).run()
    except (OSError, pa.AccountsError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except Exception:  # exit 1 stays reserved for an unfinished rollback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
