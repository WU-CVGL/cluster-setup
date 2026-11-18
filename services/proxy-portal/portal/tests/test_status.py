import datetime
import time
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from portal import health, queries
from portal.headline import FAILING, PASSING, UNKNOWN, headline
from portal.models import Incident

from .base import PortalTestCase
from .stubs import vector

GENERAL = queries.STATUS["ha-general"]


class HeadlineTest(SimpleTestCase):
    def test_table(self):
        cases = [
            # http, socks, passing, configured -> level, text
            (PASSING, PASSING, 4, 4, "ok", "Default proxy available; all 4 pool proxies passing"),
            (PASSING, PASSING, 3, 4, "degraded", "Default proxy available; 1 pool proxy not passing, failover working"),
            (PASSING, PASSING, 2, 4, "degraded",
             "Default proxy available; 2 pool proxies not passing, failover working"),
            (PASSING, PASSING, 1, 4, "degraded",
             "Default proxy available on its last passing pool proxy, without a spare"),
            (PASSING, PASSING, 0, 4, "degraded", "Default proxy answering, but no pool proxy passes its probe"),
            (PASSING, PASSING, None, None, "ok", "Default proxy available"),
            (FAILING, PASSING, 4, 4, "degraded", "HTTP entry failing, SOCKS5 still available"),
            (PASSING, FAILING, 4, 4, "degraded", "SOCKS5 entry failing, HTTP still available"),
            (FAILING, FAILING, 3, 4, "down",
             "Default proxy failing while its pool proxies pass: the HA entry itself is down"),
            (FAILING, FAILING, 0, 4, "down", "Default proxy failing: no pool proxy passes its probe"),
            (FAILING, FAILING, None, None, "down", "Default proxy failing"),
            (UNKNOWN, UNKNOWN, 4, 4, "unknown", "Status unknown: no recent probe results for the default proxy"),
            (PASSING, UNKNOWN, 4, 4, "degraded", "HTTP entry available; SOCKS5 status unknown"),
            (UNKNOWN, PASSING, 4, 4, "degraded", "SOCKS5 entry available; HTTP status unknown"),
            (FAILING, UNKNOWN, 4, 4, "down", "HTTP entry failing; SOCKS5 status unknown"),
            (UNKNOWN, FAILING, 4, 4, "down", "SOCKS5 entry failing; HTTP status unknown"),
        ]
        for http, socks, passing, configured, level, text in cases:
            with self.subTest(http=http, socks=socks, passing=passing):
                self.assertEqual(headline(http, socks, passing, configured), (level, text))

    def test_unknown_is_never_ok(self):
        for http in (PASSING, FAILING, UNKNOWN):
            for socks in (PASSING, FAILING, UNKNOWN):
                if UNKNOWN in (http, socks):
                    self.assertNotEqual(headline(http, socks, 4, 4)[0], "ok")


class StateTest(SimpleTestCase):
    def test_states(self):
        now = 1000.0
        self.assertEqual(health.state(1, 10, now, now), PASSING)
        self.assertEqual(health.state(0, 10, now, now), FAILING)
        self.assertEqual(health.state(-1, 10, now, now), UNKNOWN)
        self.assertEqual(health.state(None, 10, now, now), UNKNOWN)
        self.assertEqual(health.state(1, None, now, now), UNKNOWN)
        self.assertEqual(health.state(1, 91, now, now), UNKNOWN)
        # 20 s old when queried, then 75 s in a cache: 95 s old, unknown
        self.assertEqual(health.state(1, 20, now, now + 75), UNKNOWN)

    def test_day_states(self):
        self.assertEqual(health.day_state(None, None), "unknown")
        self.assertEqual(health.day_state(1.0, 1.0), "ok")
        # too little data is grey, whatever the few results say
        self.assertEqual(health.day_state(1.0, 0.3), "unknown")
        self.assertEqual(health.day_state(0.95, 0.1), "unknown")
        self.assertEqual(health.day_state(0.0, 0.001), "unknown")
        self.assertEqual(health.day_state(0.5, None), "unknown")
        self.assertEqual(health.day_state(0.95, 1.0), "degraded")
        self.assertEqual(health.day_state(0.95, 0.5), "degraded")
        self.assertEqual(health.day_state(0.5, 1.0), "bad")


