"""Tests for scripts/create_user.py (stdlib unittest only; no network, no SSH).

fabric and my_secrets are replaced by stubs, SSH hosts by FakeHost objects that
simulate the shell commands the script sends, and requests.request by FakeAPI
(TrueNAS + Harbor). The fstab append script and the quoting checks run through
a real `sh`/`bash`.

Run from the repository root (Python 3.8+):
    python3 -B -m unittest discover -s scripts/tests -v
"""

import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import quote

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


# --- stubs installed before importing create_user ---------------------------

class FakeConfig:
    def __init__(self, overrides=None, **kwargs):
        self.overrides = overrides


class UnpatchedConnection:
    instances = 0

    def __init__(self, *args, **kwargs):
        UnpatchedConnection.instances += 1
        raise AssertionError("a test opened a Connection without patching it")


fabric_stub = types.ModuleType("fabric")
fabric_stub.Connection = UnpatchedConnection
fabric_stub.Config = FakeConfig
sys.modules["fabric"] = fabric_stub

secrets_stub = types.ModuleType("my_secrets")
for _name in ("TRUENAS_USERNAME", "TRUENAS_PASSWORD", "SUDO_PASSWORD", "DET_PASSWORD", "HARBOR_PASSWORD"):
    setattr(secrets_stub, _name, "placeholder-" + _name.lower())
sys.modules["my_secrets"] = secrets_stub

try:
    import requests  # noqa: F401  (real requests, if installed, is never allowed to send anything)
except ImportError:
    requests_stub = types.ModuleType("requests")

    class RequestException(IOError):
        pass

    class HTTPError(RequestException):
        pass

    def _unpatched_request(*args, **kwargs):
        raise AssertionError("a test sent an HTTP request without patching requests.request")

    requests_stub.RequestException = RequestException
    requests_stub.HTTPError = HTTPError
    requests_stub.ConnectionError = type("ConnectionError", (RequestException,), {})
    requests_stub.request = _unpatched_request
    sys.modules["requests"] = requests_stub

sys.path.insert(0, str(SCRIPTS_DIR))
import create_user as cu  # noqa: E402

CONNECTIONS_AT_IMPORT = UnpatchedConnection.instances

NASTY_PASSWORDS = [
    "Pa$$w0rd12",
    "Abc$1234x",
    "Back\\slash9Z",
    "Abc1`id -un`x",
    "$(touch pwned)Aa1",
    "Abc1'23xY",
    'Abc1"23xY',
    "Abc1;rm2xY",
    "Abc12&x9Y",
    "Abc12>outxY",
    "Abc1(2)xY",
    "Mix$`'\"\\;&>()Zz9 end",
]
NASTY_FULL_NAME = "O'Brien \"Bob\" $(id) `id` ; & > ( ) \\ Ünïcode"


# --- fake SSH hosts ---------------------------------------------------------

