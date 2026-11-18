import json
from pathlib import Path

from django.test import SimpleTestCase

from portal import prom, queries

DASHBOARDS = Path(__file__).resolve().parents[3] / "grafana" / "provisioning" / "dashboards" / "json"


def dashboard_exprs(name):
    exprs = set()

    def walk(panels):
        for panel in panels:
            walk(panel.get("panels", []))
            for target in panel.get("targets", []):
                exprs.add(target["expr"])

    walk(json.loads((DASHBOARDS / name).read_text())["panels"])
    return exprs


class QueriesTest(SimpleTestCase):
    def test_status_queries_are_the_availability_dashboards(self):
        availability = dashboard_exprs("proxy-availability.json")
        for group in ("ha-general", "ha-private", "proxies"):
            for key, expr in queries.STATUS[group].items():
                self.assertIn(expr, availability, f"{group} {key}")
        for window, exprs in queries.BARS.items():
            for key, expr in exprs.items():
                self.assertIn(expr, availability, f"bars {window} {key}")

    def test_usage_queries_are_the_usage_dashboards(self):
        usage = dashboard_exprs("proxy-usage.json")
        for table, windows in ((queries.USAGE_TOTALS, queries.USAGE_WINDOWS),
                               (queries.USAGE_TREND, {w: b for w, (b, _) in queries.USAGE_BUCKETS.items()})):
            for window, exprs in table.items():
                width = window if table is queries.USAGE_TOTALS else windows[window]
                for expr in exprs.values():
                    self.assertIn(expr.replace(f"[{width}]", "[$__range]"), usage)

    def test_only_fixed_queries_can_be_sent(self):
        with self.assertRaises(ValueError):
            prom.instant("up", 0)
        with self.assertRaises(ValueError):
            prom.series('probe_success{job="proxy-probe"}', 0, 1, 1)

    def test_catalog_size(self):
        self.assertEqual(len(queries.ALL), 40)
