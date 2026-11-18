from portal import queries

from .base import PortalTestCase


class UsageTest(PortalTestCase):
    def test_anonymous_refused(self):
        self.assertEqual(self.client.get("/api/me/usage").status_code, 401)
        self.assertEqual(self.client.get("/usage").status_code, 302)
        self.assertEqual(self.prometheus.queries, [])

    def test_own_usage_only(self):
        self.sign_in("alice")
        view = self.client.get("/api/me/usage").json()
        self.assertEqual(view["username"], "alice")
        self.assertEqual(view["windows"]["30d"], {"download": 3_100_000_000, "upload": 420_000_000,
                                                  "total": 3_520_000_000, "observed": True})
        self.assertEqual(view["windows"]["24h"]["download"], 186_000_000)
        self.assertEqual(len(view["trend"]["24h"]["points"]), 24)
        self.assertEqual(len(view["trend"]["7d"]["points"]), 28)
        self.assertEqual(len(view["trend"]["30d"]["points"]), 30)
        self.assertGreater(sum(p["download"] for p in view["trend"]["30d"]["points"]), 0)
        body = self.client.get("/api/me/usage").content.decode()
        self.assertNotIn("carol", body)
        self.assertNotIn("12400000000", body)  # carol's 30-day download
        self.assert_only_fixed_queries()

    def test_supplied_username_refused(self):
        self.sign_in("alice")
        for query in ("username=carol", "target=carol", "user=carol", "window=1y", "query=up"):
            response = self.client.get("/api/me/usage?" + query)
            self.assertEqual(response.status_code, 400, query)
            self.assertNotIn(b"12400000000", response.content)
        self.assertEqual(self.client.post("/api/me/usage", {"username": "carol"}).status_code, 405)
        # the page ignores any parameter: still alice's
        page = self.client.get("/usage?username=carol")
        self.assertContains(page, "Observed traffic of the account alice")
        self.assert_only_fixed_queries()

    def test_queries_never_contain_the_username(self):
        self.master.users["evil\"}or{x"] = ("pw", False, True)
        self.sign_in("evil\"}or{x", "pw")
        self.client.get("/api/me/usage")
        self.client.get("/usage")
        self.assert_only_fixed_queries()
        for _, expr, _ in self.prometheus.queries:
            self.assertNotIn("evil", expr)
        sent = {q[1] for q in self.prometheus.queries}
        expected = {e for w in queries.USAGE_TOTALS.values() for e in w.values()}
        expected |= {e for w in queries.USAGE_TREND.values() for e in w.values()}
        self.assertEqual(sent, expected)

    def test_no_traffic(self):
        self.master.users["frank"] = ("pw-frank", False, True)
        self.sign_in("frank")
        view = self.client.get("/api/me/usage").json()
        self.assertEqual(view["windows"]["7d"], {"download": 0, "upload": 0, "total": 0, "observed": False})
        self.assertContains(self.client.get("/usage"), "No traffic observed")

    def test_prometheus_down(self):
        self.prometheus.down = True
        self.sign_in("alice")
        with self.assertLogs("portal.usage", level="WARNING"):
            view = self.client.get("/api/me/usage").json()
        self.assertFalse(view["available"])
        self.assertEqual(view["windows"], {})
        self.assertContains(self.client.get("/usage"), "Usage data is unavailable")

    def test_page(self):
        self.sign_in("carol")
        page = self.client.get("/usage")
        self.assertContains(page, "Observed totals")
        self.assertContains(page, "11.55")  # 12.4e9 B in GiB
        self.assertContains(page, "<svg", count=3)
