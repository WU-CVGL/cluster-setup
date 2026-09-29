"""A service restart checks the Determined token without sending a notification.

PR #4 made the start-up token refresh silent. The weekly refresh it scheduled
around is gone: TokenManager renews the token at start and at every hourly check
when needed (missing, undecodable, expiring within 48 h, or HTTP 401). Each test
asserts PR #4's behaviour against the current code:

1. the start-up check posts nothing to Slack, on success and on failure;
2. an hourly renewal still notifies (success and FAILED);
3. no duplicate notification right after a start-up renewal, and the next
   renewal notifies (PR #4: "Thursday start-up skips the duplicate");
4. a failed start-up renewal is retried at the next hourly check, which notifies.

Run: cd services/determined-watchdog/build && python3 -B -m unittest test_startup_refresh.py
Needs `requests` (the watchdog modules import it); no network, no credentials.
The watchdog's full test suite is ../tests/.
"""
import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
BUILD_DIR = Path(__file__).resolve().parent
if str(BUILD_DIR) not in sys.path:
    sys.path.insert(0, str(BUILD_DIR))

import alert_response_handler_v02 as main_module  # noqa: E402
from alert_config import Config  # noqa: E402
from alert_TokenManager import TokenError  # noqa: E402

SUCCESS = ("notification", "Automatic update success ~")
FAILED = ("ERROR", "Automatic update FAILED ~")