class StatusTest(PortalTestCase):
    def test_page_is_public(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Default proxy available; 1 pool proxy not passing, failover working")
        self.assertContains(response, "Private proxies")
        self.assertContains(response, "us-west-racknerd-12tb")
        self.assert_only_fixed_queries()

    def test_api(self):
        data = self.client.get("/api/status").json()
        self.assertEqual(data["headline"]["level"], "degraded")
        general = data["entries"]["ha-general"]
        self.assertEqual(general["proxies"], {"passing": 3, "configured": 4})
        self.assertEqual(general["protocols"]["http"]["state"], "passing")
        self.assertEqual(data["entries"]["ha-private"]["access"], "private")
        self.assertEqual(data["entries"]["ha-private"]["proxies"], {"passing": 7, "configured": 9})
        proxies = {proxy["name"]: proxy for proxy in data["proxies"]}
        self.assertEqual(len(proxies), 9)
        self.assertEqual(proxies["us-west-racknerd-12tb"]["protocols"]["http"]["state"], "failing")
        self.assertEqual(proxies["miyaip-jp-01_relay_jpty1-bwh-2tb"]["protocols"]["socks"]["state"], "unknown")
        self.assertEqual(proxies["jpty1-bwh-2tb"]["access"], "private")
        self.assertEqual([proxy["access"] for proxy in data["proxies"]][:4], ["public"] * 4)
        self.assertNotIn("accounts", str(data.keys()))
        self.assert_only_fixed_queries()

    def test_page_carries_the_data_its_script_refreshes(self):
        import json
        import re
        response = self.client.get("/")
        html = response.content.decode()
        live = json.loads(re.search(r'<script id="status-live" type="application/json">(.*?)</script>', html).group(1))
        api = self.client.get("/api/status").json()
        for key in ("monitoring_available", "headline", "entries", "proxies", "stale_after_seconds"):
            self.assertEqual(live[key], api[key], key)
        # both texts on the page, for the script to switch: the note on missing monitoring data hidden
        self.assertIn('<p data-live-monitoring="off" hidden>Monitoring data is unavailable', html)
        self.assertIn('<p data-live-monitoring="on">The default proxy ha-general', html)
        texts = json.loads(re.search(r'<script id="status-texts" type="application/json">(.*?)</script>', html).group(1))
        self.assertEqual((texts["passing"], texts["s_ago"]), ("Passing", "%(n)d s ago"))
        nonce = response["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertEqual(html.count(f'<script nonce="{nonce}">'), 2)  # the base script and the status poller
        self.assertEqual(html.count("<script"), 4)  # and the two JSON blocks, which never run
        self.assertIn("fetch('/api/status'", html)
        for proxy in api["proxies"]:
            self.assertIn(f'data-live-state="proxy:{proxy["name"]}.protocols.http"', html)
        for entry in api["entries"]:
            self.assertIn(f'data-live-entry-count="{entry}"', html)
        self.assertEqual(texts["entry_count"], "%(passing)s of %(configured)s proxies passing")
        zh = self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN").content.decode()
        texts = json.loads(re.search(r'<script id="status-texts" type="application/json">(.*?)</script>', zh).group(1))
        self.assertNotEqual(texts["passing"], "Passing")
        self.assertEqual(texts["entry_count"], "%(configured)s 个代理中 %(passing)s 个正常")

    def test_history(self):
        days = self.client.get("/api/status").json()["history"]["days"]
        self.assertEqual(len(days), 30)
        self.assertEqual(days[14]["state"], "unknown")       # no samples
        self.assertIsNone(days[14]["success_rate"])
        self.assertEqual(days[20]["state"], "unknown")       # 18 % coverage, all passing: not green
        self.assertEqual(days[20]["success_rate"], 1.0)
        self.assertEqual([days[8]["state"], days[9]["state"]], ["bad", "bad"])
        self.assertAlmostEqual(days[8]["success_rate"], 0.82)  # the worse protocol
        self.assertEqual(days[25]["state"], "degraded")
        self.assertEqual(days[0]["state"], "ok")
        self.assertEqual(days[-1]["end"] - days[0]["end"], 29 * 86400)
        range_queries = [q for q in self.prometheus.queries if q[0].endswith("query_range")]
        self.assertEqual({q[1] for q in range_queries}, {queries.HISTORY["rate"], queries.HISTORY["coverage"]})
        self.assertEqual({q[2]["step"] for q in range_queries}, {"86400"})

    def test_stale_result_is_unknown(self):
        self.prometheus.answers[GENERAL["last_check"]] = vector(({"protocol": "http"}, 95), ({"protocol": "socks"}, 5))
        data = self.client.get("/api/status").json()
        self.assertEqual(data["entries"]["ha-general"]["protocols"]["http"]["state"], "unknown")
        self.assertEqual(data["entries"]["ha-general"]["protocols"]["socks"]["state"], "passing")
        self.assertEqual(data["headline"]["text"], "SOCKS5 entry available; HTTP status unknown")

    def test_stale_by_prometheus_is_unknown(self):
        # the dashboard expression answers -1 for a target without a result in the last 90 s
        self.prometheus.answers[GENERAL["http"]] = vector(({"protocol": "http"}, -1))
        self.prometheus.answers[GENERAL["socks"]] = vector(({"protocol": "socks"}, -1))
        data = self.client.get("/api/status").json()
        self.assertEqual(data["headline"]["level"], "unknown")

    def test_cache_age_counts(self):
        self.enterContext(self.settings(STATUS_CACHE_SECONDS=1000, HISTORY_CACHE_SECONDS=1000))
        start = time.time()
        with mock.patch("portal.health.time.time", return_value=start):
            self.assertEqual(self.client.get("/api/status").json()["headline"]["level"], "degraded")
        with mock.patch("portal.health.time.time", return_value=start + 85):  # still cached, 96 s old
            data = self.client.get("/api/status").json()
        self.assertEqual(data["headline"]["level"], "unknown")
        self.assertEqual(data["entries"]["ha-general"]["protocols"]["socks"]["state"], "unknown")

    def test_prometheus_down_is_unknown_everywhere(self):
        self.prometheus.down = True
        with self.assertLogs("portal", level="WARNING"):
            data = self.client.get("/api/status").json()
        self.assertFalse(data["monitoring_available"])
        self.assertEqual(data["headline"]["level"], "unknown")
        states = [p["state"] for e in data["entries"].values() for p in e["protocols"].values()]
        states += [p["state"] for proxy in data["proxies"] for p in proxy["protocols"].values()]
        self.assertEqual(set(states), {"unknown"})
        self.assertEqual({day["state"] for day in data["history"]["days"]}, {"unknown"})
        # the proxies still come from the applier's status, as unknown
        self.assertEqual(len(data["proxies"]), 9)
        page = self.client.get("/")
        self.assertContains(page, '<p data-live-monitoring="off">Monitoring data is unavailable')
        self.assertContains(page, '<p data-live-monitoring="on" hidden>')
        self.assertIn('"monitoring_available": false', page.content.decode())
        self.assertNotContains(page, 'class="chip passing"')
        # no check age is not "never": the probes may have run, the portal cannot tell
        self.assertNotContains(page, ">never<")
        self.assertNotContains(page, ": never")
        self.assertContains(page, ">Unknown<")
        self.assertIsNone(data["last_check_age_seconds"])
        self.assertNotContains(self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN"), "从未")

    def test_unknown_proxy_shows_no_duration(self):
        proxies = {proxy["name"]: proxy for proxy in self.client.get("/api/status").json()["proxies"]}
        stale = proxies["miyaip-jp-01_relay_jpty1-bwh-2tb"]["protocols"]
        self.assertEqual({p["state"] for p in stale.values()}, {"unknown"})
        self.assertEqual({p["duration_seconds"] for p in stale.values()}, {None})
        self.assertIsNotNone(proxies["us-west-racknerd-12tb"]["protocols"]["http"]["duration_seconds"])

    def test_parameters_never_reach_prometheus(self):
        self.client.get("/api/status?query=up&target=evil&entry=x")
        self.client.get("/?query=up")
        self.assert_only_fixed_queries()
        self.assertEqual(len({q[1] for q in self.prometheus.queries}), 24)  # history reuses two

    def test_status_is_cached(self):
        self.client.get("/api/status")
        n = len(self.prometheus.queries)
        self.client.get("/api/status")
        self.client.get("/")
        self.assertEqual(len(self.prometheus.queries), n)

    def test_incidents(self):
        now = timezone.now()
        Incident.objects.create(title="Proxy us-west down", body="Use the default proxy.", component="proxies",
                                state="investigating", started_at=now - datetime.timedelta(hours=1))
        Incident.objects.create(title="Old outage", component="ha-general", state="resolved",
                                started_at=now - datetime.timedelta(days=40),
                                resolved_at=now - datetime.timedelta(days=39))
        Incident.objects.create(title="Recent maintenance", component="ha-general", state="resolved",
                                started_at=now - datetime.timedelta(days=2), resolved_at=now - datetime.timedelta(days=2))
        data = self.client.get("/api/status").json()
        self.assertEqual([i["title"] for i in data["incidents"]], ["Proxy us-west down", "Recent maintenance"])
        self.assertTrue(data["incidents"][0]["open"])
        page = self.client.get("/")
        self.assertContains(page, "Proxy us-west down")
        self.assertContains(page, "Investigating")
        self.assertNotContains(page, "Old outage")

    def test_security_headers(self):
        response = self.client.get("/")
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'nonce-", csp)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertContains(response, f'<script nonce="{nonce}">')

    def test_plain_http_redirected_and_other_hosts_refused(self):
        from django.test import Client
        response = Client(HTTP_HOST="testserver").get("/")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))
        with self.settings(ALLOWED_HOSTS=["proxy.cvgl.lab"]):
            self.assertEqual(Client(HTTP_HOST="10.0.1.68", HTTP_X_FORWARDED_PROTO="https").get("/")
                             .status_code, 400)
