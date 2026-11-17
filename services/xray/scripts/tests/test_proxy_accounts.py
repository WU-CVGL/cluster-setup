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
        for rel in (pa.HA_EXAMPLE, pa.BLACKBOX_EXAMPLE):
            (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SERVICES / rel, self.base / rel)

    def accounts(self):
        return json.loads((self.base / pa.ACCOUNTS_FILE).read_text())

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
        first = self.accounts()["accounts"]["alice"]
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(self.accounts()["raw_auth"], False)

        code, out, _ = run(self.base, "rotate", "alice", "--show")
        second = self.accounts()["accounts"]["alice"]
        self.assertEqual((code, out), (0, second + "\n"))
        self.assertNotEqual(first, second)

        code, out, _ = run(self.base, "add", "bob", "--show")
        self.assertEqual(out, self.accounts()["accounts"]["bob"] + "\n")
        code, out, _ = run(self.base, "list")
        self.assertEqual(out, "alice\nbob\n")

        self.assertEqual(run(self.base, "add", "alice")[0], 1)       # exists
        self.assertEqual(run(self.base, "rotate", "carol")[0], 1)    # missing
        self.assertEqual(run(self.base, "add", "Carol")[0], 1)       # invalid
        self.assertEqual(run(self.base, "remove", "alice")[0], 0)
        self.assertEqual(list(self.accounts()["accounts"]), ["bob"])
        self.assertEqual(mode(path), 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["accounts.json"])  # no temp files left

    def test_raw_auth_setting(self):
        self.assertEqual(run(self.base, "raw-auth")[1], "off\n")
        run(self.base, "raw-auth", "on")
        self.assertEqual(run(self.base, "raw-auth")[1], "on\n")
        self.assertIs(self.accounts()["raw_auth"], True)


