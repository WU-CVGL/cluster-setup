"""Prometheus over its HTTP API, server-side only, for the fixed expressions of queries.py and nothing else."""

import math
import urllib.parse

from django.conf import settings

from . import queries
from .upstream import UpstreamError, request_json


class PrometheusError(Exception):
    pass


def _get(path, params):
    if params["query"] not in queries.ALL:
        raise ValueError("not one of the portal's fixed queries")
    url = f"{settings.PROMETHEUS_URL}{path}?{urllib.parse.urlencode(params)}"
    try:
        answer = request_json(url)
    except UpstreamError as e:
        raise PrometheusError(str(e)) from None
    if not isinstance(answer, dict) or answer.get("status") != "success" or not isinstance(answer.get("data"), dict):
        raise PrometheusError("unexpected answer")
    return answer["data"]


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def instant(expr, at):
    """[(labels, value)] of an instant vector at time at; a NaN value is None."""
    data = _get("/api/v1/query", {"query": expr, "time": f"{at:.3f}"})
    if data.get("resultType") != "vector":
        raise PrometheusError("not a vector")
    rows = []
    for item in data.get("result") or []:
        value = item.get("value") or [None, None]
        rows.append((item.get("metric") or {}, _number(value[1])))
    return rows


def series(expr, start, end, step):
    """[(labels, {timestamp: value})] of a range query."""
    data = _get("/api/v1/query_range", {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}",
                                        "step": str(int(step))})
    if data.get("resultType") != "matrix":
        raise PrometheusError("not a matrix")
    rows = []
    for item in data.get("result") or []:
        points = {}
        for ts, value in item.get("values") or []:
            points[int(round(float(ts)))] = _number(value)
        rows.append((item.get("metric") or {}, points))
    return rows
