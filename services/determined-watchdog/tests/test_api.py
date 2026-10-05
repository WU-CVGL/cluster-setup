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
        self.assertEqual({info["kind"] for info in result.values()}, {"shell"})

    def test_parse_shells_and_notebooks(self):
        shells = {"shells": [{"id": "s1", "username": "alice", "description": "Shell (x)"}]}
        notebooks = {"notebooks": [
            {"id": "n1", "username": "bob", "description": "JupyterLab (y)", "startTime": "t1",
             "serviceAddress": "/proxy/n1/?token=PLACEHOLDER-jupyter-token"},
            {"id": "n2", "username": "carol"},  # CPU-only or still queued: no container yet
            {"id": "n3", "username": "dave"},  # no task summary
        ]}
        tasks = {"allocationIdToSummary": {
            "s1.1": {"resources": [{"containerId": "c1", "agentDevices": None}]},
            # A notebook's single allocation is "<notebook id>.1", as for shells.
            "n1.1": {"resources": [{"containerId": "c2", "agentDevices": {
                "node07": {"devices": [{"id": 4}]},
            }}]},
            "n2.1": {"resources": [{"containerId": None, "agentDevices": {}}]},
            # Commands and TensorBoards also have "<id>.1" allocations: never policed.
            "cmd1.1": {"resources": [{"containerId": "c3", "agentDevices": None}]},
        }}
        result = self.quiet(self.api.parse_api_data, shells, tasks, notebooks)
        self.assertEqual(sorted(result), ["n1", "s1"])
        self.assertEqual(result["s1"]["kind"], "shell")
        self.assertEqual(
            result["n1"],
            {"kind": "notebook", "container_id": "c2", "description": "JupyterLab (y)",
             "username": "bob", "startTime": "t1", "device_count": 1, "devices": [{"id": 4}]},
        )  # the serviceAddress (Jupyter token) is not copied
        # Without notebook data (older callers), only the shells.
        self.assertEqual(sorted(self.quiet(self.api.parse_api_data, shells, tasks)), ["s1"])

    def test_empty_responses(self):
        self.assertEqual(self.api.parse_api_data({}, {}), {})
        self.assertEqual(self.api.parse_api_data({"shells": None}, {"allocationIdToSummary": None}), {})
        self.assertEqual(self.api.parse_api_data({}, {}, {}), {})
        self.assertEqual(
            self.api.parse_api_data({}, {"allocationIdToSummary": {}}, {"notebooks": None}), {}
        )

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

    def test_notebook_fetch(self):
        headers = {"Authorization": "Bearer x"}
        for route in [(401, {"code": 16}), (500, "boom"), (200, "not json"), (200, {"error": {"x": 1}}),
                      (200, {"notebooks": "nope"})]:
            with self.subTest(route=route):
                self.stub.routes[("GET", "/api/v1/notebooks/")] = route
                with self.assertRaises(DetAPIError) as ctx:
                    self.api.get_notebook_api_data(headers)
                self.assertEqual(ctx.exception.status_code, route[0] if route[0] >= 300 else None)
        body = {"notebooks": [{"id": "n1"}], "pagination": {}}
        self.stub.routes[("GET", "/api/v1/notebooks/")] = (200, body)
        self.assertEqual(self.api.get_notebook_api_data(headers), body)
        (req,) = self.stub.requests_to("GET", "/api/v1/notebooks/")[-1:]
        self.assertEqual(req["headers"]["Authorization"], "Bearer x")


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

    def test_kill_endpoint_follows_the_kind(self):
        for path in ("/api/v1/notebooks/nb/kill", "/api/v1/shells/sh/kill", "/api/v1/shells/old/kill"):
            self.stub.routes[("POST", path)] = (200, {})
        tasks = {
            "nb": {"kind": "notebook", "username": "alice"},
            "sh": {"kind": "shell", "username": "bob"},
            "old": {"username": "carol"},  # saved before notebooks were policed: a shell
        }
        killed, failed = self.quiet(self.api.kill_containers, tasks, False, {})
        self.assertEqual((sorted(killed), failed), (["nb", "old", "sh"], {}))
        self.assertEqual(
            sorted(r["path"] for r in self.stub.requests),
            ["/api/v1/notebooks/nb/kill", "/api/v1/shells/old/kill", "/api/v1/shells/sh/kill"],
        )
        self.assertEqual(killed["nb"], {"kind": "notebook", "username": "alice"})

    def test_failed_notebook_kill(self):
        self.stub.routes[("POST", "/api/v1/notebooks/err/kill")] = (500, {"error": "x"})
        out = io.StringIO()
        with redirect_stdout(out):
            killed, failed = self.api.kill_containers({"err": {"kind": "notebook"}}, False, {})
        self.assertEqual((killed, sorted(failed)), ({}, ["err"]))
        self.assertIn("Failed to kill notebook err", out.getvalue())

    def test_debug_mode_only_gets_the_notebook(self):
        self.stub.routes[("GET", "/api/v1/notebooks/nb")] = (200, {"notebook": {}, "config": {}})
        killed, failed = self.quiet(self.api.kill_containers, {"nb": {"kind": "notebook"}}, True, {})
        self.assertEqual((sorted(killed), failed), (["nb"], {}))
        self.assertEqual([(r["method"], r["path"]) for r in self.stub.requests],
                         [("GET", "/api/v1/notebooks/nb")])

    def test_unknown_kind_is_a_failed_kill_without_a_request(self):
        killed, failed = self.quiet(self.api.kill_containers, {"x": {"kind": "tensorboard"}}, False, {})
        self.assertEqual((killed, sorted(failed)), ({}, ["x"]))
        self.assertEqual(self.stub.requests, [])


if __name__ == "__main__":
    unittest.main()