class FakeResult:
    def __init__(self, stdout="", stderr="", exited=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exited = exited

    @property
    def ok(self):
        return self.exited == 0


class UnexpectedExit(Exception):
    """Like invoke's: str() contains the command line."""

    def __init__(self, result, command):
        self.result = result
        super().__init__(f"Encountered a bad command exit code!\n\nCommand: {command!r}")


class Call:
    def __init__(self, kind, command, user, stdin):
        self.kind, self.command, self.user, self.stdin = kind, command, user, stdin
        self.argv = shlex.split(command)

    def __repr__(self):
        return f"Call({self.kind}, {self.command!r}, user={self.user})"


FAKE_HASH_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def fake_sha512_hash(password):
    # deterministic, format-correct stand-in for `openssl passwd -6`
    digest = "".join(FAKE_HASH_CHARS[(ord(c) * 7 + i) % 64] for i, c in enumerate((password * 90)[:86]))
    return "$6$saltsaltsaltsalt$" + digest


class FakeHost:
    """Simulates the commands create_user.py runs on a GPU node."""

    def __init__(self, name, fstab="# /etc/fstab\nUUID=abcd / ext4 defaults 0 1\n"):
        self.name = name
        self.fstab = fstab
        self.mounted = set()
        self.dirs = set()
        self.calls = []
        self.fail_connect = False
        self.fail_mount = False
        self.closed = False

    # fabric.Connection API used by the script
    def run(self, command, **kwargs):
        return self._exec("run", command, kwargs)

    def sudo(self, command, **kwargs):
        return self._exec("sudo", command, kwargs)

    def close(self):
        self.closed = True

    def _exec(self, kind, command, kwargs):
        if self.fail_connect:
            raise OSError(f"[Errno 113] No route to host: {self.name}")
        stream = kwargs.get("in_stream")
        call = Call(kind, command, kwargs.get("user"), stream.getvalue() if stream is not None else None)
        self.calls.append(call)
        result = self.handle(call)
        if result is None:
            raise AssertionError(f"{self.name}: unexpected command {call}")
        if not result.ok and not kwargs.get("warn"):
            raise UnexpectedExit(result, command)
        return result

    def need_root(self, call):
        if call.kind != "sudo" or call.user is not None:
            return FakeResult(stderr="Permission denied", exited=1)
        return None

    def handle(self, call):
        argv = call.argv
        if argv[:2] == ["mkdir", "-p"]:
            denied = self.need_root(call)
            if denied:
                return denied
            self.dirs.add(argv[2])
            return FakeResult()
        if argv == ["cat", "/etc/fstab"]:
            return FakeResult(stdout=self.fstab)
        if argv[:2] == ["sh", "-c"]:
            denied = self.need_root(call)
            if denied:
                return denied
            return self.run_real_sh(argv)
        if argv[:2] == ["mountpoint", "-q"]:
            return FakeResult(exited=0 if argv[2] in self.mounted else 32)
        if argv[0] == "mount":
            denied = self.need_root(call)
            if denied:
                return denied
            assert argv[1:] and argv[1] != "-a", argv
            target = argv[1]
            if self.fail_mount:
                return FakeResult(stderr=f"mount.nfs: Connection timed out for {target}", exited=32)
            if not cu.fstab_entries_for(self.fstab, target):
                return FakeResult(stderr=f"mount: {target}: can't find in /etc/fstab.", exited=1)
            if target not in self.dirs:
                return FakeResult(stderr=f"mount: {target}: mount point does not exist.", exited=32)
            self.mounted.add(target)
            return FakeResult()
        return None

    def run_real_sh(self, argv):
        """Run the script's own `sh -c SCRIPT sh LINE /etc/fstab` against a temp copy."""
        assert argv[3] == "sh" and argv[5] == "/etc/fstab" and len(argv) == 6, argv
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fstab")
            with open(path, "w") as f:
                f.write(self.fstab)
            proc = subprocess.run(["sh", "-c", argv[2], "sh", argv[4], path],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            with open(path) as f:
                self.fstab = f.read()
        return FakeResult(proc.stdout, proc.stderr, proc.returncode)


class LoginHost(FakeHost):
    """Adds Linux accounts, /home contents and the det CLI."""

    def __init__(self, name=cu.LOGIN_HOST, **kwargs):
        super().__init__(name, **kwargs)
        self.users = {}       # name -> {"uid", "gid", "groups", "pw", "hash"}
        self.next_id = 1100
        self.nfs_home = {}    # user -> set of files in the NFS home
        self.det_users = {}   # name -> {"password", "agent", "display"}
        self.det_logged_in = False
        self.det_create_fails = False

    def add_user(self, name, uid, pw="P", groups=("docker",)):
        self.users[name] = {"uid": uid, "gid": uid, "groups": set(groups), "pw": pw, "hash": None}

    def handle(self, call):
        argv = call.argv
        users = self.users
        if argv[0] == "id":
            flag, name = argv[1], argv[2]
            if name not in users:
                return FakeResult(stderr=f"id: '{name}': no such user", exited=1)
            u = users[name]
            if flag == "-u":
                return FakeResult(stdout=f"{u['uid']}\n")
            if flag == "-g":
                return FakeResult(stdout=f"{u['gid']}\n")
            if flag == "-nG":
                return FakeResult(stdout=" ".join([name] + sorted(u["groups"])) + "\n")
        if argv[:4] == ["useradd", "-m", "-s", "/bin/bash"]:
            denied = self.need_root(call)
            if denied:
                return denied
            if argv[4] in users:
                return FakeResult(stderr=f"useradd: user '{argv[4]}' already exists", exited=9)
            self.add_user(argv[4], self.next_id, pw="L", groups=())
            self.next_id += 1
            return FakeResult()
        if argv[:2] == ["passwd", "-S"]:
            denied = self.need_root(call)
            if denied:
                return denied
            return FakeResult(stdout=f"{argv[2]} {users[argv[2]]['pw']} 09/28/2026 0 99999 7 -1\n")
        if argv == ["openssl", "passwd", "-6", "-stdin"]:
            assert call.kind == "run", "openssl must not run under sudo"
            assert call.stdin is not None and call.stdin.endswith("\n") and call.stdin.count("\n") == 1
            return FakeResult(stdout=fake_sha512_hash(call.stdin[:-1]) + "\n")
        if argv[:2] == ["usermod", "-p"]:
            denied = self.need_root(call)
            if denied:
                return denied
            assert cu.SHA512_CRYPT_RE.fullmatch(argv[2]), argv[2]
            users[argv[3]].update(pw="P", hash=argv[2])
            return FakeResult()
        if argv[:3] == ["usermod", "-aG", "docker"]:
            denied = self.need_root(call)
            if denied:
                return denied
            users[argv[3]]["groups"].add("docker")
            return FakeResult()
        if argv[:2] == ["test", "-e"]:
            assert call.kind == "sudo" and call.user is not None
            home, _, rel = argv[2][len("/home/"):].partition("/")
            if f"/home/{home}" not in self.mounted:
                return FakeResult()  # local home made by useradd -m has the skel files
            return FakeResult(exited=0 if rel in self.nfs_home.get(home, set()) else 1)
        if argv == ["xdg-user-dirs-update", "--force"]:
            assert call.kind == "sudo" and call.user is not None
            self.nfs_home.setdefault(call.user, set()).update({"Desktop", "Documents"})
            return FakeResult()
        if argv[:3] == ["cp", "-a", "/etc/skel/."]:
            assert call.kind == "sudo" and call.user is not None
            assert argv[3] == f"/home/{call.user}/", argv
            self.nfs_home.setdefault(call.user, set()).update({".bashrc", ".profile", ".bash_logout"})
            return FakeResult()
        if argv[0] == "det":
            assert call.kind == "run", "det must not run under sudo"
            return self.handle_det(call)
        return super().handle(call)

    def handle_det(self, call):
        argv = call.argv
        if argv == ["det", "user", "login", "admin"]:
            assert call.stdin == cu.DET_PASSWORD + "\n"
            self.det_logged_in = True
            return FakeResult()
        if not self.det_logged_in:
            return FakeResult(stderr="not logged in", exited=1)
        if argv == ["det", "user", "list"]:
            rows = [" User Id | Username   | Display Name   | Admin   | Active",
                    "---------+------------+----------------+---------+---------",
                    "       1 | admin      | N/A            | True    | True"]
            for i, (name, info) in enumerate(sorted(self.det_users.items()), start=2):
                rows.append(f"{i:>8} | {name:<10} | {info['display'] or 'N/A':<14} | False   | True")
            return FakeResult(stdout="\n".join(rows) + "\n")
        if argv[:3] == ["det", "user", "create"]:
            assert argv[4] == "--password" and len(argv) == 6, argv
            if self.det_create_fails or argv[3] in self.det_users:
                return FakeResult(stderr="Error: user already exists or the master rejected it", exited=1)
            self.det_users[argv[3]] = {"password": argv[5], "agent": None, "display": None}
            return FakeResult()
        if argv[:3] == ["det", "user", "link-with-agent-user"]:
            name = argv[3]
            opts = dict(zip(argv[4::2], argv[5::2]))
            self.det_users[name]["agent"] = (int(opts["--agent-uid"]), opts["--agent-user"],
                                             int(opts["--agent-gid"]), opts["--agent-group"])
            return FakeResult()
        if argv[:3] == ["det", "user", "edit"]:
            assert argv[4] == "--display-name" and len(argv) == 6, argv
            self.det_users[argv[3]]["display"] = argv[5]
            return FakeResult()
        return None


# --- fake HTTP APIs -----------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        if raw is None:
            raw = "" if body is None else json.dumps(body)
        self.text = raw
        self.content = raw.encode()

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cu.requests.HTTPError(f"{self.status_code} Client Error")


class FakeAPI:
    """TrueNAS and Harbor, just enough for create_user.py."""

    def __init__(self):
        self.groups = {}        # name -> {"id", "gid", "name"}
        self.tn_users = {}      # name -> {"id", "uid", "group"}
        self.datasets = set()
        self.owners = {}        # path -> (uid, gid)
        self.nfs_shares = set()
        self.jobs = {}          # id -> {"states": [...], "error", "on_success"}
        self.job_states = ["RUNNING", "SUCCESS"]
        self.job_error = None
        self.ignore_filters = False  # a server that returns everything for a query
        self.harbor_users = set()
        self.harbor_members = set()
        self.calls = []
        self.next_pk = 100

    def add_complete_user(self, name, uid, gid):
        self.groups[name] = {"id": 1, "gid": gid, "name": name}
        self.tn_users[name] = {"id": 2, "uid": uid, "group": 1, "username": name}
        ds = cu.home_dataset(name)
        self.datasets.add(ds)
        self.owners["/mnt/" + ds] = (uid, gid)
        self.nfs_shares.add("/mnt/" + ds)
        self.harbor_users.add(name)
        self.harbor_members.add(name)

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] != "/filesystem/stat/"]

    def request(self, method, url, auth=None, params=None, json=None, timeout=None, **kwargs):
        assert timeout is not None, "every request needs a timeout"
        assert not kwargs, kwargs
        if url.startswith(cu.TRUENAS_API_URL):
            path = url[len(cu.TRUENAS_API_URL):]
            assert auth == (cu.TRUENAS_USERNAME, cu.TRUENAS_PASSWORD)
            handler = self.truenas
        elif url.startswith(cu.HARBOR_API_URL):
            path = url[len(cu.HARBOR_API_URL):]
            assert auth == ("admin", cu.HARBOR_PASSWORD)
            handler = self.harbor
        else:
            raise AssertionError(f"unexpected URL {url}")
        self.calls.append((method, path, params, json))
        response = handler(method, path, params or {}, json)
        if response is None:
            raise AssertionError(f"unexpected request {method} {url} {params} {json}")
        return response

    def _pk(self):
        self.next_pk += 1
        return self.next_pk

    def truenas(self, method, path, params, body):
        if self.ignore_filters and method == "GET" and path != "/core/get_jobs":
            everything = {"/group/": list(self.groups.values()), "/user/": list(self.tn_users.values()),
                          "/pool/dataset/": [{"id": d} for d in sorted(self.datasets)],
                          "/sharing/nfs": [{"path": p} for p in sorted(self.nfs_shares)]}
            return FakeResponse(body=everything[path])
        if (method, path) == ("GET", "/group/"):
            g = self.groups.get(params["name"])
            return FakeResponse(body=[g] if g else [])
        if (method, path) == ("POST", "/group/"):
            if body["name"] in self.groups:
                return FakeResponse(422, {"group_create.name": [{"message": "exists"}]})
            assert isinstance(body["gid"], int)
            pk = self._pk()
            self.groups[body["name"]] = {"id": pk, "gid": body["gid"], "name": body["name"]}
            return FakeResponse(body=pk)
        if (method, path) == ("GET", "/user/"):
            u = self.tn_users.get(params["username"])
            return FakeResponse(body=[u] if u else [])
        if (method, path) == ("POST", "/user/"):
            assert isinstance(body["uid"], int)
            assert body["group"] in [g["id"] for g in self.groups.values()]
            assert body["password_disabled"] is True and body["group_create"] is False
            pk = self._pk()
            self.tn_users[body["username"]] = {"id": pk, "uid": body["uid"], "group": body["group"],
                                               "username": body["username"]}
            return FakeResponse(body=pk)
        if (method, path) == ("GET", "/pool/dataset/"):
            return FakeResponse(body=[{"id": params["id"]}] if params["id"] in self.datasets else [])
        if (method, path) == ("POST", "/pool/dataset/"):
            assert body["quota"] == 8 * 1024**4
            self.datasets.add(body["name"])
            self.owners["/mnt/" + body["name"]] = (0, 0)
            return FakeResponse(body={"id": body["name"]})
        if (method, path) == ("POST", "/filesystem/stat/"):
            if body not in self.owners:
                return FakeResponse(422, {"message": f"{body}: not found"})
            uid, gid = self.owners[body]
            return FakeResponse(body={"uid": uid, "gid": gid, "acl": True})
        m = re.fullmatch(r"/pool/dataset/id/([^/]+)/permission", path)
        if method == "POST" and m:
            dataset = m.group(1)
            assert "/" not in dataset and "%2F" in dataset, dataset
            name = body["user"]
            # Pins the payload: no "acl" list (TrueNAS ignores it with set_default_acl) and no "mode".
            assert body == {"user": name, "group": name,
                            "options": {"set_default_acl": True, "stripacl": False,
                                        "recursive": True, "traverse": True}}, body
            job_id = self._pk()
            owner = (self.tn_users[name]["uid"], self.groups[name]["gid"])
            path_ = "/mnt/" + dataset.replace("%2F", "/")
            self.jobs[job_id] = {"states": list(self.job_states), "error": self.job_error,
                                 "on_success": lambda: self.owners.__setitem__(path_, owner)}
            return FakeResponse(body=job_id)
        if (method, path) == ("GET", "/core/get_jobs"):
            job = self.jobs.get(params["id"])
            if job is None:
                return FakeResponse(body=[])
            state = job["states"].pop(0) if len(job["states"]) > 1 else job["states"][0]
            if state == "SUCCESS" and job["on_success"]:
                job["on_success"]()
                job["on_success"] = None
            return FakeResponse(body=[{"id": params["id"], "state": state,
                                       "error": job["error"] if state == "FAILED" else None}])
        if (method, path) == ("GET", "/sharing/nfs"):
            return FakeResponse(body=[{"path": params["path"]}] if params["path"] in self.nfs_shares else [])
        if (method, path) == ("POST", "/sharing/nfs"):
            assert body["networks"] == ["192.168.233.0/24", "10.0.1.64/27"] and body["enabled"] is True
            self.nfs_shares.add(body["path"])
            return FakeResponse(body={"id": self._pk()})
        return None

    def harbor(self, method, path, params, body):
        if (method, path) == ("GET", "/users"):
            m = re.fullmatch(r"username=(.*)", params["q"])
            # Harbor may return near matches; the script must match exactly
            found = [{"username": u} for u in sorted(self.harbor_users) if u.startswith(m.group(1))]
            return FakeResponse(body=found)
        if (method, path) == ("POST", "/users"):
            if body["username"] in self.harbor_users:
                return FakeResponse(409, {"errors": [{"code": "CONFLICT"}]})
            self.harbor_users.add(body["username"])
            return FakeResponse(201)
        if path == f"/projects/{cu.HARBOR_PROJECT_ID}/members":
            if method == "GET":
                found = [{"entity_type": "u", "entity_name": u, "role_id": 2}
                         for u in sorted(self.harbor_members) if params["entityname"] in u]
                return FakeResponse(body=found)
            if method == "POST":
                assert body == {"role_id": 2, "member_user": {"username": body["member_user"]["username"]}}
                self.harbor_members.add(body["member_user"]["username"])
                return FakeResponse(201)
        return None


