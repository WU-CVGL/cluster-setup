"""Single private proxies granted to an account: the status, the admins' requests, and My access."""

import json
import os
import unittest

from django.utils import translation

from portal import accounts, lifecycle, proxy_status
from portal.models import AccountRequest

from .base import PortalTestCase, https_client
from .test_admin_requests import APPLIER, load_applier, requests_in

JP = "jpty1-bwh-2tb"
UK = "miyaip-uk-01_relay_jpty1-bwh-2tb"
PUBLIC = "usca5-bwh-sla-1tb"


def grant(name, *proxies):
    """A status change: the account is granted these proxies."""
    def change(data):
        next(a for a in data["accounts"] if a["name"] == name)["proxies"] = list(proxies)
    return change


class StatusTest(PortalTestCase):
    def test_parse(self):
        self.write_status(lambda d: [grant("alice", UK, JP, JP, "no-longer-private", PUBLIC)(d),
                                     grant("bob", "Bad name", 7, None)(d), grant("svc-docker-cvgl-node01", JP)(d),
                                     grant("carol", "x" * 200)(d)])
        status = proxy_status.load()
        rows = {r.name: r for r in status.all_accounts}
        # the admins see every granted name, also one that is not a private proxy (any more), to take it back
        self.assertEqual(rows["alice"].proxies, [JP, UK, "no-longer-private", PUBLIC])
        self.assertEqual((rows["bob"].proxies, rows["carol"].proxies, rows["svc-docker-cvgl-node01"].proxies),
                         ([], [], []))
        # a user's own account: only the private proxies of this status
        self.assertEqual(status.accounts["alice"].proxies, [JP, UK])
        self.assertIn(JP, status.private_proxies)
        self.assertNotIn(PUBLIC, status.private_proxies)

    def test_an_older_status_without_grants(self):
        self.write_status(lambda d: [a.pop("proxies", None) for a in d["accounts"]])
        status = proxy_status.load()
        self.assertTrue(status.available)
        self.assertEqual({r.name: r.proxies for r in status.all_accounts if r.proxies}, {})
        self.write_status(lambda d: [grant("alice", JP)(d)]
                          + [a.update(proxies="jpty1") for a in d["accounts"] if a["name"] == "bob"])
        self.assertEqual(proxy_status.load().accounts["bob"].proxies, [])


class GrantRequestTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.write_status(grant("alice", UK))
        self.sign_in("carol")

    def post(self, op, username, proxy=None, confirm=None, client=None):
        data = {"op": op, "username": username}
        if proxy is not None:
            data["proxy"] = proxy
        if confirm is not None:
            data["confirm"] = confirm
        return (client or self.client).post("/api/admin/requests", json.dumps(data), content_type="application/json")

    def test_grant_through_the_confirm_page(self):
        page = self.client.get("/admin/accounts/grant?username=alice")  # first the proxy
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'<option value="{JP}">')
        self.assertNotContains(page, f'<option value="{UK}">')     # granted already
        self.assertNotContains(page, f'<option value="{PUBLIC}">')  # public
        self.assertNotContains(page, "Send the request")
        page = self.client.get(f"/admin/accounts/grant?username=alice&proxy={JP}")
        self.assertContains(page, f"Lets alice use the private proxy {JP} at its own address.")
        self.assertContains(page, f"Applying restarts {JP}. Established connections through it drop")
        self.assertContains(page, f'<input type="hidden" name="proxy" value="{JP}">')
        self.assertNotContains(page, 'name="confirm"')  # nothing is taken away
        response = self.client.post("/admin/accounts/grant", {"username": "alice", "proxy": JP})
        self.assertEqual(response.status_code, 302)
        row = AccountRequest.objects.get()
        self.assertEqual((row.op, row.username, row.proxy, row.requested_by), ("grant", "alice", JP, "carol"))
        self.assertEqual(response["Location"], f"/admin/requests/{row.id}")
        path = self.spool / "requests" / f"{row.id}.json"
        self.assertEqual(json.loads(path.read_text()), {"id": row.id, "op": "grant", "username": "alice",
                                                        "proxy": JP})
        page = self.client.get(f"/admin/requests/{row.id}")
        self.assertContains(page, "Private proxy on: <span class=\"mono\">alice</span>")
        self.assertContains(page, f'<dd class="mono">{JP}</dd>')
        self.assertContains(self.client.get("/admin/log"), f"Private proxy on · <code>{JP}</code>")
        self.assertEqual(self.client.get(f"/api/admin/requests/{row.id}").json()["proxy"], JP)

    def test_revoke_needs_the_name_typed(self):
        page = self.client.get("/admin/accounts/revoke?username=alice")
        self.assertContains(page, f'<option value="{UK}">')
        page = self.client.get(f"/admin/accounts/revoke?username=alice&proxy={UK}")
        self.assertContains(page, f"{UK} stops accepting alice. Nothing else changes for the account.")
        self.assertContains(page, f"Applying restarts {UK}.")
        self.assertContains(page, 'name="confirm"')
        response = self.client.post("/admin/accounts/revoke", {"username": "alice", "proxy": UK, "confirm": "al"})
        self.assertContains(response, "Type the account name to confirm.", status_code=400)
        self.assertEqual(requests_in(self.spool), [])
        response = self.client.post("/admin/accounts/revoke", {"username": "alice", "proxy": UK, "confirm": "alice"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AccountRequest.objects.get().proxy, UK)

    def test_refused_before_any_file(self):
        self.write_status(lambda d: [grant("alice", UK, "gone-proxy")(d), grant("bob", JP)(d)])
        for op, name, proxy, status, text in (
                ("grant", "alice", PUBLIC, 400, f"{PUBLIC} is not a private proxy."),
                ("grant", "alice", "no-such-proxy", 400, "is not a private proxy."),
                ("grant", "alice", "../x", 400, "Choose a private proxy."),
                ("grant", "alice", "", 400, "Choose a private proxy."),
                ("grant", "alice", UK, 409, f"alice may use {UK} already."),
                ("grant", "carol", JP, 409, "carol is an admin account: it may use every private proxy already."),
                ("grant", "svc-docker-cvgl-node01", JP, 400, "A service account cannot be given a private proxy."),
                ("grant", "svc-ha", JP, 403, "can only be changed outside the portal"),
                ("grant", "nobody", JP, 404, "There is no account nobody."),
                ("revoke", "alice", JP, 409, f"alice was not given {JP}."),
                ("revoke", "carol", JP, 409, f"carol was not given {JP}.")):
            response = self.post(op, name, proxy, confirm=name)
            self.assertEqual(response.status_code, status, (op, name, proxy))
            self.assertIn(text, response.json()["error"], (op, name, proxy))
            if proxy:  # without one, the page asks for it first
                page = self.client.get(f"/admin/accounts/{op}?username={name}&proxy={proxy}")
                self.assertEqual(page.status_code, status, (op, name, proxy))
                self.assertNotContains(page, "Send the request", status_code=status)
        # no proxy for the other ops, and none missing for these
        self.assertEqual(self.post("disable", "alice", JP, confirm="alice").status_code, 400)
        self.assertEqual(self.post("grant", "alice").status_code, 400)
        self.assertEqual(self.post("grant", "alice", 7).status_code, 400)
        self.assertEqual(requests_in(self.spool), [])
        self.assertEqual(AccountRequest.objects.count(), 0)
        # a grant that is no longer a private proxy can be taken back
        self.assertEqual(self.post("revoke", "alice", "gone-proxy", confirm="alice").status_code, 202)
        # nothing left to choose
        page = self.client.get("/admin/accounts/grant?username=carol")
        self.assertEqual(page.status_code, 409)
        self.write_status(lambda d: [grant("alice", UK, "gone-proxy")(d),
                                     grant("bob", *[p["name"] for p in d["proxies"] if p["access"] == "private"])(d)])
        page = self.client.get("/admin/accounts/grant?username=bob")
        self.assertContains(page, "There is no private proxy to choose for bob.", status_code=409)
        self.assertContains(self.client.get("/admin/accounts/revoke?username=alice"),
                            '<option value="gone-proxy">')

    def test_api_admins_only_and_csrf(self):
        anonymous = https_client()
        self.assertEqual(self.post("grant", "alice", JP, client=anonymous).status_code, 401)
        alice = https_client()
        self.sign_in("alice", client=alice)
        self.assertEqual(self.post("grant", "alice", JP, client=alice).status_code, 403)
        strict = https_client(enforce_csrf_checks=True)
        strict.get("/signin")
        strict.post("/signin", {"username": "carol", "password": "pw-carol",
                                "csrfmiddlewaretoken": strict.cookies["proxyportal_csrf"].value})
        self.assertEqual(strict.session["username"], "carol")
        self.assertEqual(self.post("grant", "alice", JP, client=strict).status_code, 403)  # no CSRF token
        self.assertEqual(strict.post("/admin/accounts/grant", {"username": "alice", "proxy": JP}).status_code, 403)
        self.assertEqual(requests_in(self.spool), [])
        response = self.post("grant", "alice", JP)
        self.assertEqual(response.status_code, 202)
        self.assertEqual((response.json()["op"], response.json()["proxy"]), ("grant", JP))

    @unittest.skipUnless(APPLIER.exists(), "needs the applier in this checkout")
    def test_the_applier_accepts_the_request_files(self):
        ap = load_applier()
        self.assertEqual(self.post("grant", "alice", JP).status_code, 202)
        self.assertEqual(self.post("revoke", "alice", UK, confirm="alice").status_code, 202)
        spool = ap.Spool(self.spool, os.getuid())
        found = []
        for name in requests_in(self.spool):
            request, error, _ = spool.read(name)
            self.assertIsNone(error, name)
            found.append((request["op"], request["proxy"]))
        self.assertEqual(sorted(found), [("grant", JP), ("revoke", UK)])
        self.assertEqual(set(accounts.GRANT_OPS), set(ap.GRANT_OPS))
        self.assertEqual(set(accounts.OPS), set(ap.OPS))

    def test_users_page(self):
        self.write_status(lambda d: [grant("alice", UK, JP)(d), grant("carol", JP)(d)])
        page = self.client.get("/admin/users")
        self.assertContains(page, "<th scope=\"col\">Private proxies</th>")
        self.assertContains(page, f"<code>{JP}</code> <code>{UK}</code>")
        self.assertContains(page, "Every private proxy")
        self.assertContains(page, f"Also given: {JP}")
        self.assertContains(page, 'href="/admin/accounts/grant?username=alice"')
        self.assertContains(page, 'href="/admin/accounts/revoke?username=alice"')
        self.assertNotContains(page, 'href="/admin/accounts/grant?username=carol"')  # an admin: nothing to grant
        self.assertNotContains(page, 'href="/admin/accounts/grant?username=svc-docker-cvgl-node01"')
        rows = {r["name"]: r for r in self.client.get("/api/admin/users").json()["users"]}
        self.assertEqual(rows["alice"]["proxies"], [JP, UK])
        self.assertEqual(rows["alice"]["actions"], ["rotate", "disable", "admin-on", "grant", "revoke", "remove"])
        self.assertEqual(rows["carol"]["actions"], ["rotate", "disable", "admin-off", "revoke", "remove"])
        zh = self.client.get("/admin/users", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(zh, "<th scope=\"col\">私有代理</th>")
        self.assertContains(zh, f"<code>{JP}</code> <code>{UK}</code>")

    def test_restart_texts_for_a_granted_account(self):
        status = proxy_status.load()
        alice = next(r for r in status.all_accounts if r.name == "alice")
        with translation.override("en"):
            for op in ("rotate", "disable", "enable", "remove"):
                self.assertIn(f"both auto-switching proxies and {UK}", accounts.restart_text(op, alice, status), op)
            self.assertIn(f"Applying restarts the Auto-switching Proxy and {UK}.",
                          accounts.restart_text("rotate", status.accounts["alice"], status, own=True))
            bob = next(r for r in status.all_accounts if r.name == "bob")  # proxy access off: no proxy changes
            self.assertIn("Applying restarts no proxy", accounts.restart_text("grant", bob, status, proxy=JP))
            page = self.client.get("/admin/accounts/admin-off?username=carol")
            self.assertContains(page, "the Private Auto-switching Proxy and the private proxies refuse it.")
        self.write_status(grant("carol", JP))
        page = self.client.get("/admin/accounts/admin-off?username=carol")
        self.assertContains(page, f"refuse it, except {JP}, which it was given.")

    def test_unconfirmed_text(self):
        row = AccountRequest.objects.create(id="00000000-0000-4000-8000-0000000000aa", op="revoke", username="alice",
                                            proxy=UK, requested_by="carol", state="unconfirmed")
        with translation.override("en"):
            self.assertEqual(lifecycle.describe(row), f"Live, but not confirmed yet: {UK} did not answer the checks "
                                                      "in time, or is not running.")
        self.assertIn("revoke", lifecycle.REVOKES)


class MyAccessTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.write_status(lambda d: [grant("alice", JP, "no-longer-private", PUBLIC)(d), grant("bob", UK)(d),
                                     grant("carol", JP)(d)])

    def view(self, username):
        self.client.post("/signout")
        self.sign_in(username)
        return self.client.get("/api/me/access").json()

    def test_granted_proxy_with_addresses_and_settings(self):
        view = self.view("alice")
        self.assertEqual(view["account"]["proxies"], [JP])
        entries = {e["id"]: e for e in view["entries"]}
        self.assertEqual([e["id"] for e in view["entries"] if e["access"] == "private"], [JP])  # no other private
        self.assertNotIn("ha-private", entries)
        self.assertEqual((entries[JP]["usable"], entries[JP]["requires_account"], entries[JP]["granted"]),
                         (True, True, True))
        self.assertEqual(entries[JP]["addresses"][0]["http"], "192.168.233.8:55889")
        self.assertEqual(entries[JP]["addresses"][1]["socks"], "10.0.1.68:55089")
        self.assertEqual([p["id"] for p in view["templates"]["proxies"]], [JP])
        texts = [t["text"] for t in view["templates"]["proxies"][0]["networks"]["private"]]
        self.assertIn("export http_proxy=http://alice:<password>@192.168.233.8:55889", texts[0])
        self.assertTrue(any("socks5h://alice:<password>@192.168.233.8:55089" in t for t in texts))
        page = self.client.get("/access")
        self.assertContains(page, f"It may also use the private proxy {JP}, at its own address.")
        self.assertContains(page, "One private proxy, no switching, given to your account.")
        self.assertContains(page, f'<pre id="tpl-{JP}-private-environment">')
        self.assertContains(page, "For the Auto-switching Proxy, then for each of your private proxies.")
        self.assertNotContains(page, UK)
        self.assertNotContains(page, "<code>ha-private</code>")
        zh = self.client.get("/access", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(zh, f"它还可以使用私有代理 {JP}")
        self.assertContains(zh, "单个私有代理，不切换，已授予你的账号。")

    def test_proxy_access_off(self):
        view = self.view("bob")  # disabled, granted UK
        entry = next(e for e in view["entries"] if e["id"] == UK)
        self.assertFalse(entry["usable"])
        self.assertIsNone(view["templates"])
        page = self.client.get("/access")
        self.assertContains(page, "The Auto-switching Proxy and your private proxies refuse it.")

    def test_admin_account_lists_each_private_proxy_once(self):
        view = self.view("carol")
        ids = [e["id"] for e in view["entries"]]
        self.assertEqual(ids.count(JP), 1)
        self.assertIn("ha-private", ids)
        self.assertEqual(view["account"]["proxies"], [])
        self.assertEqual(view["templates"]["proxies"], [])

    def test_other_users_see_no_private_proxy(self):
        self.write_status(lambda d: next(a for a in d["accounts"] if a["name"] == "alice").pop("proxies", None))
        view = self.view("alice")
        self.assertEqual([e for e in view["entries"] if e["access"] == "private"], [])
        self.assertEqual(view["templates"]["proxies"], [])
        self.assertNotContains(self.client.get("/access"), JP)
