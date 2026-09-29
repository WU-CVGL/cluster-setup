import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import requests

import watchdog_test_support as support
from watchdog_test_support import StubServer, closed_port_url, make_config

from alert_APIHandler import APIHandler, DetAPIError

ALERTS_PATH = "/api/alertmanager/grafana/api/v2/alerts/"


class APITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub = StubServer()
        self.stub.__enter__()
        self.config = make_config(self.tmp.name, det_web=self.stub.url, grafana_web=self.stub.url)
        with redirect_stdout(io.StringIO()):
            self.api = APIHandler(self.config)

    def tearDown(self):
        self.stub.__exit__(None, None, None)
        self.tmp.cleanup()

    def quiet(self, func, *args):
        with redirect_stdout(io.StringIO()):
            return func(*args)


class GrafanaQueryTest(APITestCase):
    def test_query_excludes_silenced_and_inhibited(self):
        alerts = [{"labels": {"alertname": "IdleKillAlert", "container_id": "c1"}}]
        self.stub.routes[("GET", ALERTS_PATH)] = (200, alerts)
        self.assertEqual(self.quiet(self.api.get_alert_rules), alerts)
        (req,) = self.stub.requests_to("GET", ALERTS_PATH)
        self.assertEqual(
            req["query"], {"active": ["true"], "silenced": ["false"], "inhibited": ["false"]}
        )
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + support.FAKE_GRAFANA_TOKEN)

    def test_every_request_has_a_timeout(self):
        with mock.patch("alert_APIHandler.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = []
            self.quiet(self.api.get_alert_rules)
        self.assertIsNotNone(get.call_args.kwargs.get("timeout"))
        self.assertEqual(get.call_args.kwargs["params"],
                         {"active": "true", "silenced": "false", "inhibited": "false"})

    def test_failures_return_none(self):
        for route in [(500, "boom"), (200, "not json"), (200, {"not": "a list"}), (401, {"message": "x"})]:
            with self.subTest(route=route):
                self.stub.routes[("GET", ALERTS_PATH)] = route
                self.assertIsNone(self.quiet(self.api.get_alert_rules))
        self.config.grafana_web = closed_port_url()
        with redirect_stdout(io.StringIO()):
            api = APIHandler(self.config)
            self.assertIsNone(api.get_alert_rules())

    def test_group_by_alertname(self):
        alerts = [
            {"labels": {"alertname": "IdleKillAlert", "container_id": "c1"}},
            {"labels": {"alertname": "IdleKillAlert", "container_id": "c2"}},
            {"labels": {"alertname": "DiskFull"}},
            {"no_labels": True},
            "junk",
        ]
        self.assertEqual(
            self.api.get_container_ids_by_alertname(alerts),
            {"IdleKillAlert": {"c1", "c2"}, "DiskFull": set()},
        )


class DeterminedDataTest(APITestCase):
    def test_parse_shells_and_devices(self):
        shells = {"shells": [
            {"id": "s1", "username": "alice", "description": "d1", "startTime": "t1"},
            {"id": "s2", "username": "bob", "description": "d2", "startTime": "t2"},
            {"id": "s3", "username": "carol"},  # no task summary
            {"id": "s4", "username": "dave"},  # no container yet
            {"id": "s5", "username": "erin"},  # no resources
        ]}
        tasks = {"allocationIdToSummary": {
            "s1.1": {"resources": [{"containerId": "c1", "agentDevices": {
                "node01": {"devices": [{"id": 0}, {"id": 1}]},
                "node02": {"devices": [{"id": 3}]},
            }}]},
            "s2.1": {"resources": [{"containerId": "c2", "agentDevices": None}]},
            "s4.1": {"resources": [{"containerId": None, "agentDevices": {}}]},
            "s5.1": {"resources": None},
        }}
        result = self.quiet(self.api.parse_api_data, shells, tasks)
        self.assertEqual(sorted(result), ["s1", "s2"])
        self.assertEqual(result["s1"]["container_id"], "c1")
        self.assertEqual(result["s1"]["device_count"], 3)
        self.assertEqual(result["s1"]["username"], "alice")
        self.assertEqual(result["s2"]["device_count"], 0)
        self.assertEqual(result["s2"]["devices"], [])

    def test_empty_responses(self):
        self.assertEqual(self.api.parse_api_data({}, {}), {})
        self.assertEqual(self.api.parse_api_data({"shells": None}, {"allocationIdToSummary": None}), {})

    def test_fetch_errors_raise_det_api_error(self):
        headers = {"Authorization": "Bearer x"}
        for route in [(401, {"code": 16}), (500, "boom"), (200, "not json"), (200, {"error": {"x": 1}}),
                      (200, {"shells": "nope"})]:
            with self.subTest(route=route):
                self.stub.routes[("GET", "/api/v1/shells/")] = route
                with self.assertRaises(DetAPIError) as ctx:
                    self.api.get_shell_api_data(headers)
                # The HTTP status is kept only for non-2xx responses (401 triggers a token renewal).
                self.assertEqual(ctx.exception.status_code, route[0] if route[0] >= 300 else None)
        self.api.det_task_api = closed_port_url("/api/v1/tasks/")
        with self.assertRaises(DetAPIError) as ctx:
            self.api.get_task_api_data(headers)
        self.assertIsNone(ctx.exception.status_code)  # connection refused: no HTTP status
        self.stub.routes[("GET", "/api/v1/shells/")] = (200, {"shells": []})
        self.assertEqual(self.api.get_shell_api_data(headers), {"shells": []})
        self.assertEqual(self.stub.requests_to("GET", "/api/v1/shells/")[-1]["headers"]["Authorization"],
                         "Bearer x")


class KillReportingTest(APITestCase):
    SHELLS = {
        "ok": {"username": "alice"},
        "err": {"username": "bob"},
        "gone": {"username": "carol"},
    }

    def test_successful_and_failed_kills(self):
        self.stub.routes[("POST", "/api/v1/shells/ok/kill")] = (200, {})
        self.stub.routes[("POST", "/api/v1/shells/err/kill")] = (500, {"error": "x"})
        # "gone": no route -> 404
        killed, failed = self.quiet(self.api.kill_containers, self.SHELLS, False, {"Authorization": "Bearer t"})
        self.assertEqual(sorted(killed), ["ok"])
        self.assertEqual(sorted(failed), ["err", "gone"])
        self.assertEqual(failed["err"], {"username": "bob"})

    def test_connection_error_is_a_failed_kill(self):
        with mock.patch("alert_APIHandler.requests.post",
                        side_effect=requests.exceptions.ConnectTimeout("timed out")) as post:
            killed, failed = self.quiet(self.api.kill_containers, {"s": {}}, False, {})
        self.assertEqual((killed, sorted(failed)), ({}, ["s"]))
        self.assertIsNotNone(post.call_args.kwargs.get("timeout"))

    def test_debug_mode_only_gets_the_shell(self):
        self.stub.routes[("GET", "/api/v1/shells/ok")] = (200, {"shell": {}})
        killed, failed = self.quiet(self.api.kill_containers, {"ok": {}, "err": {}}, True, {})
        self.assertEqual((sorted(killed), sorted(failed)), (["ok"], ["err"]))
        self.assertEqual([r for r in self.stub.requests if r["method"] == "POST"], [])


if __name__ == "__main__":
    unittest.main()
