"""Every admin page and API: anonymous users to sign-in (pages) or 401 (APIs), non-admins 403, CSRF on every post."""

import json
import os

from django.urls import get_resolver

from portal import admin_views
from portal.models import AccountRequest, Incident

from .base import PortalTestCase, https_client

RID = "0f8e1c2a-1111-4222-8333-944455556666"


def admin_patterns():
    """(name, path, kind) of every URL whose view is an admin view, with sample arguments."""
    result = []
    for pattern in get_resolver().url_patterns:
        callback = pattern.callback
        kind = getattr(callback, "admin_only", None)
        name = pattern.name or ""
        if kind is None:
            continue
        route = str(pattern.pattern)
        path = "/" + route.replace("<rid:request_id>", RID).replace("<int:pk>", "1").replace("<str:op>", "disable")
        result.append((name, path, kind))
    return result


class AdminGuardTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        AccountRequest.objects.create(id=RID, op="disable", username="alice", requested_by="carol")
        Incident.objects.create(id=1, title="t", component="proxies", state="investigating",
                                started_at="2026-01-01T00:00:00Z")

    def test_every_admin_url_is_guarded(self):
        names = {name for name, _, _ in admin_patterns()}
        everything = {p.name for p in get_resolver().url_patterns}
        # every route named admin_* or api_admin_* is an admin view, and nothing else is
        self.assertEqual(names, {n for n in everything if n.startswith(("admin_", "api_admin_"))})
        self.assertIn("admin_proxies", names)

    def test_anonymous(self):
        for name, path, kind in admin_patterns():
            for method in ("get", "post"):
                response = getattr(https_client(enforce_csrf_checks=True), method)(path)
                if kind == "api":
                    self.assertEqual(response.status_code, 401, (method, path))
                    self.assertEqual(response.json(), {"error": "sign-in required"})
                else:
                    self.assertEqual(response.status_code, 302, (method, path))
                    self.assertEqual(response["Location"], "/signin?next=" + path)

    def test_not_admin(self):
        self.sign_in("alice")  # not a Determined admin
        for name, path, kind in admin_patterns():
            for method in ("get", "post"):
                response = getattr(self.client, method)(path, {"op": "disable", "username": "bob", "confirm": "bob"})
                self.assertEqual(response.status_code, 403, (method, path))
                if kind == "page":
                    self.assertContains(response, "Admins only", status_code=403)
        self.assertEqual(os.listdir(self.spool / "requests"), [])
        self.assertEqual(AccountRequest.objects.count(), 1)

    def test_admin_flag_from_the_session_only(self):
        # an admin account in the proxy accounts file is not a Determined admin
        self.master.users["carol"] = ("pw-carol", False, True)
        self.sign_in("carol")
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertNotContains(self.client.get("/"), 'href="/admin/users"')

    def test_admin_allowed(self):
        self.sign_in("carol")
        for name, path, kind in admin_patterns():
            response = self.client.get(path + ("?username=alice" if name == "admin_confirm" else ""))
            self.assertIn(response.status_code, (302,) if name == "admin_accounts" else (200, 405), (name, path))

    def test_nav_link_for_admins_only(self):
        self.assertNotContains(self.client.get("/"), 'href="/admin/users"')
        self.sign_in("alice")
        self.assertNotContains(self.client.get("/"), 'href="/admin/users"')
        admin = https_client()
        self.sign_in("carol", client=admin)
        self.assertContains(admin.get("/"), 'href="/admin/users"')


class CsrfTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.client = https_client(enforce_csrf_checks=True)
        self.client.get("/signin")
        token = self.client.cookies["proxyportal_csrf"].value
        self.client.post("/signin", {"username": "carol", "password": "pw-carol", "csrfmiddlewaretoken": token})
        self.assertEqual(self.client.session["username"], "carol")

    def token(self):
        page = self.client.get("/admin/users").content.decode()
        return page.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]

    def test_api_post_needs_the_token(self):
        body = json.dumps({"op": "enable", "username": "bob"})
        response = self.client.post("/api/admin/requests", body, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(os.listdir(self.spool / "requests"), [])
        response = self.client.post("/api/admin/requests", body, content_type="application/json",
                                    HTTP_X_CSRFTOKEN=self.token())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(os.listdir(self.spool / "requests")), 1)

    def test_form_posts_need_the_token(self):
        for path, data in (("/admin/accounts/disable", {"username": "alice", "confirm": "alice"}),
                           ("/admin/incidents", {"title": "x", "component": "proxies", "state": "investigating"}),
                           (f"/admin/requests/{RID}/password", {})):
            self.assertEqual(self.client.post(path, data).status_code, 403, path)
        self.assertEqual(os.listdir(self.spool / "requests"), [])
        self.assertEqual(Incident.objects.count(), 0)
        response = self.client.post("/admin/accounts/disable", {"username": "alice", "confirm": "alice",
                                                                "csrfmiddlewaretoken": self.token()})
        self.assertEqual(response.status_code, 302)

    def test_not_admin_without_token(self):
        alice = https_client(enforce_csrf_checks=True)
        alice.get("/signin")
        alice.post("/signin", {"username": "alice", "password": "pw-alice",
                               "csrfmiddlewaretoken": alice.cookies["proxyportal_csrf"].value})
        self.assertEqual(alice.session["username"], "alice")
        for name, path, kind in admin_patterns():
            response = alice.post(path, {"op": "disable", "username": "bob", "confirm": "bob"})
            self.assertEqual(response.status_code, 403, path)
            if kind == "page":
                self.assertContains(response, "Admins only", status_code=403)
            else:
                self.assertEqual(response.json(), {"error": "Determined admins only"})
        self.assertEqual(os.listdir(self.spool / "requests"), [])
        self.assertEqual(Incident.objects.count(), 0)

    def test_other_origin_refused(self):
        token = self.token()
        response = self.client.post("/admin/accounts/disable", {"username": "alice", "confirm": "alice",
                                                                "csrfmiddlewaretoken": token},
                                    HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(os.listdir(self.spool / "requests"), [])


class GuardUnitTest(PortalTestCase):
    def test_decorator_marks_views(self):
        self.assertEqual(admin_views.accounts_page.admin_only, "page")
        self.assertEqual(admin_views.api_requests.admin_only, "api")
