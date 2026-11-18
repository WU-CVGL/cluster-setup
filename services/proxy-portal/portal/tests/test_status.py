import datetime
import re
import time
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from portal import health, queries
from portal.headline import FAILING, PASSING, UNKNOWN, headline
from portal.models import Incident

from . import stubs
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

    def test_bucket_states(self):
        self.assertEqual(health.bucket_state(None, None), "unknown")
        self.assertEqual(health.bucket_state(1.0, 1.0), "ok")
        self.assertEqual(health.bucket_state(0.99, 1.0), "ok")
        # too little data is grey, whatever the few results say
        self.assertEqual(health.bucket_state(1.0, 0.3), "unknown")
        self.assertEqual(health.bucket_state(1.0, 0.4999), "unknown")
        self.assertEqual(health.bucket_state(0.95, 0.1), "unknown")
        self.assertEqual(health.bucket_state(0.0, 0.001), "unknown")
        self.assertEqual(health.bucket_state(0.5, None), "unknown")
        self.assertEqual(health.bucket_state(0.9899, 1.0), "degraded")
        self.assertEqual(health.bucket_state(0.95, 0.5), "degraded")
        self.assertEqual(health.bucket_state(0.9, 1.0), "degraded")
        self.assertEqual(health.bucket_state(0.8999, 1.0), "bad")
        self.assertEqual(health.bucket_state(0.5, 1.0), "bad")

    def test_bars_end_at_the_last_full_hour(self):
        self.assertEqual(health.bars_end(7 * 3600 + 59 * 60 + 59.9), 7 * 3600)
        self.assertEqual(health.bars_end(7 * 3600), 7 * 3600)


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
        self.assertNotIn("bars", live)  # the bars update on reload
        # both texts on the page, for the script to switch: the note on missing monitoring data hidden
        self.assertIn('<p data-live-monitoring="off" hidden>Monitoring data is unavailable', html)
        self.assertIn('<p data-live-monitoring="on">ha-general sends each new connection', html)
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
            for p in ("http", "socks"):
                self.assertIn(f'data-live-state="entries.{entry}.protocols.{p}" '
                              f'data-live-age-of="entries.{entry}.protocols.{p}.last_check_age_seconds"', html)
                self.assertIn(f'data-live-ms="entries.{entry}.protocols.{p}"', html)
            self.assertIn(f'data-live-age="entries.{entry}.last_check_age_seconds"', html)
        self.assertEqual(texts["entry_count"], "%(passing)s of %(configured)s proxies passing")
        zh = self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN").content.decode()
        texts = json.loads(re.search(r'<script id="status-texts" type="application/json">(.*?)</script>', zh).group(1))
        self.assertNotEqual(texts["passing"], "Passing")
        self.assertEqual(texts["entry_count"], "%(configured)s 个代理中 %(passing)s 个正常")

    def test_bars(self):
        bars = self.client.get("/api/status").json()["bars"]
        self.assertEqual(set(bars), {"hours", "days"})
        names = ["ha-general", "ha-private"] + [proxy["name"] for proxy in
                                                self.client.get("/api/status").json()["proxies"]]
        for window, step, count in (("hours", 3600, 96), ("days", 86400, 30)):
            with self.subTest(window=window):
                found = bars[window]
                self.assertEqual((found["step_seconds"], found["count"]), (step, count))
                self.assertEqual(found["end"] % 3600, 0)
                self.assertEqual(list(found["rows"]), names)  # every row shown, in the page's order
                for row in found["rows"].values():
                    self.assertEqual({len(v) for v in row.values()}, {count})
                general = found["rows"]["ha-general"]
                self.assertEqual(general["state"][14], "unknown")    # no samples
                self.assertIsNone(general["success_rate"][14])
                self.assertIsNone(general["coverage"][14])
                self.assertEqual(general["state"][20], "unknown")    # 18 % coverage, all passing: not green
                self.assertEqual(general["success_rate"][20], 1.0)
                self.assertEqual(general["coverage"][20], 0.18)
                self.assertEqual(general["state"][8:10], ["bad", "bad"])
                self.assertAlmostEqual(general["success_rate"][8], 0.82)  # the worse protocol
                self.assertEqual(general["state"][25], "degraded")
                self.assertEqual(general["state"][0], "ok")
                failing = found["rows"]["us-west-racknerd-12tb"]
                self.assertEqual(failing["state"][-3:], ["bad"] * 3)
                self.assertEqual(failing["success_rate"][-1], 0.38)
                # the lower coverage of the two protocols: SOCKS5 at 20 % in the last bucket
                self.assertEqual(found["rows"]["miyaip-jp-01_relay_jpty1-bwh-2tb"]["state"][-1], "unknown")
        range_queries = [q for q in self.prometheus.queries if q[0].endswith("query_range")]
        self.assertEqual(len(range_queries), 4)  # one per value and bucket size, for every row
        self.assertEqual({(q[1], q[2]["step"]) for q in range_queries},
                         {(queries.BARS[w][k], str(s)) for w, s in (("hours", 3600), ("days", 86400))
                          for k in ("rate", "coverage")})
        for q in range_queries:
            params = q[2]
            span = float(params["end"]) - float(params["start"])
            self.assertEqual(span, 95 * 3600 if params["step"] == "3600" else 29 * 86400)

    def test_bucket_with_one_protocol_missing(self):
        """A protocol without results covers nothing: the bucket is grey, whatever the other protocol says."""
        def rate(params):
            return stubs._range(params, lambda labels, t: 1.0,
                                [{"entry": "ha-general", "protocol": "http"}])

        def coverage(params):
            return stubs._range(params, lambda labels, t: 1.0,
                                [{"entry": "ha-general", "protocol": "http"}])

        for window in queries.BARS:
            self.prometheus.answers[("range", queries.BARS[window]["rate"])] = rate
            self.prometheus.answers[("range", queries.BARS[window]["coverage"])] = coverage
        bars = self.client.get("/api/status").json()["bars"]
        self.assertEqual(set(bars["hours"]["rows"]["ha-general"]["state"]), {"unknown"})
        self.assertEqual(set(bars["hours"]["rows"]["ha-general"]["coverage"]), {0.0})
        # a row Prometheus has no bars for: every bucket grey, without values
        self.assertEqual(set(bars["days"]["rows"]["usca6-bwh-1tb"]["state"]), {"unknown"})
        self.assertEqual(set(bars["days"]["rows"]["usca6-bwh-1tb"]["success_rate"]), {None})

    def test_ports(self):
        data = self.client.get("/api/status").json()
        self.assertEqual(data["entries"]["ha-general"]["ports"], {"http": 30889, "socks": 30089})
        self.assertEqual(data["entries"]["ha-private"]["ports"], {"http": 31889, "socks": 31089})
        proxies = {proxy["name"]: proxy for proxy in data["proxies"]}
        self.assertEqual(proxies["usca1-bwh-megaboxpro-2tb"]["ports"], {"http": 28889, "socks": 20089})
        self.assertEqual(proxies["jpty1-bwh-2tb"]["ports"], {"http": 55889, "socks": 55089})
        page = self.client.get("/").content.decode()
        self.assertIn('<td class="mono ports">30889 / 30089</td>', page)
        self.assertIn('<td class="mono ports">28889 / 20089</td>', page)
        self.assertIn("Ports", page)

    def test_ports_without_the_applier_status(self):
        self.status_file.unlink()
        data = self.client.get("/api/status").json()
        self.assertEqual(data["entries"]["ha-general"]["ports"], {"http": None, "socks": None})
        self.assertEqual({str(proxy["ports"]) for proxy in data["proxies"]}, {str({"http": None, "socks": None})})
        self.assertIn('<td class="mono ports">— / —</td>', self.client.get("/").content.decode())

    def test_page_layout(self):
        page = self.client.get("/").content.decode()
        # one section for the HA entries, in the proxies' table layout; no metric cards, no separate history
        self.assertIn("Auto-switching High-availability Proxies", page)
        self.assertNotIn("summary-grid", page)
        self.assertNotIn('class="history"', page)
        self.assertNotIn("Past 30 days", page)
        for old in (">Entries<", "Default proxy ·", "Private entry"):
            self.assertNotIn(old, page)
        self.assertEqual(page.count('<table class="table status-table">'), 2)
        self.assertEqual(page.count("<thead>"), 2)
        rows = re.findall(r'<tr class="row-main" data-row="([^"]+)">', page)
        api = self.client.get("/api/status").json()
        self.assertEqual(rows, ["ha-general", "ha-private"] + [proxy["name"] for proxy in api["proxies"]])
        self.assertEqual(page.count('<tr class="row-bars">'), len(rows))
        # the switch: rendered with 96 hours chosen, hidden until the script shows it and applies a kept choice
        switch = re.search(r'<div class="seg" data-switch="bars"[^>]*>(.*?)</div>', page, re.S)
        self.assertIn('data-default="hours" data-store="portal.status.bars" hidden', switch.group(0))
        self.assertIn('<button type="button" data-value="hours" aria-pressed="true">96 hours</button>',
                      switch.group(1))
        self.assertIn('<button type="button" data-value="days" aria-pressed="false">30 days</button>',
                      switch.group(1))
        hours = re.findall(r'<ol class="bars" data-panel="bars" data-value="hours" aria-label="[^"]+">(.*?)</ol>', page)
        days = re.findall(r'<ol class="bars days" data-panel="bars" data-value="days" hidden aria-label="[^"]+">(.*?)'
                          r'</ol>', page)
        self.assertEqual((len(hours), len(days)), (len(rows), len(rows)))
        self.assertEqual({h.count("<li ") for h in hours}, {96})
        self.assertEqual({d.count("<li ") for d in days}, {30})
        # one tab stop per list: its newest bar
        for bars in hours + days:
            self.assertEqual(bars.count('tabindex="0"'), 1)
            self.assertTrue(bars.endswith(' tabindex="0"></li>'))

    def test_bar_labels(self):
        with mock.patch("portal.health.time.time", return_value=1763539200 + 1800):  # 2025-11-19 08:30 UTC
            page = self.client.get("/").content.decode()
        general = re.search(r'<tr class="row-main" data-row="ha-general">.*?<tr class="row-bars">(.*?)</tr>', page,
                            re.S).group(1)
        labels = re.findall(r'<li class="bar (\w+)" aria-label="([^"]+)"', general)
        hours, days = labels[:96], labels[96:]
        # in local time (Asia/Shanghai): the last hour ends at 16:00, the last day at 16:00 too
        self.assertEqual(hours[-1], ("ok", "2025-11-19 15:00–16:00 · Passing · success 99.95 %, coverage 99 %"))
        self.assertEqual(hours[14], ("unknown", "2025-11-16 06:00–07:00 · Unknown · no probe results"))
        self.assertEqual(hours[20][0], "unknown")
        self.assertIn("· Unknown · success 100.00 %, coverage 18 %", hours[20][1])
        self.assertEqual(hours[8][0], "bad")
        self.assertIn("· Failing · success 82.00 %", hours[8][1])
        self.assertIn("· Degraded · success 95.50 %", hours[25][1])
        self.assertEqual(days[-1], ("ok", "2025-11-18 16:00 – 2025-11-19 16:00 · Passing · success 99.95 %, "
                                          "coverage 99 %"))
        zh = self.client.get("/", HTTP_ACCEPT_LANGUAGE="zh-CN").content.decode()
        self.assertIn("自动切换高可用代理", zh)
        self.assertIn("96 小时", zh)
        self.assertRegex(zh, r'aria-label="[0-9 :–-]+ · 正常 · 成功率 99.95 %，覆盖率 99 %"')

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
        self.enterContext(self.settings(STATUS_CACHE_SECONDS=1000, BARS_CACHE_SECONDS=1000))
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
        for bars in data["bars"].values():
            self.assertEqual(len(bars["rows"]), 11)
            self.assertEqual({s for row in bars["rows"].values() for s in row["state"]}, {"unknown"})
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
        self.assertEqual(len({q[1] for q in self.prometheus.queries}), 28)

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


