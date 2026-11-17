import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import connection

from portal.models import SignInFailure

from .base import PortalTestCase, PortalTransactionTestCase, https_client

SENTINEL = "Sentinel-Passw0rd-7f3c1e"


def _server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class SignInTest(PortalTestCase):
    def test_success_keeps_username_and_admin_flag_only(self):
        self.master.users["carol"] = (SENTINEL, True, True)
        response = self.sign_in("carol", SENTINEL)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/access")
        self.assertEqual(self.client.session["username"], "carol")
        self.assertIs(self.client.session["is_determined_admin"], True)
        self.assertEqual(set(self.client.session.keys()), {"username", "is_determined_admin", "signed_in_at"})
        login = self.master.logins[-1]
        self.assertEqual(login, ("carol", SENTINEL, False))
        # the token the master issued was handed back to logout at once: no master session stays open
        self.assertEqual(len(self.master.logouts), 1)
        self.assertEqual(self.master.tokens, {})

    def test_not_admin(self):
        self.sign_in("alice")
        self.assertIs(self.client.session["is_determined_admin"], False)

    def test_username_is_the_masters(self):
        # the master's canonical name, not what was typed around it
        self.master.users["alice"] = ("pw-alice", False, True)
        self.client.post("/signin", {"username": "  alice ", "password": "pw-alice"})
        self.assertEqual(self.client.session["username"], "alice")

    def test_wrong_password(self):
        response = self.sign_in("alice", "nope")
        self.assertEqual(response.status_code, 401)
        self.assertContains(response, "Wrong username or password.", status_code=401)
        self.assertNotIn("username", self.client.session)
        self.assertEqual(self.master.logouts, [])
        self.assertEqual(SignInFailure.objects.count(), 2)  # per client and per username

    def test_unknown_user(self):
        self.assertEqual(self.sign_in("mallory", "x").status_code, 401)

    def test_inactive_refused_by_master(self):
        response = self.sign_in("dave")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "deactivated", status_code=403)
        self.assertNotIn("username", self.client.session)

    def test_inactive_in_a_successful_answer(self):
        # a master that answers 200 for an inactive user: refused, and the token it issued is logged out
        self.master.active_flag_override = False
        response = self.sign_in("alice")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("username", self.client.session)
        self.assertEqual(len(self.master.logouts), 1)
        self.assertEqual(self.master.tokens, {})

    def test_logout_failure_still_signs_in_and_keeps_no_token(self):
        self.master.fail_logout = True
        with self.assertLogs("portal.signin", level="WARNING") as logs:
            response = self.sign_in("alice")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["username"], "alice")
        token = next(iter(self.master.tokens))
        self.assertNotIn(token, "\n".join(logs.output))
        self.assertNotIn(token, str(dict(self.client.session)))

    def test_master_unreachable_is_not_a_failure(self):
        with self.settings(MASTER_URL="http://127.0.0.1:9"):
            with self.assertLogs("portal.signin", level="WARNING"):
                response = self.sign_in("alice")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(SignInFailure.objects.count(), 0)

    def test_tests_never_reach_a_real_master(self):
        from portal import master
        with self.settings(MASTER_URL="http://192.168.233.6:8080"):
            with self.assertRaises(RuntimeError):
                master.check_credentials("alice", "pw-alice")

    def test_session_cookie_flags(self):
        response = self.sign_in("alice")
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(int(cookie["max-age"]), 8 * 3600)

    def test_session_key_cycled(self):
        self.client.get("/signin")
        self.client.cookies[settings.SESSION_COOKIE_NAME] = "fixedsessionkey0000000000000000aa"
        self.sign_in("alice")
        self.assertNotEqual(self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                            "fixedsessionkey0000000000000000aa")

    def test_sign_out(self):
        self.sign_in("alice")
        response = self.client.post("/signout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/me/access").status_code, 401)

    def test_next_must_be_local(self):
        response = self.client.post("/signin", {"username": "alice", "password": "pw-alice",
                                                "next": "https://evil.example/"})
        self.assertEqual(response["Location"], "/access")
        response = https_client().post("/signin", {"username": "alice", "password": "pw-alice", "next": "/usage"})
        self.assertEqual(response["Location"], "/usage")

    def test_page_says_admin_flag_is_read_at_sign_in(self):
        self.assertContains(self.client.get("/signin"), "Your Determined admin flag is read at sign-in")


class NothingKeptTest(PortalTransactionTestCase):
    def test_no_password_or_token_kept_anywhere(self):
        self.master.users["alice"] = (SENTINEL, False, True)
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Keep(level=logging.DEBUG)
        root = logging.getLogger()
        portal = logging.getLogger("portal")  # does not propagate to root
        root.addHandler(handler)
        portal.addHandler(handler)
        old_level, old_portal_level = root.level, portal.level
        root.setLevel(logging.DEBUG)
        portal.setLevel(logging.DEBUG)
        self.master.fail_logout = True  # a warning from portal.signin, so the handler sees the portal's logs
        try:
            self.sign_in("alice", SENTINEL)
            self.sign_in("alice", SENTINEL + "-wrong", client=https_client())
            self.client.get("/access")
        finally:
            root.removeHandler(handler)
            portal.removeHandler(handler)
            root.setLevel(old_level)
            portal.setLevel(old_portal_level)
        self.assertTrue(any("logout" in r for r in records))
        token = self.master.logouts[0]
        self.assertTrue(token.startswith("v2.public."))
        secrets_ = [SENTINEL.encode(), token.encode(), token.split(".")[-1].encode()]
        # the raw database file, with every table, sessions included
        connection.close()
        raw = open(connection.settings_dict["NAME"], "rb").read()
        self.assertIn(b"django_session", raw)
        for secret in secrets_:
            self.assertNotIn(secret, raw)
        for session in Session.objects.all():
            decoded = repr(session.get_decoded()).encode()
            for secret in secrets_:
                self.assertNotIn(secret, decoded)
        logged = "\n".join(records).encode()
        for secret in secrets_:
            self.assertNotIn(secret, logged)


