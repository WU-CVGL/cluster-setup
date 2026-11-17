"""
The public status: the HA entries, the proxies and the 30-day history, from the fixed queries of queries.py.

Results are cached for a few seconds and aged on every view: a probe result older than 90 s, counting the time it
spent in the cache, is unknown, as is everything Prometheus does not answer. Unknown is never shown as passing.
"""

import concurrent.futures
import logging
import time

from django.conf import settings
from django.core.cache import cache

from . import prom, proxy_status, queries
from .headline import FAILING, PASSING, UNKNOWN, headline

log = logging.getLogger("portal.status")
PROTOCOLS = ("http", "socks")
HA_ENTRIES = ("ha-general", "ha-private")

# History thresholds: a day needs probe results for at least half of it, or it is grey; then it is green from
# 99 % probe success, amber from 90 %, red below.
DAY_OK = 0.99
DAY_DEGRADED = 0.90
DAY_MIN_COVERAGE = 0.5


def _run_instant(expr, at):
    try:
        return prom.instant(expr, at)
    except prom.PrometheusError as e:
        log.warning("Prometheus query failed: %s", e)
        return None


def _by(rows, label):
    if rows is None:
        return None
    return {labels.get(label, ""): value for labels, value in rows}


def _scalar(rows):
    if not rows:
        return None
    return rows[0][1]


def collect(at):
    """Every status query at time at: {"time", "ok", "ha": {...}, "proxies": {...}}, None where Prometheus failed."""
    jobs = {}
    for entry in HA_ENTRIES:
        for key, expr in queries.STATUS[entry].items():
            jobs[(entry, key)] = expr
    for key, expr in queries.STATUS["proxies"].items():
        jobs[("proxies", key)] = expr
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {name: pool.submit(_run_instant, expr, at) for name, expr in jobs.items()}
        results = {name: future.result() for name, future in futures.items()}
    ha = {}
    for entry in HA_ENTRIES:
        r = {key: results[(entry, key)] for key in queries.STATUS[entry]}
        ha[entry] = {
            "http": _scalar(r["http"]), "socks": _scalar(r["socks"]),
            "proxies_passing": _scalar(r["proxies_passing"]), "proxies_configured": _scalar(r["proxies_configured"]),
            "duration": _by(r["duration"], "protocol"), "last_check": _by(r["last_check"], "protocol"),
            "rate_24h": _by(r["rate_24h"], "protocol"), "coverage_24h": _by(r["coverage_24h"], "protocol"),
        }
    r = {key: results[("proxies", key)] for key in queries.STATUS["proxies"]}
    proxies = {}
    for labels, value in r["http"] or []:
        proxies.setdefault(labels.get("entry", ""), {})["labels"] = {"pool": labels.get("pool"),
                                                                      "access": labels.get("access")}
    proxies_data = {"http": _by(r["http"], "entry"), "socks": _by(r["socks"], "entry"),
                    "duration_http": _by(r["duration_http"], "entry"),
                    "duration_socks": _by(r["duration_socks"], "entry"),
                    "last_check": _by(r["last_check"], "entry"),
                    "rate_24h_http": _by(r["rate_24h_http"], "entry"),
                    "rate_24h_socks": _by(r["rate_24h_socks"], "entry"),
                    "coverage_24h": _by(r["coverage_24h"], "entry")}
    names = set(proxies)
    for values in proxies_data.values():
        names.update(values or {})
    names.discard("")
    for name in names:
        proxy = proxies.setdefault(name, {})
        proxy.setdefault("labels", {"pool": None, "access": None})
        for key, values in proxies_data.items():
            proxy[key] = (values or {}).get(name)
    ok = any(value is not None for value in results.values())
    return {"time": at, "ok": ok, "ha": ha, "proxies": proxies}


def cached_collect(now):
    data = cache.get("status")
    if data is None:
        data = collect(now)
        cache.set("status", data, settings.STATUS_CACHE_SECONDS if data["ok"] else 5)
    return data


def state(value, age, query_time, now):
    """passing, failing or unknown: unknown when Prometheus said -1 or nothing, or the result is older than 90 s."""
    if value is None or value < 0 or age is None:
        return UNKNOWN
    if age + (now - query_time) > settings.STALE_SECONDS:
        return UNKNOWN
    return PASSING if value >= 1 else FAILING


def _ratio(value):
    return None if value is None else max(0.0, min(1.0, value))


def _seconds(value):
    return None if value is None else round(value, 3)


def entry_view(data, entry, now):
    ha = data["ha"][entry]
    last = ha["last_check"] or {}
    view = {"name": entry, "access": "public" if entry == "ha-general" else "private", "protocols": {}}
    for protocol in PROTOCOLS:
        age = last.get(protocol)
        current = state(ha[protocol], age, data["time"], now)
        view["protocols"][protocol] = {
            "state": current,
            # an old duration is never shown beside Unknown
            "duration_seconds": None if current == UNKNOWN else _seconds((ha["duration"] or {}).get(protocol)),
            "last_check_age_seconds": None if age is None else round(age + now - data["time"]),
            "success_rate_24h": _ratio((ha["rate_24h"] or {}).get(protocol)),
            "coverage_24h": _ratio((ha["coverage_24h"] or {}).get(protocol)),
        }
    passing, configured = ha["proxies_passing"], ha["proxies_configured"]
    view["proxies"] = {"passing": None if passing is None or configured is None else int(passing),
                       "configured": None if configured is None else int(configured)}
    return view


