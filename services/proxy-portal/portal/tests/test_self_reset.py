"""A user's reset of their own proxy password from My access: own account only, limits, and a result for them only."""

import datetime
import json
import logging
import os

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from portal.models import AccountRequest

from .base import PortalTestCase, PortalTransactionTestCase, https_client
from .test_admin_password import Capture

SENTINEL = "Sentinel-SelfReset-5e1a90c2"


class SelfResetMixin:
    def reset(self, client=None, **kwargs):
        return (client or self.client).post("/api/me/reset", **kwargs)

    def files(self):
        return sorted(os.listdir(self.spool / "requests"))

    def request_file(self, request_id):
        return json.loads((self.spool / "requests" / f"{request_id}.json").read_text())

    def status(self, request_id, state="applied", result=True, password=SENTINEL, username="alice", **extra):
        (self.spool / "status" / f"{request_id}.json").write_text(json.dumps(dict(
            {"id": request_id, "op": "rotate", "username": username, "status": state, "changed": True,
             "restarted": ["xray-ha"], "result": result if state in ("applied", "unconfirmed") else None,
             "updated_at": "2026-01-01T00:00:00Z"}, **extra)))
        if state in ("applied", "unconfirmed") and result and password:
            path = self.spool / "results" / f"{request_id}.json"
            path.write_text(json.dumps({"id": request_id, "username": username, "password": password}) + "\n")
            os.chmod(path, 0o400)
            return path
        return None