class CsrfTest(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.client = https_client(enforce_csrf_checks=True)

    def test_sign_in_without_token_refused(self):
        response = self.sign_in("alice")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.master.logins, [])

    def test_sign_in_with_token(self):
        page = self.client.get("/signin")
        token = page.context["csrf_token"]
        response = self.client.post("/signin", {"username": "alice", "password": "pw-alice",
                                                "csrfmiddlewaretoken": str(token)})
        self.assertEqual(response.status_code, 302)

    def test_sign_out_and_language_need_a_token(self):
        self.assertEqual(self.client.post("/signout").status_code, 403)
        self.assertEqual(self.client.post("/language", {"language": "zh-hans"}).status_code, 403)

    def test_cross_origin_post_refused(self):
        page = self.client.get("/signin")
        token = str(page.context["csrf_token"])
        response = self.client.post("/signin", {"username": "alice", "password": "pw-alice",
                                                "csrfmiddlewaretoken": token}, HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)


class LimitTest(PortalTestCase):
    def test_per_username(self):
        for _ in range(settings.SIGNIN_MAX_PER_USERNAME):
            self.assertEqual(self.sign_in("alice", "wrong").status_code, 401)
        calls = len(self.master.logins)
        # even the right password, from another client
        response = https_client(HTTP_X_REAL_IP="10.9.9.9").post("/signin", {"username": "alice",
                                                                           "password": "pw-alice"})
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Try again in 15 minutes.", status_code=429)
        self.assertEqual(len(self.master.logins), calls)  # the master is not asked
        with self.settings(SIGNIN_WINDOW=61):  # minutes rounded up
            self.assertContains(self.sign_in("alice"), "Try again in 2 minutes.", status_code=429)
        # another user is not affected
        self.assertEqual(https_client(HTTP_X_REAL_IP="10.9.9.8").post(
            "/signin", {"username": "carol", "password": "pw-carol"}).status_code, 302)

    def test_per_client(self):
        client = https_client(HTTP_X_REAL_IP="10.1.2.3")
        for i in range(settings.SIGNIN_MAX_PER_CLIENT):
            self.assertEqual(client.post("/signin", {"username": f"u{i}", "password": "x"}).status_code, 401)
        self.assertEqual(client.post("/signin", {"username": "alice", "password": "pw-alice"}).status_code, 429)
        other = https_client(HTTP_X_REAL_IP="10.1.2.4")
        self.assertEqual(other.post("/signin", {"username": "alice", "password": "pw-alice"}).status_code, 302)

    def test_window_expires(self):
        import datetime

        from django.utils import timezone
        for _ in range(settings.SIGNIN_MAX_PER_USERNAME):
            self.sign_in("alice", "wrong")
        SignInFailure.objects.update(at=timezone.now() - datetime.timedelta(seconds=settings.SIGNIN_WINDOW + 1))
        self.assertEqual(self.sign_in("alice").status_code, 302)
        self.assertEqual(SignInFailure.objects.count(), 0)


class RedirectTest(PortalTestCase):
    """A master URL that answers with a redirect: the portal follows none, so no token or password moves on."""

    def setUp(self):
        super().setUp()
        received = self.received = []

        class Elsewhere(BaseHTTPRequestHandler):
            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                received.append((self.command, self.path, self.headers.get("Authorization"),
                                 self.rfile.read(length) if length else b""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"token": "elsewhere", "user": {"username": "carol", "admin": true, "active": true}}')

            do_GET = do_POST = _do

            def log_message(self, *args):
                pass

        self.elsewhere, elsewhere_url = _server(Elsewhere)
        self.addCleanup(self.elsewhere.shutdown)
        self.code = 302

        test = self

        class Redirecting(BaseHTTPRequestHandler):
            def _do(self):
                self.send_response(test.code)
                self.send_header("Location", elsewhere_url + "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _do

            def log_message(self, *args):
                pass

        self.redirecting, self.redirecting_url = _server(Redirecting)
        self.addCleanup(self.redirecting.shutdown)

    def test_logout_redirect_not_followed(self):
        from portal import master
        for code in (301, 302, 303, 307, 308):
            self.code = code
            with self.settings(MASTER_URL=self.redirecting_url):
                with self.assertLogs("portal.signin", level="WARNING"):
                    self.assertIs(master._logout("SECRET-TOKEN"), False)
        self.assertEqual(self.received, [])

    def test_login_redirect_is_unavailable(self):
        for code in (301, 302, 303, 307, 308):
            self.code = code
            with self.settings(MASTER_URL=self.redirecting_url):
                with self.assertLogs("portal.signin", level="WARNING"):
                    response = self.sign_in("carol", SENTINEL)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("username", self.client.session)
        self.assertEqual(self.received, [])
        self.assertEqual(SignInFailure.objects.count(), 0)