# --- cluster wiring -------------------------------------------------------------

class Cluster:
    def __init__(self):
        self.login = LoginHost()
        self.gpus = {h: FakeHost(h) for h in cu.GPU_HOSTS}
        self.api = FakeAPI()
        self.log = []
        self.connected = []

    def hosts(self):
        return [self.login] + list(self.gpus.values())

    def connect(self, host):
        self.connected.append(host)
        if host == cu.LOGIN_HOST:
            return self.login
        return self.gpus[host]

    def fabric_connection(self, host, config=None, connect_kwargs=None):
        """Replacement for fabric.Connection, for tests that go through main()."""
        assert config.overrides == {"sudo": {"password": cu.SUDO_PASSWORD}}
        assert "passphrase" in connect_kwargs
        return self.connect(host)

    def patches(self):
        return [mock.patch.object(cu.requests, "request", side_effect=self.api.request),
                mock.patch.object(cu.time, "sleep")]

    def create(self, user, docker_group=True):
        requests_patch, sleep_patch = self.patches()
        with requests_patch, sleep_patch:
            return cu.create_user(user, self.login, docker_group=docker_group,
                                  connect=self.connect, log=self.log.append)

    def main(self, argv):
        out, err = StringIO(), StringIO()
        with mock.patch.object(cu, "Connection", side_effect=self.fabric_connection), \
                mock.patch.object(cu.requests, "request", side_effect=self.api.request), \
                mock.patch.object(cu.time, "sleep"), redirect_stdout(out), redirect_stderr(err):
            code = cu.main(argv)
        return code, out.getvalue(), err.getvalue()

    def all_calls(self):
        return [c for h in self.hosts() for c in h.calls]