def proxies_view(data, status, now):
    """The proxies, public ones first: from the probes, and from the applier's status for proxies without results."""
    access = {proxy.name: proxy.public for proxy in status.proxies} if status.available else {}
    names = set(data["proxies"]) | set(access)
    rows = []
    for name in names:
        proxy = data["proxies"].get(name) or {"labels": {}}
        labels = proxy.get("labels") or {}
        public = access.get(name, labels.get("access") == "public")
        age = proxy.get("last_check")
        row = {"name": name, "access": "public" if public else "private", "pool": labels.get("pool"),
               "last_check_age_seconds": None if age is None else round(age + now - data["time"]),
               "coverage_24h": _ratio(proxy.get("coverage_24h")), "protocols": {}}
        for protocol in PROTOCOLS:
            current = state(proxy.get(protocol), age, data["time"], now)
            row["protocols"][protocol] = {
                "state": current,
                "duration_seconds": None if current == UNKNOWN else _seconds(proxy.get("duration_" + protocol)),
                "success_rate_24h": _ratio(proxy.get("rate_24h_" + protocol)),
            }
        rows.append(row)
    rows.sort(key=lambda row: (row["access"] != "public", row["name"]))
    return rows


def day_state(rate, coverage):
    if rate is None or coverage is None or coverage < DAY_MIN_COVERAGE:
        return "unknown"
    if rate < DAY_DEGRADED:
        return "bad"
    if rate < DAY_OK:
        return "degraded"
    return "ok"


def collect_history(end):
    """30 values a day apart, the last at end: ha-general's 24-hour probe success rate and coverage."""
    start = end - (queries.HISTORY_DAYS - 1) * queries.DAY
    try:
        rates = prom.series(queries.HISTORY["rate"], start, end, queries.DAY)
        coverages = prom.series(queries.HISTORY["coverage"], start, end, queries.DAY)
    except prom.PrometheusError as e:
        log.warning("Prometheus history query failed: %s", e)
        return None
    by_protocol = {"rate": {}, "coverage": {}}
    for key, rows in (("rate", rates), ("coverage", coverages)):
        for labels, points in rows:
            by_protocol[key][labels.get("protocol", "")] = points
    days = []
    for i in range(queries.HISTORY_DAYS):
        ts = int(start + i * queries.DAY)
        per = {}
        for protocol in PROTOCOLS:
            per[protocol] = {"success_rate": _ratio(by_protocol["rate"].get(protocol, {}).get(ts)),
                             "coverage": _ratio(by_protocol["coverage"].get(protocol, {}).get(ts))}
        known = [p["success_rate"] for p in per.values() if p["success_rate"] is not None]
        rate = min(known) if known else None
        # a protocol without results covers nothing of that day
        coverage = min((p["coverage"] or 0.0) for p in per.values())
        days.append({"end": ts, "success_rate": rate, "coverage": coverage if known else None,
                     "state": day_state(rate, coverage if known else None), "protocols": per})
    return days


def cached_history(now):
    end = int(now // 60 * 60)
    days = cache.get("history")
    if days is None:
        days = collect_history(end)
        cache.set("history", days, settings.HISTORY_CACHE_SECONDS if days is not None else 5)
    if days is None:
        start = end - (queries.HISTORY_DAYS - 1) * queries.DAY
        days = [{"end": int(start + i * queries.DAY), "success_rate": None, "coverage": None, "state": "unknown",
                 "protocols": {p: {"success_rate": None, "coverage": None} for p in PROTOCOLS}}
                for i in range(queries.HISTORY_DAYS)]
    return days


def build(now=None):
    """The whole public status, as /api/status returns it (without the incidents)."""
    now = time.time() if now is None else now
    data = cached_collect(now)
    status = proxy_status.load()
    entries = {entry: entry_view(data, entry, now) for entry in HA_ENTRIES}
    general = entries["ha-general"]
    level, text = headline(general["protocols"]["http"]["state"], general["protocols"]["socks"]["state"],
                           general["proxies"]["passing"], general["proxies"]["configured"])
    ages = [p["last_check_age_seconds"] for p in general["protocols"].values()
            if p["last_check_age_seconds"] is not None]
    return {
        "generated_at": now,
        "monitoring_available": data["ok"],
        "headline": {"level": level, "text": text},
        "last_check_age_seconds": max(ages) if ages else None,
        "stale_after_seconds": settings.STALE_SECONDS,
        "entries": entries,
        "proxies": proxies_view(data, status, now),
        "history": {"entry": "ha-general", "days": cached_history(now)},
    }