class SelfResetTest(SelfResetMixin, PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("alice")

    def test_own_account_only(self):
        response = self.reset()
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(self.files(), [f"{data['id']}.json"])
        self.assertEqual(self.request_file(data["id"]), {"id": data["id"], "op": "rotate", "username": "alice"})
        row = AccountRequest.objects.get(id=data["id"])
        self.assertEqual((row.op, row.username, row.requested_by, row.self_service), ("rotate", "alice", "alice", True))
        self.assertEqual((data["state"], data["password"]), ("pending", "waiting"))
        # what a user sees: no stage, error or restarted services
        self.assertFalse({"stage", "error", "restarted", "requested_by", "username"} & set(data))

    def test_supplied_username_refused(self):
        for kwargs in ({"data": {"username": "bob"}},
                       {"data": json.dumps({"username": "bob"}), "content_type": "application/json"},
                       {"data": json.dumps({"op": "remove"}), "content_type": "application/json"},
                       {"data": "[]", "content_type": "application/json"}):
            response = self.reset(**kwargs)
            self.assertEqual(response.status_code, 400, kwargs)
        self.assertEqual(self.client.post("/api/me/reset?username=bob").status_code, 400)
        self.assertEqual(self.files(), [])
        self.assertEqual(AccountRequest.objects.count(), 0)
        # an empty JSON object is nothing
        self.assertEqual(self.reset(data="{}", content_type="application/json").status_code, 202)
        self.assertEqual(self.request_file(self.files()[0][:-5])["username"], "alice")

    def test_methods_and_anonymous(self):
        self.assertEqual(self.client.get("/api/me/reset").status_code, 405)
        anonymous = https_client()
        for method, path in (("post", "/api/me/reset"), ("get", f"/api/me/reset/{'a' * 36}"),
                             ("post", f"/api/me/reset/{'a' * 36}/password")):
            response = getattr(anonymous, method)(path)
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual(self.files(), [])

    def test_pending_refused(self):
        first = self.reset().json()["id"]
        response = self.reset()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "pending")
        self.status(first, "applying")
        self.assertEqual(self.reset().status_code, 409)
        self.status(first, "applied")
        self.assertEqual(self.reset().status_code, 202)  # the outcome is read before the check

    def test_admin_request_for_the_account_pending_refused(self):
        admin = https_client()
        self.sign_in("carol", client=admin)
        admin.post("/api/admin/requests", json.dumps({"op": "admin-on", "username": "alice"}),
                   content_type="application/json")
        response = self.reset()
        self.assertEqual((response.status_code, response.json()["reason"]), (409, "pending"))

    def test_rate_limit(self):
        for _ in range(settings.SELF_RESET_MAX_PER_HOUR):
            request_id = self.reset().json()["id"]
            self.status(request_id, "failed", stage="verify", restored=True)
        response = self.reset()
        self.assertEqual(response.status_code, 429)
        self.assertIn("3 times per hour", response.json()["error"])
        self.assertEqual(len(self.files()), 3)
        # an hour later it is allowed again
        AccountRequest.objects.update(requested_at=timezone.now() - datetime.timedelta(minutes=61))
        self.assertEqual(self.reset().status_code, 202)

    @override_settings(SELF_RESET_MAX_PER_HOUR=1)
    def test_rate_limit_setting(self):
        request_id = self.reset().json()["id"]
        self.status(request_id)
        self.assertEqual(self.reset().status_code, 429)
        self.assertContains(self.client.get("/access"), "1 time per hour")

    def test_disabled_account_refused(self):
        bob = https_client()
        self.sign_in("bob", client=bob)
        response = self.reset(bob)
        self.assertEqual((response.status_code, response.json()["reason"]), (409, "disabled"))
        page = bob.get("/access")
        self.assertContains(page, "Password reset is unavailable while proxy access is off for your account.")
        self.assertContains(page, 'id="reset-open" hidden disabled')
        self.assertEqual(self.files(), [])

    def test_no_account_refused(self):
        dave = https_client()
        self.master.users["dave"] = ("pw-dave", False, True)
        self.sign_in("dave", client=dave)
        response = self.reset(dave)
        self.assertEqual((response.status_code, response.json()["reason"]), (404, "none"))
        page = dave.get("/access")
        self.assertNotContains(page, "reset-open")
        self.assertNotContains(page, "<dialog")
        self.assertContains(page, "Need an account? Ask an admin.")

    def test_service_account_refused(self):
        for name in ("svc-docker-cvgl-node01", "svc-ha"):
            self.master.users[name] = (f"pw-{name}", False, True)
            client = https_client()
            self.sign_in(name, client=client)
            response = self.reset(client)
            self.assertEqual((response.status_code, response.json()["reason"]), (404, "none"), name)
            self.assertNotContains(client.get("/access"), "reset-open")
        self.assertEqual(self.files(), [])

    def test_status_unavailable(self):
        self.status_file.unlink()
        self.assertEqual(self.reset().status_code, 503)
        self.assertNotContains(self.client.get("/access"), "reset-open")

    def test_button_and_modal_on_my_access(self):
        page = self.client.get("/access")
        self.assertContains(page, 'id="reset-open" hidden>Reset proxy password</button>')
        self.assertContains(page, '<dialog class="modal" id="reset-dialog"')
        self.assertContains(page, "The old password stops working: update every client that uses it.")
        self.assertContains(page, "Applying restarts the HA entries (xray-ha). Established connections through them "
                                  "drop")
        self.assertNotContains(page, "private proxies. Established")
        carol = https_client()
        self.sign_in("carol", client=carol)  # an admin account: the private proxies restart too
        self.assertContains(carol.get("/access"), "the HA entries (xray-ha) and the private proxies")

    def test_my_access_shows_the_applied_state_only(self):
        request_id = self.reset().json()["id"]
        page = self.client.get("/access")
        self.assertContains(page, "A password reset is in progress")
        self.assertContains(page, f'data-current="{request_id}"')
        self.assertContains(page, "Proxy access is <b>on</b> for the account <b>alice</b>.")
        self.status(request_id)
        self.assertContains(self.client.get("/access"), "Your new password is ready")

    def test_resumed_after_more_than_an_hour(self):
        # an applier that took long: the reset is still the user's to follow and collect, not a stranger's pending
        request_id = self.reset().json()["id"]
        AccountRequest.objects.update(requested_at=timezone.now() - datetime.timedelta(hours=2))
        page = self.client.get("/access")
        self.assertContains(page, f'data-current="{request_id}"')
        self.assertNotContains(page, "still waiting or being applied")
        self.status(request_id)
        self.assertContains(self.client.get("/access"), "Your new password is ready")
        self.assertEqual(self.client.post(f"/api/me/reset/{request_id}/password").json()["value"], SENTINEL)

    def test_state_only_for_the_requester(self):
        request_id = self.reset().json()["id"]
        self.status(request_id)
        mine = self.client.get(f"/api/me/reset/{request_id}")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual((mine.json()["state"], mine.json()["password"]), ("applied", "ready"))
        self.assertNotIn(SENTINEL, mine.content.decode())
        self.assertEqual(self.client.get(f"/api/me/reset/{request_id}?x=1").status_code, 400)
        self.assertEqual(self.client.get(f"/api/me/reset/{'b' * 36}").status_code, 404)
        for name in ("carol", "bob"):  # an admin and another user
            other = https_client()
            self.sign_in(name, client=other)
            self.assertEqual(other.get(f"/api/me/reset/{request_id}").status_code, 403, name)
            response = other.post(f"/api/me/reset/{request_id}/password")
            self.assertEqual(response.status_code, 403, name)
            self.assertNotIn(SENTINEL, response.content.decode())
        # nor through the admin console
        admin = https_client()
        self.sign_in("carol", client=admin)
        self.assertContains(admin.get(f"/admin/requests/{request_id}"), "Only alice, who made this request")
        response = admin.post(f"/admin/requests/{request_id}/password")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SENTINEL, response.content.decode())
        self.assertTrue((self.spool / "results" / f"{request_id}.json").exists())

    def test_admin_requests_not_collected_here(self):
        # an admin's rotate of alice's account: the user cannot follow it or collect its password
        admin = https_client()
        self.sign_in("carol", client=admin)
        request_id = admin.post("/api/admin/requests", json.dumps({"op": "rotate", "username": "alice",
                                                                   "confirm": "alice"}),
                                content_type="application/json").json()["id"]
        self.status(request_id)
        self.assertEqual(self.client.get(f"/api/me/reset/{request_id}").status_code, 403)
        self.assertEqual(self.client.post(f"/api/me/reset/{request_id}/password").status_code, 403)
        # an admin's own console request is not a self-service one either
        own = admin.post("/api/admin/requests", json.dumps({"op": "rotate", "username": "carol", "confirm": "carol"}),
                         content_type="application/json").json()["id"]
        self.assertEqual(admin.get(f"/api/me/reset/{own}").status_code, 404)

    def test_failed_and_unconfirmed(self):
        request_id = self.reset().json()["id"]
        self.status(request_id, "failed", stage="verify", error="ha-general: 502", restored=True)
        data = self.client.get(f"/api/me/reset/{request_id}").json()
        self.assertEqual((data["state"], data["level"], data["password"]), ("failed", "bad", "none"))
        self.assertIn("Failed; the previous state is back", data["text"])
        self.assertNotIn("502", json.dumps(data))
        second = self.reset().json()["id"]
        self.status(second, "unconfirmed")
        data = self.client.get(f"/api/me/reset/{second}").json()
        self.assertEqual((data["state"], data["password"]), ("unconfirmed", "ready"))
        self.assertIn("The new password is not confirmed yet", data["text"])
        shown = self.client.post(f"/api/me/reset/{second}/password")
        self.assertEqual(shown.json()["value"], SENTINEL)

    def test_chinese(self):
        page = self.client.get("/access", HTTP_ACCEPT_LANGUAGE="zh-CN")
        self.assertContains(page, "重置代理密码")
        self.assertNotContains(page, "Reset proxy password")
        request_id = self.reset(HTTP_ACCEPT_LANGUAGE="zh-CN").json()["id"]
        data = self.client.get(f"/api/me/reset/{request_id}", HTTP_ACCEPT_LANGUAGE="zh-CN").json()
        self.assertEqual(data["state_label"], "等待中")
        self.assertNotIn("Waiting", data["text"])


