# -*- coding: utf-8 -*-
"""
proxy_apply.py: request validation, the spool, and the apply state machine with injected failures.

    cd services/proxy-portal/applier && python3 -B -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proxy_apply as ap  # noqa: E402
import proxy_accounts as pa  # noqa: E402
from XrayConfigHandler import XrayConfigHandler  # noqa: E402

SERVICES = Path(__file__).resolve().parents[3]
IMAGE = "teddysun/xray:1@sha256:" + "0" * 64
IMAGE_ID = "sha256:" + "1" * 64      # the local image ID of IMAGE
OLD_IMAGE_ID = "sha256:" + "2" * 64  # a container created from an older image
PROXY = "usca5-bwh-sla-1tb"      # a public proxy (xray/public-proxies.txt)
PRIVATE = "jpty1-bwh-2tb"        # a private proxy


class Crash(BaseException):
    """Stands for the applier being killed mid-apply."""


class FakeDocker:
    """docker and docker compose, with the HA entries and the proxies as login tables loaded at each restart."""

    SERVICES = ["xray-ha", "xray-" + PROXY, "xray-" + PRIVATE, "blackbox-exporter", "xray-ha-exporter"]

    def __init__(self, base):
        self.base = base
        self.calls = []
        self.running = set(self.SERVICES)
        self.fail_test = None        # substring of a config that makes xray -test fail
        self.fail_restart = 0        # restarts that fail before one succeeds
        self.crash_restart = False
        self.stale = False
        self.image = IMAGE                                     # xray-ha's image in the Compose file
        self.proxy_image = IMAGE                               # the proxies'
        self.containers = {"xray-ha": IMAGE_ID, "xray-" + PROXY: IMAGE_ID,  # what each container was created from
                           "xray-" + PRIVATE: IMAGE_ID}
        self.ha_users = {}       # ha-general
        self.ha_private_users = {}   # ha-private
        self.private_users = {}  # the private proxy's own entry
        self.load()

    @staticmethod
    def users(path, tag):
        if not path.exists():
            return {}
        inbound = next(i for i in json.loads(path.read_text())["inbounds"] if i.get("tag") == tag)
        return {u["user"]: u["pass"] for u in inbound["settings"].get("users", [])}

    def load(self):
        self.ha_users = self.users(self.base / pa.HA_CONFIG, "http_IN")
        self.ha_private_users = self.users(self.base / pa.HA_CONFIG, "ha-private_http_IN")
        self.private_users = self.users(self.base / "xray" / PRIVATE / "config" / "config.json", "http_IN")

    def __call__(self, argv, input_text, timeout):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return (0, IMAGE_ID + "\n", "") if argv[-1] == IMAGE else (1, "", "Error: No such image: " + argv[-1])
        if argv[:2] == ["docker", "inspect"]:
            return 0, "".join(self.containers[cid[len("cid-"):]] + "\n" for cid in argv[4:]), ""
        if argv[:2] == ["docker", "run"]:
            json.loads(input_text)
            if self.fail_test and self.fail_test in input_text:
                return 23, "", "Failed to start: main: failed to load config files: bad (password " + \
                    input_text.split('"pass": "')[1].split('"')[0] + ")"
            return 0, "Configuration OK.\n", ""
        args = argv[argv.index("-f") + 2:]
        if args == ["config", "--services"]:
            return 0, "\n".join(self.SERVICES) + "\n", ""
        if args[:3] == ["ps", "--services", "--status"]:
            return 0, "\n".join(sorted(self.running)) + "\n", ""
        if args == ["config", "--format", "json"]:
            return 0, json.dumps({"services": {"xray-ha": {"image": self.image}, "xray-" + PROXY: {"image": self.proxy_image},
                                               "xray-" + PRIVATE: {"image": self.proxy_image},
                                               "blackbox-exporter": {"image": "prom/blackbox-exporter"}}}), ""
        if args[:2] == ["ps", "-q"]:
            return 0, (f"cid-{args[2]}\n" if args[2] in self.running else ""), ""
        if args[0] == "restart":
            if self.crash_restart:
                raise Crash()
            if self.fail_restart:
                self.fail_restart -= 1
                return 1, "", "Error response from daemon: cannot restart"
            if not self.stale:
                self.load()
            return 0, "", ""
        raise AssertionError(argv)

    def restarts(self):
        return [c[c.index("-f") + 3:] for c in self.calls if "restart" in c]

    def tests(self):
        return sum(c[:2] == ["docker", "run"] for c in self.calls)


class FakeProbe:
    """The HA entries: 204 for a login it knows, 407 otherwise; or a down entry, or one without proxies."""

    def __init__(self, docker):
        self.docker = docker
        self.mode = "ok"      # ok | down | silent | noproxies
        self.calls = []       # users
        self.entry_calls = []  # (entry, user)
        self.logins = []      # (user, password) of each probe

    def __call__(self, entry, user, password):
        self.calls.append(user)
        self.entry_calls.append((entry, user))
        self.logins.append((user, password))
        if self.mode == "down":
            return "unreachable", None
        if self.mode == "silent":
            return "noresponse", None
        users = {"ha-general": self.docker.ha_users, "ha-private": self.docker.ha_private_users}[entry]
        known = users.get(user) == password
        if not known:
            return "status", 407
        return ("noresponse", None) if self.mode == "noproxies" else ("status", 204)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.base = self.tmp / "services"
        for rel in (pa.HA_EXAMPLE, pa.BLACKBOX_EXAMPLE, pa.PUBLIC_PROXIES_FILE):
            (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SERVICES / rel, self.base / rel)
        outbound = {"protocol": "freedom", "settings": {}, "tag": "proxy"}
        text = json.dumps(XrayConfigHandler.build_xray_config(outbound, include_stats_api=True), indent=4)
        self.proxy = self.base / "xray" / PROXY / "config" / "config.json"
        self.private = self.base / "xray" / PRIVATE / "config" / "config.json"
        for path in (self.proxy, self.private):
            path.parent.mkdir(parents=True)
            path.write_text(text)
        accounts = pa.Accounts(self.base)
        for name in ("svc-monitor", "svc-ha", "alice", "bob", "boss", "svc-docker-node01"):
            accounts.accounts[name] = pa.new_password()
        accounts.admins.add("boss")
        accounts.save()
        pa.render(accounts)
        self.mark_applied()
        self.spool = ap.Spool(self.tmp / "spool", os.getuid())
        self.spool.init(os.getgid())
        self.docker = FakeDocker(self.base)
        self.probe = FakeProbe(self.docker)
        self.time = [0.0]
        self.out = io.StringIO()
        self.seen_passwords = set(self.passwords().values())

    def passwords(self):
        return dict(pa.Accounts(self.base).accounts)

    def mark_applied(self):
        """applied.json as a verified apply leaves it: the running services hold the files on disk."""
        accounts = pa.Accounts(self.base)
        (self.base / pa.APPLIED_FILE).write_text(json.dumps({
            "accounts_sha256": pa.sha256_text(accounts.text()),
            "render_sha256": pa.render_sha256(self.base, pa.render_files(accounts)), "applied_at": "x"}))

    def applier(self):
        def sleep(seconds):
            self.time[0] += seconds
        return ap.Applier(self.base, self.spool, ap.Compose(self.base / "compose.yml", None, self.docker), self.probe,
                          verify_timeout=10, verify_interval=2, sleep=sleep, clock=lambda: self.time[0])

    def request(self, request_id, op, username, raw=None):
        path = self.spool.requests / f"{request_id}.json"
        path.write_bytes(raw if raw is not None else json.dumps({"id": request_id, "op": op,
                                                                  "username": username}).encode())
        return path

    def run_applier(self):
        with contextlib.redirect_stdout(self.out):
            code = self.applier().run()
        self.seen_passwords |= set(self.passwords().values())
        return code

    def status(self, request_id):
        return json.loads((self.spool.status / f"{request_id}.json").read_text())

    def files(self):
        return {rel: (self.base / rel).read_text()
                for rel in (pa.ACCOUNTS_FILE, pa.HA_CONFIG, pa.BLACKBOX_CONFIG, self.proxy.relative_to(self.base),
                            self.private.relative_to(self.base))}

    def assert_no_leaks(self):
        texts = [self.out.getvalue()] + [p.read_text() for p in self.spool.status.iterdir()]
        texts.append((self.spool.root / "status.json").read_text() if (self.spool.root / "status.json").exists()
                     else "")
        for text in texts:
            for password in self.seen_passwords:
                self.assertNotIn(password, text)


class ParseRequestTest(unittest.TestCase):
    def ok(self, **kw):
        data = {"id": "0f8e1c2a-1111-4222-8333-944455556666", "op": "disable", "username": "alice"}
        data.update(kw)
        return json.dumps(data).encode()

    def test_valid(self):
        for op in ap.OPS:
            self.assertEqual(ap.parse_request(self.ok(op=op))["op"], op)
        self.assertEqual(ap.parse_request(self.ok(username="svc-docker-node01"))["username"], "svc-docker-node01")

    def test_invalid(self):
        bad = [
            self.ok(op="render"), self.ok(op="raw-auth"), self.ok(op="ADD"), self.ok(op=""), self.ok(op="admin"),
            self.ok(op="admin-on "), self.ok(op="admin_on"),
            self.ok(username="Alice"), self.ok(username="../etc/passwd"), self.ok(username="a/b"),
            self.ok(username="alice\n"), self.ok(username="svc-foo"), self.ok(username="x" * 65),
            self.ok(username="svc-monitor"), self.ok(username="svc-ha"), self.ok(username="a:b"),
            self.ok(id="../../etc/x"), self.ok(id="a/b-cdefgh"), self.ok(id="short"), self.ok(id="ABCDEFGH-1"),
            self.ok(id="x" * 65), self.ok(id=".hidden-id"),
            self.ok(password="chosen"), self.ok(path="/etc/shadow"), self.ok(command="id"),
            json.dumps({"id": "abcdefgh-1", "op": "add"}).encode(),
            json.dumps({"id": "abcdefgh-1", "op": "add", "username": ["alice"]}).encode(),
            json.dumps({"id": "abcdefgh-1", "op": "add", "username": None}).encode(),
            b'{"id": "abcdefgh-1", "op": "add", "username": "alice", "username": "bob"}',
            b'["abcdefgh-1", "add", "alice"]', b"not json", b"\xff\xfe", b"",
            self.ok(username="a" * 5000),
            b" " * (ap.MAX_REQUEST_BYTES + 1),
            b"[" * 4000, b'{"a": ' * 680,  # deep nesting: RecursionError on Python 3.8 to 3.11
        ]
        for raw in bad:
            with self.assertRaises(ap.RequestError, msg=raw[:80]):
                ap.parse_request(raw)


class SpoolTest(_Base):
    def test_init_modes(self):
        for path, mode in ((self.spool.root, 0o755), (self.spool.status, 0o755), (self.spool.requests, 0o700),
                           (self.spool.results, 0o700)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode)
        self.spool.init(os.getgid())  # idempotent

    def test_every_json_entry_is_consumed(self):
        target = self.tmp / "target.json"
        target.write_text(json.dumps({"id": "abcdefgh-2", "op": "disable", "username": "alice"}))
        os.symlink(str(target), str(self.spool.requests / "abcdefgh-1.json"))       # symlink
        (self.spool.requests / "abcdefgh-3.json").mkdir()                            # folder
        (self.spool.requests / "abcdefgh-3.json" / "x").write_text("x")
        hard = self.request("abcdefgh-4", "disable", "alice")                         # hard link
        os.link(str(hard), str(self.tmp / "other-link"))
        os.mkfifo(str(self.spool.requests / "abcdefgh-5.json"))                       # fifo
        self.request("abcdefgh-6", "", "", raw=b"x" * (ap.MAX_REQUEST_BYTES + 100))  # oversized
        self.request("abcdefgh-7", "disable", "alice", raw=json.dumps(
            {"id": "abcdefgh-8", "op": "disable", "username": "alice"}).encode())     # id is not the file name
        self.request("Bad_Name", "disable", "alice")                                  # file name
        self.request("abcdefgh-9", "remove", "svc-monitor")                           # protected
        (self.spool.requests / "abcdefgh-10.json.tmp").write_text("{}")               # a temp file: left alone
        files = self.files()
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(sorted(os.listdir(self.spool.requests)), ["abcdefgh-10.json.tmp"])
        self.assertTrue(target.exists())
        self.assertEqual(self.files(), files)
        self.assertEqual(self.docker.restarts(), [])
        for request_id in ("abcdefgh-1", "abcdefgh-3", "abcdefgh-4", "abcdefgh-5", "abcdefgh-6", "abcdefgh-7",
                           "abcdefgh-9"):
            self.assertEqual(self.status(request_id)["status"], "failed", request_id)
            self.assertEqual(self.status(request_id)["stage"], "request")
        self.assertIn("managed with proxy_accounts.py only", self.status("abcdefgh-9")["error"])
        self.assertFalse((self.spool.status / "abcdefgh-8.json").exists())
        self.assertEqual(sorted(os.listdir(self.spool.status)),
                         sorted(f"abcdefgh-{i}.json" for i in (1, 3, 4, 5, 6, 7, 9)))

    def test_other_owner_refused(self):
        self.spool.requester_uid = os.getuid() + 1
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(os.listdir(self.spool.requests), [])
        self.assertIn("not the requester", self.status("abcdefgh-1")["error"])
        self.assertTrue(pa.Accounts(self.base).enabled("alice"))

    def test_stale_temp_files_removed(self):
        tmp = self.spool.requests / "abcdefgh-1.json.tmp"
        tmp.write_text("{")
        os.utime(str(tmp), (1, 1))
        self.run_applier()
        self.assertEqual(os.listdir(self.spool.requests), [])

    def test_result_does_not_follow_planted_links(self):
        victim = self.tmp / "victim"
        victim.write_text("keep")
        os.symlink(str(victim), str(self.spool.results / "abcdefgh-1.json"))
        os.symlink(str(victim), str(self.spool.results / ".abcdefgh-2.tmp"))
        self.request("abcdefgh-1", "rotate", "alice")
        self.request("abcdefgh-2", "rotate", "bob")
        self.run_applier()
        self.assertEqual(victim.read_text(), "keep")
        result = self.spool.results / "abcdefgh-1.json"
        self.assertFalse(result.is_symlink())  # the link itself was replaced
        self.assertEqual(stat.S_IMODE(os.lstat(result).st_mode), 0o400)
        self.assertEqual(json.loads(result.read_text())["password"], self.passwords()["alice"])
        self.assertEqual(self.status("abcdefgh-2")["status"], "applied")
        self.assertIs(self.status("abcdefgh-2")["result"], False)  # not delivered through the planted link

    def test_results_expire(self):
        self.request("abcdefgh-1", "add", "carol")
        self.run_applier()
        result = self.spool.results / "abcdefgh-1.json"
        self.assertTrue(result.exists())
        self.spool.expire_results(ttl=3600)
        self.assertTrue(result.exists())
        os.utime(str(result), (1, 1))
        self.run_applier()
        self.assertFalse(result.exists())

    def test_nested_json_next_to_a_valid_request(self):
        self.request("abcdefgh-1", "disable", "alice")
        os.utime(str(self.spool.requests / "abcdefgh-1.json"), (1, 1))
        self.request("abcdefgh-2", "", "", raw=b"[" * 4000)
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(os.listdir(self.spool.requests), [])
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        self.assertEqual((self.status("abcdefgh-2")["status"], self.status("abcdefgh-2")["stage"]),
                         ("failed", "request"))
        self.assertFalse(pa.Accounts(self.base).enabled("alice"))

    def test_unexpected_read_error_refuses_the_request(self):
        self.request("abcdefgh-1", "disable", "alice")
        with mock.patch.object(ap, "parse_request", side_effect=MemoryError):
            self.assertEqual(self.run_applier(), 0)
        self.assertEqual(os.listdir(self.spool.requests), [])
        self.assertEqual(self.status("abcdefgh-1")["error"], "cannot read: MemoryError")

    def test_entry_kept_until_its_status_is_written(self):
        self.request("abcdefgh-1", "disable", "alice")
        os.chmod(str(self.spool.status), 0o555)
        self.addCleanup(os.chmod, str(self.spool.status), 0o755)
        if os.access(str(self.spool.status), os.W_OK):
            self.skipTest("needs a non-root user")
        with self.assertRaises(OSError):
            self.run_applier()
        self.assertEqual(os.listdir(self.spool.requests), ["abcdefgh-1.json"])

    def test_killed_after_take_is_failed_next_run(self):
        self.request("abcdefgh-1", "disable", "alice")
        with mock.patch.object(ap.Applier, "apply_batch", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_applier()
        self.assertEqual(os.listdir(self.spool.requests), [])
        self.assertEqual(self.status("abcdefgh-1")["status"], "applying")
        self.run_applier()
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "interrupted", True))
        self.assertTrue(pa.Accounts(self.base).enabled("alice"))

    def test_entry_that_cannot_be_removed_ends_the_run(self):
        self.request("abcdefgh-1", "disable", "alice")
        with mock.patch.object(ap.Spool, "discard", side_effect=PermissionError):
            self.assertEqual(self.run_applier(), 2)
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        self.assertEqual(self.docker.restarts(), [["xray-ha"]])
        self.assertEqual(self.run_applier(), 0)  # dropped as a used id
        self.assertEqual(os.listdir(self.spool.requests), [])

    def test_reused_id_dropped(self):
        self.request("abcdefgh-1", "disable", "alice")
        self.run_applier()
        first = self.status("abcdefgh-1")
        self.request("abcdefgh-1", "enable", "alice")
        self.run_applier()
        self.assertEqual(os.listdir(self.spool.requests), [])
        self.assertEqual(self.status("abcdefgh-1"), first)
        self.assertFalse(pa.Accounts(self.base).enabled("alice"))


class ApplyTest(_Base):
    def test_disable_and_enable(self):
        alice = self.passwords()["alice"]
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["changed"], record["restarted"]), ("applied", True, ["xray-ha"]))
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertIn("bob", self.docker.ha_users)
        self.assertNotIn(alice, (self.base / pa.HA_CONFIG).read_text())
        self.assertEqual(self.passwords()["alice"], alice)
        self.assertIn("alice", self.probe.calls)  # the refused login was checked
        self.assertEqual(self.docker.tests(), 1)  # only the HA config changed
        view = json.loads((self.spool.root / "status.json").read_text())
        self.assertEqual(next(a for a in view["accounts"] if a["name"] == "alice")["enabled"], False)
        self.assertEqual(view["disabled_refused_at"], ["ha-general", "ha-private", "private-proxies"])
        self.assertIs(view["unapplied_changes"], False)
        self.assertFalse((self.base / ap.BACKUP_DIR).exists())
        self.assertEqual(os.listdir(self.spool.results), [])

        self.request("abcdefgh-2", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.docker.ha_users["alice"], alice)
        self.assert_no_leaks()

    def test_noop_does_not_restart(self):
        self.request("abcdefgh-1", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual((self.status("abcdefgh-1")["status"], self.status("abcdefgh-1")["changed"]),
                         ("applied", False))
        self.assertEqual(self.docker.restarts(), [])

    def test_add_and_rotate_deliver_a_result(self):
        old_bob = self.passwords()["bob"]
        self.request("abcdefgh-1", "add", "carol")
        self.request("abcdefgh-2", "rotate", "bob")
        self.request("abcdefgh-3", "add", "alice")  # exists: fails alone
        self.assertEqual(self.run_applier(), 0)
        passwords = self.passwords()
        for request_id, name in (("abcdefgh-1", "carol"), ("abcdefgh-2", "bob")):
            record = self.status(request_id)
            self.assertEqual((record["status"], record["result"]), ("applied", True))
            result = json.loads((self.spool.results / f"{request_id}.json").read_text())
            self.assertEqual(result, {"id": request_id, "username": name, "password": passwords[name]})
            self.assertGreaterEqual(len(result["password"]), 32)
        self.assertNotEqual(passwords["bob"], old_bob)
        self.assertEqual((self.status("abcdefgh-3")["status"], self.status("abcdefgh-3")["stage"]),
                         ("failed", "request"))
        self.assertFalse((self.spool.results / "abcdefgh-3.json").exists())
        self.assertEqual(self.docker.ha_users["carol"], passwords["carol"])
        self.assertEqual(self.docker.restarts(), [["xray-ha"]])  # one batch, one restart
        self.assert_no_leaks()

    def results_live(self):
        """Every result file left holds a password the HA entry accepts."""
        for path in self.spool.results.iterdir():
            result = json.loads(path.read_text())
            self.assertEqual(self.docker.ha_users.get(result["username"]), result["password"], path.name)

    def same_account(self, *ops, batches=2):
        for i, (op, name) in enumerate(ops, 1):
            self.request(f"abcdefgh-{i}", op, name)
            os.utime(str(self.spool.requests / f"abcdefgh-{i}.json"), (i, i))
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(len(self.docker.restarts()), batches)  # one verified restart per batch
        self.results_live()
        self.assert_no_leaks()

    def test_same_account_add_then_rotate(self):
        self.same_account(("add", "carol"), ("rotate", "carol"))
        first, second = self.status("abcdefgh-1"), self.status("abcdefgh-2")
        self.assertGreater(second["applied_seq"], first["applied_seq"])  # the order applied, within one run
        self.assertEqual((first["status"], first["result"], first["superseded_by"]), ("applied", False, "abcdefgh-2"))
        self.assertEqual((second["status"], second["result"]), ("applied", True))
        self.assertEqual(os.listdir(self.spool.results), ["abcdefgh-2.json"])

    def test_same_account_rotate_then_remove(self):
        self.same_account(("rotate", "bob"), ("remove", "bob"))
        self.assertEqual((self.status("abcdefgh-1")["result"], self.status("abcdefgh-1")["superseded_by"]),
                         (False, "abcdefgh-2"))
        self.assertEqual(self.status("abcdefgh-2")["status"], "applied")
        self.assertEqual(os.listdir(self.spool.results), [])
        self.assertNotIn("bob", self.passwords())

    def test_same_account_disable_then_enable(self):
        alice = self.passwords()["alice"]
        self.same_account(("disable", "alice"), ("enable", "alice"), ("disable", "bob"))
        for request_id in ("abcdefgh-1", "abcdefgh-2", "abcdefgh-3"):
            self.assertEqual(self.status(request_id)["restarted"], ["xray-ha"])
        self.assertEqual(self.docker.ha_users["alice"], alice)
        self.assertNotIn("bob", self.docker.ha_users)  # the third request joined the second batch
        self.assertIn("alice", self.probe.calls)

    def test_remove(self):
        self.request("abcdefgh-1", "remove", "svc-docker-node01")
        self.assertEqual(self.run_applier(), 0)
        self.assertNotIn("svc-docker-node01", self.passwords())
        self.assertNotIn("svc-docker-node01", self.docker.ha_users)

    def test_raw_auth_restarts_the_proxies(self):
        accounts = pa.Accounts(self.base)
        accounts.raw_auth = True
        accounts.save()
        pa.render(accounts)
        self.mark_applied()  # and restarted
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["restarted"], ["xray-ha", "xray-" + PRIVATE, "xray-" + PROXY])
        proxy = json.loads(self.proxy.read_text())
        users = [u["user"] for u in proxy["inbounds"][0]["settings"]["users"]]
        self.assertNotIn("alice", users)
        self.assertIn("bob", users)
        self.assertEqual(self.docker.tests(), 2)  # the HA config and the proxy
        view = json.loads((self.spool.root / "status.json").read_text())
        self.assertEqual(view["disabled_refused_at"], ["ha-general", "ha-private", "private-proxies", "public-proxies"])

    def test_raw_auth_proxy_not_running_is_not_started(self):
        accounts = pa.Accounts(self.base)
        accounts.raw_auth = True
        accounts.save()
        pa.render(accounts)
        self.mark_applied()  # and restarted
        self.docker.running.discard("xray-" + PROXY)
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["restarted"], ["xray-ha", "xray-" + PRIVATE])

    def test_migrates_a_version_1_file(self):
        accounts = pa.Accounts(self.base)
        (self.base / pa.ACCOUNTS_FILE).write_text(json.dumps({"raw_auth": False, "accounts": accounts.accounts}))
        self.request("abcdefgh-1", "disable", "bob")
        self.assertEqual(self.run_applier(), 0)
        data = json.loads((self.base / pa.ACCOUNTS_FILE).read_text())
        self.assertEqual(data["version"], 3)
        self.assertEqual({n: e["password"] for n, e in data["accounts"].items()}, accounts.accounts)
        self.assertEqual([n for n, e in data["accounts"].items() if not e["enabled"]], ["bob"])
        self.assertEqual([n for n, e in data["accounts"].items() if e["admin"]], [])  # version 1: no admins

    def test_migrates_a_version_2_file_and_admin_on(self):
        accounts = pa.Accounts(self.base)
        v2 = {"version": 2, "raw_auth": False,
              "accounts": {n: {"password": p, "enabled": True} for n, p in accounts.accounts.items()}}
        (self.base / pa.ACCOUNTS_FILE).write_text(json.dumps(v2))
        self.request("abcdefgh-1", "admin-on", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        data = json.loads((self.base / pa.ACCOUNTS_FILE).read_text())
        self.assertEqual(data["version"], 3)
        self.assertEqual([n for n, e in data["accounts"].items() if e["admin"]], ["alice"])


class AdminTest(_Base):
    """admin-on and admin-off: ha-private and the private proxies follow, the public proxies and ha-general do not."""

    def test_admin_on_and_off(self):
        alice = self.passwords()["alice"]
        public_before = self.proxy.read_text()
        self.request("abcdefgh-1", "admin-on", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["changed"], record["restarted"]),
                         ("applied", True, ["xray-ha", "xray-" + PRIVATE]))
        self.assertEqual(self.docker.ha_private_users.get("alice"), alice)
        self.assertEqual(self.docker.private_users.get("alice"), alice)
        self.assertEqual(self.docker.ha_users.get("alice"), alice)
        self.assertIn(("ha-private", "alice"), self.probe.entry_calls)        # 204 on ha-private checked
        self.assertIn(("ha-private", "bob"), self.probe.entry_calls)          # a non-admin's 407 on ha-private checked
        self.assertEqual(self.docker.tests(), 2)  # the HA config and the private proxy
        view = json.loads((self.spool.root / "status.json").read_text())
        self.assertTrue(next(a for a in view["accounts"] if a["name"] == "alice")["admin"])

        self.probe.entry_calls.clear()
        self.request("abcdefgh-2", "admin-off", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["restarted"]), ("applied", ["xray-ha", "xray-" + PRIVATE]))
        self.assertNotIn("alice", self.docker.ha_private_users)
        self.assertNotIn("alice", self.docker.private_users)
        self.assertEqual(self.docker.ha_users.get("alice"), alice)  # ha-general unchanged
        self.assertIn(("ha-private", "alice"), self.probe.entry_calls)   # her 407 on ha-private checked
        self.assertEqual(self.proxy.read_text(), public_before)      # raw auth off: the public proxy untouched
        self.assert_no_leaks()

    def test_admin_rotate_and_disable_restart_the_private_proxy(self):
        boss = self.passwords()["boss"]
        self.request("abcdefgh-1", "rotate", "boss")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["restarted"], ["xray-ha", "xray-" + PRIVATE])
        new = self.passwords()["boss"]
        self.assertEqual((self.docker.ha_private_users["boss"], self.docker.private_users["boss"]), (new, new))
        result = json.loads((self.spool.results / "abcdefgh-1.json").read_text())
        self.assertEqual(result["password"], new)
        self.assertNotEqual(new, boss)
        self.request("abcdefgh-2", "disable", "boss")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-2")["restarted"], ["xray-ha", "xray-" + PRIVATE])
        for users in (self.docker.ha_users, self.docker.ha_private_users, self.docker.private_users):
            self.assertNotIn("boss", users)
        self.assert_no_leaks()

    def test_non_admin_changes_leave_the_private_proxy(self):
        self.request("abcdefgh-1", "add", "carol")
        self.request("abcdefgh-2", "rotate", "bob")
        self.assertEqual(self.run_applier(), 0)
        for request_id in ("abcdefgh-1", "abcdefgh-2"):
            self.assertEqual(self.status(request_id)["restarted"], ["xray-ha"])
        self.assertNotIn("carol", self.docker.ha_private_users)
        self.assertIn(("ha-private", "carol"), self.probe.entry_calls)
        self.assertIn(("ha-private", "bob"), self.probe.entry_calls)

    def test_service_accounts_cannot_be_admins(self):
        before = self.files()
        self.request("abcdefgh-1", "admin-on", "svc-docker-node01")
        self.request("abcdefgh-2", "admin-on", "svc-monitor")
        self.request("abcdefgh-3", "admin-on", "nobody")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual((self.status("abcdefgh-1")["status"], self.status("abcdefgh-1")["stage"]),
                         ("failed", "request"))
        self.assertIn("cannot be an admin", self.status("abcdefgh-1")["error"])
        self.assertEqual(self.status("abcdefgh-2")["stage"], "request")  # refused when read
        self.assertEqual(self.status("abcdefgh-3")["stage"], "request")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.docker.restarts(), [])

    def test_ha_private_open_to_a_non_admin_rolls_back(self):
        # An HA config that lets every account into ha-private: the non-admin's 200 instead of 407 fails the apply
        real = pa.Accounts.ha_private_users
        before = self.files()
        with mock.patch.object(pa.Accounts, "ha_private_users", lambda a: real(a) + a.pairs(exclude=(pa.RELAY,
                                                                                                 pa.MONITOR, "boss"))):
            self.request("abcdefgh-1", "admin-on", "alice")
            self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "verify", True))
        self.assertIn("(not an admin) on ha-private: expected 407, got 204", record["error"])
        self.assertEqual(self.files(), before)

    def test_new_admin_refused_on_ha_private_rolls_back(self):
        # The HA config leaves ha-private as it was: the new admin's 407 fails the apply
        before = self.files()
        with mock.patch.object(pa.Accounts, "ha_private_users",
                               lambda a: sorted([(n, p) for n, p in a.admin_pairs() if n != "alice"]
                                                + [(pa.MONITOR, a.accounts[pa.MONITOR])])):
            self.request("abcdefgh-1", "admin-on", "alice")
            self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"]), ("failed", "verify"))
        self.assertIn("alice on ha-private: refused with 407", record["error"])
        self.assertEqual(self.files(), before)


class RunningStateTest(_Base):
    """A request that changes nothing in the files restarts nothing only when the running entries hold them."""

    def cli(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pa.main(["--base-dir", str(self.base)] + list(argv)), 0)

    def cli_disable_without_restart(self):
        """A live apply, then an admin's "disable alice; render" without a restart: the entry still takes alice."""
        self.request("abcdefgh-1", "disable", "bob")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        alice = self.passwords()["alice"]
        self.cli("disable", "alice")
        self.cli("render")
        self.assertEqual(self.docker.ha_users.get("alice"), alice)
        self.request("abcdefgh-2", "disable", "alice")
        return alice

    def test_change_on_disk_not_running_is_restarted_and_verified(self):
        alice = self.cli_disable_without_restart()
        restarts = len(self.docker.restarts())
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-ha", record["restarted"])
        self.assertEqual(len(self.docker.restarts()), restarts + 1)
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertIn(("alice", alice), self.probe.logins)  # the request's own account, checked directly

    def test_without_applied_state_a_noop_is_restarted_and_verified(self):
        (self.base / pa.APPLIED_FILE).unlink()
        alice = self.passwords()["alice"]
        self.request("abcdefgh-1", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertEqual(record["restarted"],  # in full
                         ["xray-ha", "xray-" + PRIVATE, "xray-" + PROXY, "blackbox-exporter"])
        self.assertIn(("alice", alice), self.probe.logins)
        self.request("abcdefgh-2", "enable", "alice")  # now known: nothing restarts
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-2")["restarted"], [])
        self.assertEqual(len(self.docker.restarts()), 1)

    def test_apply_sequence_increases_across_runs(self):
        self.request("abcdefgh-1", "add", "carol")
        self.assertEqual(self.run_applier(), 0)
        self.request("abcdefgh-2", "rotate", "carol")
        self.assertEqual(self.run_applier(), 0)
        first, second = self.status("abcdefgh-1")["applied_seq"], self.status("abcdefgh-2")["applied_seq"]
        self.assertGreater(second, first)
        self.assertEqual(json.loads((self.spool.root / ap.SEQUENCE_FILE).read_text()), {"last": second})
        (self.spool.root / ap.SEQUENCE_FILE).unlink()  # lost: continues from the statuses
        self.request("abcdefgh-3", "remove", "carol")
        self.assertEqual(self.run_applier(), 0)
        self.assertGreater(self.status("abcdefgh-3")["applied_seq"], second)
        self.request("abcdefgh-4", "add", "alice")  # refused: no sequence number
        self.assertEqual(self.run_applier(), 0)
        self.assertNotIn("applied_seq", self.status("abcdefgh-4"))

    def test_admin_off_on_disk_not_running_restarts_the_private_proxy(self):
        # "admin boss off; render" with the CLI and no restart: ha-private and the private proxy still take boss
        self.request("abcdefgh-1", "disable", "bob")
        self.assertEqual(self.run_applier(), 0)
        boss = self.passwords()["boss"]
        self.cli("admin", "boss", "off")
        self.cli("render")
        self.assertEqual((self.docker.ha_private_users.get("boss"), self.docker.private_users.get("boss")), (boss, boss))
        self.request("abcdefgh-2", "admin-off", "boss")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-" + PRIVATE, record["restarted"])
        self.assertNotIn("boss", self.docker.ha_private_users)
        self.assertNotIn("boss", self.docker.private_users)
        self.assertIn(("ha-private", "boss"), self.probe.entry_calls)  # her 407 on ha-private, checked directly

    def test_noop_with_a_record_that_does_not_match_ha_private_is_applied_in_full(self):
        """applied.json matches the files after "admin boss off; render", but ha-private still takes boss."""
        self.request("abcdefgh-1", "disable", "bob")
        self.assertEqual(self.run_applier(), 0)
        self.cli("admin", "boss", "off")
        self.cli("render")
        self.mark_applied()  # as if the record had survived the change
        self.request("abcdefgh-2", "admin-off", "boss")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-" + PRIVATE, record["restarted"])
        self.assertNotIn("boss", self.docker.ha_private_users)
        self.assertNotIn("boss", self.docker.private_users)

    def test_cli_render_after_a_restart_by_hand_is_not_taken_as_applied(self):
        """Portal disable; CLI enable, render, restart; CLI disable, render, no restart; portal disable again."""
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertNotIn("alice", self.docker.ha_users)
        alice = self.passwords()["alice"]
        self.cli("enable", "alice")
        self.cli("render")
        self.docker.load()  # docker compose restart xray-ha, by hand
        self.assertEqual(self.docker.ha_users.get("alice"), alice)
        self.cli("disable", "alice")
        self.cli("render")  # the restart is missed: the entry still takes alice
        self.assertFalse((self.base / pa.APPLIED_FILE).exists())
        self.assertIsNone(pa.status(pa.Accounts(self.base))["unapplied_changes"])
        restarts = len(self.docker.restarts())
        self.request("abcdefgh-2", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-ha", record["restarted"])
        self.assertEqual(len(self.docker.restarts()), restarts + 1)
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertIn(("alice", alice), self.probe.logins)

    def test_cli_raw_auth_render_after_a_restart_by_hand_restarts_the_proxies(self):
        """The same with raw auth: on and applied, off and restarted by hand, on again without the restart."""
        self.cli("raw-auth", "on")
        self.cli("render")
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        self.cli("raw-auth", "off")
        self.cli("render")
        self.docker.load()  # docker compose restart xray-ha and the proxies, by hand
        self.cli("raw-auth", "on")
        self.cli("render")  # the restart is missed: the proxies run without accounts
        self.assertFalse((self.base / pa.APPLIED_FILE).exists())
        self.request("abcdefgh-2", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual(record["status"], "applied")
        self.assertEqual(sorted(record["restarted"]),
                         sorted(["xray-ha", "xray-" + PROXY, "xray-" + PRIVATE, "blackbox-exporter"]))

    def test_cli_raw_auth_off_without_the_restart_restarts_the_proxies(self):
        """Raw auth on and applied; off and rendered without the restart: the proxies still require accounts."""
        self.cli("raw-auth", "on")
        self.cli("render")
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertIn("xray-" + PROXY, self.status("abcdefgh-1")["restarted"])
        self.cli("raw-auth", "off")
        self.cli("render")  # the restart is missed
        self.assertFalse((self.base / pa.APPLIED_FILE).exists())
        restarts = len(self.docker.restarts())
        self.request("abcdefgh-2", "enable", "bob")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertEqual(sorted(record["restarted"]),
                         sorted(["xray-ha", "xray-" + PROXY, "xray-" + PRIVATE, "blackbox-exporter"]))
        self.assertEqual(self.docker.restarts()[restarts:], [record["restarted"]])
        # known again: an enable that changes nothing restarts nothing, a rotation only the HA entry
        self.request("abcdefgh-3", "enable", "bob")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-3")["restarted"], [])
        self.request("abcdefgh-4", "rotate", "bob")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-4")["restarted"], ["xray-ha"])
        self.assertEqual(len(self.docker.restarts()), restarts + 2)

    def test_noop_with_a_record_that_does_not_match_the_entry_is_applied_in_full(self):
        """applied.json matches the files, but the entry runs another state: the target check catches it."""
        alice = self.cli_disable_without_restart()
        self.mark_applied()  # as if the record had survived the change
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-ha", record["restarted"])
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertGreaterEqual(self.probe.logins.count(("alice", alice)), 2)  # before the restart and after

    def test_noop_checks_its_account_and_restarts_nothing(self):
        alice = self.passwords()["alice"]
        self.request("abcdefgh-1", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual((self.status("abcdefgh-1")["status"], self.status("abcdefgh-1")["restarted"]), ("applied", []))
        self.assertEqual(self.docker.restarts(), [])
        self.assertIn(("alice", alice), self.probe.logins)
        self.probe.mode = "noproxies"  # no 204 in time: unconfirmed, still nothing restarted
        self.request("abcdefgh-2", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["stage"], record["restarted"]), ("unconfirmed", "verify", []))
        self.assertEqual(self.docker.restarts(), [])

    def test_request_target_checked_even_when_the_files_did_not_change(self):
        self.cli_disable_without_restart()
        self.docker.stale = True  # the restart does not take the config: alice still gets through
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-2")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "verify", True))
        self.assertIn("alice on ha-general: expected 407, got 204", record["error"])


class FailureTest(_Base):
    def run_failing(self, stage, restored=True):
        before = self.files()
        applied = (self.base / pa.APPLIED_FILE).read_text()
        alice = self.passwords()["alice"]
        self.request("abcdefgh-1", "disable", "alice")
        self.request("abcdefgh-2", "add", "carol")
        self.assertEqual(self.run_applier(), 0 if restored else 1)
        for request_id in ("abcdefgh-1", "abcdefgh-2"):
            record = self.status(request_id)
            self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", stage, restored))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.docker.ha_users["alice"], alice)
        self.assertEqual(os.listdir(self.spool.results), [])
        self.assertFalse((self.base / ap.BACKUP_DIR).exists())
        if restored:
            self.assertEqual((self.base / pa.APPLIED_FILE).read_text(), applied)
        self.assert_no_leaks()
        return before

    def test_render_error(self):
        example = json.loads((self.base / pa.HA_EXAMPLE).read_text())
        example["outbounds"] = [o for o in example["outbounds"] if not o["tag"].startswith("pool-")]
        (self.base / pa.HA_EXAMPLE).write_text(json.dumps(example))
        self.run_failing("render")
        self.assertIn("pool-", self.status("abcdefgh-1")["error"])
        self.assertEqual(self.docker.restarts(), [])
        self.assertEqual(self.docker.tests(), 0)

    def test_xray_test_failure(self):
        self.docker.fail_test = '"carol"'
        self.run_failing("test")
        self.assertIn("***", self.status("abcdefgh-1")["error"])  # the password in xray's output is scrubbed
        self.assertEqual(self.docker.restarts(), [])

    def test_unpinned_image(self):
        self.docker.image = "teddysun/xray:latest"
        self.run_failing("test")
        self.assertEqual(self.docker.restarts(), [])

    def raw_auth_on(self):
        accounts = pa.Accounts(self.base)
        accounts.raw_auth = True
        accounts.save()
        pa.render(accounts)
        self.mark_applied()

    def test_private_proxy_container_on_an_older_image_refused(self):
        self.docker.containers["xray-" + PRIVATE] = OLD_IMAGE_ID  # raw auth off: an admin change restarts it
        self.request("abcdefgh-1", "admin-on", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "precheck", True))
        self.assertIn(f"xray-{PRIVATE} runs another image", record["error"])
        self.assertEqual(self.docker.restarts(), [])

    def test_proxy_container_on_an_older_image_refused(self):
        self.raw_auth_on()
        self.docker.containers["xray-" + PROXY] = OLD_IMAGE_ID  # the Compose file changed, the container was restarted
        self.run_failing("precheck")
        self.assertIn(f"xray-{PROXY} runs another image than {IMAGE}: recreate it with docker compose up -d",
                      self.status("abcdefgh-1")["error"])
        self.assertEqual(self.docker.restarts(), [])

    def test_proxy_configured_with_another_image_refused(self):
        self.raw_auth_on()
        self.docker.proxy_image = "teddysun/xray:latest"
        self.run_failing("precheck")
        self.assertRegex(self.status("abcdefgh-1")["error"], r"xray-\S+ is configured with teddysun/xray:latest, not ")
        self.assertEqual(self.docker.restarts(), [])

    def test_ha_container_on_an_older_image_refused(self):
        self.docker.containers["xray-ha"] = OLD_IMAGE_ID
        self.run_failing("precheck")
        self.assertEqual(self.docker.restarts(), [])

    def test_proxy_not_restarted_is_not_checked(self):
        self.docker.containers["xray-" + PROXY] = OLD_IMAGE_ID  # raw auth off: only xray-ha restarts
        self.request("abcdefgh-1", "disable", "alice")
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")

    def test_ha_entry_not_running(self):
        self.docker.running.discard("xray-ha")
        self.run_failing("precheck")
        self.assertEqual(self.docker.restarts(), [])

    def test_restart_failure_rolls_back(self):
        self.docker.fail_restart = 1
        self.run_failing("restart")
        self.assertEqual(self.docker.restarts(), [["xray-ha"], ["xray-ha"]])  # the rollback restarts it again

    def test_verify_failure_rolls_back(self):
        self.docker.stale = True  # restarts do not take the new config: the disabled login still gets through
        before = self.run_failing("verify", restored=True)
        self.assertIn("alice on ha-general: expected 407, got 204", self.status("abcdefgh-1")["error"])
        self.assertEqual(self.docker.restarts(), [["xray-ha"], ["xray-ha"]])
        self.assertEqual(self.files(), before)

    def test_unreachable_entry_rolls_back(self):
        self.probe.mode = "down"
        self.run_failing("verify", restored=False)  # the entry stays down after the rollback too
        self.assertIn("no 407 from xray-ha in 10 s for alice on ha-general (unreachable), carol (not an admin) on "
                      "ha-private (unreachable), alice on ha-private (unreachable), a wrong password on ha-general "
                      "(unreachable), a wrong password on ha-private (unreachable)", self.status("abcdefgh-1")["error"])

    def test_entry_that_takes_connections_but_never_answers_rolls_back(self):
        self.probe.mode = "silent"  # what docker-proxy looks like in front of a port nothing listens on
        self.run_failing("verify", restored=False)
        self.assertIn("a wrong password on ha-private (noresponse)", self.status("abcdefgh-1")["error"])

    def test_rollback_restart_failure_reported(self):
        self.docker.fail_restart = 2
        self.run_failing("restart", restored=False)

    def test_after_an_unfinished_rollback_a_noop_is_restarted_and_verified(self):
        # the files are back, but the services may run anything: nothing is known to be running
        self.docker.fail_restart = 2
        self.run_failing("restart", restored=False)
        alice = self.passwords()["alice"]
        self.probe.logins.clear()
        self.request("abcdefgh-3", "enable", "alice")
        self.assertEqual(self.run_applier(), 0)
        record = self.status("abcdefgh-3")
        self.assertEqual((record["status"], record["changed"]), ("applied", False))
        self.assertIn("xray-ha", record["restarted"])
        self.assertIn(("alice", alice), self.probe.logins)

    def test_unconfirmed_keeps_the_change_and_delivers(self):
        self.probe.mode = "noproxies"  # logins are taken, no proxy answers
        self.request("abcdefgh-1", "disable", "alice")
        self.request("abcdefgh-2", "add", "carol")
        self.assertEqual(self.run_applier(), 0)
        for request_id in ("abcdefgh-1", "abcdefgh-2"):
            self.assertEqual(self.status(request_id)["status"], "unconfirmed")
        self.assertEqual(self.status("abcdefgh-1")["error"], "no 204 for svc-monitor on ha-general (noresponse), "
                         "svc-monitor on ha-private (noresponse), carol on ha-general (noresponse)")
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertTrue(self.status("abcdefgh-2")["result"])
        self.assertTrue((self.spool.results / "abcdefgh-2.json").exists())
        self.assertTrue((self.base / pa.APPLIED_FILE).exists())
        self.assert_no_leaks()

    def test_interrupted_apply_is_recovered(self):
        before = self.files()
        self.docker.crash_restart = True
        self.request("abcdefgh-1", "disable", "alice")
        with self.assertRaises(Crash):
            self.run_applier()
        self.assertEqual(self.status("abcdefgh-1")["status"], "applying")
        self.assertNotEqual(self.files(), before)
        self.assertTrue((self.base / ap.BACKUP_DIR / ap.MANIFEST).exists())
        self.docker.crash_restart = False
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.files(), before)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "interrupted", True))
        self.assertFalse((self.base / ap.BACKUP_DIR).exists())
        self.assertEqual(self.docker.restarts(), [["xray-ha"], ["xray-ha"]])  # the interrupted one, the recovery

    def test_killed_after_commit_is_finished_not_rolled_back(self):
        real_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            manifest = Path(path) / ap.MANIFEST
            if manifest.exists() and json.loads(manifest.read_text()).get("committed"):
                raise Crash()
            return real_rmtree(path, *args, **kwargs)
        self.request("abcdefgh-1", "disable", "alice")
        with mock.patch.object(ap.shutil, "rmtree", rmtree):
            with self.assertRaises(Crash):
                self.run_applier()
        self.assertEqual(self.status("abcdefgh-1")["status"], "applied")
        seq = self.status("abcdefgh-1")["applied_seq"]
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual((self.status("abcdefgh-1")["status"], self.status("abcdefgh-1")["applied_seq"]), ("applied", seq))
        self.assertFalse(pa.Accounts(self.base).enabled("alice"))
        self.assertNotIn("alice", self.docker.ha_users)
        self.assertFalse((self.base / ap.BACKUP_DIR).exists())
        self.assertEqual(self.docker.restarts(), [["xray-ha"]])  # no rollback restart
        view = json.loads((self.spool.root / "status.json").read_text())
        self.assertIsNone(view["unapplied_changes"])  # not recorded: the next batch restarts and verifies all
        self.assertFalse((self.base / pa.APPLIED_FILE).exists())

    def test_killed_before_the_results_loses_only_the_result(self):
        self.request("abcdefgh-1", "add", "carol")
        self.request("abcdefgh-2", "disable", "alice")
        with mock.patch.object(ap.Spool, "write_result", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_applier()
        self.assertEqual(self.run_applier(), 0)
        first, second = self.status("abcdefgh-1"), self.status("abcdefgh-2")
        self.assertEqual((first["status"], first["result"]), ("applied", False))
        last = json.loads((self.spool.root / ap.SEQUENCE_FILE).read_text())["last"]
        self.assertEqual((first["applied_seq"], second["applied_seq"]), (last, last))  # from the manifest
        self.assertEqual((second["status"], second["restarted"]), ("applied", ["xray-ha"]))
        self.assertNotIn("result", second)
        self.assertIn("carol", self.passwords())
        self.assertEqual(os.listdir(self.spool.results), [])
        self.assertEqual(self.docker.restarts(), [["xray-ha"]])
        self.assert_no_leaks()

    def test_unrestored_files_stop_the_run(self):
        self.docker.fail_restart = 1
        self.request("abcdefgh-1", "disable", "alice")
        self.request("abcdefgh-2", "enable", "alice")  # same account: the next batch
        os.utime(str(self.spool.requests / "abcdefgh-1.json"), (1, 1))
        with mock.patch.object(ap.Applier, "restore_files", side_effect=OSError("read-only")):
            self.assertEqual(self.run_applier(), 1)
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "restart", False))
        self.assertTrue((self.base / ap.BACKUP_DIR / ap.MANIFEST).exists())
        self.assertEqual(os.listdir(self.spool.requests), ["abcdefgh-2.json"])
        self.assertFalse((self.spool.status / "abcdefgh-2.json").exists())
        with mock.patch.object(ap.Applier, "restore_files", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):  # main() exits 2
                self.run_applier()
        self.assertEqual(os.listdir(self.spool.requests), ["abcdefgh-2.json"])
        self.assertEqual(self.run_applier(), 0)  # the files can be restored again
        self.assertEqual(self.status("abcdefgh-1")["status"], "failed")
        self.assertEqual(self.status("abcdefgh-2")["status"], "applied")
        self.assertTrue(pa.Accounts(self.base).enabled("alice"))
        self.assertFalse((self.base / ap.BACKUP_DIR).exists())

    def test_applying_without_backup_is_failed(self):
        self.spool.write_status("abcdefgh-1", {"id": "abcdefgh-1", "status": "applying"})
        self.run_applier()
        record = self.status("abcdefgh-1")
        self.assertEqual((record["status"], record["stage"], record["restored"]), ("failed", "interrupted", True))