class RenderTest(_Base):
    def setUp(self):
        super().setUp()
        self.raw = self.base / "xray/proxy-a/config/config.json"
        self.raw.parent.mkdir(parents=True)
        self.raw_text = proxy_config()
        self.raw.write_text(self.raw_text)

    def ha(self):
        return json.loads((self.base / pa.HA_CONFIG).read_text())

    def inbound_users(self, config):
        users = {}
        for inbound in config["inbounds"]:
            if inbound["protocol"] in ("http", "socks"):
                users[inbound["protocol"]] = {u["user"]: u["pass"] for u in inbound["settings"].get("users", [])}
        return users

    def test_requires_service_accounts(self):
        self.add("alice")
        code, _, err = run(self.base, "render")
        self.assertEqual(code, 1)
        self.assertIn("svc-monitor", err)
        self.assertFalse((self.base / pa.HA_CONFIG).exists())
        self.add("svc-monitor")
        run(self.base, "raw-auth", "on")
        code, _, err = run(self.base, "render")
        self.assertEqual(code, 1)
        self.assertIn("svc-ha", err)
        self.assertFalse((self.base / pa.HA_CONFIG).exists())

    def test_render_without_raw_auth(self):
        self.add("alice", "bob", "svc-monitor", "svc-ha")
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [str(pa.HA_CONFIG), str(pa.BLACKBOX_CONFIG)])  # proxy untouched
        self.assertEqual(self.raw.read_text(), self.raw_text)
        accounts = self.accounts()["accounts"]
        ha = self.ha()
        expected = {k: v for k, v in accounts.items() if k != "svc-ha"}
        self.assertEqual(self.inbound_users(ha), {"http": expected, "socks": expected})
        socks = next(i for i in ha["inbounds"] if i["protocol"] == "socks")
        self.assertEqual((socks["settings"]["auth"], socks["settings"]["udp"]), ("password", True))
        for outbound in ha["outbounds"]:
            for server in outbound.get("settings", {}).get("servers", []):
                self.assertNotIn("users", server)
        # Apart from the users, the rendered config is the example
        example = json.loads((self.base / pa.HA_EXAMPLE).read_text())
        for config in (ha, example):
            for inbound in config["inbounds"]:
                inbound["settings"].pop("users", None)
        self.assertEqual(ha, example)
        for rel in (pa.HA_CONFIG, pa.BLACKBOX_CONFIG):
            self.assertEqual(mode(self.base / rel), 0o600)
        self.assertEqual(run(self.base, "render")[1], "")  # nothing changed

    def test_render_with_raw_auth_and_back(self):
        self.add("alice", "svc-monitor", "svc-ha")
        run(self.base, "raw-auth", "on")
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertIn("xray/proxy-a/config/config.json", out.split())
        accounts = self.accounts()["accounts"]
        raw = json.loads(self.raw.read_text())
        self.assertEqual(self.inbound_users(raw), {"http": accounts, "socks": accounts})
        self.assertEqual(mode(self.raw), 0o600)
        pool = [o for o in self.ha()["outbounds"] if o["tag"].startswith("pool-")]
        self.assertEqual(len(pool), 4)
        for outbound in pool:
            self.assertEqual(outbound["settings"]["servers"][0]["users"],
                             [{"user": "svc-ha", "pass": accounts["svc-ha"]}])
        blackbox = (self.base / pa.BLACKBOX_CONFIG).read_text()
        self.assertIn("proxy_url: socks5://svc-monitor:%s@xray-usca5-bwh-sla-1tb:1089" % accounts["svc-monitor"],
                      blackbox)

        run(self.base, "raw-auth", "off")
        code, out, _ = run(self.base, "render")
        self.assertIn("xray/proxy-a/config/config.json", out.split())
        self.assertEqual(self.raw.read_text(), self.raw_text)  # byte-identical to the generated file
        self.assertNotIn("users", json.dumps(self.ha()["outbounds"]))

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
        self.add("svc-monitor")
        odd = self.base / "xray/proxy-b/config/config.json"
        odd.parent.mkdir(parents=True)
        text = '{"inbounds": [{"protocol": "socks", "settings": {"udp": true}}, {"protocol": "vmess"}]}'
        odd.write_text(text)
        odd.chmod(0o644)
        code, out, _ = run(self.base, "render")
        self.assertEqual(code, 0)
        self.assertNotIn("xray/proxy-b/config/config.json", out.split())
        self.assertEqual(odd.read_text(), text)
        self.assertEqual(mode(odd), 0o644)

    def test_blackbox(self):
        self.add("svc-monitor", "svc-ha")
        data = self.accounts()
        data["accounts"]["svc-monitor"] = "p/w@x:y%z"
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
        self.assertEqual(changed, [
            ("      proxy_url: http://svc-monitor:PASSWORD@xray-ha:8889", f"      proxy_url: http://{auth}xray-ha:8889"),
            ("      proxy_url: socks5://svc-monitor:PASSWORD@xray-ha:1089",
             f"      proxy_url: socks5://{auth}xray-ha:1089"),
        ])
        self.assertEqual(sum("xray-ha:" in line for line in rendered), 2)
        self.assertFalse([line for line in rendered if "proxy_url:" in line and "PASSWORD" in line])


class ExampleTest(unittest.TestCase):
    def test_example_placeholders_cannot_log_in(self):
        # Basic auth splits user:pass at the first ':', so no HTTP client can send a user name holding one;
        # an empty list would turn HTTP authentication off, so the HTTP placeholder stays.
        config = json.loads((SERVICES / pa.HA_EXAMPLE).read_text())
        inbounds = {i["protocol"]: i["settings"] for i in config["inbounds"] if i["protocol"] in ("http", "socks")}
        self.assertTrue(inbounds["http"]["users"])
        for user in inbounds["http"]["users"]:
            self.assertIn(":", user["user"])
        self.assertEqual((inbounds["socks"]["auth"], inbounds["socks"]["users"]), ("password", []))


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
                    # write_private's temp files, left behind if render is killed before the rename
                    "services/proxy-accounts/.accounts.json.k3j9x2a1", "services/xray-ha/config/.config.json.k3j9x2a1",
                    "services/blackbox/config/.blackbox.yml.k3j9x2a1",
                    "services/xray/usca5-bwh-sla-1tb/config/.config.json.k3j9x2a1")

    def test_secret_paths_ignored_and_untracked(self):
        repo = SERVICES.parent
        for path in self.SECRET_PATHS:
            ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", path])
            self.assertEqual(ignored.returncode, 0, f"{path} is not gitignored")
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--", *self.SECRET_PATHS],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual(tracked, "")
        for path in ("services/xray-ha/config.example.json", "services/blackbox/blackbox.example.yml"):
            ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", path])
            self.assertEqual(ignored.returncode, 1, f"{path} should be in git")


if __name__ == "__main__":
    unittest.main()
