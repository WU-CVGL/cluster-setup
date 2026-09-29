import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import watchdog_test_support as support
from watchdog_test_support import StubServer, closed_port_url, make_config, make_token

import alert_TokenManager as tm
import metrics_token
from alert_MessageNotifier import MessageNotifier

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
MARGIN = timedelta(hours=48)


class TokenExpiryDecodeTest(unittest.TestCase):
    def test_go_rfc3339_nano_with_footer(self):
        token = make_token(expiry="2026-10-05T08:30:00.123456789Z", footer='{"kid":"x"}')
        self.assertEqual(
            tm.token_expiry(token), datetime(2026, 10, 5, 8, 30, 0, 123456, tzinfo=timezone.utc)
        )

    def test_without_footer_and_with_offset(self):
        token = make_token(expiry="2026-10-05T16:30:00+08:00")
        self.assertEqual(tm.token_expiry(token), datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc))

    def test_exp_claim_and_epoch_seconds(self):
        self.assertEqual(
            tm.token_expiry(make_token(claims={"exp": "2026-10-05T08:30:00Z"})),
            datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc),
        )
        epoch = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            tm.token_expiry(make_token(claims={"expiry": epoch})),
            datetime(2026, 10, 5, tzinfo=timezone.utc),
        )

    def test_payload_claims_are_returned(self):
        token = make_token(claims={"id": 7, "user_id": 1, "expiry": "2026-10-05T00:00:00Z"})
        self.assertEqual(tm.decode_paseto_payload(token)["user_id"], 1)

    def test_undecodable_tokens(self):
        bad = [
            None,
            "",
            "garbage",
            "v2.local." + support.b64url(b'{"expiry":"2026-10-05T00:00:00Z"}' + b"x" * 64),
            "v1.public." + support.b64url(b'{"expiry":"2026-10-05T00:00:00Z"}' + b"x" * 64),
            "v2.public.%%%notbase64%%%",
            "v2.public." + support.b64url(b"x" * 64),  # signature only, no payload
            "v2.public." + support.b64url(b"not json" + b"x" * 64),
            "v2.public." + support.b64url(b"[1, 2]" + b"x" * 64),
            make_token(claims={"id": 1}),  # no expiry claim
            make_token(claims={"expiry": "next tuesday"}),
            make_token(claims={"expiry": True}),
        ]
        for token in bad:
            with self.subTest(token=token):
                self.assertIsNone(tm.token_expiry(token))


class RenewalDecisionTest(unittest.TestCase):
    def reason(self, token):
        return tm.renewal_reason(token, NOW, MARGIN)

    def test_missing_or_empty(self):
        self.assertIsNotNone(self.reason(None))
        self.assertIsNotNone(self.reason(""))

    def test_undecodable_means_renew(self):
        self.assertIn("decode", self.reason("v2.public.garbage"))

    def test_expiry_windows(self):
        cases = [
            (timedelta(days=7), False),
            (timedelta(hours=49), False),
            (timedelta(hours=47, minutes=59), True),
            (timedelta(hours=1), True),
            (timedelta(hours=-5), True),  # already expired
        ]
        for delta, renew in cases:
            with self.subTest(delta=delta):
                token = support.token_expiring_in(delta, now=NOW)
                self.assertEqual(self.reason(token) is not None, renew)


