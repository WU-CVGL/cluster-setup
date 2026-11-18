import datetime
import json

from .base import PortalTestCase

PUBLIC = ["us-west-racknerd-12tb", "usca1-bwh-megaboxpro-2tb", "usca5-bwh-sla-1tb", "usca6-bwh-1tb"]
OPEN = ["us-west-racknerd-12tb", "usca1-bwh-megaboxpro-2tb"]  # open-proxies.txt
PRIVATE = ["jpty1-bwh-2tb", "miyaip-jp-01_relay_jpty1-bwh-2tb", "miyaip-jp-01_relay_usca5-bwh-sla-2tb",
           "miyaip-uk-01_relay_jpty1-bwh-2tb", "miyaip-uk-01_relay_usca5-bwh-sla-2tb"]


def account(data, name):
    return next(a for a in data["accounts"] if a["name"] == name)


class AccessTest(PortalTestCase):
    def get(self, username):
        self.sign_in(username)
        return self.client.get("/api/me/access").json()

    def test_anonymous_refused(self):
        self.assertEqual(self.client.get("/api/me/access").status_code, 401)
        response = self.client.get("/access")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/signin?next=/access")

    def test_parameters_refused(self):
        self.sign_in("alice")
        response = self.client.get("/api/me/access?username=carol")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("carol", response.content.decode())
        self.assertEqual(self.client.post("/api/me/access", {"username": "carol"}).status_code, 405)

    def test_regular_account(self):
        view = self.get("alice")
        self.assertEqual(view["account"], {"name": "alice", "enabled": True, "admin": False})
        self.assertEqual([e["id"] for e in view["entries"]], ["ha-general"] + PUBLIC)
        general = view["entries"][0]
        self.assertTrue(general["usable"])
        self.assertEqual(general["addresses"], [
            {"network": "private", "host": "192.168.233.8", "http": "192.168.233.8:30889",
             "socks": "192.168.233.8:30089"},
            {"network": "campus", "host": "10.0.1.68", "http": "10.0.1.68:30889", "socks": "10.0.1.68:30089"}])
        proxy = view["entries"][3]
        self.assertEqual(proxy["id"], "usca5-bwh-sla-1tb")
        self.assertEqual(proxy["ports"], {"http": 59889, "socks": 59880})
        self.assertFalse(proxy["requires_account"])
        self.assertNotIn("ha-private", json.dumps(view["entries"]))
        texts = [t["text"] for t in view["templates"]["networks"]["private"]]
        self.assertIn("export http_proxy=http://alice:<password>@192.168.233.8:30889", texts[0])
        self.assertTrue(any("socks5h://alice:<password>@192.168.233.8:30089" in t for t in texts))
        self.assertTrue(any(t.startswith("git config --global http.https://github.com/.proxy") for t in texts))
        self.assertTrue(any(t.startswith("pip install --proxy http://alice:<password>@") for t in texts))
        self.assertIn("10.0.1.68:30889", view["templates"]["networks"]["campus"][0]["text"])

    def test_admin_account(self):
        view = self.get("carol")
        self.assertTrue(view["account"]["admin"])
        self.assertEqual([e["id"] for e in view["entries"]], ["ha-general", "ha-private"] + PUBLIC + PRIVATE)
        ha_private = view["entries"][1]
        self.assertEqual((ha_private["access"], ha_private["ports"], ha_private["usable"]),
                         ("private", {"http": 31889, "socks": 31089}, True))
        page = self.client.get("/access")
        self.assertContains(page, "This is an admin account")
        self.assertContains(page, "<span class=\"tag admin\">Private</span>", count=len(PRIVATE))

    def test_proxy_admin_flag_is_not_the_determined_role(self):
        # Determined admin, regular proxy account: no ha-private
        self.master.users["alice"] = ("pw-alice", True, True)
        view = self.get("alice")
        self.assertIs(self.client.session["is_determined_admin"], True)
        self.assertNotIn("ha-private", [e["id"] for e in view["entries"]])
        # proxy admin, not a Determined admin: ha-private
        self.client.post("/signout")
        self.master.users["carol"] = ("pw-carol", False, True)
        view = self.get("carol")
        self.assertIs(self.client.session["is_determined_admin"], False)
        self.assertIn("ha-private", [e["id"] for e in view["entries"]])

    def test_disabled_account_raw_auth_off(self):
        view = self.get("bob")
        self.assertEqual(view["account"], {"name": "bob", "enabled": False, "admin": False})
        usable = {e["id"]: e["usable"] for e in view["entries"]}
        self.assertFalse(usable["ha-general"])
        self.assertTrue(all(usable[name] for name in PUBLIC))  # raw auth off: anyone
        self.assertIsNone(view["templates"])
        page = self.client.get("/access")
        self.assertContains(page, "Proxy access is <b>off</b>")
        # what stops for a non-admin account, once, without the raw-auth paragraph repeating it
        self.assertContains(page, "The Auto-switching Proxy refuses it. The public proxies' own addresses still work "
                                  "for you: they need no account.")
        self.assertNotContains(page, "The auto-switching proxies and the private proxies refuse it")
        self.assertNotContains(page, "turning an account off stops only")
        self.assertContains(page, "is not a change to your cluster account")

    def test_raw_auth_said_plainly_while_on_or_without_account(self):
        self.get("alice")
        self.assertContains(self.client.get("/access"),
                            "so turning an account off stops only the auto-switching proxies")
        self.client.post("/signout")
        self.master.users["frank"] = ("pw-frank", False, True)
        self.get("frank")
        self.assertContains(self.client.get("/access"),
                            "so turning an account off stops only the auto-switching proxies")

    def test_disabled_admin(self):
        view = self.get("erin")
        usable = {e["id"]: e["usable"] for e in view["entries"]}
        self.assertFalse(usable["ha-private"])
        self.assertFalse(any(usable[name] for name in PRIVATE))
        self.assertContains(self.client.get("/access"), "The auto-switching proxies and the private proxies refuse it.")

    def test_raw_auth_on(self):
        self.write_status(lambda d: d.update(raw_auth=True))
        alice = self.get("alice")
        self.assertEqual(alice["open_proxies"], OPEN)
        self.assertTrue(all(e["usable"] for e in alice["entries"]))
        self.assertEqual([e["id"] for e in alice["entries"] if not e["requires_account"]], OPEN)
        page = self.client.get("/access")
        self.assertContains(page, "Every proxy requires an enabled account, except us-west-racknerd-12tb, "
                                  "usca1-bwh-megaboxpro-2tb at their own addresses, which need none.")
        self.assertEqual(page.content.decode().count("No account needed"), 2)
        self.client.post("/signout")
        bob = self.get("bob")
        # the open proxies still take bob: they never need an account
        self.assertEqual([e["id"] for e in bob["entries"] if e["usable"]], OPEN)
        page = self.client.get("/access")
        self.assertContains(page, "Every proxy refuses it, except us-west-racknerd-12tb, "
                                  "usca1-bwh-megaboxpro-2tb at their own addresses, which need no account.")
        zh = self.client.get("/access", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(zh, "除 us-west-racknerd-12tb, usca1-bwh-megaboxpro-2tb 自己的地址外，所有代理都拒绝它")
        self.assertContains(zh, "无需账号")

    def test_raw_auth_on_without_open_proxies(self):
        def change(data):
            data["raw_auth"] = True
            for proxy in data["proxies"]:
                proxy["open"] = False
        self.write_status(change)
        alice = self.get("alice")
        self.assertTrue(all(e["requires_account"] and e["usable"] for e in alice["entries"]))
        self.client.post("/signout")
        bob = self.get("bob")
        self.assertFalse(any(e["usable"] for e in bob["entries"]))
        self.assertContains(self.client.get("/access"), "Every proxy refuses it, the public proxies' own addresses")

    def test_no_account_raw_auth_on(self):
        self.write_status(lambda d: d.update(raw_auth=True))
        self.master.users["frank"] = ("pw-frank", False, True)
        view = self.get("frank")
        self.assertEqual([e["id"] for e in view["entries"] if e["usable"]], OPEN)
        self.assertContains(self.client.get("/access"), "Without an account you can use us-west-racknerd-12tb, "
                                                        "usca1-bwh-megaboxpro-2tb at their own addresses: they never "
                                                        "need one.")

    def test_open_only_for_a_public_proxy(self):
        """A status that marks a private proxy open is not believed: it still requires an admin account."""
        def change(data):
            data["raw_auth"] = True
            next(p for p in data["proxies"] if p["name"] == "jpty1-bwh-2tb")["open"] = True
        self.write_status(change)
        view = self.get("erin")  # a disabled admin
        entries = {e["id"]: e for e in view["entries"]}
        self.assertTrue(entries["jpty1-bwh-2tb"]["requires_account"])
        self.assertFalse(entries["jpty1-bwh-2tb"]["usable"])
        self.assertNotIn("jpty1-bwh-2tb", view["open_proxies"])

    def test_no_account(self):
        self.master.users["frank"] = ("pw-frank", False, True)
        view = self.get("frank")
        self.assertIsNone(view["account"])
        self.assertEqual([e["id"] for e in view["entries"]], ["ha-general"] + PUBLIC)
        self.assertFalse(view["entries"][0]["usable"])
        page = self.client.get("/access")
        self.assertContains(page, "has no proxy account")
        self.assertContains(page, "ask an admin")

    def test_service_account_name_is_nobodys(self):
        self.master.users["svc-monitor"] = ("pw-svc-monitor", False, True)
        self.assertIsNone(self.get("svc-monitor")["account"])

    def test_applier_status_only(self):
        # the page reflects the applier's status file, nothing else
        self.write_status(lambda d: account(d, "alice").update(enabled=False))
        view = self.get("alice")
        self.assertFalse(view["account"]["enabled"])
        self.assertIsNone(view["status_unapplied_changes"])
        self.write_status(lambda d: d.update(unapplied_changes=True))
        self.assertIs(self.client.get("/api/me/access").json()["status_unapplied_changes"], True)

    def test_stale_and_missing_status(self):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        self.write_status(generated_at=old)
        self.assertTrue(self.get("alice")["status_stale"])
        self.assertContains(self.client.get("/access"), "may be out of date")
        self.status_file.unlink()
        view = self.client.get("/api/me/access").json()
        self.assertFalse(view["status_available"])
        self.assertIsNone(view["account"])
        self.assertContains(self.client.get("/access"), "Account information is unavailable")

    def test_no_password_anywhere(self):
        for name in ("alice", "carol", "bob"):
            self.client.post("/signout")
            self.sign_in(name)
            body = self.client.get("/api/me/access").content.decode() + self.client.get("/access").content.decode()
            self.assertNotIn(f"pw-{name}", body)
        self.assertNotIn("password", self.status_file.read_text())

    def test_page_says_raw_auth_off_plainly(self):
        self.sign_in("alice")
        page = self.client.get("/access")
        self.assertContains(page, "The public proxies' own addresses need no account, so turning an account off")
        self.assertContains(page, "Replace &lt;password&gt; with your account")
