"""
The fixed Prometheus queries of the portal. They are the expressions of the Grafana dashboards Proxy Availability
and Proxy Usage, verbatim, with Grafana's $__range replaced by a fixed window (tests/test_queries.py checks this),
so the portal and Grafana agree. Nothing a client sends ever becomes part of a query: the per-user queries return
every user, and the portal picks the session's username from the result.
"""

PROBE_STALE = "90"  # seconds, as in the dashboards


def _status(selector, by):
    """1 passing, 0 failing, -1 unknown (no result in the last 90 s but a target that was up within a day)."""
    return (f"(max by ({by}) (probe_success{{{selector}}} unless on (instance) "
            f"(time() - timestamp(probe_success{{{selector}}}) > {PROBE_STALE}))) or "
            f"(max by ({by}) (max_over_time(up{{{selector}}}[1d])) * 0 - 1)")


def _ha(pool):
    sel = f'job="proxy-probe",kind="ha",pool="{pool}"'
    proxies = 'pool="ha-general"' if pool == "ha-general" else 'pool=~"ha-general|ha-private"'
    raw = f'job="proxy-probe",kind="raw",{proxies},protocol="socks"'
    return {
        "http": _status(sel + ',protocol="http"', "protocol"),
        "socks": _status(sel + ',protocol="socks"', "protocol"),
        "proxies_passing": f"count(({_status(raw, 'entry')}) == 1) or vector(0)",
        "proxies_configured": f"count(max by (entry) (max_over_time(up{{{raw}}}[1d])))",
        "duration": (f"max by (protocol) (probe_duration_seconds{{{sel}}} unless on (instance) "
                     f"(time() - timestamp(probe_duration_seconds{{{sel}}}) > {PROBE_STALE}))"),
        "last_check": f"max by (protocol) (time() - timestamp(probe_success{{{sel}}}))",
        "rate_24h": f"max by (protocol) (avg_over_time(probe_success{{{sel}}}[24h]))",
        "coverage_24h": f"min by (protocol) (clamp_max(count_over_time(probe_success{{{sel}}}[24h]) / 2880, 1))",
    }


_RAW = 'job="proxy-probe",kind="raw"'

STATUS = {
    "ha-general": _ha("ha-general"),
    "ha-private": _ha("ha-private"),
    "proxies": {
        "http": _status(_RAW + ',protocol="http"', "entry, pool, access"),
        "socks": _status(_RAW + ',protocol="socks"', "entry"),
        "duration_http": f'max by (entry) (probe_duration_seconds{{{_RAW},protocol="http"}})',
        "duration_socks": f'max by (entry) (probe_duration_seconds{{{_RAW},protocol="socks"}})',
        "last_check": f"max by (entry) (time() - timestamp(probe_success{{{_RAW}}}))",
        "rate_24h_http": f'max by (entry) (avg_over_time(probe_success{{{_RAW},protocol="http"}}[24h]))',
        "rate_24h_socks": f'max by (entry) (avg_over_time(probe_success{{{_RAW},protocol="socks"}}[24h]))',
        "coverage_24h": f"min by (entry) (clamp_max(count_over_time(probe_success{{{_RAW}}}[24h]) / 2880, 1))",
    },
}

# The status bars: every probed entry's (the HA entries' and each proxy's) probe success rate and coverage per
# bucket, by protocol, from range queries whose step is the bucket, so each value covers the bucket before its time:
# 96 one-hour buckets, or 30 one-day buckets. One query per value and bucket size covers every row. A proxy whose
# probe labels change (public to private) has two series in that bucket: the rate pools their samples (passing
# samples over all samples) and the coverage counts them together, at most 1 when both overlap.
BAR_WINDOWS = {"hours": ("1h", 3600, 96), "days": ("24h", 86400, 30)}
PROBE_INTERVAL = 30  # seconds between two probes of a target, as in Prometheus


def _bars(width, seconds):
    sel = 'job="proxy-probe"'
    expected = seconds // PROBE_INTERVAL
    samples = f"sum by (entry, protocol) (count_over_time(probe_success{{{sel}}}[{width}]))"
    return {
        "rate": f"sum by (entry, protocol) (sum_over_time(probe_success{{{sel}}}[{width}])) / {samples}",
        "coverage": f"clamp_max({samples} / {expected}, 1)",
    }


BARS = {window: _bars(width, seconds) for window, (width, seconds, _) in BAR_WINDOWS.items()}


def _user_totals(direction, window):
    return (f'sum by (target) (increase(v2ray_traffic_{direction}link_bytes_total'
            f'{{job="v2ray",dimension="user",target!~"svc-.*"}}[{window}]))')


# The Proxy Usage dashboard's "User totals" (download, upload) over each window, for the cards, and over each
# bucket of the trend chart (a range query whose step is the bucket).
USAGE_WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
USAGE_BUCKETS = {"24h": ("1h", 3600), "7d": ("6h", 6 * 3600), "30d": ("1d", 86400)}
USAGE_TOTALS = {window: {"download": _user_totals("down", window), "upload": _user_totals("up", window)}
                for window in USAGE_WINDOWS}
USAGE_TREND = {window: {"download": _user_totals("down", bucket), "upload": _user_totals("up", bucket)}
               for window, (bucket, _) in USAGE_BUCKETS.items()}


def all_queries():
    """Every expression the portal may send."""
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, str):
            found.add(value)

    walk(STATUS)
    walk(BARS)
    walk(USAGE_TOTALS)
    walk(USAGE_TREND)
    return frozenset(found)


ALL = all_queries()
