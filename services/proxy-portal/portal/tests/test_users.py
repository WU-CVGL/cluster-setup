"""The cluster user list read at an admin's sign-in, and the admins' Users page with its filters and actions."""

import itertools
import json
import logging
import os
import time
import types
from unittest import mock

from django.conf import settings
from django.db import connection

from portal import master, upstream
from portal.models import AccountRequest, ClusterUser

from .base import PortalTestCase, PortalTransactionTestCase, https_client
from .test_admin_password import Capture


class CaptureTest(PortalTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.capture = Capture()
        self.logger = logging.getLogger("portal")
        self.logger.addHandler(self.capture)

    def tearDown(self):
        self.logger.removeHandler(self.capture)
        super().tearDown()

    def listed(self):
        return {u.username: (u.active, u.admin) for u in ClusterUser.objects.all()}

    def test_read_with_the_token_before_logout(self):
        self.assertEqual(self.sign_in("carol").status_code, 302)
        kinds = [kind for kind, _ in self.master.events]
        self.assertEqual(kinds, ["login", "users", "logout"])
        users_token, logout_token = self.master.events[1][1], self.master.events[2][1]
        self.assertTrue(users_token.startswith("v2.public."))
        self.assertEqual(users_token, logout_token)  # the session's own token, ended right after
        self.assertEqual(self.master.tokens, {})
        self.assertEqual(self.listed(), {"alice": (True, False), "bob": (True, False), "carol": (True, True),
                                         "dave": (False, False), "erin": (True, True)})
        # the token is kept nowhere: not in the database, the session or the logs
        connection.close()
        raw = open(settings.DATABASES["default"]["NAME"], "rb").read()
        self.assertNotIn(users_token.encode(), raw)
        self.assertNotIn(b"v2.public.", raw)
        self.assertNotIn(users_token, json.dumps(dict(self.client.session)))
        self.assertNotIn(users_token, "\n".join(self.capture.lines))

    def test_paged(self):
        self.master.page_size = 2
        self.master.users.update({f"user{i:02d}": (f"pw-user{i:02d}", False, True) for i in range(7)})
        self.sign_in("carol")
        self.assertEqual(len(self.listed()), 12)
        self.assertEqual([k for k, _ in self.master.events].count("users"), 6)

    def test_replaced_at_each_admin_sign_in(self):
        self.sign_in("carol")
        del self.master.users["bob"]
        self.master.users["frank"] = ("pw-frank", False, True)
        self.sign_in("erin", client=https_client())
        self.assertIn("frank", self.listed())
        self.assertNotIn("bob", self.listed())

    def test_failure_keeps_the_previous_list(self):
        self.sign_in("carol")
        before = self.listed()
        captured = ClusterUser.objects.first().captured_at
        for answer in ((500, {"code": 13, "message": "internal"}), (200, {"users": "nope"}),
                       (200, {"users": [{"username": "x", "active": "yes", "admin": False}]}), (200, {"users": []}),
                       (200, {"users": [{"username": "x", "active": True, "admin": False}] * 2})):
            self.master.users_answer = answer
            client = https_client()
            self.assertEqual(self.sign_in("carol", client=client).status_code, 302, answer)  # still signed in
            self.assertIs(client.session["is_determined_admin"], True)
            self.assertEqual(self.listed(), before, answer)
            self.assertEqual(ClusterUser.objects.first().captured_at, captured)
        warnings = [line for line in self.capture.lines if "user list" in line]
        self.assertEqual(len(warnings), 5)
        self.assertNotIn("v2.public.", "\n".join(self.capture.lines))
        self.assertEqual(self.master.tokens, {})  # every session was still ended

    def test_broken_answer_keeps_the_previous_list(self):
        self.sign_in("carol")
        before = self.listed()
        cut = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"100\r\n{\"users\": [")
        for raw in (cut, b"garbage\r\n\r\n", b"HTTP/1.1 200 " + b"x" * 70000 + b"\r\n\r\n"):
            self.master.users_answer = ("raw", raw)
            client = https_client()
            self.assertEqual(self.sign_in("carol", client=client).status_code, 302, raw[:40])
            self.assertIs(client.session["is_determined_admin"], True)
            self.assertEqual(self.listed(), before)
        self.assertEqual(len([line for line in self.capture.lines if "user list" in line]), 3)
        self.assertNotIn("v2.public.", "\n".join(self.capture.lines))
        self.assertEqual(self.master.tokens, {})  # every session was still ended

    def users_calls(self):
        return [kind for kind, _ in self.master.events].count("users")

    def test_a_master_ignoring_the_offset_is_read_twice_only(self):
        self.sign_in("carol")
        before = self.listed()
        rows = [{"username": f"user{i:03d}", "active": True, "admin": False} for i in range(master.USERS_PAGE)]
        self.master.users_answer = (200, {"users": rows})  # every page the same, full, with no total
        self.master.events.clear()
        self.assertEqual(self.sign_in("carol", client=https_client()).status_code, 302)
        self.assertEqual(self.users_calls(), 2)
        self.assertEqual(self.listed(), before)
        self.assertTrue(any("repeated names" in line for line in self.capture.lines))

    def test_more_than_the_limit_keeps_the_previous_list(self):
        self.sign_in("carol")
        before = self.listed()
        self.master.page_size = 2
        self.master.users.update({f"user{i:02d}": (f"pw-user{i:02d}", False, True) for i in range(7)})
        self.master.events.clear()
        with mock.patch.object(master, "USERS_MAX", 4):
            self.assertEqual(self.sign_in("carol", client=https_client()).status_code, 302)
        self.assertEqual(self.users_calls(), 2)
        self.assertEqual(self.listed(), before)
        self.assertTrue(any("more than 4 users" in line for line in self.capture.lines))

    def test_a_slow_list_stops_at_the_time_limit(self):
        self.sign_in("carol")
        before = self.listed()
        self.master.page_size = 2
        self.master.events.clear()
        clock = types.SimpleNamespace(monotonic=itertools.count(0, 10).__next__)  # 10 s pass per page
        with mock.patch.object(master, "time", clock), \
                mock.patch.object(master, "request_json", wraps=upstream.request_json) as call:
            self.assertEqual(self.sign_in("carol", client=https_client()).status_code, 302)
        users = [c for c in call.call_args_list if "/api/v1/users" in c.args[0]]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].kwargs["timeout"], master.USERS_SECONDS - 10)  # the time left, not 10 s more
        self.assertEqual(self.listed(), before)
        self.assertTrue(any("not read within 15 seconds" in line for line in self.capture.lines))
        self.assertEqual(self.master.tokens, {})

    def test_a_complete_list_that_arrives_too_late_is_dropped(self):
        """One page with every user, answered only after the time is up: the list is not taken."""
        self.sign_in("carol")
        before = self.listed()
        self.master.users["frank"] = ("pw-frank", False, True)  # would show if the late list were taken
        self.master.events.clear()
        clock = types.SimpleNamespace(monotonic=itertools.count(0, 10).__next__)  # the answer comes 10 s later
        client = https_client()
        with mock.patch.object(master, "time", clock), \
                mock.patch.object(master, "request_json", wraps=upstream.request_json) as call:
            self.assertEqual(self.sign_in("carol", client=client).status_code, 302)
        self.assertIs(client.session["is_determined_admin"], True)
        self.assertEqual(len([c for c in call.call_args_list if "/api/v1/users" in c.args[0]]), 1)
        self.assertEqual([kind for kind, _ in self.master.events], ["login", "users", "logout"])
        self.assertEqual(self.listed(), before)
        self.assertTrue(any("not read within 15 seconds" in line for line in self.capture.lines))
        self.assertEqual(self.master.tokens, {})

    def test_a_list_written_slowly_stops_at_the_time_limit(self):
        """A real server writing one valid page a byte at a time: the sign-in ends at the limit with the old list."""
        self.sign_in("carol")
        before = self.listed()
        body = json.dumps({"users": [{"username": "zed", "active": True, "admin": False}],
                           "pagination": {"total": 1}}).encode()
        head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode()
        self.master.users_answer = ("slow", (head, body, 0.05))  # about 4 s
        client = https_client()
        start = time.monotonic()
        with mock.patch.object(master, "USERS_SECONDS", 1):
            response = self.sign_in("carol", client=client)
        seconds = time.monotonic() - start
        self.assertEqual(response.status_code, 302)
        self.assertIs(client.session["is_determined_admin"], True)
        self.assertLess(seconds, 2)
        self.assertEqual(self.listed(), before)
        self.assertEqual(self.master.tokens, {})  # logged out after the list read gave up

    def test_anything_raised_keeps_the_previous_list(self):
        self.sign_in("carol")
        before = self.listed()
        real = upstream.request_json

        def broken(url, *args, **kwargs):
            if "/api/v1/users" in url:
                raise RuntimeError("v2.public.not-logged")
            return real(url, *args, **kwargs)

        with mock.patch.object(master, "request_json", side_effect=broken):
            client = https_client()
            self.assertEqual(self.sign_in("carol", client=client).status_code, 302)
        self.assertIs(client.session["is_determined_admin"], True)
        self.assertEqual(self.listed(), before)
        self.assertTrue(any("user list at an admin sign-in: RuntimeError;" in line for line in self.capture.lines))
        self.assertNotIn("v2.public.", "\n".join(self.capture.lines))  # the type only, never the message
        self.assertEqual(self.master.tokens, {})

    def test_unreachable_keeps_the_previous_list(self):
        self.sign_in("carol")
        before = self.listed()
        self.master.users_answer = (401, {"code": 16, "message": "invalid token"})
        self.sign_in("carol", client=https_client())
        self.assertEqual(self.listed(), before)

    def test_non_admins_never_read_it(self):
        for name in ("alice", "bob"):
            self.sign_in(name, client=https_client())
        self.assertNotIn("users", [kind for kind, _ in self.master.events])
        self.assertEqual(ClusterUser.objects.count(), 0)


class UsersPageTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.master.users.update({"frank": ("pw-frank", False, True), "Hana.W": ("pw-hana", False, True),
                                  "gina": ("pw-gina", False, False)})
        self.sign_in("carol")

    def rows(self, query=""):
        data = self.client.get("/api/admin/users" + query).json()
        return {u["name"]: u for u in data["users"]}, data

    def test_authorization(self):
        for path in ("/admin/users", "/api/admin/users"):
            anonymous = https_client().get(path)
            self.assertEqual(anonymous.status_code, 302 if path.startswith("/admin") else 401, path)
            alice = https_client()
            self.sign_in("alice", client=alice)
            self.assertEqual(alice.get(path).status_code, 403, path)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)

    def test_rows(self):
        rows, data = self.rows()
        self.assertEqual(rows["alice"], {"name": "alice", "listed": True, "active": True, "determined_admin": False,
                                         "account": "enabled", "private": False, "protected": False,
                                         "proxies": [], "valid_name": True,
                                         "actions": ["rotate", "disable", "admin-on", "grant", "remove"],
                                         "pending": None})
        self.assertEqual((rows["carol"]["determined_admin"], rows["carol"]["private"]), (True, True))
        self.assertEqual(rows["carol"]["actions"], ["rotate", "disable", "admin-off", "remove"])
        self.assertEqual((rows["bob"]["account"], rows["bob"]["actions"]),
                         ("disabled", ["rotate", "enable", "admin-on", "grant", "remove"]))
        self.assertEqual((rows["frank"]["account"], rows["frank"]["actions"]), ("none", ["add"]))
        self.assertEqual((rows["Hana.W"]["valid_name"], rows["Hana.W"]["actions"]), (False, []))
        self.assertEqual((rows["gina"]["active"], rows["gina"]["account"]), (False, "none"))
        self.assertNotIn("svc-ha", rows)
        self.assertEqual([a["name"] for a in data["service_accounts"]],
                         ["svc-docker-cvgl-node01", "svc-ha", "svc-monitor"])
        self.assertEqual(data["service_accounts"][1]["actions"], [])
        self.assertIsNotNone(data["users_captured_at"])

    def test_accounts_missing_from_the_list_are_shown(self):
        ClusterUser.objects.filter(username="erin").delete()
        rows, _ = self.rows()
        self.assertEqual((rows["erin"]["listed"], rows["erin"]["active"], rows["erin"]["account"]),
                         (False, None, "disabled"))
        self.assertContains(self.client.get("/admin/users"), "Not in the user list")

    def test_filters(self):
        def change(data):
            for a in data["accounts"]:
                if a["name"] == "bob":
                    a["enabled"] = True
        self.write_status(change)
        self.master.users["bob"] = ("pw-bob", False, False)  # deactivated in Determined, proxy access still on
        self.sign_in("carol", client=https_client())
        for flt, expected in (("no-account", ["Hana.W", "dave", "frank", "gina"]), ("disabled", ["erin"]),
                              ("private", ["carol", "erin"]), ("inactive", ["bob"])):
            rows, data = self.rows(f"?filter={flt}")
            self.assertEqual(sorted(rows), sorted(expected), flt)
            self.assertEqual(data["counts"][flt], len(expected), flt)
            page = self.client.get(f"/admin/users?filter={flt}")
            for name in expected:
                self.assertContains(page, f">{name}<", msg_prefix=flt)
        rows, _ = self.rows("?filter=bogus")
        self.assertEqual(len(rows), 8)

    def test_search(self):
        rows, _ = self.rows("?q=AR")
        self.assertEqual(sorted(rows), ["carol"])
        rows, _ = self.rows("?q=a&filter=private")
        self.assertEqual(sorted(rows), ["carol"])
        page = self.client.get("/admin/users?q=fr")
        self.assertContains(page, ">frank<")
        self.assertNotContains(page, ">alice<")
        self.assertContains(page, 'href="/admin/accounts/add?username=frank"')

    def test_list_time_and_no_list_yet(self):
        page = self.client.get("/admin/users")
        self.assertContains(page, "User list as of ")
        self.assertContains(page, ", refreshed at each admin sign-in")
        ClusterUser.objects.all().delete()
        page = self.client.get("/admin/users")
        self.assertContains(page, "No user list yet")
        self.assertContains(page, ">alice<")  # the accounts are still listed

    def test_status_unavailable(self):
        self.status_file.unlink()
        rows, data = self.rows()
        self.assertEqual(rows["alice"]["account"], None)
        self.assertEqual(rows["alice"]["actions"], [])
        self.assertEqual(rows["frank"]["actions"], [])
        self.assertEqual(data["service_accounts"], [])
        self.assertContains(self.client.get("/admin/users"),
                            "Account information is unavailable right now, so no change can be requested.")

    def test_actions_create_request_files(self):
        cases = (("add", "frank", None), ("rotate", "alice", "alice"), ("disable", "alice", "alice"),
                 ("enable", "bob", None), ("admin-on", "bob", None), ("admin-off", "carol", "carol"),
                 ("remove", "erin", "erin"))
        page = self.client.get("/admin/users").content.decode()
        for op, name, confirm in cases:
            link = f'href="/admin/accounts/{op}?username={name}"'
            self.assertIn(link, page, op)
            self.assertEqual(self.client.get(f"/admin/accounts/{op}?username={name}").status_code, 200, op)
            data = {"username": name}
            if confirm:
                data["confirm"] = confirm
            response = self.client.post(f"/admin/accounts/{op}", data)
            self.assertEqual(response.status_code, 302, op)
            request_id = response["Location"].rsplit("/", 1)[1]
            path = self.spool / "requests" / f"{request_id}.json"
            self.assertEqual(json.loads(path.read_text()), {"id": request_id, "op": op, "username": name})
            row = AccountRequest.objects.get(id=request_id)
            self.assertEqual((row.requested_by, row.self_service), ("carol", False))
        self.assertEqual(len(os.listdir(self.spool / "requests")), len(cases))
        rows, _ = self.rows()
        self.assertEqual(rows["frank"]["pending"]["op"], "add")
        self.assertContains(self.client.get("/admin/users"), "Back to the users", count=0)
        self.assertContains(self.client.get("/admin/accounts/rotate?username=alice"), 'href="/admin/users"')

    def test_no_create_for_invalid_names(self):
        response = self.client.post("/admin/accounts/add", {"username": "Hana.W"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(os.listdir(self.spool / "requests"), [])

    def test_service_accounts_as_before(self):
        page = self.client.get("/admin/users")
        self.assertContains(page, "Changed outside the portal only", count=2)
        self.assertContains(page, 'href="/admin/accounts/rotate?username=svc-docker-cvgl-node01"')
        self.assertNotContains(page, "username=svc-ha")
        self.assertEqual(self.client.post("/admin/accounts/disable", {"username": "svc-ha", "confirm": "svc-ha"})
                         .status_code, 403)

    def test_open_proxies_with_raw_auth_off(self):
        """Named for what stays so once the public proxies' own addresses require an account."""
        page = self.client.get("/admin/users")
        self.assertContains(page, "a public proxy&#x27;s own address still works for that user. us-west-racknerd-12tb, "
                                  "usca1-bwh-megaboxpro-2tb need no account at their own addresses even when the "
                                  "public proxies require one.")
        zh = self.client.get("/admin/users", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(zh, "该用户仍可使用公共代理自己的地址。即使公共代理需要账号，us-west-racknerd-12tb、"
                                "usca1-bwh-megaboxpro-2tb 自己的地址也无需账号。")

        def one_open(data):
            next(p for p in data["proxies"] if p["name"] == "usca1-bwh-megaboxpro-2tb")["open"] = False
        self.write_status(one_open)
        self.assertContains(self.client.get("/admin/users"), "us-west-racknerd-12tb needs no account at its own "
                                                             "address even when the public proxies require one.")

        def none_open(data):
            for proxy in data["proxies"]:
                proxy["open"] = False
        self.write_status(none_open)
        self.assertNotContains(self.client.get("/admin/users"), "even when the public proxies require one")

    def test_open_proxies_with_raw_auth_on(self):
        self.write_status(lambda d: d.update(raw_auth=True))
        page = self.client.get("/admin/users")
        self.assertContains(page, "own addresses require an account, except those of us-west-racknerd-12tb, "
                                  "usca1-bwh-megaboxpro-2tb, which need none: an account whose proxy access is off is "
                                  "refused everywhere else.")
        zh = self.client.get("/admin/users", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(zh, "us-west-racknerd-12tb、usca1-bwh-megaboxpro-2tb 的地址除外（无需账号）")

    def test_chinese(self):
        page = self.client.get("/admin/users", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "用户列表")
        self.assertContains(page, "服务账号")
        self.assertNotContains(page, "refreshed at each admin sign-in")
