"""Account requests: validation before any file is written, the request files, and their lifecycle."""

import datetime
import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from portal import accounts, lifecycle
from portal.models import AccountRequest

from .base import PortalTestCase, https_client

SERVICES = Path(__file__).resolve().parents[3]
APPLIER = SERVICES / "proxy-portal" / "applier" / "proxy_apply.py"


def load_applier():
    """The real applier and proxy_accounts.py of this checkout (not in the portal's image)."""
    sys.path.insert(0, str(SERVICES / "xray" / "scripts"))
    spec = importlib.util.spec_from_file_location("proxy_apply_for_tests", APPLIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NAMES = ["alice", "a", "a.b_c-d", "0abc", "x" * 64, "x" * 65, "Alice", "-alice", ".alice", "_a", "al ice", "al:ice",
         "alice\n", "", "svc-ha", "svc-monitor", "svc-docker-cvgl-node01", "svc-docker-", "svc-docker-x.y",
         "svc-other", "svc-docker-Node", "ålice", "alice>>>", "../etc", "a/b", "svc-", "svc-hax", "svc-docker--a"]


@unittest.skipUnless(APPLIER.exists(), "needs the applier in this checkout")
class ValidatorParityTest(SimpleTestCase):
    def test_same_rule_as_proxy_accounts(self):
        pa = load_applier().pa
        for name in NAMES:
            try:
                pa.validate_name(name)
                expected = True
            except pa.AccountsError:
                expected = False
            self.assertEqual(accounts.valid_name(name), expected, repr(name))

    def test_protected_names_match(self):
        pa = load_applier().pa
        self.assertEqual(set(accounts.PROTECTED), set(pa.PROTECTED))


def requests_in(spool):
    return sorted(os.listdir(spool / "requests"))


class RequestFileTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("carol")

    def post(self, op, username, confirm=None):
        data = {"op": op, "username": username}
        if confirm is not None:
            data["confirm"] = confirm
        return self.client.post("/api/admin/requests", json.dumps(data), content_type="application/json")

    def test_request_file(self):
        response = self.post("disable", "alice", confirm="alice")
        self.assertEqual(response.status_code, 202)
        request_id = response.json()["id"]
        self.assertEqual(requests_in(self.spool), [f"{request_id}.json"])  # no temp file left
        path = self.spool / "requests" / f"{request_id}.json"
        self.assertEqual(json.loads(path.read_text()), {"id": request_id, "op": "disable", "username": "alice"})
        st = os.lstat(path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual((st.st_nlink, st.st_uid), (1, os.getuid()))
        row = AccountRequest.objects.get(id=request_id)
        self.assertEqual((row.op, row.username, row.requested_by, row.state), ("disable", "alice", "carol", "pending"))
        self.assertEqual(response.json()["state"], "pending")

    @unittest.skipUnless(APPLIER.exists(), "needs the applier in this checkout")
    def test_the_applier_accepts_every_op(self):
        ap = load_applier()
        for op, name in (("add", "dave"), ("rotate", "alice"), ("enable", "bob"), ("disable", "alice"),
                         ("remove", "bob"), ("admin-on", "alice"), ("admin-off", "carol"),
                         ("rotate", "svc-docker-cvgl-node01"), ("add", "svc-docker-cvgl-node02")):
            response = self.post(op, name, confirm=name)
            self.assertEqual(response.status_code, 202, (op, name, response.content))
        names = requests_in(self.spool)
        self.assertEqual(len(names), 9)
        spool = ap.Spool(self.spool, os.getuid())
        self.assertEqual(spool.pending(), sorted(names, key=lambda n: os.lstat(self.spool / "requests" / n)
                                                 .st_mtime_ns))
        for name in names:
            request, error, uid = spool.read(name)
            self.assertIsNone(error, name)
            self.assertEqual(uid, os.getuid())
            self.assertEqual(f"{request['id']}.json", name)

    def test_ids_are_new_and_valid(self):
        ids = {self.post("enable", "bob").json()["id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)
        for request_id in ids:
            self.assertRegex(request_id, r"^[a-z0-9][a-z0-9-]{7,63}$")

    def test_invalid_names_refused_before_any_file(self):
        for name in [n for n in NAMES if not accounts.valid_name(n)] + ["a" * 200]:
            for op in ("add", "disable"):
                response = self.post(op, name, confirm=name)
                self.assertEqual(response.status_code, 400, repr(name))
                self.assertIn("Invalid account name", response.json()["error"])
        self.assertEqual(requests_in(self.spool), [])
        self.assertEqual(AccountRequest.objects.count(), 0)

    def test_invalid_request_shapes(self):
        for body in ('{"op": "disable"}', '{"op": "enable", "username": "bob", "password": "x"}', "[1]",
                     '{"op": 1, "username": "bob"}', "not json", '{"op": "explode", "username": "bob"}'):
            response = self.client.post("/api/admin/requests", body, content_type="application/json")
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(requests_in(self.spool), [])

    def test_protected_accounts_refused(self):
        for name in ("svc-ha", "svc-monitor"):
            for op in accounts.OPS:
                response = self.post(op, name, confirm=name)
                self.assertEqual(response.status_code, 403, (op, name))
                self.assertIn("proxy_accounts.py", response.json()["error"])
            page = self.client.get(f"/admin/accounts/disable?username={name}")
            self.assertEqual(page.status_code, 403)
            self.assertNotContains(page, "Send the request", status_code=403)
            self.assertEqual(self.client.post("/admin/accounts/disable", {"username": name, "confirm": name})
                             .status_code, 403)
        self.assertEqual(requests_in(self.spool), [])

    def test_protected_flag_from_the_status_file(self):
        def protect(data):
            next(a for a in data["accounts"] if a["name"] == "svc-docker-cvgl-node01")["protected"] = True
        self.write_status(protect)
        self.assertEqual(self.post("rotate", "svc-docker-cvgl-node01", confirm="svc-docker-cvgl-node01")
                         .status_code, 403)
        self.assertEqual(requests_in(self.spool), [])

    def test_ops_that_cannot_apply(self):
        self.assertEqual(self.post("add", "alice").status_code, 409)
        self.assertEqual(self.post("disable", "nobody", confirm="nobody").status_code, 404)
        self.assertEqual(self.post("admin-on", "svc-docker-cvgl-node01").status_code, 400)
        self.assertEqual(requests_in(self.spool), [])

    def test_destructive_ops_need_confirmation(self):
        for op in accounts.DESTRUCTIVE:
            self.assertEqual(self.post(op, "alice").status_code, 400, op)
            self.assertEqual(self.post(op, "alice", confirm="bob").status_code, 400, op)
            page = self.client.post(f"/admin/accounts/{op}", {"username": "alice", "confirm": "alic"})
            self.assertEqual(page.status_code, 400, op)
            self.assertContains(page, "Type the account name to confirm.", status_code=400)
        self.assertEqual(requests_in(self.spool), [])
        page = self.client.get("/admin/accounts/remove?username=alice")
        self.assertContains(page, 'name="confirm"')
        response = self.client.post("/admin/accounts/remove", {"username": "alice", "confirm": "alice"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(requests_in(self.spool)), 1)
        # enable is not destructive: the confirmation page is the step
        self.assertNotContains(self.client.get("/admin/accounts/enable?username=bob"), 'name="confirm"')
        self.assertEqual(self.client.post("/admin/accounts/enable", {"username": "bob"}).status_code, 302)

    def test_spool_not_writable(self):
        os.rmdir(self.spool / "requests")
        response = self.post("enable", "bob")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(AccountRequest.objects.count(), 0)

    def test_status_unavailable(self):
        self.status_file.unlink()
        self.assertEqual(self.post("enable", "bob").status_code, 503)
        self.assertEqual(requests_in(self.spool), [])

    def test_my_access_shows_the_applied_state_only(self):
        self.post("disable", "alice", confirm="alice")
        alice = https_client()
        self.sign_in("alice", client=alice)
        self.assertEqual(alice.get("/api/me/access").json()["account"]["enabled"], True)


class AccountsListTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("carol")

    def test_api(self):
        data = self.client.get("/api/admin/accounts").json()
        rows = {a["name"]: a for a in data["accounts"]}
        self.assertEqual(rows["svc-ha"], {"name": "svc-ha", "kind": "service", "enabled": True, "admin": False,
                                          "protected": True, "actions": [], "pending": None})
        self.assertEqual(rows["alice"]["actions"], ["rotate", "disable", "admin-on", "remove"])
        self.assertEqual(rows["bob"]["actions"], ["rotate", "enable", "admin-on", "remove"])
        self.assertEqual(rows["carol"]["actions"], ["rotate", "disable", "admin-off", "remove"])
        self.assertEqual(rows["svc-docker-cvgl-node01"]["actions"], ["rotate", "disable", "remove"])
        self.assertIs(data["raw_auth"], False)
        self.assertIn("public proxy's own entry", data["raw_auth_text"])
        self.assertNotIn("password", json.dumps(data).replace("raw_auth", ""))

    def test_search(self):
        names = [a["name"] for a in self.client.get("/api/admin/accounts?q=svc").json()["accounts"]]
        self.assertEqual(names, ["svc-docker-cvgl-node01", "svc-ha", "svc-monitor"])
        page = self.client.get("/admin/accounts?q=car")
        self.assertContains(page, ">carol<")
        self.assertNotContains(page, ">alice<")

    def test_page(self):
        page = self.client.get("/admin/accounts")
        self.assertContains(page, "Changed with proxy_accounts.py only", count=2)
        self.assertContains(page, "Off at the HA entries", count=2)  # bob and erin
        self.assertContains(page, "own entry needs no account, so it still works for that user")
        self.assertContains(page, '<option value="svc-docker-cvgl-node02">')
        self.assertNotContains(page, '<option value="svc-docker-cvgl-node01">')  # exists already
        self.assertContains(page, "uses svc-docker-&lt;node&gt;.")

    def test_applied_state_notice(self):
        unknown = "No apply is recorded since the configs were last rendered with proxy_accounts.py"
        changed = "holds a change made with proxy_accounts.py since the applier"
        for value, shown in ((None, unknown), (True, changed), (False, None)):
            self.write_status(lambda d: d.update(unapplied_changes=value))
            page = self.client.get("/admin/accounts")
            for text in (unknown, changed):
                (self.assertContains if text == shown else self.assertNotContains)(page, text)

    def test_docker_daemon_note(self):
        rotate = self.client.get("/admin/accounts/rotate?username=svc-docker-cvgl-node01")
        self.assertContains(rotate, "put the new password in proxy.env on the node")
        for op in ("disable", "remove"):
            page = self.client.get(f"/admin/accounts/{op}?username=svc-docker-cvgl-node01")
            self.assertContains(page, "its pulls through the HA entry are refused.")
            self.assertNotContains(page, "proxy.env")
        self.assertNotContains(self.client.get("/admin/accounts/rotate?username=alice"), "Docker daemon")

    def test_pending_shown(self):
        self.client.post("/admin/accounts/disable", {"username": "alice", "confirm": "alice"})
        rows = {a["name"]: a for a in self.client.get("/api/admin/accounts").json()["accounts"]}
        self.assertEqual((rows["alice"]["pending"]["op"], rows["alice"]["pending"]["state"]), ("disable", "pending"))


class LifecycleTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("carol")

    def request(self, op="disable", username="alice"):
        response = self.client.post("/api/admin/requests", json.dumps({"op": op, "username": username,
                                                                        "confirm": username}),
                                    content_type="application/json")
        return response.json()["id"]

    def status(self, request_id, **record):
        record.setdefault("updated_at", "2026-01-01T00:00:00Z")
        (self.spool / "status" / f"{request_id}.json").write_text(json.dumps(dict({"id": request_id}, **record)))

    def get(self, request_id):
        return self.client.get(f"/api/admin/requests/{request_id}").json()

    def test_states(self):
        cases = [
            ("disable", None, "pending", "the old access still works"),
            ("disable", {"status": "applying"}, "applying",
             "The revoke is not confirmed yet: the old access may still work"),
            ("enable", {"status": "applying"}, "applying", "Being applied"),
            ("disable", {"status": "applied", "changed": True, "restarted": ["xray-ha"]}, "applied",
             "Applied and verified."),
            ("enable", {"status": "applied", "changed": False, "restarted": []}, "applied",
             "Nothing to change: the account was already in that state. Nothing restarted."),
            ("enable", {"status": "applied", "changed": False, "restarted": ["xray-ha"]}, "applied",
             "Nothing to change: the account was already in that state. The entries restarted to apply other "
             "changes."),
            ("disable", {"status": "failed", "stage": "test", "error": "xray-ha/config/config.json: bad",
                         "restored": True}, "failed", "the previous state is back: the account is unchanged and the "
                                                       "old access still works."),
            ("enable", {"status": "failed", "stage": "verify", "error": "no 204", "restored": True}, "failed",
             "the previous state is back: the account is unchanged. The entries restarted: established "
             "connections dropped."),
            ("enable", {"status": "failed", "stage": "verify", "error": "x", "restored": False}, "failed",
             "the rollback did not finish"),
            ("enable", {"status": "failed", "stage": "request", "error": "account exists", "restored": True},
             "failed", "Refused by the applier"),
            ("disable", {"status": "unconfirmed", "stage": "verify", "error": "no 204", "restarted": ["xray-ha"]},
             "unconfirmed", "the entries refuse the old access (verified)"),
            ("enable", {"status": "unconfirmed", "stage": "verify"}, "unconfirmed", "Live, but not confirmed yet"),
            ("rotate", {"status": "unconfirmed", "stage": "verify", "result": True}, "unconfirmed",
             "the old password is refused (verified)"),
        ]
        for op, record, state, text in cases:
            request_id = self.request(op, "bob" if op == "enable" else "alice")
            if record:
                self.status(request_id, op=op, username="x", **record)
            data = self.get(request_id)
            self.assertEqual(data["state"], state, (op, record))
            self.assertIn(text, data["text"], (op, record))
            self.assertEqual(data["final"], state in ("applied", "failed", "unconfirmed"))
            page = self.client.get(f"/admin/requests/{request_id}")
            self.assertContains(page, text.replace("'", "&#x27;"))
            if not data["final"] or (state == "failed" and record.get("restored") is False):
                self.assertContains(page, "data-poll=")  # an unfinished rollback is followed too
            else:
                self.assertNotContains(page, "data-poll=")
        test_failure = AccountRequest.objects.get(state="failed", stage="test")
        self.assertNotIn("The entries restarted", lifecycle.describe(test_failure))
        failed = AccountRequest.objects.filter(state="failed", stage="test").get()
        self.assertEqual((failed.error, failed.restored), ("xray-ha/config/config.json: bad", True))

    def test_unfinished_rollback_is_read_again_until_its_outcome_is_known(self):
        request_id = self.request("disable", "alice")
        self.status(request_id, status="failed", stage="verify", error="no 407", restored=False)
        self.assertIn("the rollback did not finish", self.get(request_id)["text"])
        # the applier's next run restores the files and the entries: the same status file, restored now
        self.status(request_id, status="failed", stage="interrupted", error="the apply was interrupted",
                    restored=True, updated_at="2026-01-01T00:05:00Z")
        data = self.get(request_id)
        self.assertEqual((data["restored"], data["stage"]), (True, "interrupted"))
        self.assertIn("the previous state is back", data["text"])
        # known now: the row is final and the file no longer read
        self.status(request_id, status="failed", stage="verify", restored=False, updated_at="2026-01-01T00:09:00Z")
        self.assertEqual(self.get(request_id)["restored"], True)

    def test_pending_too_long(self):
        request_id = self.request()
        AccountRequest.objects.filter(id=request_id).update(requested_at=timezone.now()
                                                            - datetime.timedelta(minutes=20))
        self.assertIn("Not taken after 20 minutes", self.get(request_id)["text"])

    def test_unreadable_or_foreign_status_ignored(self):
        request_id = self.request()
        (self.spool / "status" / f"{request_id}.json").write_text("{not json")
        self.assertEqual(self.get(request_id)["state"], "pending")
        self.status(request_id, status="applied")
        (self.spool / "status" / f"{request_id}.json").write_text(json.dumps({"id": "other-id-123", "status":
                                                                               "applied"}))
        self.assertEqual(self.get(request_id)["state"], "pending")

    def test_change_log(self):
        a = self.request("disable", "alice")
        b = self.request("enable", "bob")
        self.status(a, status="applied", changed=True, restarted=["xray-ha"])
        self.status(b, status="failed", stage="test", error="bad", restored=True)
        page = self.client.get("/admin/log")
        self.assertContains(page, "Applied and verified.")
        self.assertContains(page, "carol", count=None)
        rows = self.client.get("/api/admin/requests?state=failed").json()["requests"]
        self.assertEqual([r["id"] for r in rows], [b])
        rows = self.client.get("/api/admin/requests?username=ali").json()["requests"]
        self.assertEqual([r["id"] for r in rows], [a])
        rows = self.client.get("/api/admin/requests?op=enable&requested_by=carol").json()["requests"]
        self.assertEqual([r["id"] for r in rows], [b])
        self.assertEqual(self.client.get("/api/admin/requests?requested_by=erin").json()["requests"], [])
        page = self.client.get("/admin/log?state=failed")
        self.assertContains(page, f'href="/admin/requests/{b}"')
        self.assertNotContains(page, f'href="/admin/requests/{a}"')


class RestartTextTest(PortalTestCase):
    def test_confirm_page_says_what_restarts(self):
        self.sign_in("carol")
        self.assertContains(self.client.get("/admin/accounts/disable?username=alice"),
                            "Applying restarts the HA entries (xray-ha). Established connections")
        self.assertContains(self.client.get("/admin/accounts/disable?username=carol"),
                            "the HA entries (xray-ha) and the private proxies")
        self.write_status(lambda d: d.update(raw_auth=True))
        self.assertContains(self.client.get("/admin/accounts/disable?username=alice"),
                            "the HA entries (xray-ha) and every proxy")


@override_settings(REQUEST_PENDING_WARN=360)
class DescribeUnitTest(SimpleTestCase):
    def test_every_state_has_text(self):
        for op, _ in AccountRequest.OPS:
            for state, _ in AccountRequest.STATES:
                for restored in (True, False):
                    row = AccountRequest(id="x" * 8, op=op, username="a", requested_by="c", state=state,
                                         restored=restored, requested_at=timezone.now())
                    self.assertTrue(lifecycle.describe(row))


class RawAuthAndEscapingTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in("carol")

    def test_raw_auth_caveat_wherever_access_is_shown(self):
        response = self.client.post("/api/admin/requests", json.dumps({"op": "disable", "username": "alice",
                                                                        "confirm": "alice"}),
                                    content_type="application/json")
        request_id = response.json()["id"]
        caveat = "A public proxy's own entry needs no account, so it still works for that user."
        self.assertIn(caveat, response.json()["raw_auth_text"])
        self.assertIn(caveat, self.client.get(f"/api/admin/requests/{request_id}").json()["raw_auth_text"])
        self.assertIn(caveat, self.client.get("/api/admin/requests").json()["requests"][0]["raw_auth_text"])
        html = caveat.replace("'", "&#x27;")
        for path in ("/admin/log", f"/admin/requests/{request_id}", "/admin/accounts",
                     "/admin/accounts/disable?username=bob"):
            self.assertContains(self.client.get(path), html, msg_prefix=path)
        self.write_status(lambda d: d.update(raw_auth=True))
        self.assertIn("Raw auth is on", self.client.get(f"/api/admin/requests/{request_id}").json()["raw_auth_text"])
        enable = self.client.post("/api/admin/requests", json.dumps({"op": "admin-on", "username": "alice"}),
                                  content_type="application/json")
        self.assertIsNone(enable.json()["raw_auth_text"])

    def test_reflected_input_is_escaped(self):
        tag = "<script>x</script>"
        for path in (f"/admin/accounts?q={tag}", f"/admin/accounts/disable?username={tag}",
                     f"/admin/accounts/add?username={tag}",
                     f"/admin/log?username={tag}&requested_by={tag}&op={tag}&state={tag}"):
            page = self.client.get(path).content.decode()
            self.assertNotIn(tag, page, path)
        page = self.client.post("/admin/accounts/disable", {"username": tag, "confirm": tag}).content.decode()
        self.assertNotIn(tag, page)