class SelfResetCsrfTest(SelfResetMixin, PortalTestCase):
    def test_posts_need_the_token(self):
        client = https_client(enforce_csrf_checks=True)
        client.get("/signin")
        client.post("/signin", {"username": "alice", "password": "pw-alice",
                                "csrfmiddlewaretoken": client.cookies["proxyportal_csrf"].value})
        self.assertEqual(client.session["username"], "alice")
        self.assertEqual(self.reset(client).status_code, 403)
        self.assertEqual(self.files(), [])
        page = client.get("/access").content.decode()
        token = page.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
        self.assertEqual(self.reset(client, HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://evil.example").status_code,
                         403)
        self.assertEqual(self.files(), [])
        response = self.reset(client, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 202)
        request_id = response.json()["id"]
        self.status(request_id)
        self.assertEqual(client.post(f"/api/me/reset/{request_id}/password").status_code, 403)
        self.assertTrue((self.spool / "results" / f"{request_id}.json").exists())
        shown = client.post(f"/api/me/reset/{request_id}/password", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(shown.json()["value"], SENTINEL)


class SelfResetPasswordTest(SelfResetMixin, PortalTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.capture = Capture()
        self.logger = logging.getLogger("portal")
        self.logger.addHandler(self.capture)
        self.sign_in("alice")

    def tearDown(self):
        self.logger.removeHandler(self.capture)
        super().tearDown()

    def test_shown_once_then_deleted(self):
        request_id = self.reset().json()["id"]
        self.assertEqual(self.client.post(f"/api/me/reset/{request_id}/password").status_code, 409)  # not live
        path = self.status(request_id)
        shown = self.client.post(f"/api/me/reset/{request_id}/password")
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(shown.json()["value"], SENTINEL)
        self.assertIn("no-store", shown["Cache-Control"])
        self.assertFalse(path.exists())
        again = self.client.post(f"/api/me/reset/{request_id}/password")
        self.assertEqual(again.status_code, 409)
        self.assertNotIn(SENTINEL, again.content.decode())
        self.assertEqual(again.json()["password"], "shown")
        self.assertIn("The password was shown once", again.json()["password_text"])
        self.assertEqual(AccountRequest.objects.get(id=request_id).result_state, "shown")
        self.assertNotContains(self.client.get("/access"), "Your new password is ready")
        connection.close()
        self.assertNotIn(SENTINEL.encode(), open(settings.DATABASES["default"]["NAME"], "rb").read())
        self.assertNotIn(SENTINEL, "\n".join(self.capture.lines))
        self.assertTrue(any("(self-service)" in line for line in self.capture.lines))

    def test_superseded_by_a_later_admin_rotate(self):
        request_id = self.reset().json()["id"]
        path = self.status(request_id, applied_seq=4)
        admin = https_client()
        self.sign_in("carol", client=admin)
        later = admin.post("/api/admin/requests", json.dumps({"op": "rotate", "username": "alice",
                                                              "confirm": "alice"}),
                           content_type="application/json").json()["id"]
        self.status(later, password="Admin-Pass-1", applied_seq=5)
        response = self.client.post(f"/api/me/reset/{request_id}/password")
        self.assertEqual(response.json()["password"], "superseded")
        self.assertNotIn(SENTINEL, response.content.decode())
        self.assertFalse(path.exists())

    def test_expired(self):
        request_id = self.reset().json()["id"]
        os.unlink(self.status(request_id))
        response = self.client.post(f"/api/me/reset/{request_id}/password")
        self.assertEqual(response.json()["password"], "expired")
