"""The main loop survives failing cycles and keeps its hourly schedule."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

import requests

import watchdog_test_support as support
from watchdog_test_support import closed_port_url, make_config

import alert_response_handler_v02 as main_module
from alert_response_handler_v02 import MainApplication


class StopLoop(BaseException):
    """Raised from the patched time.sleep to leave the infinite loop."""


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Nothing reachable: Determined/Grafana refuse connections, Slack cannot be resolved.
        self.config = make_config(
            self.tmp.name, det_web=closed_port_url(), grafana_web=closed_port_url()
        )
        with redirect_stdout(io.StringIO()):
            self.app = MainApplication(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, times, sleeps):
        """Run app.run() with self.now() returning `times` in turn; stop after `sleeps` sleeps."""
        clock = iter(times)
        self.app.now = lambda: next(clock)
        calls = {"n": 0}

        def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] >= sleeps:
                raise StopLoop()

        out = io.StringIO()
        with mock.patch.object(main_module.time, "sleep", side_effect=fake_sleep), redirect_stdout(out):
            with self.assertRaises(StopLoop):
                self.app.run()
        return calls["n"], out.getvalue()

    def test_failing_cycles_do_not_end_the_loop(self):
        self.app.token_manager.ensure_token = mock.Mock(side_effect=RuntimeError("token boom"))
        self.app.check_alerts = mock.Mock(side_effect=RuntimeError("cycle boom"))
        t = lambda minute, second=0: datetime(2026, 9, 28, 10, minute, second)
        times = [t(59), t(0), t(0, 10), t(1), t(0), t(2)]  # start time + one per loop iteration
        n, out = self.run_loop(times, sleeps=5)
        self.assertEqual(n, 5)  # the loop kept running after each failure
        self.assertEqual(self.app.check_alerts.call_count, 2)  # minute 0 twice, once each
        # The start-up check is silent (W4); the hourly checks notify.
        self.assertEqual(
            self.app.token_manager.ensure_token.call_args_list,
            [mock.call(notify=False), mock.call(), mock.call()],
        )
        self.assertIn("cycle boom", out)
        self.assertIn("token boom", out)

    def test_token_check_runs_before_alert_check(self):
        order = []
        self.app.token_manager.ensure_token = mock.Mock(side_effect=lambda: order.append("token"))
        self.app.check_alerts = mock.Mock(side_effect=lambda: order.append("alerts"))
        self.app.tick(datetime(2026, 9, 28, 10, 0))
        self.app.tick(datetime(2026, 9, 28, 10, 0, 30))  # same minute: no second run
        self.assertEqual(order, ["token", "alerts"])

    def test_real_cycle_with_everything_down_does_not_raise(self):
        dns_error = requests.exceptions.ConnectionError(
            "Failed to resolve 'hooks.slack.invalid' ([Errno -2] Name or service not known)"
        )
        real_post = requests.post
        slack_calls = []

        def post(url, **kwargs):  # requests.post is shared by all modules: fail only Slack
            if url == self.config.slack_webhook_url:
                slack_calls.append(kwargs["data"])
                raise dns_error
            return real_post(url, **kwargs)

        with mock.patch("requests.post", side_effect=post):
            out = io.StringIO()
            with redirect_stdout(out):
                self.app.hourly_cycle()  # token renewal fails, Grafana fails, Slack fails
        # "Automatic update FAILED" + "Failed to fetch Grafana alert"
        self.assertEqual(len(slack_calls), 2)
        self.assertIn("Automatic update FAILED", slack_calls[0])
        self.assertIn("Failed to fetch Grafana alert", slack_calls[1])
        self.assertNotIn("failed, continuing", out.getvalue())  # handled, not a crash
        self.assertNotIn(support.FAKE_PASSWORD, out.getvalue())
        self.assertNotIn(support.FAKE_SLACK_PATH, out.getvalue())

    def test_real_startup_with_everything_down_posts_nothing(self):
        # W4: the start-up token check (login refused) only logs; the next hourly
        # check retries the login and posts "Automatic update FAILED".
        real_post = requests.post
        slack_calls = []

        def post(url, **kwargs):
            if url == self.config.slack_webhook_url:
                slack_calls.append(kwargs["data"])
                raise requests.exceptions.ConnectionError("Failed to resolve 'hooks.slack.invalid'")
            return real_post(url, **kwargs)

        self.app.check_alerts = mock.Mock()
        t = lambda minute: datetime(2026, 9, 28, 10, minute)
        with mock.patch("requests.post", side_effect=post):
            _, out = self.run_loop([t(30), t(30), t(59)], sleeps=2)
            self.assertEqual(slack_calls, [])
            self.assertIn("renewal FAILED", out)
            self.assertIn("not posted to Slack", out)
            self.assertNotIn("failed, continuing", out)  # handled, not a crash
            with redirect_stdout(io.StringIO()):
                self.app.tick(t(0))  # the next hourly check
        self.assertEqual(len(slack_calls), 1)
        self.assertIn("Automatic update FAILED", slack_calls[0])

    def test_debug_mode_survives_failing_checks(self):
        self.config.is_debug = True
        self.app.check_alerts = mock.Mock(side_effect=RuntimeError("debug boom"))
        self.app.token_manager.ensure_token = mock.Mock(return_value=False)
        t = lambda minute: datetime(2026, 9, 28, 10, minute)
        n, out = self.run_loop([t(5), t(5), t(5), t(5)], sleeps=6)
        self.assertEqual(n, 6)
        self.assertEqual(self.app.check_alerts.call_count, 3)

    def test_startup_output_has_no_secrets(self):
        self.app.token_manager.ensure_token = mock.Mock(return_value=False)
        _, out = self.run_loop([datetime(2026, 9, 28, 10, 5)] * 3, sleeps=1)
        with redirect_stdout(io.StringIO()) as buf:
            print(self.config)
        text = out + buf.getvalue()
        self.assertIn("self check", text)
        for secret in (support.FAKE_PASSWORD, support.FAKE_GRAFANA_TOKEN, support.FAKE_SLACK_PATH):
            self.assertNotIn(secret, text)


class SigtermTest(unittest.TestCase):
    def test_handler_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            main_module._exit_on_sigterm(15, None)
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