class ProxyProbeTest(unittest.TestCase):
    """proxy_probe against a stub proxy: plain GET, CONNECT with 407, CONNECT then TLS."""

    def serve(self, handler):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)

        def loop():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    try:
                        handler(conn)
                    except OSError:
                        pass
        threading.Thread(target=loop, daemon=True).start()
        return server.getsockname()

    @staticmethod
    def head(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode()

    def test_http(self):
        seen = []

        def handler(conn):
            request = self.head(conn)
            seen.append(request)
            ok = "Proxy-Authorization: Basic YWxpY2U6cHc=" in request  # alice:pw
            conn.sendall(b"HTTP/1.1 204 No Content\r\n\r\n" if ok else
                         b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        proxy = self.serve(handler)
        self.assertEqual(ap.proxy_probe(proxy, "http://target/generate_204", "alice", "pw"), ("status", 204))
        self.assertEqual(ap.proxy_probe(proxy, "http://target/generate_204", "alice", "x"), ("status", 407))
        self.assertTrue(seen[0].startswith("GET http://target/generate_204 HTTP/1.1\r\n"))

    def test_noresponse_and_unreachable(self):
        proxy = self.serve(lambda conn: self.head(conn))
        self.assertEqual(ap.proxy_probe(proxy, "http://target/", "a", "b"), ("noresponse", None))
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        self.assertEqual(ap.proxy_probe(("127.0.0.1", port), "http://target/", "a", "b"), ("unreachable", None))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_https_through_connect(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                        "/CN=probe.test", "-addext", "subjectAltName=DNS:probe.test",
                        "-keyout", str(tmp / "key.pem"), "-out", str(tmp / "cert.pem")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(str(tmp / "cert.pem"), str(tmp / "key.pem"))
        seen = []

        def handler(conn):
            request = self.head(conn)
            seen.append(request.split("\r\n")[0])
            if "Basic YWxpY2U6cHc=" not in request:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                seen.append(self.head(tls).split("\r\n")[0])
                tls.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")
        proxy = self.serve(handler)
        client_ctx = ssl.create_default_context(cafile=str(tmp / "cert.pem"))
        url = "https://probe.test/generate_204"
        self.assertEqual(ap.proxy_probe(proxy, url, "alice", "x", context=client_ctx), ("status", 407))
        self.assertEqual(ap.proxy_probe(proxy, url, "alice", "pw", context=client_ctx), ("status", 204))
        self.assertEqual(seen, ["CONNECT probe.test:443 HTTP/1.1", "CONNECT probe.test:443 HTTP/1.1",
                                "GET /generate_204 HTTP/1.1"])
        # an untrusted certificate is no answer, not a pass
        self.assertEqual(ap.proxy_probe(proxy, url, "alice", "pw"), ("noresponse", None))


class CommandLineTest(unittest.TestCase):
    def test_settings_come_from_the_environment(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        env = dict(os.environ, PROXY_APPLY_SPOOL=str(tmp / "spool"), PROXY_APPLY_REQUESTER=str(os.getuid()),
                   PROXY_APPLY_SERVICES_DIR=str(tmp / "services"))
        script = Path(ap.__file__)
        subprocess.run([sys.executable, "-I", "-B", str(script), "init"], env=env, check=True)
        self.assertTrue((tmp / "spool" / "requests").is_dir())
        p = subprocess.run([sys.executable, "-I", "-B", str(script), "status"], env=env, check=True,
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(json.loads(p.stdout)["accounts"], [])
        env["PROXY_APPLY_REQUESTER"] = "no-such-user-proxy-apply"
        p = subprocess.run([sys.executable, "-I", "-B", str(script), "run"], env=env, stderr=subprocess.PIPE,
                           universal_newlines=True)
        self.assertEqual((p.returncode, p.stderr), (2, "error: no user 'no-such-user-proxy-apply' "
                                                        "(PROXY_APPLY_REQUESTER)\n"))


if __name__ == "__main__":
    unittest.main()