class TokenFileTest(unittest.TestCase):
    """Reading the shared file; writing it is metrics_token's job (build/test_metrics_token.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token"

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_token(self):
        self.assertIsNone(tm.read_token(self.path))
        self.path.write_text("v2.public.X\n")  # as write_metrics_token leaves it
        self.assertEqual(tm.read_token(self.path), "v2.public.X")
        self.path.write_text("  v2.public.X\n")
        self.assertEqual(tm.read_token(self.path), "v2.public.X")
        self.path.write_text("\n")
        self.assertIsNone(tm.read_token(self.path))

    def test_the_only_writer_is_metrics_token(self):
        self.assertIs(tm.write_metrics_token, metrics_token.write_metrics_token)
        self.assertFalse(hasattr(tm, "write_token_atomic"))


class EnsureTokenTest(unittest.TestCase):
    """TokenManager.ensure_token against a stub Determined login endpoint."""

    LOGIN = ("POST", "/api/v1/auth/login/")
    SLACK = ("POST", support.FAKE_SLACK_PATH)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = StubServer({self.SLACK: (200, "ok")})
        self.stub.__enter__()
        self.config = make_config(
            self.tmp.name,
            det_web=self.stub.url,
            slack_webhook_url=self.stub.url.rstrip("/") + support.FAKE_SLACK_PATH,
        )
        self.path = Path(self.config.det_metrics_token_path)
        self.manager = tm.TokenManager(self.config, MessageNotifier(self.config), now=lambda: NOW)

    def tearDown(self):
        self.stub.__exit__(None, None, None)
        self.tmp.cleanup()

    def ensure(self, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            result = self.manager.ensure_token(**kwargs)
        return result, out.getvalue()

    def slack_texts(self):
        import json

        texts = []
        for r in self.stub.requests_to(*self.SLACK):
            for a in json.loads(r["body"])["attachments"]:
                texts.append((a["fields"][0]["value"], a["fields"][0]["title"]))
        return texts

    def test_missing_file_logs_in_and_writes_token(self):
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new, "user": {"id": 1}})
        ok, out = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["token"])
        self.assertEqual(self.manager.det_headers(), {"Authorization": "Bearer " + new})
        login = self.stub.requests_to(*self.LOGIN)
        self.assertEqual(len(login), 1)
        self.assertIn(b'"username": "admin"', login[0]["body"])
        self.assertEqual(self.slack_texts(), [("notification", "Automatic update success ~\n")])
        self.assertNotIn(new, out)
        self.assertNotIn(support.FAKE_PASSWORD, out)

    def test_valid_file_token_is_reused_without_login(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        self.path.write_text(current)
        ok, _ = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(self.stub.requests_to(*self.LOGIN), [])
        self.assertEqual(self.slack_texts(), [])
        self.assertEqual(self.manager.token, current)

    def test_near_expiry_is_renewed(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(old)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        ok, _ = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.manager.token, new)

    def test_force_renews_a_valid_looking_token(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(current)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        self.assertTrue(self.ensure()[0])  # not forced: kept, no login
        self.assertEqual(self.stub.requests_to(*self.LOGIN), [])
        out = io.StringIO()
        with redirect_stdout(out):
            ok = self.manager.ensure_token(force=True)
        self.assertTrue(ok)
        self.assertIn("rejected the token (HTTP 401)", out.getvalue())
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.manager.token, new)
        self.assertEqual(self.slack_texts(), [("notification", "Automatic update success ~\n")])

    ME = ("GET", "/api/v1/me")

    def test_revoked_file_token_is_renewed_at_the_hourly_check(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(current)
        self.stub.routes[self.ME] = (401, {"message": "invalid credentials"})
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        ok, out = self.ensure()
        self.assertTrue(ok)
        self.assertIn("HTTP 401 from /api/v1/me", out)
        probe = self.stub.requests_to(*self.ME)
        self.assertEqual(len(probe), 1)
        self.assertEqual(probe[0]["headers"].get("Authorization"), "Bearer " + current)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.manager.token, new)
        self.assertNotIn(current, out)
        # The 401 path keeps notifying at the hourly check.
        self.assertEqual(self.slack_texts(), [("notification", "Automatic update success ~\n")])

    def test_probe_answer_other_than_401_keeps_the_token(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        self.path.write_text(current)
        for status in (200, 403, 500):
            self.stub.routes[self.ME] = (status, {"user": {"id": 1}})
            self.assertTrue(self.ensure()[0])
        self.assertEqual(len(self.stub.requests_to(*self.ME)), 3)
        self.assertEqual(self.stub.requests_to(*self.LOGIN), [])
        self.assertEqual(self.manager.token, current)

    def test_unreachable_master_keeps_the_token_without_login(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        config = make_config(self.tmp.name, det_web=support.closed_port_url())
        Path(config.det_metrics_token_path).write_text(current)
        manager = tm.TokenManager(config, MessageNotifier(config), now=lambda: NOW)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.ensure_token())
        self.assertEqual(manager.token, current)

    def test_failed_forced_renewal_keeps_old_token(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        self.path.write_text(current)
        self.stub.routes[self.LOGIN] = (500, {"error": "boom"})
        self.ensure()
        with redirect_stdout(io.StringIO()):
            ok = self.manager.ensure_token(force=True)
        self.assertFalse(ok)
        self.assertEqual(self.path.read_text(), current)
        self.assertEqual(self.manager.token, current)
        texts = self.slack_texts()
        self.assertEqual([t[0] for t in texts], ["ERROR"])
        self.assertTrue(texts[0][1].startswith("Automatic update FAILED ~"))

    def assert_failed_renewal_keeps_old_token(self, route):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        self.path.write_text(old)
        if route is not None:
            self.stub.routes[self.LOGIN] = route
        ok, out = self.ensure()
        self.assertFalse(ok)
        self.assertEqual(self.path.read_text(), old)
        self.assertEqual(self.manager.token, old)
        texts = self.slack_texts()
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0][0], "ERROR")
        self.assertTrue(texts[0][1].startswith("Automatic update FAILED ~"))
        self.assertNotIn(support.FAKE_PASSWORD, out + texts[0][1])
        return out

    def test_http_401_keeps_old_token(self):
        out = self.assert_failed_renewal_keeps_old_token((401, {"message": "invalid credentials"}))
        self.assertIn("HTTP 401", out)

    def test_empty_token_keeps_old_token(self):
        self.assert_failed_renewal_keeps_old_token((200, {"token": ""}))

    def test_token_with_whitespace_is_rejected(self):
        # Same rule as metrics_token.write_metrics_token; rejected before it is used.
        for bad in ["v2.public.a b", "v2.public.ab\n", " v2.public.ab", "\t"]:
            with self.subTest(token=bad):
                self.stub.requests.clear()
                self.manager = tm.TokenManager(
                    self.config, MessageNotifier(self.config), now=lambda: NOW
                )
                self.assert_failed_renewal_keeps_old_token((200, {"token": bad}))

    def test_non_string_token_is_rejected(self):
        self.assert_failed_renewal_keeps_old_token((200, {"token": 123}))

    def test_non_json_keeps_old_token(self):
        self.assert_failed_renewal_keeps_old_token((502, "<html>bad gateway</html>"))
        self.stub.requests.clear()
        self.assert_failed_renewal_keeps_old_token((200, "<html>not json</html>"))

    def test_connection_error_keeps_old_token(self):
        self.config.det_web = closed_port_url()
        self.assert_failed_renewal_keeps_old_token(None)

    def test_failure_with_slack_down_does_not_raise(self):
        self.config.slack_webhook_url = closed_port_url(support.FAKE_SLACK_PATH)
        self.stub.routes[self.LOGIN] = (500, {"error": "boom"})
        ok, out = self.ensure()
        self.assertFalse(ok)
        self.assertNotIn(support.FAKE_SLACK_PATH, out)

    def test_renewal_writes_through_metrics_token(self):
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        with mock.patch(
            "alert_TokenManager.write_metrics_token", wraps=metrics_token.write_metrics_token
        ) as writer:
            ok, _ = self.ensure()
        self.assertTrue(ok)
        writer.assert_called_once_with(self.path, new)
        self.assertEqual(self.path.read_text(), new + "\n")

    def test_written_file_is_private_even_with_permissive_umask(self):
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        old_umask = os.umask(0)
        try:
            self.assertTrue(self.ensure()[0])
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_replace_keeps_old_file(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(old + "\n")
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        with mock.patch("metrics_token.os.replace", side_effect=OSError("disk error")):
            ok, out = self.ensure()
        self.assertFalse(ok)
        self.assertIn("FAILED", out)
        self.assertEqual(self.path.read_text(), old + "\n")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["token"])
        self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR"])
        # The fresh session is still used for the watchdog's own API calls.
        self.assertEqual(self.manager.token, new)
        self.assertNotIn(new, out)

    def test_unwritable_directory_does_not_raise(self):
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        self.config.det_metrics_token_path = Path(self.tmp.name) / "no-such-dir" / "token"
        self.manager = tm.TokenManager(self.config, MessageNotifier(self.config), now=lambda: NOW)
        ok, out = self.ensure()
        self.assertFalse(ok)
        self.assertIn("FAILED", out)
        # The fresh session is still used for the watchdog's own API calls.
        self.assertEqual(self.manager.token, new)
        # The next check retries only the write (still failing here): no new login.
        ok, out = self.ensure()
        self.assertFalse(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertEqual(self.manager.token, new)
        self.assertNotIn(new, out)

    def fail_write_once(self, old, new):
        """A login returning `new` whose write fails; the file keeps `old`."""
        self.path.write_text(old + "\n")
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        with mock.patch("metrics_token.os.replace", side_effect=OSError("disk full")):
            ok, _ = self.ensure()
        self.assertFalse(ok)
        self.assertEqual(self.manager.token, new)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)

    def test_failed_write_is_retried_without_a_new_login(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.fail_write_once(old, new)

        with mock.patch("metrics_token.os.replace", side_effect=OSError("disk full")):
            ok, out = self.ensure()  # still failing: warn again, keep the session
        self.assertFalse(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertEqual(self.path.read_text(), old + "\n")
        self.assertEqual(self.manager.token, new)
        self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR", "ERROR"])
        self.assertNotIn(new, out)

        ok, out = self.ensure()  # writable again
        self.assertTrue(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.manager.token, new)
        self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR", "ERROR"])  # log only
        self.assertIn("Wrote the Determined token obtained earlier", out)
        self.assertNotIn(new, out)

        ok, _ = self.ensure()  # an ordinary check of the written file
        self.assertTrue(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)

    def test_unwritten_token_near_expiry_is_renewed(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        newer = support.token_expiring_in(timedelta(days=13), now=NOW)
        self.fail_write_once(old, new)

        later = NOW + timedelta(days=6)  # `new` now expires in 24 h
        self.manager._now = lambda: later
        self.stub.routes[self.LOGIN] = (200, {"token": newer})
        ok, out = self.ensure()
        self.assertTrue(ok)
        self.assertIn("unwritten token obtained earlier needs renewal", out)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 2)
        self.assertEqual(self.path.read_text(), newer + "\n")
        self.assertEqual(self.manager.token, newer)

    def test_unwritten_token_is_kept_when_its_renewal_fails(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.fail_write_once(old, new)

        self.manager._now = lambda: NOW + timedelta(days=6)
        self.stub.routes[self.LOGIN] = (500, {"error": "boom"})
        ok, _ = self.ensure()
        self.assertFalse(ok)
        self.assertEqual(self.manager.token, new)
        self.assertEqual(self.path.read_text(), old + "\n")

        self.manager._now = lambda: NOW + timedelta(hours=1)  # not near expiry: write only
        ok, _ = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 2)
        self.assertEqual(self.path.read_text(), new + "\n")

    def test_replaced_file_wins_over_the_unwritten_token(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        hand = support.token_expiring_in(timedelta(days=5), now=NOW)
        self.fail_write_once(old, new)

        self.path.write_text(hand + "\n")  # placed by hand
        with mock.patch("alert_TokenManager.write_metrics_token") as writer:
            ok, out = self.ensure()
        self.assertTrue(ok)
        writer.assert_not_called()
        self.assertEqual(self.manager.token, hand)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)
        self.assertIn("was replaced since the failed write", out)

    def test_rejected_unwritten_token_is_not_written_later(self):
        # A forced renewal (HTTP 401) drops the unwritten session: it was just rejected.
        old = support.token_expiring_in(timedelta(days=5), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(old + "\n")
        self.ensure()
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        with mock.patch("metrics_token.os.replace", side_effect=OSError("disk full")):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(self.manager.ensure_token(force=True))
        self.assertEqual(self.manager.token, new)

        self.stub.routes[self.LOGIN] = (500, {"error": "boom"})  # `new` is rejected too
        with redirect_stdout(io.StringIO()):
            self.assertFalse(self.manager.ensure_token(force=True))
        with mock.patch("alert_TokenManager.write_metrics_token") as writer:
            ok, _ = self.ensure()
        self.assertTrue(ok)
        writer.assert_not_called()
        self.assertEqual(self.manager.token, old)  # back to the file token
        self.assertEqual(self.path.read_text(), old + "\n")

    # notify=False: the start-up check (W4). Same decisions, logged, never posted to Slack.

    def test_silent_check_renewal_posts_nothing(self):
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        ok, out = self.ensure(notify=False)
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.manager.token, new)
        self.assertIn("Obtained new Determined token", out)
        self.assertEqual(self.slack_texts(), [])

    def test_silent_check_probe_401_renewal_posts_nothing(self):
        current = support.token_expiring_in(timedelta(days=5), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(current)
        self.stub.routes[self.ME] = (401, {"message": "invalid credentials"})
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        ok, out = self.ensure(notify=False)
        self.assertTrue(ok)
        self.assertIn("HTTP 401 from /api/v1/me", out)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.slack_texts(), [])

    def test_silent_check_failed_login_posts_nothing_then_hourly_check_notifies(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(old)
        self.stub.routes[self.LOGIN] = (500, {"error": "boom"})
        ok, out = self.ensure(notify=False)
        self.assertFalse(ok)
        self.assertIn("renewal FAILED", out)
        self.assertIn("retrying at the next hourly check", out)
        self.assertIn("not posted to Slack", out)
        self.assertEqual(self.manager.token, old)
        self.assertEqual(self.slack_texts(), [])

        ok, out = self.ensure()  # the next hourly check retries and notifies
        self.assertFalse(ok)
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 2)
        self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR"])
        self.assertNotIn("not posted to Slack", out)

        self.stub.routes[self.LOGIN] = (200, {"token": new})
        self.assertTrue(self.ensure()[0])
        self.assertEqual(
            self.slack_texts()[1:], [("notification", "Automatic update success ~\n")]
        )

    def test_silent_check_failed_write_posts_nothing_then_hourly_retry_notifies(self):
        old = support.token_expiring_in(timedelta(hours=10), now=NOW)
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.path.write_text(old + "\n")
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        with mock.patch("metrics_token.os.replace", side_effect=OSError("disk full")):
            ok, out = self.ensure(notify=False)
            self.assertFalse(ok)
            self.assertIn("renewal FAILED (OSError: disk full)", out)
            self.assertEqual(self.slack_texts(), [])
            self.assertEqual(self.manager.token, new)  # the new session is used anyway

            ok, out = self.ensure(notify=False)  # a silent write retry is silent too
            self.assertFalse(ok)
            self.assertEqual(self.slack_texts(), [])

            ok, out = self.ensure()  # the hourly write retry notifies
            self.assertFalse(ok)
            self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR"])
        self.assertEqual(len(self.stub.requests_to(*self.LOGIN)), 1)  # no new login

        ok, out = self.ensure()  # writable again: written, only logged (as without W4)
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual([t[0] for t in self.slack_texts()], ["ERROR"])
        self.assertNotIn(new, out)

    def test_undecodable_file_token_is_renewed(self):
        self.path.write_text("v2.public.not-a-real-token")
        new = support.token_expiring_in(timedelta(days=7), now=NOW)
        self.stub.routes[self.LOGIN] = (200, {"token": new})
        ok, out = self.ensure()
        self.assertTrue(ok)
        self.assertIn("cannot decode", out)
        self.assertEqual(self.path.read_text(), new + "\n")

    def test_new_token_with_undecodable_expiry_is_used_without_slack(self):
        self.stub.routes[self.LOGIN] = (200, {"token": "v2.public.opaque-format"})
        ok, out = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(self.path.read_text(), "v2.public.opaque-format\n")
        self.assertEqual(self.manager.token, "v2.public.opaque-format")
        self.assertIn("renewed at every hourly check", out)
        self.assertEqual(self.slack_texts(), [])


if __name__ == "__main__":
    unittest.main()
