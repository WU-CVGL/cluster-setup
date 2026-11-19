"""
The public status: the HA entries and the proxies, each with its status bars, from the fixed queries of queries.py.

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

# Status bar thresholds: a bucket needs probe results for at least half of it, or it is grey; then it is green from
# 99 % probe success, amber from 90 %, red below, on the worse of HTTP and SOCKS5.
BAR_OK = 0.99
BAR_DEGRADED = 0.90
BAR_MIN_COVERAGE = 0.5


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


def _one_per_entry(rows):
    """
    The status rows by entry, one each: should an entry have two (a reclassified proxy's old and new probe labels), a
    result (0 or 1) wins over unknown (-1 or none), then the lower value, then the labels in order, whatever the row
    order.
    """
    if rows is None:
        return None

    def rank(row):
        labels, value = row
        unknown = value is None or value < 0
        return unknown, 0 if unknown else value, sorted(labels.items())

    found = {}
    for labels, value in sorted(rows, key=rank):
        found.setdefault(labels.get("entry", ""), (labels, value))
    return found


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
    http, socks = _one_per_entry(r["http"]), _one_per_entry(r["socks"])
    proxies = {}
    for name, (labels, _) in (http or {}).items():
        proxies.setdefault(name, {})["labels"] = {"pool": labels.get("pool"), "access": labels.get("access")}
    proxies_data = {"http": None if http is None else {name: value for name, (_, value) in http.items()},
                    "socks": None if socks is None else {name: value for name, (_, value) in socks.items()},
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


def _no_ports():
    return {protocol: None for protocol in PROTOCOLS}


def entry_view(data, entry, status, now):
    ha = data["ha"][entry]
    last = ha["last_check"] or {}
    view = {"name": entry, "access": "public" if entry == "ha-general" else "private",
            "ports": status.entry_ports.get(entry) or _no_ports() if status.available else _no_ports(),
            "protocols": {}}
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
    ages = [p["last_check_age_seconds"] for p in view["protocols"].values() if p["last_check_age_seconds"] is not None]
    view["last_check_age_seconds"] = max(ages) if ages else None
    passing, configured = ha["proxies_passing"], ha["proxies_configured"]
    view["proxies"] = {"passing": None if passing is None or configured is None else int(passing),
                       "configured": None if configured is None else int(configured)}
    return view


def proxies_view(data, status, now):
    """The proxies, public ones first: from the probes, and from the applier's status for proxies without results."""
    known = {proxy.name: proxy for proxy in status.proxies} if status.available else {}
    access = {name: proxy.public for name, proxy in known.items()}
    names = set(data["proxies"]) | set(access)
    rows = []
    for name in names:
        proxy = data["proxies"].get(name) or {"labels": {}}
        labels = proxy.get("labels") or {}
        public = access.get(name, labels.get("access") == "public")
        age = proxy.get("last_check")
        row = {"name": name, "access": "public" if public else "private", "pool": labels.get("pool"),
               "ports": dict(known[name].ports) if name in known else _no_ports(),
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


def bucket_state(rate, coverage):
    if rate is None or coverage is None or coverage < BAR_MIN_COVERAGE:
        return "unknown"
    if rate < BAR_DEGRADED:
        return "bad"
    if rate < BAR_OK:
        return "degraded"
    return "ok"


def bars_end(now):
    """The end of the last bucket: the last full hour, for both bucket sizes."""
    return int(now // 3600 * 3600)


def _round(value):
    return None if value is None else round(value, 5)


def collect_bars(window, end):
    """
    Every entry's buckets of one size, the last ending at end: {"rows": {entry: {"state", "success_rate",
    "coverage"}}}, a list of one value per bucket each, oldest first; None when Prometheus failed. A bucket's rate is
    the worse of HTTP and SOCKS5 and its coverage the lower one; a protocol without results covers nothing.
    """
    _, step, count = queries.BAR_WINDOWS[window]
    start = end - (count - 1) * step
    try:
        rates = prom.series(queries.BARS[window]["rate"], start, end, step)
        coverages = prom.series(queries.BARS[window]["coverage"], start, end, step)
    except prom.PrometheusError as e:
        log.warning("Prometheus status bars query failed: %s", e)
        return None
    found = {"rate": {}, "coverage": {}}
    for key, rows in (("rate", rates), ("coverage", coverages)):
        for labels, points in rows:
            found[key][(labels.get("entry", ""), labels.get("protocol", ""))] = points
    names = {name for values in found.values() for name, _ in values}
    names.discard("")
    result = {}
    for name in names:
        row = {"state": [], "success_rate": [], "coverage": []}
        for i in range(count):
            ts = int(start + i * step)
            per = [(_ratio(found["rate"].get((name, p), {}).get(ts)),
                    _ratio(found["coverage"].get((name, p), {}).get(ts))) for p in PROTOCOLS]
            known = [rate for rate, _ in per if rate is not None]
            rate = min(known) if known else None
            coverage = min(c or 0.0 for _, c in per) if known else None
            row["state"].append(bucket_state(rate, coverage))
            row["success_rate"].append(_round(rate))
            row["coverage"].append(_round(coverage))
        result[name] = row
    return {"rows": result}


def cached_bars(window, now):
    end = bars_end(now)
    key = f"bars:{window}:{end}"
    found = cache.get(key)
    if found is None:
        rows = collect_bars(window, end)
        found = rows if rows is not None else {"rows": None}
        cache.set(key, found, settings.BARS_CACHE_SECONDS if rows is not None else 5)
    _, step, count = queries.BAR_WINDOWS[window]
    return {"end": end, "step_seconds": step, "count": count, "rows": found["rows"] or {}}


def bars_view(window, now, names):
    """The bars of one size for the rows shown, every row with its buckets: grey where there is no data."""
    found = cached_bars(window, now)
    empty = {"state": ["unknown"] * found["count"], "success_rate": [None] * found["count"],
             "coverage": [None] * found["count"]}
    return dict(found, rows={name: found["rows"].get(name, empty) for name in names})


def build(now=None, bars=True):
    """The whole public status, as /api/status returns it (without the incidents); without the bars if not bars."""
    now = time.time() if now is None else now
    data = cached_collect(now)
    status = proxy_status.load()
    entries = {entry: entry_view(data, entry, status, now) for entry in HA_ENTRIES}
    proxies = proxies_view(data, status, now)
    names = list(entries) + [proxy["name"] for proxy in proxies]
    general = entries["ha-general"]
    level, text = headline(general["protocols"]["http"]["state"], general["protocols"]["socks"]["state"],
                           general["proxies"]["passing"], general["proxies"]["configured"])
    ages = [p["last_check_age_seconds"] for p in general["protocols"].values()
            if p["last_check_age_seconds"] is not None]
    result = {
        "generated_at": now,
        "monitoring_available": data["ok"],
        "headline": {"level": level, "text": text},
        "last_check_age_seconds": max(ages) if ages else None,
        "stale_after_seconds": settings.STALE_SECONDS,
        "entries": entries,
        "proxies": proxies,
    }
    if bars:
        result["bars"] = {window: bars_view(window, now, names) for window in queries.BAR_WINDOWS}
    return result