class BarsCostTest(PortalTestCase):
    """The cost of the bars with every row the cluster has: 2 HA entries and 12 proxies, 96 + 30 buckets each."""

    EXTRA = ["miyaip-jp-02_relay_jpty1-bwh-2tb", "miyaip-jp-02_relay_usca5-bwh-sla-2tb",
             "miyaip-us-01_relay_usca5-bwh-sla-2tb"]

    def setUp(self):
        super().setUp()

        def add(data):
            for i, name in enumerate(self.EXTRA):
                data["proxies"].append({"name": name, "access": "private",
                                        "ports": {"http": 41889 + i, "socks": 41089 + i}})
        self.write_status(add)
        labels = stubs.BAR_LABELS + [{"entry": n, "protocol": p} for n in self.EXTRA for p in ("http", "socks")]
        self.enterContext(mock.patch.object(stubs, "BAR_LABELS", labels))

    def test_fourteen_rows(self):
        api = self.client.get("/api/status")
        bars = api.json()["bars"]
        self.assertEqual([len(bars[w]["rows"]) for w in ("hours", "days")], [14, 14])
        self.assertEqual(len([q for q in self.prometheus.queries if q[0].endswith("query_range")]), 4)
        page = self.client.get("/")
        self.assertEqual(page.content.decode().count('<li class="bar '), 14 * (96 + 30))
        # bounds against growth: about 45 kB and 260 kB with these rows
        self.assertLess(len(api.content), 64 * 1024)
        self.assertLess(len(page.content), 320 * 1024)
        # the polled answer is cached with the rest: no more queries
        n = len(self.prometheus.queries)
        self.client.get("/api/status")
        self.assertEqual(len(self.prometheus.queries), n)
