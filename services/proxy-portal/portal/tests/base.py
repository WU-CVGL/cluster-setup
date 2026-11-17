import datetime
import json
import shutil
import tempfile
from pathlib import Path

from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings

from . import stubs

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "status.json"
HTTPS = {"HTTP_X_FORWARDED_PROTO": "https", "HTTP_HOST": "testserver", "HTTP_ORIGIN": "https://testserver"}


def https_client(**kwargs):
    return Client(**HTTPS, **kwargs)


class PortalMixin:
    """The portal against a stub master, a stub Prometheus (demo answers) and a copy of the status fixture."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.master = stubs.MasterStub()
        cls.prometheus = stubs.PrometheusStub()
        cls._master_server, master_url = stubs.serve(cls.master)
        cls._prom_server, prom_url = stubs.serve(cls.prometheus)
        cls.tmp = Path(tempfile.mkdtemp())
        cls.status_file = cls.tmp / "status.json"
        cls._settings = override_settings(MASTER_URL=master_url, PROMETHEUS_URL=prom_url,
                                          STATUS_FILE=cls.status_file)
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        cls._master_server.shutdown()
        cls._prom_server.shutdown()
        shutil.rmtree(cls.tmp)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.master.__init__()
        self.prometheus.__init__(stubs.demo_answers())
        self.write_status()
        self.client = https_client()

    def write_status(self, change=None, generated_at=None):
        data = json.loads(FIXTURE.read_text())
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        data["generated_at"] = (generated_at or now).isoformat().replace("+00:00", "Z")
        if change:
            change(data)
        self.status_file.write_text(json.dumps(data))
        return data

    def sign_in(self, username="alice", password=None, client=None):
        client = client or self.client
        return client.post("/signin", {"username": username, "password": password or f"pw-{username}"})

    def assert_only_fixed_queries(self):
        from portal import queries
        self.assertEqual(self.prometheus.refused, [])
        for _, expr, _ in self.prometheus.queries:
            self.assertIn(expr, queries.ALL)


class PortalTestCase(PortalMixin, TestCase):
    pass


class PortalTransactionTestCase(PortalMixin, TransactionTestCase):
    pass
