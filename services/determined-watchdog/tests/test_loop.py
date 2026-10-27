"""The scheduler runs the check on its cron trigger and survives failing checks."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import requests
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.executors.base import run_job
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import watchdog_test_support as support
from watchdog_test_support import closed_port_url, make_config

import alert_response_handler_v02 as main_module
from alert_response_handler_v02 import CHECK_JOB_ID, DEBUG_CHECK_JOB_ID, MainApplication


class SchedulerTest(unittest.TestCase):
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

    def jobs(self):
        return {job.id: job for job in self.app.build_scheduler().get_jobs()}

    def run_app(self):
        """app.run() with the scheduler's start() replaced: returns (start mock, output)."""
        out = io.StringIO()
        with mock.patch.object(main_module.BlockingScheduler, "start") as start, redirect_stdout(out):
            self.app.run()
        return start, out.getvalue()

    def test_production_has_only_the_check_job(self):
        jobs = self.jobs()
        self.assertEqual(list(jobs), [CHECK_JOB_ID])
        job = jobs[CHECK_JOB_ID]
        self.assertIsInstance(job.trigger, CronTrigger)
        self.assertEqual(
            str(job.trigger), "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/30']"
        )
        self.assertEqual(job.func, self.app.check_cycle)
        self.assertEqual(
            (job.max_instances, job.coalesce, job.misfire_grace_time),
            (1, True, main_module.MISFIRE_GRACE_SECONDS),
        )

    def test_debug_mode_adds_the_interval_check(self):
        self.config.is_debug = True
        jobs = self.jobs()
        self.assertEqual(sorted(jobs), sorted([CHECK_JOB_ID, DEBUG_CHECK_JOB_ID]))
        job = jobs[DEBUG_CHECK_JOB_ID]
        self.assertIsInstance(job.trigger, IntervalTrigger)
        self.assertEqual(job.trigger.interval.total_seconds(), main_module.DEBUG_CHECK_SECONDS)
        self.assertEqual((job.max_instances, job.coalesce), (1, True))

    def test_debug_check_survives_a_failing_check(self):
        self.app.check_alerts = mock.Mock(side_effect=RuntimeError("debug boom"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.debug_check_job()
            self.app.debug_check_job()
        self.assertEqual(self.app.check_alerts.call_count, 2)
        self.assertIn("debug boom", out.getvalue())

    def test_token_check_runs_before_alert_check(self):
        order = []
        self.app.token_manager.ensure_token = mock.Mock(side_effect=lambda: order.append("token"))
        self.app.check_alerts = mock.Mock(side_effect=lambda: order.append("alerts"))
        self.app.check_cycle()
        self.assertEqual(order, ["token", "alerts"])

    def test_failing_steps_do_not_stop_the_check(self):
        self.app.token_manager.ensure_token = mock.Mock(side_effect=RuntimeError("token boom"))
        self.app.check_alerts = mock.Mock(side_effect=RuntimeError("cycle boom"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.check_cycle()  # does not raise
            self.app.check_cycle()
        self.assertEqual(self.app.check_alerts.call_count, 2)  # the alert check ran each time
        self.assertIn("cycle boom", out.getvalue())
        self.assertIn("token boom", out.getvalue())

    def test_exception_escaping_a_job_is_logged_and_contained(self):
        # As the scheduler's executor runs the job: the exception becomes an event, which
        # log_job_event logs (redacted); the scheduler itself keeps running.
        self.app.check_cycle = mock.Mock(
            side_effect=RuntimeError("escaped with " + support.FAKE_PASSWORD)
        )
        job = self.jobs()[CHECK_JOB_ID]
        now = datetime.now(timezone.utc)
        events = run_job(job, "default", [now], "apscheduler.executors.default")
        self.assertEqual([e.code for e in events], [EVENT_JOB_ERROR])
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.log_job_event(events[0])
        self.assertIn("check failed, continuing", out.getvalue())
        self.assertIn("escaped with <redacted>", out.getvalue())
        self.assertNotIn(support.FAKE_PASSWORD, out.getvalue())

        self.app.check_cycle.side_effect = None
        events = run_job(job, "default", [now], "apscheduler.executors.default")
        self.assertEqual([e.code for e in events], [EVENT_JOB_EXECUTED])

    def test_missed_check_is_logged(self):
        when = datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc)
        out = io.StringIO()
        with redirect_stdout(out):
            self.app.log_job_event(JobExecutionEvent(EVENT_JOB_MISSED, CHECK_JOB_ID, "default", when))
        self.assertIn("check missed (scheduled for 2026-09-28 10:30:00+00:00)", out.getvalue())

    def test_run_checks_the_token_silently_and_starts_the_scheduler(self):
        calls = []
        self.app.token_manager.ensure_token = mock.Mock(side_effect=lambda **kw: calls.append(kw))
        self.app.check_alerts = mock.Mock()
        start, out = self.run_app()
        start.assert_called_once_with()
        # The start-up check is silent; a start never runs a check.
        self.assertEqual(calls, [{"notify": False}])
        self.app.check_alerts.assert_not_called()
        self.assertIn("Check schedule: */30 * * * * (every 30 min); next check at", out)

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
                self.app.check_cycle()  # token renewal fails, Grafana fails, Slack fails
        # "Automatic update FAILED" + "Failed to fetch Grafana alert"
        self.assertEqual(len(slack_calls), 2)
        self.assertIn("Automatic update FAILED", slack_calls[0])
        self.assertIn("Failed to fetch Grafana alert", slack_calls[1])
        self.assertNotIn("failed, continuing", out.getvalue())  # handled, not a crash
        self.assertNotIn(support.FAKE_PASSWORD, out.getvalue())
        self.assertNotIn(support.FAKE_SLACK_PATH, out.getvalue())

    def test_real_startup_with_everything_down_posts_nothing(self):
        # W4: the start-up token check (login refused) only logs; the next check retries
        # the login and posts "Automatic update FAILED".
        real_post = requests.post
        slack_calls = []

        def post(url, **kwargs):
            if url == self.config.slack_webhook_url:
                slack_calls.append(kwargs["data"])
                raise requests.exceptions.ConnectionError("Failed to resolve 'hooks.slack.invalid'")
            return real_post(url, **kwargs)

        self.app.check_alerts = mock.Mock()
        with mock.patch("requests.post", side_effect=post):
            _, out = self.run_app()
            self.assertEqual(slack_calls, [])
            self.assertIn("renewal FAILED", out)
            self.assertIn("not posted to Slack", out)
            self.assertNotIn("failed, continuing", out)  # handled, not a crash
            with redirect_stdout(io.StringIO()):
                self.app.check_cycle()  # the next check
        self.assertEqual(len(slack_calls), 1)
        self.assertIn("Automatic update FAILED", slack_calls[0])

    def test_startup_output_has_no_secrets(self):
        self.app.token_manager.ensure_token = mock.Mock(return_value=False)
        _, out = self.run_app()
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
