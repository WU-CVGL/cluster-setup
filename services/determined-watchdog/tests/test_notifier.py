import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import requests

import watchdog_test_support as support
from watchdog_test_support import StubServer, closed_port_url, make_config

from alert_MessageNotifier import MessageNotifier

USERS = {"alice": {"UID": "U0ALICE", "slack_id": "alice"}}
WARN = {"shell-a": {"container_id": "c-a", "username": "alice", "description": "Shell (a)"}}
KILLED = {"shell-b": {"container_id": "c-b", "username": "bob", "description": "Shell (b)"}}


class SlackFailureTest(unittest.TestCase):
    """Slack delivery is best-effort: no failure may raise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        self.notifier = MessageNotifier(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def call_all(self, url):
        out = io.StringIO()
        with redirect_stdout(out):
            results = [
                self.notifier.send_slack_warning("ERROR", "something", url),
                self.notifier.send_slack_notification(WARN, KILLED, USERS, url),
            ]
        return results, out.getvalue()

    def test_connection_refused(self):
        url = closed_port_url(support.FAKE_SLACK_PATH)
        results, out = self.call_all(url)
        self.assertEqual(results, [False, False])
        self.assertIn("Slack delivery failed", out)
        self.assertNotIn(support.FAKE_SLACK_PATH, out)

    def test_dns_failure(self):
        # What urllib3 reports when hooks.slack.com cannot be resolved (2026-09-09 incident).
        url = self.config.slack_webhook_url
        error = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='hooks.slack.invalid', port=443): Max retries exceeded "
            "with url: %s (Caused by NameResolutionError(\"Failed to resolve "
            "'hooks.slack.invalid' ([Errno -2] Name or service not known)\"))" % support.FAKE_SLACK_PATH
        )
        with mock.patch("alert_MessageNotifier.requests.post", side_effect=error) as post:
            results, out = self.call_all(url)
        self.assertEqual(results, [False, False])
        self.assertEqual(post.call_count, 2)
        self.assertIsNotNone(post.call_args.kwargs.get("timeout"))
        self.assertIn("ConnectionError", out)
        self.assertNotIn(support.FAKE_SLACK_PATH, out)
        self.assertNotIn("placeholderwebhooksecret", out)

    def test_unexpected_exception(self):
        with mock.patch("alert_MessageNotifier.requests.post", side_effect=RuntimeError("x")):
            results, _ = self.call_all(self.config.slack_webhook_url)
        self.assertEqual(results, [False, False])

    def test_non_200(self):
        for status in (404, 500, 302):
            with self.subTest(status=status):
                with StubServer({("POST", support.FAKE_SLACK_PATH): (status, "no_service")}) as stub:
                    results, out = self.call_all(stub.url.rstrip("/") + support.FAKE_SLACK_PATH)
                self.assertEqual(results, [False, False])
                self.assertIn("HTTP %d" % status, out)

    def test_success(self):
        with StubServer({("POST", support.FAKE_SLACK_PATH): (200, "ok")}) as stub:
            results, _ = self.call_all(stub.url.rstrip("/") + support.FAKE_SLACK_PATH)
        self.assertEqual(results, [True, True])


class SlackMessageFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = StubServer({("POST", "/hook"): (200, "ok")})
        self.stub.__enter__()
        self.url = self.stub.url + "hook"

    def tearDown(self):
        self.stub.__exit__(None, None, None)
        self.tmp.cleanup()

    def posted(self):
        return [json.loads(r["body"]) for r in self.stub.requests_to("POST", "/hook")]

    def notify(self, new, killed, debug=False):
        notifier = MessageNotifier(make_config(self.tmp.name, is_debug=debug))
        with redirect_stdout(io.StringIO()):
            return notifier.send_slack_notification(new, killed, USERS, self.url)

    def test_warning_only(self):
        self.assertTrue(self.notify(WARN, {}))
        (msg,) = self.posted()
        self.assertEqual(msg["blocks"], [])
        (att,) = msg["attachments"]
        self.assertEqual(
            att,
            {
                "fallback": "Warning",
                "color": "warning",
                "title": "Warning",
                "fields": [{"value": "<@U0ALICE>", "title": "Shell (a)\n", "short": True}],
                "footer": "Your container will be released in 60 minutes. Please check your task!!!",
            },
        )

    def test_terminated_only(self):
        self.assertTrue(self.notify({}, KILLED))
        (att,) = self.posted()[0]["attachments"]
        self.assertEqual(att["title"], "Terminated")
        self.assertEqual(att["fallback"], "Terminated")
        self.assertEqual(att["color"], "good")
        self.assertEqual(att["footer"], "These GPU containers have been released")
        # bob has no User.json entry: plain username
        self.assertEqual(att["fields"], [{"value": "bob", "title": "Shell (b)\n", "short": True}])

    def test_both(self):
        self.assertTrue(self.notify(WARN, KILLED))
        titles = [a["title"] for a in self.posted()[0]["attachments"]]
        self.assertEqual(titles, ["Warning", "Terminated"])

    def test_nothing_to_report_posts_nothing(self):
        self.assertFalse(self.notify({}, {}))
        self.assertEqual(self.posted(), [])

    def test_debug_mode_does_not_mention(self):
        self.notify(WARN, {}, debug=True)
        self.assertEqual(self.posted()[0]["attachments"][0]["fields"][0]["value"], "alice")

    def test_warning_format(self):
        notifier = MessageNotifier(make_config(self.tmp.name))
        notifier.send_slack_warning("det api miss", "need update api!", self.url)
        self.assertEqual(
            self.posted(),
            [{
                "attachments": [{
                    "fallback": "Warning",
                    "color": "warning",
                    "title": "Warning",
                    "fields": [{"value": "det api miss", "title": "need update api!\n", "short": True}],
                    "footer": "det api miss",
                }]
            }],
        )


if __name__ == "__main__":
    unittest.main()