CREATE_COMMANDS = ("useradd", "usermod", "openssl", "sh", "mount", "cp", "xdg-user-dirs-update")


def remote_creates(cluster):
    """Commands that change something (link/edit on det are idempotent 'set' operations)."""
    found = []
    for call in cluster.all_calls():
        if call.argv[0] in CREATE_COMMANDS or call.argv[:3] == ["det", "user", "create"]:
            found.append(call)
    return found


def alice(password="Correct-Horse9", full_name="Alice Example"):
    return cu.NewUser("alice", full_name, password)


def write_csv(tmpdir, text, name="users.csv"):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


# --- tests ------------------------------------------------------------------------

class ImportTest(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        self.assertEqual(CONNECTIONS_AT_IMPORT, 0)
        self.assertTrue(callable(cu.main))

    def test_nfs_options(self):
        for opt in ("rsize=1048576", "wsize=1048576", "hard", "nconnect=16"):
            self.assertIn(opt, cu.NFS_MOUNT_OPTIONS.split(","))
        self.assertNotIn("soft", cu.NFS_MOUNT_OPTIONS.split(","))
        self.assertNotIn("32769", Path(cu.__file__).read_text())

    def test_nfs_options_match_remount_script(self):
        script = (Path(cu.__file__).parent / "nfs-remount.sh").read_text()
        self.assertIn(f'OPTS="{cu.NFS_MOUNT_OPTIONS}"', script)


class ValidationTest(unittest.TestCase):
    def test_valid_user(self):
        self.assertEqual(cu.validate_users([alice()]), [])
        self.assertEqual(cu.validate_users([cu.NewUser("_svc-1_x", "X", "Aa345678")]), [])

    def test_usernames(self):
        for bad in ["", "Alice", "1alice", "-alice", "al ice", "alice\n", "al;ice", "a" * 33,
                    "alice$", "ali.ce", "ålice", "true", "null", "false"]:
            with self.subTest(username=bad):
                errors = cu.validate_users([cu.NewUser(bad, "X", "Correct-Horse9")])
                self.assertTrue(errors, bad)
        self.assertEqual(cu.validate_users([cu.NewUser("a" * 32, "X", "Correct-Horse9")]), [])

    def test_password_rules(self):
        cases = {
            "Short1a": "at least 8 characters",
            "alllower1": "uppercase letter",
            "ALLUPPER1": "lowercase letter",
            "NoDigitsHere": "number",
            "Line\nbreak1A": "control characters",
            "Tab\tbreak1A": "control characters",
            " Leading1A": "whitespace",
            "Trailing1A ": "whitespace",
            "Aa1" + "x" * 126: "at most 128",
        }
        for password, expected in cases.items():
            with self.subTest(password=password):
                errors = cu.validate_users([cu.NewUser("bob", "Bob", password)])
                self.assertTrue(any(expected in e for e in errors), errors)
        for password in NASTY_PASSWORDS:
            with self.subTest(password=password):
                self.assertEqual(cu.validate_users([cu.NewUser("bob", "Bob", password)]), [])

    def test_duplicates_and_full_name(self):
        errors = cu.validate_users([alice(), alice()])
        self.assertTrue(any("duplicate" in e for e in errors), errors)
        errors = cu.validate_users([alice(full_name="")])
        self.assertTrue(any("full name" in e for e in errors), errors)
        self.assertTrue(cu.validate_users([]))

    def test_errors_never_show_the_password(self):
        errors = cu.validate_users([cu.NewUser("bob", "Bob", "secretlower1")])
        self.assertTrue(errors)
        self.assertFalse(any("secretlower1" in e for e in errors))


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_parse_with_comments_bom_and_quoting(self):
        buf = StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(["username", "full_name", "password"])
        for i, password in enumerate(NASTY_PASSWORDS):
            writer.writerow([f"user{i}", f"User, {i} \"q\"", password])
        text = "﻿# comment line, with \"quotes\n\n" + buf.getvalue() + "  # indented comment\n\n"
        path = write_csv(self.tmp.name, text)
        users, errors = cu.load_users(path)
        self.assertEqual(errors, [])
        self.assertEqual([u.password for u in users], NASTY_PASSWORDS)
        self.assertEqual(users[1].full_name, 'User, 1 "q"')
        self.assertEqual(users[0].username, "user0")

    def test_columns_in_any_order_and_whitespace_trimmed(self):
        path = write_csv(self.tmp.name, "password, username ,full_name\nCorrect-Horse9, alice , Alice Example \n")
        users, errors = cu.load_users(path)
        self.assertEqual(errors, [])
        self.assertEqual(users, [cu.NewUser("alice", "Alice Example", "Correct-Horse9")])

    def test_password_with_accidental_space_is_rejected(self):
        path = write_csv(self.tmp.name, "username,full_name,password\nalice,Alice, Correct-Horse9\n")
        users, errors = cu.load_users(path)
        self.assertTrue(any("whitespace" in e for e in errors), errors)

    def test_bad_header(self):
        for header in ["username,password", "username,full_name,password,email", "user,full_name,password"]:
            with self.subTest(header=header):
                path = write_csv(self.tmp.name, header + "\nalice,Alice,Correct-Horse9\n")
                users, errors = cu.load_users(path)
                self.assertTrue(errors)
                self.assertIn("header", errors[0])

    def test_wrong_field_count_reports_original_line(self):
        text = "# c\nusername,full_name,password\n\nalice,Alice\n# c\nbob,Bob,Correct-Horse9,extra\n"
        path = write_csv(self.tmp.name, text)
        users, errors = cu.load_users(path)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("line 4", errors[0])
        self.assertIn("line 6", errors[1])

    def test_embedded_newline_in_password_is_rejected(self):
        path = write_csv(self.tmp.name, 'username,full_name,password\nalice,Alice,"Correct\nHorse9"\n')
        users, errors = cu.load_users(path)
        self.assertTrue(any("control characters" in e for e in errors), errors)

    def test_empty_and_missing_files(self):
        path = write_csv(self.tmp.name, "# only comments\n")
        self.assertTrue(cu.load_users(path)[1])
        path = write_csv(self.tmp.name, "username,full_name,password\n")
        self.assertTrue(any("no users" in e for e in cu.load_users(path)[1]))
        self.assertTrue(cu.load_users(os.path.join(self.tmp.name, "missing.csv"))[1])

    def test_shipped_example_parses_and_creates_nobody(self):
        users, errors = cu.read_users_csv(str(SCRIPTS_DIR / "new_users.example.csv"))
        self.assertEqual((users, errors), ([], []))
        # The commented-out sample rows after the header are themselves valid input.
        lines = (SCRIPTS_DIR / "new_users.example.csv").read_text().splitlines()
        header = lines.index("username,full_name,password")
        rows = [line[2:] for line in lines[header + 1:] if line.startswith("# ")]
        sample = "\n".join([lines[header]] + rows) + "\n"
        users, errors = cu.load_users(write_csv(self.tmp.name, sample))
        self.assertEqual(errors, [])
        self.assertEqual(len(users), 2)


class QuotingTest(unittest.TestCase):
    def run_user(self, password, full_name=NASTY_FULL_NAME):
        cluster = Cluster()
        failures = cluster.create(cu.NewUser("alice", full_name, password))
        self.assertEqual(failures, [])
        return cluster

    def test_password_reaches_every_system_intact(self):
        for password in NASTY_PASSWORDS:
            with self.subTest(password=password):
                cluster = self.run_user(password)
                login = cluster.login
                openssl = [c for c in login.calls if c.argv[0] == "openssl"]
                self.assertEqual(len(openssl), 1)
                self.assertEqual(openssl[0].stdin, password + "\n")
                self.assertEqual(login.users["alice"]["hash"], fake_sha512_hash(password))
                self.assertEqual(login.det_users["alice"]["password"], password)
                self.assertEqual(login.det_users["alice"]["display"], NASTY_FULL_NAME)
                harbor_post = [c for c in cluster.api.calls if c[:2] == ("POST", "/users")]
                self.assertEqual(harbor_post[0][3]["password"], password)
                # plaintext only in the det argv (documented residual), never under sudo
                for call in cluster.all_calls():
                    if call.argv[:3] != ["det", "user", "create"]:
                        self.assertNotIn(password, call.command, call)
                self.assertFalse(any(password in line for line in cluster.log))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_commands_survive_a_real_shell(self):
        """Replay the recorded commands through bash (as invoke sends them) with shims on PATH."""
        password = NASTY_PASSWORDS[-1]
        cluster = self.run_user(password)
        by_prefix = {}
        for call in cluster.login.calls:
            key = tuple(call.argv[:3]) if call.argv[0] == "det" else tuple(call.argv[:2])
            by_prefix[key] = call
        expected = {
            ("det", "user", "create"): ["det", "user", "create", "alice", "--password", password],
            ("det", "user", "edit"): ["det", "user", "edit", "alice", "--display-name", NASTY_FULL_NAME],
            ("usermod", "-p"): ["usermod", "-p", fake_sha512_hash(password), "alice"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp, "bin")
            bindir.mkdir()
            recorder = '#!/bin/sh\nprintf "%s\\0" "$(basename "$0")" "$@" > "$ARGV_OUT"\n'
            fake_sudo = ('#!/bin/sh\n[ "$1" = -S ] && [ "$2" = -p ] && [ "$3" = "[sudo] password: " ] || exit 99\n'
                         'shift 3\nexec "$@"\n')
            for tool, body in (("det", recorder), ("usermod", recorder), ("sudo", fake_sudo)):
                path = bindir / tool
                path.write_text(body)
                path.chmod(0o755)
            env = {"PATH": f"{bindir}:/usr/bin:/bin", "ARGV_OUT": str(Path(tmp, "argv"))}
            for key, argv in expected.items():
                with self.subTest(command=key):
                    call = by_prefix[key]
                    command = call.command
                    if call.kind == "sudo":
                        command = "sudo -S -p '[sudo] password: ' " + command  # invoke's format
                    proc = subprocess.run(["bash", "-c", command], env=env, cwd=tmp,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          universal_newlines=True)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    got = Path(tmp, "argv").read_bytes().decode().split("\0")[:-1]
                    self.assertEqual(got, argv)
            self.assertEqual(sorted(os.listdir(tmp)), ["argv", "bin"], "a command created a stray file")


class CreateUserTest(unittest.TestCase):
    def test_fresh_user_all_steps(self):
        cluster = Cluster()
        self.assertEqual(cluster.create(alice()), [])
        login, api = cluster.login, cluster.api
        u = login.users["alice"]
        self.assertEqual((u["uid"], u["pw"]), (1100, "P"))
        self.assertIn("docker", u["groups"])
        self.assertEqual(api.groups["alice"]["gid"], 1100)
        self.assertEqual(api.tn_users["alice"]["uid"], 1100)
        self.assertIn("Peter/Workspace/alice", api.datasets)
        self.assertEqual(api.owners["/mnt/Peter/Workspace/alice"], (1100, 1100))
        self.assertIn("/mnt/Peter/Workspace/alice", api.nfs_shares)
        line = "nas.cvgl.lab:/mnt/Peter/Workspace/alice /workspace/alice nfs " \
               "defaults,vers=3,noatime,hard,nconnect=16,rsize=1048576,wsize=1048576,_netdev 0 2"
        for host in cluster.hosts():
            self.assertEqual(host.fstab.splitlines().count(line), 1, host.name)
            self.assertIn("/workspace/alice", host.mounted)
            self.assertTrue(host.fstab.endswith("\n"))
        self.assertIn("/home/alice", login.mounted)
        self.assertIn(line.replace("/workspace/alice", "/home/alice"), login.fstab.splitlines())
        self.assertIn(".bashrc", login.nfs_home["alice"])
        self.assertEqual(login.det_users["alice"]["agent"], (1100, "alice", 1100, "alice"))
        self.assertEqual(login.det_users["alice"]["display"], "Alice Example")
        self.assertIn("alice", api.harbor_users)
        self.assertIn("alice", api.harbor_members)
        # the ACL job was polled until it finished
        self.assertTrue(any(c[:2] == ("GET", "/core/get_jobs") for c in api.calls))
        # GPU connections are closed, the login connection is left to the caller
        self.assertTrue(all(h.closed for h in cluster.gpus.values()))
        self.assertFalse(login.closed)

    def test_rerun_after_success_creates_nothing(self):
        cluster = Cluster()
        self.assertEqual(cluster.create(alice()), [])
        for host in cluster.hosts():
            host.calls.clear()
        cluster.api.calls.clear()
        fstabs = {h.name: h.fstab for h in cluster.hosts()}
        self.assertEqual(cluster.create(alice(password="Different-Pass1")), [])
        self.assertEqual(remote_creates(cluster), [])
        self.assertEqual(cluster.api.creates(), [])
        self.assertEqual({h.name: h.fstab for h in cluster.hosts()}, fstabs)

    def test_everything_exists_creates_nothing(self):
        cluster = Cluster()
        login = cluster.login
        login.add_user("alice", 1234)
        login.det_users["alice"] = {"password": "x", "agent": None, "display": None}
        login.nfs_home["alice"] = {".bashrc"}
        cluster.api.add_complete_user("alice", 1234, 1234)
        for host in cluster.hosts():
            targets = ["/workspace/alice"] + (["/home/alice"] if host is login else [])
            for target in targets:
                host.fstab += cu.fstab_line("nas.cvgl.lab:/mnt/Peter/Workspace/alice", target) + "\n"
                host.mounted.add(target)
                host.dirs.add(target)
        self.assertEqual(cluster.create(alice()), [])
        self.assertEqual(remote_creates(cluster), [])
        self.assertEqual(cluster.api.creates(), [])
        self.assertEqual(login.det_users["alice"]["agent"], (1234, "alice", 1234, "alice"))

    def test_rerun_finishes_a_half_created_user(self):
        cluster = Cluster()
        cluster.api.job_states = ["RUNNING", "FAILED"]
        cluster.api.job_error = "[EINVAL] acltype mismatch"
        failures = cluster.create(alice())
        self.assertEqual(len(failures), 1)
        self.assertIn("TrueNAS home ACL", failures[0])
        self.assertIn("acltype mismatch", failures[0])
        self.assertFalse(any(h.mounted for h in cluster.hosts()), "stopped before mounting")
        self.assertEqual(cluster.login.det_users, {})
        cluster.api.job_states = ["SUCCESS"]
        self.assertEqual(cluster.create(alice()), [])
        self.assertEqual(sum(1 for c in cluster.login.calls if c.argv[0] == "useradd"), 1)
        self.assertEqual(sum(1 for c in cluster.api.calls if c[:2] == ("POST", "/group/")), 1)
        self.assertIn("alice", cluster.api.harbor_members)

    def test_existing_password_is_not_reset(self):
        cluster = Cluster()
        cluster.login.add_user("alice", 1300, pw="P", groups=())
        self.assertEqual(cluster.create(alice()), [])
        self.assertFalse([c for c in cluster.login.calls if c.argv[0] in ("openssl", "useradd")])
        self.assertIn("docker", cluster.login.users["alice"]["groups"])

    def test_locked_existing_account_gets_password(self):
        cluster = Cluster()
        cluster.login.add_user("alice", 1300, pw="L", groups=())
        self.assertEqual(cluster.create(alice()), [])
        self.assertEqual(cluster.login.users["alice"]["pw"], "P")

    def test_no_docker_group(self):
        cluster = Cluster()
        self.assertEqual(cluster.create(alice(), docker_group=False), [])
        self.assertNotIn("docker", cluster.login.users["alice"]["groups"])
        self.assertFalse([c for c in cluster.login.calls if c.argv[:2] == ["id", "-nG"]])

    def test_uid_mismatch_on_truenas_stops_the_user(self):
        cluster = Cluster()
        cluster.api.groups["alice"] = {"id": 5, "gid": 999, "name": "alice"}
        failures = cluster.create(alice())
        self.assertEqual(len(failures), 1)
        self.assertIn("gid 999, expected 1100", failures[0])
        self.assertEqual(cluster.api.tn_users, {})
        self.assertFalse(any(h.mounted for h in cluster.hosts()))

    def test_failed_remote_command_does_not_leak_password(self):
        cluster = Cluster()
        cluster.login.det_create_fails = True
        password = "Leak-Check-Pw9"
        failures = cluster.create(alice(password=password))
        self.assertEqual(len(failures), 1)
        self.assertIn("Determined user", failures[0])
        self.assertIn("exited with 1", failures[0])
        self.assertNotIn(password, failures[0])
        self.assertFalse(any(password in line for line in cluster.log))
        self.assertIn("alice", cluster.api.harbor_members, "Harbor steps still run")

    def test_lookups_match_exactly_even_if_the_server_ignores_filters(self):
        cluster = Cluster()
        cluster.api.add_complete_user("bob", 1050, 1050)
        cluster.api.ignore_filters = True
        self.assertEqual(cluster.create(alice()), [])
        api = cluster.api
        self.assertEqual(api.groups["alice"]["gid"], 1100)
        self.assertEqual(api.tn_users["alice"]["uid"], 1100)
        self.assertIn("Peter/Workspace/alice", api.datasets)
        self.assertIn("/mnt/Peter/Workspace/alice", api.nfs_shares)
        api.calls.clear()
        self.assertEqual(cluster.create(alice()), [])
        self.assertEqual(api.creates(), [])

    def test_harbor_near_match_is_not_mistaken_for_the_user(self):
        cluster = Cluster()
        cluster.api.harbor_users.add("alice2")
        cluster.api.harbor_members.add("alice2")
        self.assertEqual(cluster.create(alice()), [])
        self.assertIn("alice", cluster.api.harbor_users)
        self.assertIn("alice", cluster.api.harbor_members)


class MountTest(unittest.TestCase):
    def test_failing_host_does_not_stop_the_others(self):
        cluster = Cluster()
        cluster.gpus["S3"].fail_connect = True
        cluster.gpus["S6"].fail_mount = True
        failures = cluster.create(alice())
        self.assertEqual(len(failures), 2, failures)
        self.assertIn("mount on S3", failures[0])
        self.assertIn("No route to host", failures[0])
        self.assertIn("mount on S6", failures[1])
        for name in ["S1", "S2", "S4", "S5", "S7", "S8"]:
            self.assertIn("/workspace/alice", cluster.gpus[name].mounted, name)
        self.assertIn("alice", cluster.login.det_users)
        self.assertIn("alice", cluster.api.harbor_members)
        # fixing S3 and rerunning only touches what is missing
        cluster.gpus["S3"].fail_connect = False
        cluster.gpus["S6"].fail_mount = False
        self.assertEqual(cluster.create(alice()), [])
        for host in cluster.hosts():
            self.assertEqual(sum(1 for l in host.fstab.splitlines() if " /workspace/alice " in l), 1)

    def test_login_mount_failure_blocks_only_home_contents(self):
        cluster = Cluster()
        cluster.login.fail_mount = True
        failures = cluster.create(alice())
        steps = [f.split(": ")[1] for f in failures]
        self.assertEqual(steps, [f"mount on {cu.LOGIN_HOST}", "home contents"], failures)
        self.assertIn("not mounted", failures[1])
        self.assertFalse([c for c in cluster.login.calls if c.argv[0] == "cp"])
        self.assertIn("alice", cluster.login.det_users)

    def test_mount_uses_the_target_not_mount_a(self):
        cluster = Cluster()
        cluster.create(alice())
        mounts = [c.argv for c in cluster.all_calls() if c.argv[0] == "mount"]
        self.assertEqual(len(mounts), 10)
        self.assertTrue(all(len(m) == 2 and m[1].startswith(("/workspace/", "/home/")) for m in mounts))

    def test_existing_entry_for_same_mount_point_is_kept(self):
        cluster = Cluster()
        old = ("nas.cvgl.lab:/mnt/Peter/Workspace/alice /workspace/alice nfs "
               "defaults,vers=3,async,noatime,soft,rsize=32769,wsize=32768,_netdev 0 2")
        cluster.gpus["S1"].fstab += old + "\n"
        self.assertEqual(cluster.create(alice()), [])
        self.assertEqual([l for l in cluster.gpus["S1"].fstab.splitlines() if "alice" in l], [old])
        self.assertIn("/workspace/alice", cluster.gpus["S1"].mounted)
        self.assertTrue(any("S1" in l and "WARNING: kept existing fstab entry" in l for l in cluster.log))

    def test_commented_out_entry_does_not_count(self):
        cluster = Cluster()
        line = cu.fstab_line("nas.cvgl.lab:/mnt/Peter/Workspace/alice", "/workspace/alice")
        cluster.gpus["S2"].fstab += "#" + line + "\n"
        self.assertEqual(cluster.create(alice()), [])
        self.assertIn(line, cluster.gpus["S2"].fstab.splitlines())

    def test_fstab_without_trailing_newline(self):
        cluster = Cluster()
        cluster.gpus["S4"].fstab = "UUID=abcd / ext4 defaults 0 1"
        self.assertEqual(cluster.create(alice()), [])
        lines = cluster.gpus["S4"].fstab.splitlines()
        self.assertEqual(lines[0], "UUID=abcd / ext4 defaults 0 1")
        self.assertTrue(lines[1].startswith("nas.cvgl.lab:/mnt/Peter/Workspace/alice /workspace/alice "))


class FstabScriptTest(unittest.TestCase):
    """The shell snippet that appends to /etc/fstab, run with a real sh."""

    LINE = cu.fstab_line("nas.cvgl.lab:/mnt/Peter/Workspace/alice", "/workspace/alice")

    def append(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fstab")
            with open(path, "w") as f:
                f.write(content)
            subprocess.run(["sh", "-c", cu.FSTAB_APPEND_SCRIPT, "sh", self.LINE, path], check=True)
            with open(path) as f:
                return f.read()

    def test_adds_missing_newline_first(self):
        self.assertEqual(self.append("a b c"), "a b c\n" + self.LINE + "\n")

    def test_no_blank_line_when_newline_present(self):
        self.assertEqual(self.append("a b c\n"), "a b c\n" + self.LINE + "\n")

    def test_empty_file(self):
        self.assertEqual(self.append(""), self.LINE + "\n")

    def test_present_line_is_not_duplicated(self):
        content = "a b c\n" + self.LINE + "\nd e f"
        self.assertEqual(self.append(content), content)

    def test_similar_line_is_not_a_match(self):
        content = self.LINE + " extra\n"
        self.assertEqual(self.append(content), content + self.LINE + "\n")


class TrueNASJobTest(unittest.TestCase):
    def run_job(self, states, error=None, timeout=60):
        api = FakeAPI()
        api.jobs[7] = {"states": list(states), "error": error, "on_success": None}
        with mock.patch.object(cu.requests, "request", side_effect=api.request), \
                mock.patch.object(cu.time, "sleep") as sleep:
            try:
                return cu.wait_truenas_job(7, "set home ACL", timeout=timeout, poll_interval=1), sleep
            except cu.StepError as e:
                return e, sleep

    def test_polls_until_success(self):
        job, sleep = self.run_job(["WAITING", "RUNNING", "RUNNING", "SUCCESS"])
        self.assertEqual(job["state"], "SUCCESS")
        self.assertEqual(sleep.call_count, 3)

    def test_failed_and_aborted_raise_with_error(self):
        err, _ = self.run_job(["RUNNING", "FAILED"], error="[EPERM] cannot set ACL")
        self.assertIsInstance(err, cu.StepError)
        self.assertIn("FAILED", str(err))
        self.assertIn("cannot set ACL", str(err))
        err, _ = self.run_job(["ABORTED"])
        self.assertIn("ABORTED", str(err))

    def test_timeout(self):
        clock = iter(range(0, 1000, 10))
        with mock.patch.object(cu.time, "monotonic", side_effect=lambda: next(clock)):
            err, sleep = self.run_job(["RUNNING"], timeout=30)
        self.assertIsInstance(err, cu.StepError)
        self.assertIn("still RUNNING", str(err))
        self.assertLess(sleep.call_count, 10)

    def test_not_a_job_id(self):
        for value in [None, {"id": 1}, True, "7"]:
            with self.assertRaises(cu.StepError):
                cu.wait_truenas_job(value, "x")

    def test_unknown_job(self):
        api = FakeAPI()
        with mock.patch.object(cu.requests, "request", side_effect=api.request):
            with self.assertRaisesRegex(cu.StepError, "not found"):
                cu.wait_truenas_job(99, "x")


class ApiRequestTest(unittest.TestCase):
    def call(self, response=None, exc=None):
        with mock.patch.object(cu.requests, "request",
                               side_effect=exc if exc else (lambda *a, **k: response)) as request:
            try:
                return cu.api_request("POST", "http://example.invalid/api", ("u", "p"), json={}), request
            except cu.ApiError as e:
                return e, request

    def test_default_timeout(self):
        result, request = self.call(FakeResponse(body={"a": 1}))
        self.assertEqual(result, {"a": 1})
        self.assertEqual(request.call_args[1]["timeout"], cu.HTTP_TIMEOUT)

    def test_http_error_includes_status_and_body(self):
        err, _ = self.call(FakeResponse(422, {"user_create.uid": [{"message": "uid in use"}]}))
        self.assertIsInstance(err, cu.ApiError)
        self.assertIn("HTTP 422", str(err))
        self.assertIn("uid in use", str(err))

    def test_empty_body_and_non_json(self):
        result, _ = self.call(FakeResponse(201))
        self.assertIsNone(result)
        err, _ = self.call(FakeResponse(200, raw="<html>proxy error</html>"))
        self.assertIsInstance(err, cu.ApiError)
        self.assertIn("non-JSON", str(err))

    def test_transport_error(self):
        err, _ = self.call(exc=cu.requests.ConnectionError("connection refused"))
        self.assertIsInstance(err, cu.ApiError)
        self.assertIn("connection refused", str(err))


class DetParsingTest(unittest.TestCase):
    def test_parse_table(self):
        text = (" User Id | Username   | Display Name | Admin | Active | Agent User\n"
                "---------+------------+--------------+-------+--------+------------\n"
                "       1 | admin      | N/A          | True  | True   | N/A\n"
                "       7 | bob        | alice        | False | True   | alice\n")
        self.assertEqual(cu.parse_det_usernames(text), {"admin", "bob"})

    def test_unparseable_output(self):
        with self.assertRaises(cu.StepError):
            cu.parse_det_usernames("error: something went wrong\n")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_help(self):
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cu.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        for text in ["--users", "--no-docker-group", "username,full_name,password", "rerun"]:
            self.assertIn(text, out.getvalue())

    def test_users_is_required(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as ctx:
            cu.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_input_touches_nothing(self):
        path = write_csv(self.tmp.name, "username,full_name,password\nalice,Alice,Correct-Horse9\nBob,Bob,weak\n")
        cluster = Cluster()
        code, out, err = cluster.main(["--users", path])
        self.assertEqual(code, 2)
        self.assertIn("nothing was changed", err)
        self.assertIn("'Bob'", err)
        self.assertNotIn("weak", err)
        self.assertEqual(cluster.connected, [])
        self.assertEqual(cluster.api.calls, [])

    def test_success(self):
        path = write_csv(self.tmp.name, "username,full_name,password\nalice,Alice,Correct-Horse9\n"
                                        "bob,Bob,Another-Pass8\n")
        cluster = Cluster()
        code, out, err = cluster.main(["--users", path])
        self.assertEqual(code, 0, err)
        self.assertIn("User alice created successfully.", out)
        self.assertIn("User bob created successfully.", out)
        self.assertTrue(cluster.login.closed)
        self.assertEqual(cluster.connected.count(cu.LOGIN_HOST), 1, "one login connection for everything")
        self.assertIn("docker", cluster.login.users["bob"]["groups"])

    def test_one_failing_user_does_not_stop_the_next(self):
        path = write_csv(self.tmp.name, "username,full_name,password\nalice,Alice,Correct-Horse9\n"
                                        "bob,Bob,Another-Pass8\n")
        cluster = Cluster()
        cluster.api.groups["alice"] = {"id": 5, "gid": 4242, "name": "alice"}
        code, out, err = cluster.main(["--users", path, "--no-docker-group"])
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 user(s) have failed steps", err)
        self.assertIn("alice: TrueNAS group", err)
        self.assertIn("User bob created successfully.", out)
        self.assertNotIn("docker", cluster.login.users["bob"]["groups"])


if __name__ == "__main__":
    unittest.main()
