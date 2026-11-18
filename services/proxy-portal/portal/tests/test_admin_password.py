"""The one-time password of an add or rotate: shown once, to the requesting admin only, deleted at once, never kept."""

import json
import logging
import os
import pickle

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import connection

from portal.models import AccountRequest

from .base import PortalTransactionTestCase, https_client

SENTINEL = "Sentinel-OneTime-9d2b7c41"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record) + repr(record.args) + repr(getattr(record, "request", "")))


class PasswordTest(PortalTransactionTestCase):
    def setUp(self):
        super().setUp()
        self.capture = Capture()
        self.loggers = [logging.getLogger(), logging.getLogger("portal"), logging.getLogger("django")]
        self.levels = [lg.level for lg in self.loggers]
        for lg in self.loggers:
            lg.addHandler(self.capture)
            lg.setLevel(logging.DEBUG)
        self.sign_in("carol")

    def tearDown(self):
        for lg, level in zip(self.loggers, self.levels):
            lg.removeHandler(self.capture)
            lg.setLevel(level)
        super().tearDown()

    def add(self, username="dave", op="add"):
        response = self.client.post("/api/admin/requests", json.dumps({"op": op, "username": username,
                                                                        "confirm": username}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 202)
        return response.json()["id"]

    def applied(self, request_id, username="dave", op="add", result=True, password=SENTINEL, **extra):
        (self.spool / "status" / f"{request_id}.json").write_text(json.dumps(dict(
            {"id": request_id, "op": op, "username": username, "status": "applied", "changed": True,
             "restarted": ["xray-ha"], "result": result, "updated_at": "2026-01-01T00:00:00Z"}, **extra)))
        if result and password:
            path = self.spool / "results" / f"{request_id}.json"
            path.write_text(json.dumps({"id": request_id, "username": username, "password": password}) + "\n")
            os.chmod(path, 0o400)
            return path
        return None

    def assert_not_kept(self):
        connection.close()  # the database file as it is on disk
        raw = open(settings.DATABASES["default"]["NAME"], "rb").read()
        self.assertNotIn(SENTINEL.encode(), raw)
        for session in Session.objects.all():
            self.assertNotIn(SENTINEL, json.dumps(session.get_decoded()))
            self.assertNotIn(SENTINEL, session.session_data)
        self.assertNotIn(SENTINEL.encode(), pickle.dumps(dict(cache._cache)))
        self.assertNotIn(SENTINEL, "\n".join(self.capture.lines))
        self.assertTrue(self.capture.lines)  # the capture works: the portal logged the requests

    def test_shown_once_then_deleted(self):
        request_id = self.add()
        self.assertEqual(self.client.get(f"/api/admin/requests/{request_id}").json()["password"], "waiting")
        path = self.applied(request_id)
        page = self.client.get(f"/admin/requests/{request_id}")
        self.assertContains(page, "Show the password once")
        self.assertNotContains(page, SENTINEL)
        polled = self.client.get(f"/api/admin/requests/{request_id}")
        self.assertEqual(polled.json()["password"], "ready")
        self.assertNotIn(SENTINEL, polled.content.decode())
        self.assertTrue(path.exists())

        shown = self.client.post(f"/admin/requests/{request_id}/password")
        self.assertContains(shown, SENTINEL, count=1)
        self.assertContains(shown, "Shown once: this password will not be shown again.")
        self.assertContains(shown, "The portal does not keep it.")
        self.assertContains(shown, 'data-copy="one-time-password"')
        self.assertIn("no-store", shown["Cache-Control"])
        self.assertFalse(path.exists())

        again = self.client.post(f"/admin/requests/{request_id}/password")
        self.assertNotContains(again, SENTINEL)
        self.assertContains(again, "The password was shown once; the portal does not keep it.")
        self.assertNotContains(self.client.get(f"/admin/requests/{request_id}"), "Show the password once")
        row = AccountRequest.objects.get(id=request_id)
        self.assertEqual(row.result_state, "shown")
        self.assert_not_kept()

    def test_rotate_unconfirmed_still_hands_out(self):
        request_id = self.add("alice", op="rotate")
        self.applied(request_id, "alice", op="rotate", status="unconfirmed", stage="verify")
        self.assertContains(self.client.post(f"/admin/requests/{request_id}/password"), SENTINEL)

    def test_other_admin_refused(self):
        request_id = self.add()
        path = self.applied(request_id)
        erin = https_client()
        self.sign_in("erin", client=erin)
        page = erin.get(f"/admin/requests/{request_id}")
        self.assertContains(page, "Only carol, who made this request, can see the new password.")
        self.assertNotContains(page, "Show the password once")
        response = erin.post(f"/admin/requests/{request_id}/password")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SENTINEL, response.content.decode())
        self.assertTrue(path.exists())
        self.assertContains(self.client.post(f"/admin/requests/{request_id}/password"), SENTINEL)
        self.assert_not_kept()

    def test_expired(self):
        request_id = self.add()
        self.applied(request_id, password=None)  # result: true, but the file is gone
        page = self.client.get(f"/admin/requests/{request_id}")
        self.assertContains(page, "The one-time password expired")
        response = self.client.post(f"/admin/requests/{request_id}/password")
        self.assertContains(response, "The one-time password expired")

    def test_expired_between_page_and_click(self):
        request_id = self.add()
        path = self.applied(request_id)
        self.client.get(f"/admin/requests/{request_id}")
        os.unlink(path)
        self.assertContains(self.client.post(f"/admin/requests/{request_id}/password"), "expired")

    def test_superseded(self):
        request_id = self.add()
        self.applied(request_id, result=False, superseded_by="1b2c3d4e-0000-4000-8000-000000000000")
        self.assertContains(self.client.get(f"/admin/requests/{request_id}"),
                            "A later request replaced this password: use the password of request "
                            "1b2c3d4e-0000-4000-8000-000000000000.")

    def test_superseded_by_a_remove(self):
        rotate = self.add("bob", op="rotate")
        remove = self.add("bob", op="remove")
        self.applied(rotate, "bob", op="rotate", result=False, superseded_by=remove)
        self.applied(remove, "bob", op="remove", result=False)
        text = self.client.get(f"/api/admin/requests/{rotate}").json()["password_text"]
        self.assertEqual(text, f"The account was removed by request {remove}: this password no longer works.")

    def test_superseded_in_a_later_run(self):
        # two applier runs: the first result stays, and its status names no later request
        first = self.add("alice", op="rotate")
        first_path = self.applied(first, "alice", op="rotate", password="Old-Pass-1111", applied_seq=1)
        second = self.add("alice", op="rotate")
        second_path = self.applied(second, "alice", op="rotate", applied_seq=2)
        data = self.client.get(f"/api/admin/requests/{first}").json()  # the second is not looked at first
        self.assertEqual(data["password"], "superseded")
        self.assertIn(f"use the password of request {second}", data["password_text"])
        self.assertNotContains(self.client.get(f"/admin/requests/{first}"), "Show the password once")
        response = self.client.post(f"/admin/requests/{first}/password")
        self.assertNotContains(response, "Old-Pass-1111")
        self.assertContains(response, f"use the password of request {second}")
        self.assertFalse(first_path.exists())
        self.assertEqual(self.client.get(f"/api/admin/requests/{second}").json()["password"], "ready")
        self.assertContains(self.client.post(f"/admin/requests/{second}/password"), SENTINEL)
        self.assertFalse(second_path.exists())

    def test_order_applied_not_order_requested(self):
        # A is recorded first, B second; the applier took B first (spool order), then A: A's password is live
        a = self.add("alice", op="rotate")
        b = self.add("alice", op="rotate")
        self.applied(b, "alice", op="rotate", password="B-Pass-3333", applied_seq=7)
        self.applied(a, "alice", op="rotate", applied_seq=8)
        self.assertEqual(self.client.get(f"/api/admin/requests/{a}").json()["password"], "ready")
        data = self.client.get(f"/api/admin/requests/{b}").json()
        self.assertEqual(data["password"], "superseded")
        self.assertIn(f"use the password of request {a}", data["password_text"])
        self.assertContains(self.client.post(f"/admin/requests/{a}/password"), SENTINEL)
        self.assertNotContains(self.client.post(f"/admin/requests/{b}/password"), "B-Pass-3333")

    def test_without_a_recorded_order_nothing_is_inferred(self):
        # statuses without applied_seq (an older applier): only its own superseded_by counts, never requested_at
        first = self.add("alice", op="rotate")
        self.applied(first, "alice", op="rotate")
        second = self.add("alice", op="rotate")
        self.applied(second, "alice", op="rotate", password="Second-4444")
        self.assertEqual(self.client.get(f"/api/admin/requests/{first}").json()["password"], "ready")
        # the applier's own superseded_by stands, whatever the order says
        third = self.add("bob", op="rotate")
        self.applied(third, "bob", op="rotate", result=False, superseded_by=second, applied_seq=9)
        self.applied(self.add("bob", op="rotate"), "bob", op="rotate", applied_seq=3)
        data = self.client.get(f"/api/admin/requests/{third}").json()
        self.assertEqual(data["password"], "superseded")
        self.assertIn(f"use the password of request {second}", data["password_text"])

    def test_removed_in_a_later_run(self):
        first = self.add("bob", op="rotate")
        path = self.applied(first, "bob", op="rotate", password="Old-Pass-2222", applied_seq=1)
        remove = self.add("bob", op="remove")
        self.applied(remove, "bob", op="remove", result=False, applied_seq=2)
        response = self.client.post(f"/admin/requests/{first}/password")
        self.assertNotContains(response, "Old-Pass-2222")
        self.assertContains(response, f"The account was removed by request {remove}: this password no longer works.")
        self.assertFalse(path.exists())

    def test_not_superseded_by_a_failed_or_unchanged_request(self):
        first = self.add("alice", op="rotate")
        self.applied(first, "alice", op="rotate")
        failed = self.add("alice", op="rotate")
        (self.spool / "status" / f"{failed}.json").write_text(json.dumps(
            {"id": failed, "status": "failed", "stage": "verify", "error": "x", "restored": True}))
        self.add("alice", op="disable")  # not a password change
        pending = self.add("alice", op="remove")  # not live yet: the password still works
        self.assertEqual(AccountRequest.objects.get(id=pending).state, "pending")
        self.assertEqual(self.client.get(f"/api/admin/requests/{first}").json()["password"], "ready")
        self.assertContains(self.client.post(f"/admin/requests/{first}/password"), SENTINEL)

    def test_not_written(self):
        request_id = self.add()
        self.applied(request_id, result=False)
        self.assertContains(self.client.get(f"/admin/requests/{request_id}"), "No password was handed out")

    def test_failed(self):
        request_id = self.add()
        (self.spool / "status" / f"{request_id}.json").write_text(json.dumps(
            {"id": request_id, "status": "failed", "stage": "render", "error": "x", "restored": True}))
        self.assertContains(self.client.get(f"/admin/requests/{request_id}"),
                            "No password: the change was not applied.")
        response = self.client.post(f"/admin/requests/{request_id}/password")
        self.assertNotContains(response, "Shown once")

    def test_planted_files_refused_and_removed(self):
        for kind in ("symlink", "mismatch", "fifo"):
            request_id = self.add()
            self.applied(request_id, password=None)
            path = self.spool / "results" / f"{request_id}.json"
            if kind == "symlink":
                target = self.spool / "elsewhere.json"
                target.write_text(json.dumps({"id": request_id, "username": "dave", "password": SENTINEL}))
                os.symlink(target, path)
            elif kind == "mismatch":
                path.write_text(json.dumps({"id": request_id, "username": "mallory", "password": SENTINEL}))
            else:
                os.mkfifo(path)
            response = self.client.post(f"/admin/requests/{request_id}/password")
            self.assertNotContains(response, SENTINEL)
            self.assertContains(response, "could not be read and was deleted")
            self.assertFalse(os.path.lexists(path), kind)
            self.assertEqual(AccountRequest.objects.get(id=request_id).result_state, "unreadable")

    def test_not_for_other_ops(self):
        response = self.client.post("/api/admin/requests", json.dumps({"op": "enable", "username": "bob"}),
                                    content_type="application/json")
        request_id = response.json()["id"]
        self.assertIsNone(response.json()["password"])
        self.assertNotContains(self.client.post(f"/admin/requests/{request_id}/password"), "Shown once")
