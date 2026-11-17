# -*- coding: utf-8 -*-
"""
proxy_accounts.py: the accounts file, the configs rendered from it, and that every secret path is gitignored.

    cd services/xray/scripts && python3 -B -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proxy_accounts as pa  # noqa: E402
from XrayConfigHandler import XrayConfigHandler  # noqa: E402

SERVICES = Path(__file__).resolve().parents[3]
PUBLIC = "usca5-bwh-sla-1tb"     # listed in xray/public-lines.txt
PRIVATE = "jpty1-bwh-2tb"        # not listed: private
HTTP_PROXY_TAGS = ("http_IN", "socks_IN", "ha-all_http_IN", "ha-all_socks_IN")


def run(base, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pa.main(["--base-dir", str(base)] + list(argv))
    return code, out.getvalue(), err.getvalue()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def proxy_config():
    outbound = {"protocol": "freedom", "settings": {}, "tag": "proxy"}
    config = XrayConfigHandler.build_xray_config(outbound, loglevel="none", include_stats_api=True)
    return json.dumps(config, indent=4, ensure_ascii=False)


class NameTest(unittest.TestCase):
    def test_valid(self):
        for name in ("alice", "a", "bob.smith", "u_1-2", "0day", "svc-ha", "svc-monitor", "svc-docker-node01",
                     "x" * 64):
            self.assertEqual(pa.validate_name(name), name)

    def test_invalid(self):
        for name in ("", "Alice", "-a", ".a", "a:b", "a>>>b", "a b", "x" * 65, "svc-", "svc-foo", "svc-docker-",
                     "svc-docker", "svc-ha2", "ä"):
            with self.assertRaises(pa.AccountsError, msg=name):
                pa.validate_name(name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        for rel in (pa.HA_EXAMPLE, pa.BLACKBOX_EXAMPLE, pa.PUBLIC_LINES_FILE):
            (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SERVICES / rel, self.base / rel)

    def line(self, name, text=None):
        path = self.base / "xray" / name / "config" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(raw_line_config() if text is None else text)
        return path

    def accounts(self):
        return json.loads((self.base / pa.ACCOUNTS_FILE).read_text())

    def passwords(self):
        return {name: entry["password"] for name, entry in self.accounts()["accounts"].items()}

    def add(self, *names):
        for name in names:
            self.assertEqual(run(self.base, "add", name)[0], 0)


class AccountsFileTest(_Base):
    def test_add_rotate_remove_list(self):
        code, out, _ = run(self.base, "add", "alice")
        self.assertEqual((code, out), (0, ""))  # no password without --show
        path = self.base / pa.ACCOUNTS_FILE
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(path.parent), 0o700)
        first = self.passwords()["alice"]
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(self.accounts()["raw_auth"], False)

        code, out, _ = run(self.base, "rotate", "alice", "--show")
        second = self.passwords()["alice"]
        self.assertEqual((code, out), (0, second + "\n"))
        self.assertNotEqual(first, second)

        code, out, _ = run(self.base, "add", "bob", "--show")
        self.assertEqual(out, self.passwords()["bob"] + "\n")
        code, out, _ = run(self.base, "list")
        self.assertEqual(out, "alice\nbob\n")

        self.assertEqual(run(self.base, "add", "alice")[0], 1)       # exists
        self.assertEqual(run(self.base, "rotate", "carol")[0], 1)    # missing
        self.assertEqual(run(self.base, "add", "Carol")[0], 1)       # invalid
        self.assertEqual(run(self.base, "remove", "alice")[0], 0)
        self.assertEqual(list(self.passwords()), ["bob"])
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [".lock", "accounts.json"])  # no temp files

    def test_raw_auth_setting(self):
        self.assertEqual(run(self.base, "raw-auth")[1], "off\n")
        run(self.base, "raw-auth", "on")
        self.assertEqual(run(self.base, "raw-auth")[1], "on\n")
        self.assertIs(self.accounts()["raw_auth"], True)


class FormatTest(_Base):
    V1 = {"raw_auth": True, "accounts": {"alice": "pw-a", "svc-ha": "pw-h", "svc-monitor": "pw-m"}}

    def write(self, data):
        path = self.base / pa.ACCOUNTS_FILE
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def test_version_1_file_is_read_and_migrated(self):
        self.write(self.V1)
        accounts = pa.Accounts(self.base)
        self.assertEqual((accounts.raw_auth, accounts.accounts, accounts.disabled), (True, self.V1["accounts"], set()))
        self.assertEqual(accounts.pairs(), sorted(self.V1["accounts"].items()))
        self.assertEqual(run(self.base, "list")[1], "alice\nsvc-ha\nsvc-monitor\n")  # reading does not rewrite
        self.assertEqual(json.loads((self.base / pa.ACCOUNTS_FILE).read_text()), self.V1)
        self.assertEqual(run(self.base, "disable", "alice")[0], 0)  # the first change writes version 3
        self.assertEqual(self.accounts(), {"version": 3, "raw_auth": True, "accounts": {
            "alice": {"password": "pw-a", "enabled": False, "admin": False},
            "svc-ha": {"password": "pw-h", "enabled": True, "admin": False},
            "svc-monitor": {"password": "pw-m", "enabled": True, "admin": False}}})

    def test_version_2_file_is_read_and_migrated(self):
        v2 = {"version": 2, "raw_auth": False, "accounts": {
            "alice": {"password": "pw-a", "enabled": False}, "bob": {"password": "pw-b", "enabled": True},
            "svc-ha": {"password": "pw-h", "enabled": True}, "svc-monitor": {"password": "pw-m", "enabled": True}}}
        self.write(v2)
        accounts = pa.Accounts(self.base)
        self.assertEqual((accounts.disabled, accounts.admins), ({"alice"}, set()))
        self.assertEqual(run(self.base, "list")[1], "alice\nbob\nsvc-ha\nsvc-monitor\n")
        self.assertEqual(json.loads((self.base / pa.ACCOUNTS_FILE).read_text()), v2)  # reading does not rewrite
        self.assertEqual(run(self.base, "render")[0], 0)  # a version 2 file renders: no admins
        ha = json.loads((self.base / pa.HA_CONFIG).read_text())
        users = {i["tag"]: [u["user"] for u in i["settings"].get("users", [])] for i in ha["inbounds"]}
        self.assertEqual(users["ha-all_http_IN"], ["svc-monitor"])
        self.assertEqual(run(self.base, "admin", "bob", "on")[0], 0)  # the first change writes version 3
        self.assertEqual(self.accounts(), {"version": 3, "raw_auth": False, "accounts": {
            "alice": {"password": "pw-a", "enabled": False, "admin": False},
            "bob": {"password": "pw-b", "enabled": True, "admin": True},
            "svc-ha": {"password": "pw-h", "enabled": True, "admin": False},
            "svc-monitor": {"password": "pw-m", "enabled": True, "admin": False}}})

    def test_version_1_and_2_render_the_same(self):
        self.write(self.V1)
        self.assertEqual(run(self.base, "render")[0], 0)
        first = {rel: (self.base / rel).read_text() for rel in (pa.HA_CONFIG, pa.BLACKBOX_CONFIG)}
        accounts = pa.Accounts(self.base)
        accounts.save()
        self.assertEqual(self.accounts()["version"], 3)
        self.assertEqual(pa.render_outputs(pa.Accounts(self.base)), [])
        self.assertEqual({rel: (self.base / rel).read_text() for rel in first}, first)

    def test_bad_files_are_refused(self):
        bad = [
            {"version": 4, "accounts": {"alice": {"password": "x", "enabled": True, "admin": False}}},
            {"version": 3, "accounts": {"alice": {"password": "x", "enabled": True}}},
            {"version": 3, "accounts": {"alice": {"password": "x", "enabled": True, "admin": "yes"}}},
            {"version": 3, "accounts": {"alice": {"password": "x", "enabled": True, "admin": False, "x": 1}}},
            {"version": 3, "accounts": {"svc-docker-n1": {"password": "x", "enabled": True, "admin": True}}},
            {"version": 3, "accounts": {"svc-monitor": {"password": "x", "enabled": True, "admin": True}}},
            {"version": 2, "accounts": {"alice": {"password": "x", "enabled": True, "admin": True}}},
            {"version": 2, "accounts": {"alice": "pw"}},
            {"accounts": {"alice": {"password": "x", "enabled": True}}},  # version 1 with version 2 entries
            {"version": 2, "accounts": {"alice": {"password": "x", "enabled": "no"}}},
            {"version": 2, "accounts": {"Alice": {"password": "x", "enabled": True}}},
            {"version": 2, "accounts": {"svc-monitor": {"password": "x", "enabled": False}}},
            {"version": 2, "accounts": {"svc-ha": {"password": "x", "enabled": False}}},
            {"version": 2, "accounts": ["alice"]},
            ["alice"],
        ]
        for data in bad:
            self.write(data)
            with self.assertRaises(pa.AccountsError, msg=data):
                pa.Accounts(self.base)
            text = (self.base / pa.ACCOUNTS_FILE).read_text()
            self.assertEqual(run(self.base, "add", "bob")[0], 1)
            self.assertEqual((self.base / pa.ACCOUNTS_FILE).read_text(), text)  # never overwritten


class EnableTest(_Base):
    def test_disable_enable(self):
        self.add("alice", "bob", "svc-monitor", "svc-ha", "svc-docker-node01")
        before = self.passwords()
        code, _, err = run(self.base, "disable", "alice")
        self.assertEqual(code, 0)
        self.assertIn("the public lines' own entries still accept it", err)
        self.assertEqual(self.passwords(), before)  # the password is kept
        self.assertFalse(self.accounts()["accounts"]["alice"]["enabled"])
        self.assertEqual(run(self.base, "disable", "alice")[0], 0)  # idempotent
        self.assertEqual(run(self.base, "disable", "svc-docker-node01")[0], 0)
        run(self.base, "raw-auth", "on")
        self.assertNotIn("still accept", run(self.base, "disable", "bob")[2])
        self.assertEqual(run(self.base, "enable", "bob")[0], 0)
        self.assertEqual(run(self.base, "enable", "carol")[0], 1)
        self.assertEqual(run(self.base, "disable", "carol")[0], 1)
        accounts = pa.Accounts(self.base)
        self.assertEqual(accounts.disabled, {"alice", "svc-docker-node01"})
        self.assertEqual([n for n, _ in accounts.pairs()], ["bob", "svc-ha", "svc-monitor"])
        self.assertEqual(run(self.base, "remove", "alice")[0], 0)  # a disabled account can be removed
        self.assertNotIn("alice", self.accounts()["accounts"])
        self.assertEqual(run(self.base, "add", "alice")[0], 0)  # and comes back enabled
        self.assertTrue(self.accounts()["accounts"]["alice"]["enabled"])

    def test_service_accounts_protected(self):
        self.add("svc-monitor", "svc-ha")
        for command in ("disable", "remove"):
            for name in pa.PROTECTED:
                code, _, err = run(self.base, command, name)
                self.assertEqual(code, 1, (command, name))
                self.assertIn("cannot be", err)
        self.assertEqual(set(self.accounts()["accounts"]), {"svc-monitor", "svc-ha"})
        self.assertTrue(all(e["enabled"] for e in self.accounts()["accounts"].values()))
        self.assertEqual(run(self.base, "rotate", "svc-monitor")[0], 0)  # the CLI still rotates them
        accounts = pa.Accounts(self.base)
        for name in pa.PROTECTED:
            with self.assertRaises(pa.AccountsError):
                accounts.set_enabled(name, False)
            with self.assertRaises(pa.AccountsError):
                accounts.remove(name)

    def test_render_leaves_disabled_accounts_out(self):
        raw = self.line(PUBLIC)
        self.add("alice", "bob", "svc-monitor", "svc-ha")
        run(self.base, "disable", "alice")
        for raw_auth in ("off", "on"):
            run(self.base, "raw-auth", raw_auth)
            self.assertEqual(run(self.base, "render")[0], 0)
            configs = [json.loads((self.base / pa.HA_CONFIG).read_text())]
            if raw_auth == "on":
                configs.append(json.loads(raw.read_text()))
            for config in configs:
                for inbound in config["inbounds"]:
                    if inbound["protocol"] in ("http", "socks") and inbound["tag"] in pa.GENERAL_INBOUNDS:
                        users = [u["user"] for u in inbound["settings"]["users"]]
                        self.assertIn("bob", users)
                        self.assertNotIn("alice", users)
            self.assertNotIn(self.passwords()["alice"], (self.base / pa.HA_CONFIG).read_text())
            self.assertNotIn(self.passwords()["alice"], raw.read_text())


class StatusTest(_Base):
    def test_status_json_has_no_passwords(self):
        self.add("alice", "bob", "svc-monitor", "svc-ha")
        self.line(PUBLIC)
        self.line(PRIVATE)
        run(self.base, "disable", "bob")
        run(self.base, "admin", "alice", "on")
        code, out, _ = run(self.base, "status", "--json")
        self.assertEqual(code, 0)
        for password in self.passwords().values():
            self.assertNotIn(password, out)
        view = json.loads(out)
        text = (self.base / pa.ACCOUNTS_FILE).read_text()
        self.assertEqual(view, {
            "raw_auth": False, "disabled_refused_at": ["ha-general", "ha-all", "private-lines"],
            "accounts": [
                {"name": "alice", "kind": "user", "enabled": True, "admin": True, "protected": False},
                {"name": "bob", "kind": "user", "enabled": False, "admin": False, "protected": False},
                {"name": "svc-ha", "kind": "service", "enabled": True, "admin": False, "protected": True},
                {"name": "svc-monitor", "kind": "service", "enabled": True, "admin": False, "protected": True}],
            "lines": [{"name": PRIVATE, "access": "admin"}, {"name": PUBLIC, "access": "public"}],
            "accounts_sha256": pa.sha256_text(text), "applied": None, "unapplied_changes": None})
        (self.base / pa.APPLIED_FILE).write_text(json.dumps({"accounts_sha256": pa.sha256_text(text),
                                                             "applied_at": "2000-01-01T00:00:00Z"}))
        view = json.loads(run(self.base, "status", "--json")[1])
        self.assertIs(view["unapplied_changes"], False)
        run(self.base, "raw-auth", "on")
        view = json.loads(run(self.base, "status", "--json")[1])
        self.assertEqual((view["unapplied_changes"], view["disabled_refused_at"]),
                         (True, ["ha-general", "ha-all", "private-lines", "public-lines"]))
        code, out, _ = run(self.base, "status")
        self.assertIn("alice                            user     enabled  admin", out)
        self.assertIn(f"line {PRIVATE:<40} admins only", out)
        self.assertIn("bob", out)
        self.assertNotIn(self.passwords()["bob"], out)

    def test_status_without_file(self):
        view = json.loads(run(self.base, "status", "--json")[1])
        self.assertEqual((view["accounts"], view["accounts_sha256"]), ([], None))


class LockTest(_Base):
    def test_changes_wait_for_the_lock(self):
        self.add("svc-monitor")
        script = ("import sys; sys.path.insert(0, %r); import proxy_accounts as pa, time\n"
                  "with pa.accounts_lock(%r):\n    print('locked', flush=True); time.sleep(1.5)\n"
                  % (str(Path(pa.__file__).parent), str(self.base)))
        holder = subprocess.Popen([sys.executable, "-B", "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        start = __import__("time").monotonic()
        code, _, err = run(self.base, "add", "alice")
        self.assertEqual(code, 0)
        self.assertIn("waiting for", err)
        self.assertGreater(__import__("time").monotonic() - start, 0.5)
        holder.stdout.close()
        self.assertEqual(run(self.base, "list")[1], "alice\nsvc-monitor\n")


class RenderTest(_Base):
    def setUp(self):
        super().setUp()
        self.raw_text = raw_line_config()
        self.raw = self.line(PUBLIC)
        self.private = self.line(PRIVATE)

    def ha(self):
        return json.loads((self.base / pa.HA_CONFIG).read_text())

    def inbound_users(self, config):
        users = {}
        for inbound in config["inbounds"]:
            if inbound["protocol"] in ("http", "socks"):
                key = inbound["tag"] if inbound["tag"].startswith("ha-all") else inbound["protocol"]
                users[key] = {u["user"]: u["pass"] for u in inbound["settings"].get("users", [])}
        return users

    def outbound_users(self, config):
        return {o["tag"]: [u["user"] for u in o["settings"]["servers"][0].get("users", [])]
                for o in config["outbounds"][1:]}

    def test_requires_service_accounts(self):
        self.add("alice")
        code, _, err = run(self.base, "render")
        self.assertEqual(code, 1)
        self.assertIn("svc-monitor", err)
        self.assertFalse((self.base / pa.HA_CONFIG).exists())
        self.add("svc-monitor")
        code, _, err = run(self.base, "render")  # the private lines always need svc-ha
        self.assertEqual(code, 1)
        self.assertIn("svc-ha", err)
        self.assertFalse((self.base / pa.HA_CONFIG).exists())

    def test_render_without_raw_auth(self):
        """The four entry types: ha-general, ha-all, a private line's own entry, a public line's own entry."""
        self.add("alice", "bob", "carol", "svc-monitor", "svc-ha", "svc-docker-node01")
        run(self.base, "admin", "alice", "on")
        run(self.base, "admin", "carol", "on")
        run(self.base, "disable", "carol")  # a disabled admin is refused everywhere
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [str(pa.HA_CONFIG), str(pa.BLACKBOX_CONFIG),
                                       f"xray/{PRIVATE}/config/config.json"])  # the public line untouched
        self.assertEqual(self.raw.read_text(), self.raw_text)
        pw = self.passwords()
        ha = self.ha()
        general = {k: pw[k] for k in ("alice", "bob", "svc-docker-node01", "svc-monitor")}
        admins = {k: pw[k] for k in ("alice", "svc-monitor")}
        self.assertEqual(self.inbound_users(ha), {"http": general, "socks": general,
                                                  "ha-all_http_IN": admins, "ha-all_socks_IN": admins})
        for inbound in ha["inbounds"]:
            if inbound["protocol"] == "socks":
                self.assertEqual((inbound["settings"]["auth"], inbound["settings"]["udp"]), ("password", True))
        # The HA entry logs in as svc-ha to the private lines only
        relay = self.outbound_users(ha)
        self.assertEqual(sorted(t for t, users in relay.items() if users == ["svc-ha"]),
                         sorted(t for t in relay if t.startswith("private-")))
        self.assertEqual(len(relay), 9)
        self.assertTrue(all(users in ([], ["svc-ha"]) for users in relay.values()))
        private = self.inbound_users(json.loads(self.private.read_text()))
        line_users = {k: pw[k] for k in ("alice", "svc-ha", "svc-monitor")}
        self.assertEqual(private, {"http": line_users, "socks": line_users})
        self.assertEqual(mode(self.private), 0o600)
        # Apart from the users, the rendered configs are the example and the generated line
        example = json.loads((self.base / pa.HA_EXAMPLE).read_text())
        for config in (ha, example):
            for inbound in config["inbounds"]:
                inbound["settings"].pop("users", None)
            for outbound in config["outbounds"][1:]:
                outbound["settings"]["servers"][0].pop("users", None)
        self.assertEqual(ha, example)
        line = json.loads(self.private.read_text())
        XrayConfigHandler.set_inbound_users(line, None)
        self.assertEqual(json.dumps(line, indent=4, ensure_ascii=False), self.raw_text)
        for rel in (pa.HA_CONFIG, pa.BLACKBOX_CONFIG):
            self.assertEqual(mode(self.base / rel), 0o600)
        self.assertEqual(run(self.base, "render")[1], "")  # nothing changed

    def test_admin_changes_reach_ha_all_and_the_private_lines(self):
        self.add("alice", "bob", "svc-monitor", "svc-ha")
        run(self.base, "render")
        public_before = self.raw.read_text()
        for command, expected in ((("admin", "bob", "on"), {"bob", "svc-monitor"}),
                                  (("rotate", "bob"), {"bob", "svc-monitor"}),
                                  (("disable", "bob"), {"svc-monitor"}),
                                  (("enable", "bob"), {"bob", "svc-monitor"}),
                                  (("admin", "bob", "off"), {"svc-monitor"})):
            self.assertEqual(run(self.base, *command)[0], 0)
            code, out, _ = run(self.base, "render")
            self.assertEqual(code, 0)
            self.assertIn(f"xray/{PRIVATE}/config/config.json", out.split(), command)
            users = self.inbound_users(self.ha())
            self.assertEqual(set(users["ha-all_http_IN"]), expected, command)
            self.assertEqual(set(users["ha-all_socks_IN"]), expected, command)
            self.assertEqual(set(self.inbound_users(json.loads(self.private.read_text()))["http"]),
                             (expected - {"svc-monitor"}) | {"svc-ha", "svc-monitor"}, command)
            self.assertEqual(users["http"].get("bob"), self.passwords()["bob"] if command[0] != "disable" else None)
        self.assertEqual(self.raw.read_text(), public_before)  # raw auth off: the public line never changes
        # A non-admin change touches neither ha-all nor the private lines
        private_before = self.private.read_text()
        run(self.base, "rotate", "alice")
        out = run(self.base, "render")[1].split()
        self.assertEqual(out, [str(pa.HA_CONFIG)])
        self.assertEqual(self.private.read_text(), private_before)

    def test_render_with_raw_auth_and_back(self):
        self.add("alice", "boss", "svc-monitor", "svc-ha")
        run(self.base, "admin", "boss", "on")
        run(self.base, "render")
        private_text = self.private.read_text()
        run(self.base, "raw-auth", "on")
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertIn(f"xray/{PUBLIC}/config/config.json", out.split())
        self.assertNotIn(f"xray/{PRIVATE}/config/config.json", out.split())  # raw auth does not change it
        self.assertEqual(self.private.read_text(), private_text)
        accounts = self.passwords()
        raw = json.loads(self.raw.read_text())
        self.assertEqual(self.inbound_users(raw), {"http": accounts, "socks": accounts})
        self.assertEqual(mode(self.raw), 0o600)
        relay = [o for o in self.ha()["outbounds"] if o["tag"].startswith(("pool-", "private-"))]
        self.assertEqual(len(relay), 9)
        for outbound in relay:
            self.assertEqual(outbound["settings"]["servers"][0]["users"],
                             [{"user": "svc-ha", "pass": accounts["svc-ha"]}])
        blackbox = (self.base / pa.BLACKBOX_CONFIG).read_text()
        self.assertIn("proxy_url: socks5://svc-monitor:%s@xray-usca5-bwh-sla-1tb:1089" % accounts["svc-monitor"],
                      blackbox)

        run(self.base, "raw-auth", "off")
        code, out, _ = run(self.base, "render")
        self.assertIn(f"xray/{PUBLIC}/config/config.json", out.split())
        self.assertEqual(self.raw.read_text(), self.raw_text)  # byte-identical to the generated file
        self.assertEqual(self.private.read_text(), private_text)
        relay = self.outbound_users(self.ha())
        self.assertEqual({t: u for t, u in relay.items() if u}, {t: ["svc-ha"] for t in relay if t.startswith("private-")})

    def test_raw_config_without_inbounds_fails_before_writing(self):
        self.add("svc-monitor", "svc-ha")
        bad = self.base / "xray/proxy-b/config/config.json"
        bad.parent.mkdir(parents=True)
        bad.write_text('{"inbounds": [{"protocol": "http", "settings": {}}]}')
        run(self.base, "raw-auth", "on")
        code, _, err = run(self.base, "render")
        self.assertEqual(code, 1)
        self.assertIn("socks", err)
        self.assertFalse((self.base / pa.HA_CONFIG).exists())
        self.assertEqual(self.raw.read_text(), self.raw_text)

    def test_raw_auth_off_leaves_other_configs_alone(self):
        self.add("svc-monitor", "svc-ha")
        odd = self.base / "xray/us-west-racknerd-12tb/config/config.json"  # a public line
        odd.parent.mkdir(parents=True)
        text = '{"inbounds": [{"protocol": "socks", "settings": {"udp": true}}, {"protocol": "vmess"}]}'
        odd.write_text(text)
        odd.chmod(0o644)
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertNotIn("xray/us-west-racknerd-12tb/config/config.json", out.split())
        self.assertEqual(odd.read_text(), text)
        self.assertEqual(mode(odd), 0o644)

    def test_blackbox(self):
        self.add("svc-monitor", "svc-ha")
        data = self.accounts()
        data["accounts"]["svc-monitor"]["password"] = "p/w@x:y%z"
        (self.base / pa.ACCOUNTS_FILE).write_text(json.dumps(data))
        run(self.base, "render")
        example = (self.base / pa.BLACKBOX_EXAMPLE).read_text().splitlines()
        rendered = (self.base / pa.BLACKBOX_CONFIG).read_text().splitlines()
        self.assertTrue(rendered[0].startswith("# Rendered by"))
        rendered = rendered[1:]
        self.assertEqual(len(rendered), len(example))
        auth = "svc-monitor:" + urllib.parse.quote("p/w@x:y%z", safe="") + "@"
        self.assertEqual(auth, "svc-monitor:p%2Fw%40x%3Ay%25z@")
        changed = [(a, b) for a, b in zip(example, rendered) if a != b]
        hosts = [("http", "xray-ha:8889"), ("socks5", "xray-ha:1089"), ("http", "xray-ha:8890"),
                 ("socks5", "xray-ha:1090")]
        for line in ("jpty1-bwh-2tb", "miyaip-uk-01_relay_jpty1-bwh-2tb", "miyaip-uk-01_relay_usca5-bwh-sla-2tb",
                     "miyaip-jp-01_relay_usca5-bwh-sla-2tb", "miyaip-jp-01_relay_jpty1-bwh-2tb"):
            hosts += [("http", f"xray-{line}:8889"), ("socks5", f"xray-{line}:1089")]
        self.assertEqual(changed, [(f"      proxy_url: {s}://svc-monitor:PASSWORD@{h}", f"      proxy_url: {s}://{auth}{h}")
                                   for s, h in hosts])
        self.assertEqual(sum("xray-ha:" in line for line in rendered), 4)
        self.assertFalse([line for line in rendered if "proxy_url:" in line and "PASSWORD" in line])
        # the public lines: no account while raw auth is off
        self.assertIn("      proxy_url: socks5://xray-usca5-bwh-sla-1tb:1089", rendered)


class ExampleTest(unittest.TestCase):
    def test_example_placeholders_cannot_log_in(self):
        # Basic auth splits user:pass at the first ':', so no HTTP client can send a user name holding one;
        # an empty list would turn HTTP authentication off, so the HTTP placeholder stays.
        config = json.loads((SERVICES / pa.HA_EXAMPLE).read_text())
        inbounds = [i for i in config["inbounds"] if i["protocol"] in ("http", "socks")]
        self.assertEqual(sorted(i["tag"] for i in inbounds), sorted(HTTP_PROXY_TAGS))
        for inbound in inbounds:
            settings = inbound["settings"]
            if inbound["protocol"] == "http":
                self.assertTrue(settings["users"])
                for user in settings["users"]:
                    self.assertIn(":", user["user"])
            else:
                self.assertEqual((settings["auth"], settings["users"]), ("password", []))

    def test_example_passes_the_isolation_check(self):
        config = json.loads((SERVICES / pa.HA_EXAMPLE).read_text())
        lines = pa.check_ha(config, pa.public_lines(SERVICES))
        self.assertEqual(sorted(lines.values()), sorted(
            pa.public_lines(SERVICES) | {"jpty1-bwh-2tb", "miyaip-uk-01_relay_jpty1-bwh-2tb",
                                         "miyaip-uk-01_relay_usca5-bwh-sla-2tb", "miyaip-jp-01_relay_usca5-bwh-sla-2tb",
                                         "miyaip-jp-01_relay_jpty1-bwh-2tb"}))
        self.assertEqual(len(lines), 9)


class IsolationTest(unittest.TestCase):
    """check_ha refuses every HA example in which ha-general could reach a private line."""

    def setUp(self):
        self.example = json.loads((SERVICES / pa.HA_EXAMPLE).read_text())
        self.public = pa.public_lines(SERVICES)

    def refused(self, change, message):
        config = json.loads(json.dumps(self.example))
        change(config)
        with self.assertRaises(pa.AccountsError) as ctx:
            pa.check_ha(config, self.public)
        self.assertIn(message, str(ctx.exception))

    def outbound(self, config, tag):
        return next(o for o in config["outbounds"] if o["tag"] == tag)

    def rule(self, config, tag):
        return next(r for r in config["routing"]["rules"] if tag in r["inboundTag"])

    def inbound(self, config, tag):
        return next(i for i in config["inbounds"] if i["tag"] == tag)

    def balancer(self, config, tag):
        return next(b for b in config["routing"]["balancers"] if b["tag"] == tag)

    def test_mutations_refused(self):
        def private_as_pool(c):
            self.outbound(c, "private-jpty1-bwh-2tb")["tag"] = "pool-jpty1-bwh-2tb"
        self.refused(private_as_pool, "not in xray/public-lines.txt")

        def private_under_pool_prefix(c):
            self.outbound(c, "private-jpty1-bwh-2tb")["tag"] = "pool-private-jpty1-bwh-2tb"
        self.refused(private_under_pool_prefix, "not in xray/public-lines.txt")

        def pool_to_private_line(c):
            self.outbound(c, "pool-usca5-bwh-sla-1tb")["settings"]["servers"][0]["address"] = "xray-jpty1-bwh-2tb"
        self.refused(pool_to_private_line, "not in xray/public-lines.txt")

        def general_selects_private(c):
            self.balancer(c, "ha-general")["selector"] = ["p"]
        self.refused(general_selects_private, "ha-general must select only pool-*")

        def general_selects_too(c):
            self.balancer(c, "ha-general")["selector"].append("private-")
        self.refused(general_selects_too, "ha-general must select only pool-*")

        def general_routed_to_all(c):
            self.rule(c, "http_IN")["balancerTag"] = "ha-all"
        self.refused(general_routed_to_all, "by inboundTag only")

        def general_routed_to_outbound(c):
            rule = self.rule(c, "socks_IN")
            del rule["balancerTag"]
            rule["outboundTag"] = "private-jpty1-bwh-2tb"
        self.refused(general_routed_to_outbound, "by inboundTag only")

        def rule_without_inbound(c):
            c["routing"]["rules"].insert(1, {"type": "field", "domain": ["example.com"],
                                             "outboundTag": "private-jpty1-bwh-2tb"})
        self.refused(rule_without_inbound, "inboundTag")

        def general_rule_with_condition(c):
            self.rule(c, "http_IN")["domain"] = ["example.com"]
        self.refused(general_rule_with_condition, "by inboundTag only")

        def mixed_rule(c):
            self.rule(c, "http_IN")["inboundTag"].append("ha-all_http_IN")
            self.rule(c, "ha-all_socks_IN")["inboundTag"].remove("ha-all_http_IN")
        self.refused(mixed_rule, "inbound")

        def default_not_blackhole(c):
            c["outbounds"].insert(0, c["outbounds"].pop(5))
        self.refused(default_not_blackhole, "first outbound must be the blackhole")

        def extra_outbound(c):
            c["outbounds"].append({"protocol": "freedom", "tag": "direct"})
        self.refused(extra_outbound, "unexpected outbound")

        def extra_inbound(c):
            c["inbounds"].append({"protocol": "socks", "port": 1091, "tag": "socks2_IN", "settings": {}})
        self.refused(extra_inbound, "exactly")

        def unrouted(c):
            self.rule(c, "ha-all_http_IN")["inboundTag"] = ["ha-all_socks_IN"]
        self.refused(unrouted, "no routing rule for ha-all_http_IN")

        def all_misses_private(c):
            self.balancer(c, "ha-all")["selector"] = ["pool-"]
        self.refused(all_misses_private, "ha-all must select every line")

        def observatory_misses_private(c):
            c["observatory"]["subjectSelector"] = ["pool-"]
        self.refused(observatory_misses_private, "observatory must cover every line")

        def no_fallback(c):
            self.balancer(c, "ha-all")["fallbackTag"] = "pool-usca5-bwh-sla-1tb"
        self.refused(no_fallback, "fall back to the blackhole")

        def private_tag_to_public_line(c):
            self.outbound(c, "private-jpty1-bwh-2tb")["settings"]["servers"][0]["address"] = "xray-usca6-bwh-1tb"
        self.refused(private_tag_to_public_line, "public lines take pool-*")

        def relay_through_ha(c):
            self.outbound(c, "private-jpty1-bwh-2tb")["settings"]["servers"][0]["address"] = "xray-ha"
        self.refused(relay_through_ha, "SOCKS to one xray-<line>")

    def test_paths_besides_the_proxy_inbounds_refused(self):
        def tunnel_to_all(c):
            c["inbounds"].append({"protocol": "dokodemo-door", "port": 2000, "tag": "fwd",
                                  "settings": {"address": "target", "port": 80}})
            c["routing"]["rules"].append({"type": "field", "inboundTag": ["fwd"], "balancerTag": "ha-all"})
        self.refused(tunnel_to_all, "the inbounds must be exactly")

        def mixed_inbound_to_all(c):
            c["inbounds"].append({"protocol": "mixed", "port": 1091, "tag": "mixed_IN", "settings": {}})
            c["routing"]["rules"].append({"type": "field", "inboundTag": ["mixed_IN"], "balancerTag": "ha-all"})
        self.refused(mixed_inbound_to_all, "the inbounds must be exactly")

        def duplicate_inbound(c):
            c["inbounds"].append(json.loads(json.dumps(c["inbounds"][2])))
            c["inbounds"][-1]["port"] = 8891
        self.refused(duplicate_inbound, "the inbounds must be exactly")

        def proxy_tag_on_other_protocol(c):
            c["inbounds"][2]["protocol"] = "mixed"
        self.refused(proxy_tag_on_other_protocol, "must be http")

        def api_as_open_proxy(c):
            self.inbound(c, "api")["protocol"] = "http"
        self.refused(api_as_open_proxy, "must be dokodemo-door or tunnel")

        def api_rule_to_all(c):
            rule = self.rule(c, "api")
            del rule["outboundTag"]
            rule["balancerTag"] = "ha-all"
        self.refused(api_rule_to_all, "the only rule for inbounds other than the proxy inbounds")

        def api_rule_to_private(c):
            self.rule(c, "api")["outboundTag"] = "private-jpty1-bwh-2tb"
        self.refused(api_rule_to_private, "the only rule for inbounds other than the proxy inbounds")

        def api_in_proxy_rule(c):
            c["routing"]["rules"].remove(self.rule(c, "api"))
            self.rule(c, "ha-all_http_IN")["inboundTag"].append("api")
        self.refused(api_in_proxy_rule, "by inboundTag only")

        def unknown_tag_rule(c):
            c["routing"]["rules"].insert(0, {"type": "field", "inboundTag": ["ghost"],
                                             "outboundTag": "private-jpty1-bwh-2tb"})
        self.refused(unknown_tag_rule, "the only rule for inbounds other than the proxy inbounds")

        def api_retagged(c):
            c["api"]["tag"] = "private-x"
        self.refused(api_retagged, "the API must be tagged api")

        def reverse_portal(c):
            c["reverse"] = {"portals": [{"tag": "pool-portal", "domain": "example.com"}]}
        self.refused(reverse_portal, "unexpected top-level keys reverse")

        def dialer_proxy(c):
            self.outbound(c, "pool-usca5-bwh-sla-1tb")["streamSettings"] = {
                "sockopt": {"dialerProxy": "private-jpty1-bwh-2tb"}}
        self.refused(dialer_proxy, "may set only protocol, tag and settings")

        def proxy_settings(c):
            self.outbound(c, "pool-usca5-bwh-sla-1tb")["proxySettings"] = {"tag": "private-jpty1-bwh-2tb"}
        self.refused(proxy_settings, "may set only protocol, tag and settings")

        def server_users(c):
            self.outbound(c, "pool-usca5-bwh-sla-1tb")["settings"]["servers"][0]["users"] = [{"user": "u"}]
        self.refused(server_users, "may set only protocol, tag and settings")

        def blackhole_chained(c):
            c["outbounds"][0]["proxySettings"] = {"tag": "private-jpty1-bwh-2tb"}
        self.refused(blackhole_chained, "the blackhole may set only")

    def test_unlisted_line_is_private(self):
        # Take a public line off the list: its pool-* outbound is refused, so it cannot stay in ha-general
        with self.assertRaises(pa.AccountsError):
            pa.check_ha(self.example, self.public - {"usca6-bwh-1tb"})


class LinesTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        (self.base / "xray").mkdir()

    def write(self, text):
        (self.base / pa.PUBLIC_LINES_FILE).write_text(text)

    def test_committed_list(self):
        self.assertEqual(pa.public_lines(SERVICES), {"usca5-bwh-sla-1tb", "usca1-bwh-megaboxpro-2tb",
                                                     "usca6-bwh-1tb", "us-west-racknerd-12tb"})

    def test_parse(self):
        self.write("# comment\n\nline-a  # trailing\n  line_b\n")
        self.assertEqual(pa.public_lines(self.base), {"line-a", "line_b"})
        self.write("")
        self.assertEqual(pa.public_lines(self.base), set())  # nothing public: every line private

    def test_refused(self):
        for text, message in (("Line-A\n", "invalid"), ("a b\n", "invalid"), ("x\nx\n", "twice"),
                              ("-a\n", "invalid"), ("a.b\n", "invalid")):
            self.write(text)
            with self.assertRaises(pa.AccountsError, msg=text) as ctx:
                pa.public_lines(self.base)
            self.assertIn(message, str(ctx.exception))

    def test_missing_file_is_an_error(self):
        with self.assertRaises(pa.AccountsError) as ctx:
            pa.public_lines(self.base)
        self.assertIn("missing", str(ctx.exception))


class AdminTest(_Base):
    def test_admin_on_off(self):
        self.add("alice", "bob", "svc-monitor", "svc-ha", "svc-docker-node01")
        self.assertEqual(run(self.base, "admin", "alice", "on")[0], 0)
        self.assertEqual(run(self.base, "admin", "alice", "on")[0], 0)  # idempotent
        self.assertTrue(self.accounts()["accounts"]["alice"]["admin"])
        self.assertEqual(run(self.base, "list")[1], "alice admin\nbob\nsvc-docker-node01\nsvc-ha\nsvc-monitor\n")
        for name in ("svc-monitor", "svc-ha", "svc-docker-node01"):
            code, _, err = run(self.base, "admin", name, "on")
            self.assertEqual(code, 1, name)
            self.assertIn("cannot be an admin", err)
            self.assertEqual(run(self.base, "admin", name, "off")[0], 0)  # off is a no-op
        self.assertEqual(run(self.base, "admin", "carol", "on")[0], 1)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            pa.main(["--base-dir", str(self.base), "admin", "alice", "maybe"])
        self.assertEqual(run(self.base, "admin", "alice", "off")[0], 0)
        self.assertFalse(self.accounts()["accounts"]["alice"]["admin"])
        accounts = pa.Accounts(self.base)
        with self.assertRaises(pa.AccountsError):
            accounts.set_admin("svc-docker-node01", True)

    def test_remove_drops_the_admin_flag(self):
        self.add("alice", "svc-monitor", "svc-ha")
        run(self.base, "admin", "alice", "on")
        run(self.base, "remove", "alice")
        run(self.base, "add", "alice")
        self.assertFalse(self.accounts()["accounts"]["alice"]["admin"])

    def test_disabled_admin_is_not_an_admin(self):
        self.add("alice", "svc-monitor", "svc-ha")
        run(self.base, "admin", "alice", "on")
        run(self.base, "disable", "alice")
        accounts = pa.Accounts(self.base)
        self.assertEqual((accounts.is_admin("alice"), accounts.admin_pairs()), (False, []))
        self.assertEqual([n for n, _ in accounts.ha_all_users()], ["svc-monitor"])
        self.assertEqual([n for n, _ in accounts.line_users(private=True)], ["svc-ha", "svc-monitor"])
        self.assertIsNone(accounts.line_users(private=False))
        self.assertTrue(self.accounts()["accounts"]["alice"]["admin"])  # the flag is kept


class HandlerUsersTest(unittest.TestCase):
    def test_default_output_unchanged(self):
        outbound = {"protocol": "freedom", "settings": {}, "tag": "proxy"}
        a = XrayConfigHandler.build_xray_config(outbound, include_stats_api=True)
        b = XrayConfigHandler.build_xray_config(outbound, include_stats_api=True, inbound_users=None)
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertNotIn("users", json.dumps(a))

    def test_users(self):
        outbound = {"protocol": "freedom", "settings": {}, "tag": "proxy"}
        config = XrayConfigHandler.build_xray_config(outbound, inbound_users=[("alice", "pw")])
        http, socks = config["inbounds"][:2]
        self.assertEqual(http["settings"]["users"], [{"user": "alice", "pass": "pw"}])
        self.assertEqual(socks["settings"]["auth"], "password")
        self.assertEqual(socks["settings"]["users"], [{"user": "alice", "pass": "pw"}])
        with self.assertRaises(ValueError):
            XrayConfigHandler.build_xray_config(outbound, inbound_users=[])


@unittest.skipUnless(shutil.which("git") and (SERVICES.parent / ".git").exists(), "needs the git checkout")
class GitIgnoreTest(unittest.TestCase):
    SECRET_PATHS = ("services/proxy-accounts/accounts.json", "services/xray-ha/config/config.json",
                    "services/blackbox/config/blackbox.yml", "services/xray/usca5-bwh-sla-1tb/config/config.json",
                    # the private lines' configs, which always hold accounts
                    "services/xray/jpty1-bwh-2tb/config/config.json",
                    "services/xray/miyaip-uk-01_relay_jpty1-bwh-2tb/config/config.json",
                    # write_private's temp files, left behind if render is killed before the rename
                    "services/proxy-accounts/.accounts.json.k3j9x2a1", "services/xray-ha/config/.config.json.k3j9x2a1",
                    "services/blackbox/config/.blackbox.yml.k3j9x2a1",
                    "services/xray/usca5-bwh-sla-1tb/config/.config.json.k3j9x2a1",
                    # the lock, the applier's record of the last apply and its backup during an apply
                    "services/proxy-accounts/.lock", "services/proxy-accounts/applied.json",
                    "services/proxy-accounts/.apply-backup/manifest.json",
                    "services/proxy-accounts/.apply-backup/0")

    def test_secret_paths_ignored_and_untracked(self):
        repo = SERVICES.parent
        for path in self.SECRET_PATHS:
            ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", path])
            self.assertEqual(ignored.returncode, 0, f"{path} is not gitignored")
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--", *self.SECRET_PATHS],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual(tracked, "")
        for path in ("services/xray-ha/config.example.json", "services/blackbox/blackbox.example.yml",
                     "services/xray/public-lines.txt"):
            ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", path])
            self.assertEqual(ignored.returncode, 1, f"{path} should be in git")


if __name__ == "__main__":
    unittest.main()