def fake_token(expiry: datetime) -> str:
    """PASETO-shaped token: v2.public.<base64url(JSON claims + 64 signature bytes)>."""
    claims = {"id": 1, "user_id": 1, "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}
    raw = json.dumps(claims).encode() + b"\x07" * 64
    return "v2.public." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def at(day, hour, minute):
    return datetime(2026, 10, day, hour, minute)


class StopLoop(BaseException):
    """Raised from the patched time.sleep to leave the endless main loop."""


class StartupRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "secrets" / "token"
        self.path.parent.mkdir()
        self.clock = at(1, 12, 5)

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self):
        """MainApplication with Slack, the Grafana/kill check and the clocks replaced.

        The token logic is real: TokenManager decides, logs in (det_login, patched
        per test) and writes the file with metrics_token.write_metrics_token.
        """
        config = Config(
            det_web="http://det.invalid:8080",
            det_username="admin",
            det_password="PLACEHOLDER-det-password",
            grafana_web="http://grafana.invalid:3000",
            grafana_api_token="PLACEHOLDER-grafana-token",
            slack_webhook_url="https://hooks.slack.invalid/services/TPLACEHOLDER/BPLACEHOLDER/x",
            base_path=Path(self.tmp.name) / "data",
            det_metrics_token_path=self.path,
        )
        with redirect_stdout(io.StringIO()):
            app = main_module.MainApplication(config)
        self.posted = []  # (clock at posting, warning_type, info)
        notifier = mock.Mock()
        notifier.send_slack_warning.side_effect = lambda warning_type, info, slack_webhook_url: (
            self.posted.append((self.clock, warning_type, info))
        )
        # TokenManager got the notifier in MainApplication.__init__: replace both references.
        app.message_notifier = app.token_manager.message_notifier = notifier
        app.check_alerts = mock.Mock()  # Grafana is not part of this test
        app.now = lambda: self.clock
        app.token_manager._now = lambda: self.clock.replace(tzinfo=timezone.utc)
        return app

    def login(self, *results):
        """Patch det_login: each call returns the next token, or raises it if an exception."""
        return mock.patch("alert_TokenManager.det_login", side_effect=list(results))

    def probe_ok(self):
        # GET /api/v1/me never answers 401 here (no network access in these tests).
        return mock.patch("alert_TokenManager.token_rejected", return_value=False)

    def run_app(self, app, times):
        """app.run(): start at times[0]; after each sleep the clock moves to the next time."""
        self.clock = times[0]
        later = iter(times[1:])

        def fake_sleep(_seconds):
            try:
                self.clock = next(later)
            except StopIteration:
                raise StopLoop() from None

        out = io.StringIO()
        with mock.patch.object(main_module.time, "sleep", side_effect=fake_sleep), redirect_stdout(out):
            with self.assertRaises(StopLoop):
                app.run()
        return out.getvalue()

    def kinds(self):
        return [(kind, info.split(" (")[0]) for _, kind, info in self.posted]

    def test_silent_startup_check_success_and_failure_do_not_notify(self):
        new = fake_token(at(8, 12, 5))
        # Success: no token file -> login -> file written; nothing posted.
        app = self.make_app()
        with self.login(new) as login, self.probe_ok():
            out = self.run_app(app, [at(1, 12, 5)])  # minute 5: no hourly check
        login.assert_called_once()
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(app.token_manager.token, new)
        self.assertIn("Obtained new Determined token", out)
        self.assertEqual(self.posted, [])
        app.check_alerts.assert_not_called()

        # Failure: the login fails -> file unchanged, logged only.
        self.path.unlink()
        app = self.make_app()
        with self.login(TokenError("login unavailable")) as login, self.probe_ok():
            out = self.run_app(app, [at(1, 12, 5)])
        login.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertIn("renewal FAILED (TokenError: login unavailable)", out)
        self.assertIn("not posted to Slack", out)
        self.assertEqual(self.posted, [])

        # Failure: the login works but the file cannot be written -> logged only.
        app = self.make_app()
        with self.login(new), self.probe_ok(), mock.patch(
            "metrics_token.os.replace", side_effect=OSError("disk full")
        ):
            out = self.run_app(app, [at(1, 12, 5)])
        self.assertFalse(self.path.exists())
        self.assertIn("renewal FAILED (OSError: disk full)", out)
        self.assertEqual(self.posted, [])
        self.assertNotIn(new, out)
        self.assertNotIn("PLACEHOLDER-det-password", out)

    def test_hourly_renewal_keeps_notification(self):
        # Success: the file token expires within 48 h -> the hourly check renews and notifies.
        self.path.write_text(fake_token(at(2, 0, 0)) + "\n")
        new = fake_token(at(8, 13, 0))
        app = self.make_app()
        with self.login(new) as login, self.probe_ok(), redirect_stdout(io.StringIO()):
            self.clock = at(1, 13, 0)
            app.tick(self.clock)  # minute alert_min (0): the hourly check
        login.assert_called_once()
        self.assertEqual(self.path.read_text(), new + "\n")
        self.assertEqual(self.kinds(), [SUCCESS])
        app.check_alerts.assert_called_once()

        # Failure: same situation, the login fails -> FAILED is posted.
        self.path.write_text(fake_token(at(2, 0, 0)) + "\n")
        app = self.make_app()
        with self.login(TokenError("login unavailable")), self.probe_ok(), redirect_stdout(io.StringIO()):
            app.tick(self.clock)
        self.assertEqual(self.kinds(), [FAILED])

    def test_startup_renewal_is_not_notified_again_then_next_renewal_notifies(self):
        start = at(1, 12, 0)  # starts in minute alert_min: the hourly check follows at once
        first = fake_token(start + timedelta(days=7))
        second = fake_token(start + timedelta(days=13))
        app = self.make_app()
        times = [
            start,  # start-up check (renews, silent) + first loop: hourly check, token fresh
            at(1, 12, 1),  # resets the hourly schedule
            at(1, 13, 0),  # next hourly check: token still fresh
            at(1, 13, 1),
            at(7, 12, 0),  # `first` now expires in 24 h: the hourly check renews and notifies
        ]
        with self.login(first, second) as login, self.probe_ok():
            self.run_app(app, times)
        self.assertEqual(login.call_count, 2)
        self.assertEqual(app.check_alerts.call_count, 3)
        self.assertEqual(self.posted, [(at(7, 12, 0),) + SUCCESS])
        self.assertEqual(self.path.read_text(), second + "\n")

    def test_failed_startup_keeps_hourly_retry_that_notifies(self):
        new = fake_token(at(8, 13, 0))
        times = [at(1, 12, 5), at(1, 12, 59), at(1, 13, 0)]
        cases = [
            ("retry fails", [TokenError("login unavailable"), TokenError("still down")], FAILED),
            ("retry succeeds", [TokenError("login unavailable"), new], SUCCESS),
        ]
        for name, results, expected in cases:
            with self.subTest(name):
                if self.path.exists():
                    self.path.unlink()
                app = self.make_app()
                with self.login(*results) as login, self.probe_ok():
                    self.run_app(app, times)
                self.assertEqual(login.call_count, 2)  # start-up + the 13:00 hourly check
                # Nothing before 13:00; exactly one message, posted by the hourly check.
                self.assertEqual([when for when, _, _ in self.posted], [at(1, 13, 0)])
                self.assertEqual(self.kinds(), [expected])


if __name__ == "__main__":
    unittest.main()
