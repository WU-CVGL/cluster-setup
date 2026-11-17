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
        self.assertContains(shown, 'data-copy="one-time-password"')
        self.assertIn("no-store", shown["Cache-Control"])
        self.assertFalse(path.exists())

        again = self.client.post(f"/admin/requests/{request_id}/password")
        self.assertNotContains(again, SENTINEL)
        self.assertContains(again, "The password was shown once and is not kept anywhere.")
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
