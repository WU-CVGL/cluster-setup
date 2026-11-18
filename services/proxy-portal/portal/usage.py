"""
My usage: the signed-in user's observed proxy traffic, from the Proxy Usage dashboard's per-user totals.

Users are counted where they enter the proxies (the HA entries, and a proxy's own entry when it requires accounts),
summed over every entry, and service accounts are left out, as in Grafana. The queries return every user; the
portal keeps only the row of the session's username. The totals are Prometheus increase() over the window.
"""

import logging
import time

from django.conf import settings
from django.core.cache import cache

from . import prom, queries

log = logging.getLogger("portal.usage")
DIRECTIONS = ("download", "upload")


def _collect_totals(at):
    totals = {}
    for window, exprs in queries.USAGE_TOTALS.items():
        totals[window] = {}
        for direction, expr in exprs.items():
            totals[window][direction] = {labels.get("target"): value for labels, value in prom.instant(expr, at)}
    return totals


def _collect_trend(at):
    trend = {}
    for window, exprs in queries.USAGE_TREND.items():
        step = queries.USAGE_BUCKETS[window][1]
        end = at // 60 * 60  # the last bucket ends now
        start = end - queries.USAGE_WINDOWS[window] + step
        trend[window] = {"start": start, "end": end, "step": step, "series": {}}
        for direction, expr in exprs.items():
            trend[window]["series"][direction] = {labels.get("target"): points
                                                  for labels, points in prom.series(expr, start, end, step)}
    return trend


def collect(at):
    """Every user's totals and trend at time at (one cache for all users), or None when Prometheus failed."""
    data = cache.get("usage")
    if data is None:
        try:
            data = {"time": at, "totals": _collect_totals(at), "trend": _collect_trend(at)}
        except prom.PrometheusError as e:
            log.warning("Prometheus usage query failed: %s", e)
            cache.set("usage", False, 5)
            return None
        cache.set("usage", data, settings.USAGE_CACHE_SECONDS)
    return data or None


def _bytes(value):
    return None if value is None else max(0, int(round(value)))


def build(username, now=None):
    now = int(time.time() if now is None else now)
    data = collect(now)
    view = {"username": username, "available": data is not None, "counted_at": "entry",
            "windows": {}, "trend": {}}
    if data is None:
        return view
    for window in queries.USAGE_WINDOWS:
        row = {d: _bytes(data["totals"][window][d].get(username)) for d in DIRECTIONS}
        observed = any(v is not None for v in row.values())
        row = {d: (row[d] or 0) if observed else 0 for d in DIRECTIONS}
        row["total"] = row["download"] + row["upload"]
        row["observed"] = observed
        view["windows"][window] = row
        trend = data["trend"][window]
        points = []
        for ts in range(int(trend["start"]), int(trend["end"]) + 1, int(trend["step"])):
            point = {"end": ts}
            for d in DIRECTIONS:
                point[d] = _bytes((trend["series"][d].get(username) or {}).get(ts)) or 0
            points.append(point)
        view["trend"][window] = {"step_seconds": trend["step"], "points": points}
    view["as_of"] = data["time"]
    return view
