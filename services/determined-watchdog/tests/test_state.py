"""Warn -> kill state machine, persisted across restarts, against stub Grafana/Determined/Slack."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import watchdog_test_support as support
from watchdog_test_support import StubServer, make_config

from alert_DataProcessor import CREATED_AT_FORMAT
from alert_response_handler_v02 import MainApplication

ALERTS = ("GET", "/api/alertmanager/grafana/api/v2/alerts/")
SHELLS = ("GET", "/api/v1/shells/")
TASKS = ("GET", "/api/v1/tasks/")
LOGIN = ("POST", "/api/v1/auth/login/")
HOOK = ("POST", "/hook")


def kill_route(shell_id):
    return ("POST", "/api/v1/shells/%s/kill" % shell_id)


def alert(container_id, name="IdleKillAlert"):
    return {"labels": {"alertname": name, "container_id": container_id}, "status": {"state": "active"}}


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = StubServer()
        self.stub.__enter__()
        self.shells = {}  # shell_id -> (container_id, username)
        self.other_tasks = {}  # command/TensorBoard/trial allocation id -> container_id
        self.stub.routes.update({
            ALERTS: (200, []),
            SHELLS: lambda r: (200, {"shells": [
                {"id": sid, "username": user, "description": "Shell (%s)" % sid,
                 "startTime": "2026-09-28T00:00:00Z", "container": None}
                for sid, (_, user) in self.shells.items()
            ]}),
            TASKS: lambda r: (200, {"allocationIdToSummary": dict(
                [
                    ("%s.1" % tid, {"resources": [{
                        "containerId": cid,
                        "agentDevices": {"cvgl-node01": {"devices": [{"id": 0}, {"id": 1}]}},
                    }]})
                    for tid, (cid, _) in self.shells.items()
                ] + [
                    (aid, {"resources": [{"containerId": cid, "agentDevices": {}}]})
                    for aid, cid in self.other_tasks.items()
                ]
            )}),
            HOOK: (200, "ok"),
        })
        self.config = make_config(
            self.tmp.name,
            det_web=self.stub.url,
            grafana_web=self.stub.url,
            slack_webhook_url=self.stub.url + "hook",
            # These tests run consecutive checks seconds apart; the minimum interval between
            # a warning and its kill is tested by the tests that call real_min_age().
            warning_min_age_minutes=0,
        )
        base = Path(self.config.base_path)
        (base / "User.json").write_text(json.dumps({"alice": {"UID": "U0ALICE", "slack_id": "alice"}}))
        # As metrics_token.write_metrics_token leaves it: the token plus a newline.
        Path(self.config.det_metrics_token_path).write_text(
            support.token_expiring_in(timedelta(days=6)) + "\n"
        )

    def tearDown(self):
        self.stub.__exit__(None, None, None)
        self.tmp.cleanup()

    def file_text(self):
        return Path(self.config.det_metrics_token_path).read_text()

    def file_token(self):
        return self.file_text().strip()

    def start_app(self):
        """A fresh process: new MainApplication on the same data dir (simulated restart)."""
        with redirect_stdout(io.StringIO()):
            app = MainApplication(self.config)
            app.token_manager.ensure_token()
        return app

    def check(self, app):
        self.stub.requests.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            app.check_alerts()
        return out.getvalue()

    def slack(self):
        """[(title, [field values])] of the Slack posts since the last check()."""
        posts = []
        for r in self.stub.requests_to(*HOOK):
            for a in json.loads(r["body"])["attachments"]:
                posts.append((a["title"], [f["value"] for f in a["fields"]]))
        return posts

    def slack_titles(self):
        """[(title, [field titles])] of the Slack posts since the last check()."""
        posts = []
        for r in self.stub.requests_to(*HOOK):
            for a in json.loads(r["body"])["attachments"]:
                posts.append((a["title"], [f["title"] for f in a["fields"]]))
        return posts

    def kills(self):
        return [r["path"] for r in self.stub.requests if r["path"].endswith("/kill")]

    def container_of(self, task_id):
        return self.shells[task_id][0]

    def set_idle(self, *task_ids, other_alerts=()):
        """Shells (by id) whose container has the idle alert firing."""
        self.stub.routes[ALERTS] = (
            200, [alert(self.container_of(t)) for t in task_ids] + [alert("x", n) for n in other_alerts]
        )

    def test_warn_then_kill_across_restart(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})

        app = self.start_app()
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        self.assertEqual(self.kills(), [])

        app = self.start_app()  # restart between warning and kill keeps the warning state
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])
        auth = self.stub.requests_to(*kill_route("shell-a"))[0]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer " + self.file_token())

    def set_warning_created_at(self, created_at):
        """Rewrite created_at of the saved warning record, as if it had been saved then."""
        path = Path(self.config.file_info_path)
        info = json.loads(path.read_text())
        for item in info["alert_local_item"]:
            if item["alert_type"] == "IdleKillAlert":
                item["created_at"] = created_at
        path.write_text(json.dumps(info))

    def warn_shell_a(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        app = self.start_app()
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        return app

    @staticmethod
    def stamp(delta):
        return (datetime.now() + delta).strftime(CREATED_AT_FORMAT)

    def real_min_age(self):
        """Use the minimum warning age derived from the default 30-minute schedule (15 min)."""
        self.config.warning_min_age_minutes = None
        self.config.__post_init__()
        self.assertEqual(self.config.warning_min_age_minutes, 15)

    def test_default_windows(self):
        self.assertEqual(
            (self.config.check_interval_minutes, self.config.warning_max_age_minutes),
            (30, 45),
        )

    def test_stale_warning_warns_again(self):
        # Check 1: warned. Check 2: shell active, nothing saved. Check 3: idle again.
        app = self.warn_shell_a()
        self.set_warning_created_at(self.stamp(-timedelta(minutes=61)))
        self.stub.routes[ALERTS] = (200, [])
        self.check(app)
        self.assertEqual(self.slack(), [])

        self.set_idle("shell-a")
        out = self.check(app)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        self.assertIn("older than 45 min", out)

        # The new warning counts at the next check.
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def test_stale_warning_after_downtime_warns_again(self):
        self.warn_shell_a()
        self.set_warning_created_at(self.stamp(-timedelta(hours=3)))
        self.check(self.start_app())  # restarted after 3 h of downtime
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])

    def test_unparsable_created_at_warns_again(self):
        app = self.warn_shell_a()
        for created_at in ["", "not a date", "2026-09-28T10:00:00", None]:
            with self.subTest(created_at=created_at):
                self.set_warning_created_at(created_at)
                out = self.check(app)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
                self.assertIn("unparsable created_at", out)

    def test_future_created_at_warns_again(self):
        app = self.warn_shell_a()
        self.set_warning_created_at(self.stamp(timedelta(minutes=10)))
        out = self.check(app)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        self.assertIn("in the future", out)

    def test_warning_older_than_45_minutes_warns_again(self):
        # e.g. a check missed while the watchdog was down: the warning is two intervals old.
        app = self.warn_shell_a()
        for minutes in (46, 59, 60):
            with self.subTest(minutes=minutes):
                self.set_warning_created_at(self.stamp(-timedelta(minutes=minutes)))
                out = self.check(app)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
                self.assertIn("older than 45 min", out)

    def test_minimum_warning_age_skips_a_check_right_after_the_previous_one(self):
        self.real_min_age()
        app = self.warn_shell_a()
        app = self.start_app()  # e.g. a second check seconds after the first one
        out = self.check(app)
        self.assertIn("skipping this check", out)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack(), [])
        self.assertEqual(self.stub.requests_to(*SHELLS), [])

        self.set_warning_created_at(self.stamp(-timedelta(minutes=30)))  # the next check
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def test_minimum_warning_age_allows_a_kill_after_it(self):
        self.real_min_age()
        app = self.warn_shell_a()
        self.set_warning_created_at(self.stamp(-timedelta(minutes=16)))
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])

    def test_minimum_warning_age_skips_every_check_within_it(self):
        # e.g. a check soon after a changed schedule: the minimum is 15 minutes, not seconds.
        self.real_min_age()
        app = self.warn_shell_a()
        for minutes in (1, 10, 14):
            with self.subTest(minutes=minutes):
                self.set_warning_created_at(self.stamp(-timedelta(minutes=minutes)))
                out = self.check(self.start_app())
                self.assertIn("skipping this check", out)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.slack(), [])
                self.assertEqual(self.stub.requests_to(*SHELLS), [])

    def test_minimum_warning_age_with_the_watchdog_clock(self):
        # created_at and the age checks use the same injectable clock (app.now).
        self.real_min_age()
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        hour = datetime(2026, 9, 28, 10, 0, 5)

        app = self.start_app()
        app.now = lambda: hour
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        item = json.loads(Path(self.config.file_info_path).read_text())["alert_local_item"][0]
        self.assertEqual(item["created_at"], "2026-09-28 10:00:05")

        app = self.start_app()  # a second check 40 s later
        app.now = lambda: hour + timedelta(seconds=40)
        self.assertIn("skipping this check", self.check(app))
        self.assertEqual((self.kills(), self.slack()), ([], []))

        app.now = lambda: hour + timedelta(minutes=30)  # the next check
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def test_debug_mode_is_not_held_back_by_the_minimum_warning_age(self):
        # Debug mode checks every ~20 s: the next check dry-runs the kill of the shell just warned.
        self.config.is_debug = True
        self.real_min_age()
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[("GET", "/api/v1/shells/shell-a")] = (200, {"shell": {}})
        app = self.start_app()
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["alice"])])
        out = self.check(app)
        self.assertNotIn("skipping this check", out)
        self.assertEqual(self.kills(), [])
        self.assertEqual(len(self.stub.requests_to("GET", "/api/v1/shells/shell-a")), 1)
        self.assertEqual(self.slack(), [("Terminated", ["alice"])])

    def test_warning_29_to_31_minutes_old_kills(self):
        # The next check after the warning comes one interval (30 min) later, give or take.
        for minutes in (29, 30, 31):
            with self.subTest(minutes=minutes):
                self.config.warning_min_age_minutes = 0  # warn right after the previous kill
                app = self.warn_shell_a()
                self.real_min_age()
                self.set_warning_created_at(self.stamp(-timedelta(minutes=minutes)))
                self.check(app)
                self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
                self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def test_saved_record_has_device_count(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.check(self.start_app())
        with open(self.config.file_info_path) as f:
            info = json.load(f)
        (item,) = [i for i in info["alert_local_item"] if i["alert_type"] == "IdleKillAlert"]
        with open(Path(item["directory"]) / item["file_name"]) as f:
            record = json.load(f)
        self.assertEqual(record["shell-a"]["device_count"], 2)
        self.assertEqual(record["shell-a"]["container_id"], "c-a")

    def test_failed_kill_is_not_reported_and_stays_tracked(self):
        self.shells = {"shell-a": ("c-a", "alice"), "shell-b": ("c-b", "bob")}
        self.set_idle("shell-a", "shell-b")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        self.stub.routes[kill_route("shell-b")] = (500, {"error": "boom"})
        app = self.start_app()

        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>", "bob"])])

        out = self.check(app)
        self.assertEqual(
            sorted(self.kills()), ["/api/v1/shells/shell-a/kill", "/api/v1/shells/shell-b/kill"]
        )
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])  # b failed: not reported
        self.assertIn("Kill FAILED for shell shell-b", out)

        # Next check: b is still tracked, so the kill is retried (no second warning).
        self.stub.routes[kill_route("shell-b")] = (200, {})
        self.set_idle("shell-b")
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-b/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["bob"])])

    def test_only_failed_kills_posts_nothing(self):
        self.shells = {"shell-b": ("c-b", "bob")}
        self.set_idle("shell-b")
        self.stub.routes[kill_route("shell-b")] = (403, {"error": "forbidden"})
        app = self.start_app()
        self.check(app)
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-b/kill"])
        self.assertEqual(self.slack(), [])  # no empty "Terminated"/"Warning" message

    def test_debug_mode_kill_is_a_dry_run(self):
        self.config.is_debug = True
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[("GET", "/api/v1/shells/shell-a")] = (200, {"shell": {}})
        app = self.start_app()
        self.check(app)
        self.check(app)
        self.assertEqual(self.kills(), [])
        self.assertEqual(len(self.stub.requests_to("GET", "/api/v1/shells/shell-a")), 1)
        self.assertEqual(self.slack(), [("Terminated", ["alice"])])  # debug: no @-mention

    def test_other_alert_or_no_alert_is_log_only(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        app = self.start_app()
        self.stub.routes[ALERTS] = (200, [alert("x", "DiskFull")])
        out = self.check(app)
        self.assertEqual(self.slack(), [])
        self.assertIn("is not firing", out)
        self.stub.routes[ALERTS] = (200, [])
        self.check(app)
        self.assertEqual(self.slack(), [])

    def test_idle_containers_without_shell_are_log_only(self):
        self.shells = {}
        self.stub.routes[ALERTS] = (200, [alert("c-trial")])
        self.check(self.start_app())
        self.assertEqual(self.slack(), [])

    def test_det_api_error_warns_and_kills_nothing(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        app = self.start_app()
        self.check(app)  # warned
        shells_route = self.stub.routes[SHELLS]
        for route in [(403, {"code": 7, "message": "forbidden"}), (502, "<html>"), (200, "nope")]:
            with self.subTest(route=route):
                self.stub.routes[SHELLS] = route
                self.check(app)
                self.assertEqual(self.kills(), [])
                self.assertEqual(self.slack(), [("Warning", ["det api miss"])])
                self.assertEqual(self.slack_titles(), [("Warning", ["need update api!\n"])])
                self.assertEqual(self.stub.requests_to(*LOGIN), [])  # only HTTP 401 renews
        self.stub.routes[SHELLS] = shells_route
        self.check(app)  # the warning saved before the errors still counts
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def reject_old_token(self, *accepted_tokens, routes=(SHELLS, TASKS)):
        """Determined rejects every token but accepted_tokens on these GETs (revoked session)."""
        accepted = {"Bearer " + t for t in accepted_tokens}

        def gate(route):
            def handler(r):
                if r["headers"].get("Authorization") in accepted:
                    return route(r)
                return 401, {"code": 16, "message": "unauthenticated"}
            return handler

        for route in routes:
            self.stub.routes[route] = gate(self.stub.routes[route])

    def test_rejected_token_is_renewed_once_and_the_cycle_continues(self):
        old_token = self.file_token()  # expires in 6 days
        new_token = support.token_expiring_in(timedelta(days=7))
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        self.stub.routes[LOGIN] = (200, {"token": new_token})
        self.reject_old_token(new_token)

        app = self.start_app()  # the file token looks valid: no login at start
        self.assertEqual(app.token_manager.token, old_token)
        out = self.check(app)
        self.assertEqual(len(self.stub.requests_to(*LOGIN)), 1)
        self.assertEqual(self.file_text(), new_token + "\n")
        self.assertEqual(
            [r["headers"]["Authorization"] for r in self.stub.requests_to(*SHELLS)],
            ["Bearer " + old_token, "Bearer " + new_token],
        )
        self.assertEqual(
            self.slack(), [("Warning", ["notification"]), ("Warning", ["<@U0ALICE>"])]
        )  # "Automatic update success ~", then the idle-shell warning
        self.assertIn("Determined rejected the token", out)
        self.assertNotIn(new_token, out)

        self.check(app)  # next check: the new token is used directly, the kill goes through
        self.assertEqual(self.stub.requests_to(*LOGIN), [])
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        auth = self.stub.requests_to(*kill_route("shell-a"))[0]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer " + new_token)

    def test_rejected_token_with_failing_login_keeps_old_token(self):
        old_token = self.file_token()
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[LOGIN] = (401, {"message": "invalid credentials"})
        self.reject_old_token("never-issued")
        app = self.start_app()
        self.check(app)
        self.assertEqual(len(self.stub.requests_to(*LOGIN)), 1)
        self.assertEqual(len(self.stub.requests_to(*SHELLS)), 1)  # no retry without a new token
        self.assertEqual(self.file_text(), old_token + "\n")
        self.assertEqual(app.token_manager.token, old_token)
        self.assertEqual(self.slack(), [("Warning", ["ERROR"]), ("Warning", ["det api miss"])])
        self.assertEqual(self.kills(), [])

    def test_still_rejected_after_renewal_is_not_retried_again(self):
        new_token = support.token_expiring_in(timedelta(days=7))
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[LOGIN] = (200, {"token": new_token})
        self.reject_old_token("never-issued")
        app = self.start_app()
        self.check(app)
        self.assertEqual(len(self.stub.requests_to(*LOGIN)), 1)
        self.assertEqual(len(self.stub.requests_to(*SHELLS)), 2)
        self.assertEqual(self.file_text(), new_token + "\n")
        self.assertEqual(self.slack(), [("Warning", ["notification"]), ("Warning", ["det api miss"])])
        self.assertEqual(self.kills(), [])

    def cycle(self, app):
        """One full check cycle: the token check, then the alert check."""
        self.stub.requests.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            app.check_cycle()
        return out.getvalue()

    def shells_auth(self):
        return [r["headers"].get("Authorization") for r in self.stub.requests_to(*SHELLS)]

    def rejected_token_with_unwritable_file(self, also_accepted=()):
        """Check 1: the file token is rejected (401), the login works, the file cannot be replaced.

        Returns (app, old_token, new_token); replacing the token file still fails
        afterwards (stop self.replace_patch to let it succeed again). Determined
        accepts new_token and also_accepted.
        """
        old_token = self.file_token()  # expires in 6 days, but revoked on the master
        new_token = support.token_expiring_in(timedelta(days=7))
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        self.stub.routes[LOGIN] = (200, {"token": new_token})
        self.reject_old_token(new_token, *also_accepted)
        app = self.start_app()  # the file token looks valid: no login at start
        self.assertEqual(app.token_manager.token, old_token)

        # Only the token file cannot be replaced (e.g. its directory's owner changed);
        # the watchdog's own state files in data/ still can. metrics_token.os is the
        # os module, so the patch applies to every os.replace call.
        token_path = Path(self.config.det_metrics_token_path)
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(dst) == token_path:
                raise OSError("disk full")
            return real_replace(src, dst, *args, **kwargs)

        self.replace_patch = mock.patch("metrics_token.os.replace", side_effect=replace)
        self.replace_patch.start()
        self.addCleanup(mock.patch.stopall)
        out = self.cycle(app)
        self.assertEqual(len(self.stub.requests_to(*LOGIN)), 1)
        # Retried once, with the new session although it could not be written.
        self.assertEqual(self.shells_auth(), ["Bearer " + old_token, "Bearer " + new_token])
        self.assertEqual(
            self.slack(), [("Warning", ["ERROR"]), ("Warning", ["<@U0ALICE>"])]
        )  # "Automatic update FAILED ~", then the idle-shell warning (no "det api miss")
        self.assertEqual(self.file_text(), old_token + "\n")
        self.assertEqual(
            [p.name for p in Path(self.config.det_metrics_token_path).parent.iterdir()], ["token"]
        )
        self.assertEqual(app.token_manager.token, new_token)
        self.assertNotIn(new_token, out)
        return app, old_token, new_token

    def test_rejected_token_with_failing_write_uses_the_new_session(self):
        app, old_token, new_token = self.rejected_token_with_unwritable_file()

        # Check 2, the file still cannot be replaced: no new login, the file token is
        # not taken back, the kill goes through with the new session.
        out = self.cycle(app)
        self.assertEqual(self.stub.requests_to(*LOGIN), [])
        self.assertEqual(self.shells_auth(), ["Bearer " + new_token])
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        auth = self.stub.requests_to(*kill_route("shell-a"))[0]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer " + new_token)
        self.assertEqual(
            self.slack(), [("Warning", ["ERROR"]), ("Terminated", ["<@U0ALICE>"])]
        )  # the retried write FAILED again (no "det api miss"), then the kill report
        self.assertEqual(self.file_text(), old_token + "\n")
        self.assertNotIn(new_token, out)

        # Check 3, writable again: the same session is written, still without a login.
        self.replace_patch.stop()
        self.stub.routes[ALERTS] = (200, [])
        out = self.cycle(app)
        self.assertEqual(self.stub.requests_to(*LOGIN), [])
        self.assertEqual(self.file_text(), new_token + "\n")
        self.assertEqual(self.slack(), [])
        self.assertIn("Wrote the Determined token obtained earlier", out)
        self.assertIsNone(app.token_manager._unwritten)
        self.assertNotIn(new_token, out)

        # Check 4: an ordinary check of the (now written) file token.
        self.cycle(app)
        self.assertEqual(self.stub.requests_to(*LOGIN), [])
        self.assertEqual(app.token_manager.token, new_token)

    def test_token_placed_by_hand_after_failing_write_is_adopted(self):
        hand_token = support.token_expiring_in(timedelta(days=5))
        app, old_token, new_token = self.rejected_token_with_unwritable_file(
            also_accepted=(hand_token,)
        )

        # An admin places a token by hand (see ../prometheus/README.md) before check 2.
        Path(self.config.det_metrics_token_path).write_text(hand_token + "\n")

        out = self.cycle(app)
        self.assertEqual(self.stub.requests_to(*LOGIN), [])
        self.assertEqual(app.token_manager.token, hand_token)
        self.assertEqual(self.shells_auth(), ["Bearer " + hand_token])
        auth = self.stub.requests_to(*kill_route("shell-a"))[0]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer " + hand_token)
        self.assertEqual(self.file_text(), hand_token + "\n")  # no write attempted
        # os.replace still fails, but no write is attempted: no "Automatic update FAILED ~".
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])
        self.assertIn("was replaced since the failed write", out)
        self.assertIsNone(app.token_manager._unwritten)

    def test_grafana_failure_warns(self):
        app = self.start_app()
        self.stub.routes[ALERTS] = (500, "boom")
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["ERROR"])])

    def saved_record(self):
        """The warning record saved by the last check that saved one."""
        info = json.loads(Path(self.config.file_info_path).read_text())
        (item,) = [i for i in info["alert_local_item"] if i["alert_type"] == "IdleKillAlert"]
        return json.loads((Path(item["directory"]) / item["file_name"]).read_text())

    def write_old_state(self, record, created_at):
        """file_info.json and a record as an earlier watchdog saved them."""
        directory = Path(self.config.base_path) / "localData" / "2026-09" / "2026-09-28"
        directory.mkdir(parents=True)
        (directory / "localData_20260928100000.json").write_text(json.dumps(record))
        empty = {"alert_type": "IdleKillAlert", "file_name": "", "directory": "",
                 "created_at": "", "file_type": ""}
        Path(self.config.file_info_path).write_text(json.dumps({
            "file_group_name": "file_info.json",
            "alert_item": [empty],
            "alert_local_item": [dict(
                empty, file_name="localData_20260928100000.json", directory=str(directory),
                created_at=created_at, file_type="IdleKillAlert",
            )],
        }, indent=4))

    OLD_SHELL_ENTRY = {"container_id": "c-a", "description": "Shell (shell-a)", "username": "alice",
                       "startTime": "2026-09-28T00:00:00Z", "device_count": 2,
                       "devices": [{"id": 0}, {"id": 1}]}

    def test_warning_without_kind_still_kills(self):
        # A record saved before "kind" was added holds shells.
        self.real_min_age()
        self.shells = {"shell-a": ("c-a", "alice")}
        self.write_old_state({"shell-a": self.OLD_SHELL_ENTRY}, self.stamp(-timedelta(minutes=30)))
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})

        self.check(self.start_app())
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack_titles(), [("Terminated", ["[Shell] Shell (shell-a)\n"])])

    def test_notebook_warned_by_an_earlier_version_is_dropped(self):
        # Determined 0.43.0 has no notebooks: the watchdog neither lists nor kills them, and an
        # upgrade between a notebook's warning and its kill only drops the notebook's entry.
        self.real_min_age()
        self.shells = {"shell-a": ("c-a", "alice")}
        self.write_old_state(
            {"shell-a": dict(self.OLD_SHELL_ENTRY, kind="shell"),
             "nb-b": {"kind": "notebook", "container_id": "c-nb", "description": "JupyterLab (nb-b)",
                      "username": "bob", "startTime": "2026-09-28T00:00:00Z", "device_count": 1,
                      "devices": [{"id": 2}]}},
            self.stamp(-timedelta(minutes=30)),
        )
        self.stub.routes[ALERTS] = (200, [alert("c-a"), alert("c-nb")])
        self.stub.routes[kill_route("shell-a")] = (200, {})

        self.check(self.start_app())
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack_titles(), [("Terminated", ["[Shell] Shell (shell-a)\n"])])
        self.assertEqual(self.saved_record(), {})
        self.assertEqual([r for r in self.stub.requests if "notebooks" in r["path"]], [])

    def test_no_notebook_listing(self):
        # A master without /api/v1/notebooks/ (0.43.0) answers 404: never requested, no warning.
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        app = self.start_app()
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])
        self.assertEqual([r for r in self.stub.requests if "notebooks" in r["path"]], [])

    def test_silenced_shell_is_exempt(self):
        self.shells = {"shell-a": ("c-a", "alice"), "shell-b": ("c-b", "bob")}
        silenced = {"c-b"}  # a Grafana silence on IdleKillAlert{container_id="c-b"}

        def alertmanager(r):
            with_silenced = r["query"].get("silenced") != ["false"]
            return 200, [alert(c) for c in ("c-a", "c-b") if with_silenced or c not in silenced]

        self.stub.routes[ALERTS] = alertmanager
        self.stub.routes[kill_route("shell-a")] = (200, {})
        self.stub.routes[kill_route("shell-b")] = (200, {})
        app = self.start_app()
        self.check(app)
        self.assertEqual(self.slack(), [("Warning", ["<@U0ALICE>"])])
        self.check(app)
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])
        self.assertEqual(self.slack(), [("Terminated", ["<@U0ALICE>"])])

    def test_failing_shell_list_warns_and_kills_nothing(self):
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        self.stub.routes[kill_route("shell-a")] = (200, {})
        app = self.start_app()
        self.check(app)
        file_info = Path(self.config.file_info_path).read_text()

        shells_route = self.stub.routes[SHELLS]
        self.stub.routes[SHELLS] = (500, "boom")
        out = self.check(app)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack_titles(), [("Warning", ["need update api!\n"])])
        self.assertEqual(self.stub.requests_to(*TASKS), [])
        self.assertIn("GET shells: HTTP 500", out)
        self.assertEqual(Path(self.config.file_info_path).read_text(), file_info)  # nothing saved

        self.stub.routes[SHELLS] = shells_route
        self.check(app)  # the warning saved before the error still counts
        self.assertEqual(self.kills(), ["/api/v1/shells/shell-a/kill"])

    def test_failing_task_list_warns_and_kills_nothing(self):
        # The task list gives the shells' containers.
        self.shells = {"shell-a": ("c-a", "alice")}
        self.set_idle("shell-a")
        app = self.start_app()
        self.check(app)
        self.stub.routes[TASKS] = (500, "boom")
        self.check(app)
        self.assertEqual(self.kills(), [])
        self.assertEqual(self.slack_titles(), [("Warning", ["need update api!\n"])])

    def test_idle_commands_and_tensorboards_are_not_policed(self):
        # Their allocations are also "<task id>.1", but they are not shells.
        self.other_tasks = {"cmd-x.1": "c-cmd", "tb-y.1": "c-tb"}
        self.stub.routes[ALERTS] = (200, [alert("c-cmd"), alert("c-tb")])
        app = self.start_app()
        self.check(app)
        self.check(app)
        self.assertEqual((self.kills(), self.slack()), ([], []))


class FileInfoInitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def init(self):
        from alert_APIHandler import APIHandler

        with redirect_stdout(io.StringIO()):
            APIHandler(self.config)
        with open(self.config.file_info_path) as f:
            return json.load(f)

    def test_missing_is_created_including_directory(self):
        self.config.base_path = Path(self.tmp.name) / "debug"  # like /app/data/debug
        data = self.init()
        self.assertEqual(data["file_group_name"], "file_info.json")
        self.assertEqual(
            data["alert_local_item"],
            [{"alert_type": "IdleKillAlert", "file_name": "", "directory": "",
              "created_at": "", "file_type": ""}],
        )

    def test_valid_file_is_kept(self):
        legacy = {
            "file_group_name": "file_info.json",
            "alert_item": [],
            "alert_local_item": [
                {"alert_type": "IdleKillAlert", "file_name": "localData_1.json", "directory": "/x",
                 "created_at": "2026-09-28 10:00:00", "file_type": "IdleKillAlert"},
                {"alert_type": "IdleWarning", "file_name": "", "directory": "",
                 "created_at": "", "file_type": ""},
            ],
        }
        Path(self.config.file_info_path).write_text(json.dumps(legacy))
        self.assertEqual(self.init(), legacy)

    def test_invalid_file_is_reinitialized(self):
        for content in ["{truncated", "[]", json.dumps({"alert_local_item": []}),
                        json.dumps({"alert_local_item": [{"alert_type": "OtherAlert"}]})]:
            with self.subTest(content=content):
                Path(self.config.file_info_path).write_text(content)
                data = self.init()
                self.assertEqual(data["alert_local_item"][0]["alert_type"], "IdleKillAlert")
                self.assertEqual(data["alert_local_item"][0]["file_name"], "")

    def test_corrupt_last_record_is_ignored(self):
        from alert_DataProcessor import DataProcessor

        self.init()
        record = Path(self.tmp.name) / "record.json"
        record.write_text("{trunc")
        processor = DataProcessor(self.config)
        now = datetime.now()
        out = io.StringIO()
        with redirect_stdout(out):
            processor.set_file_info(
                "IdleKillAlert",
                {"file_name": "record.json", "directory": self.tmp.name,
                 "created_at": now.strftime(CREATED_AT_FORMAT), "file_type": "IdleKillAlert"},
                "alert_local_item",
                self.config.file_info_path,
            )
            self.assertEqual(
                processor.load_last_output("IdleKillAlert", self.config.file_info_path, now), {}
            )
        self.assertIn("unreadable previous alert record", out.getvalue())


if __name__ == "__main__":
    unittest.main()
