"""
Local stand-ins for the Determined master and Prometheus, standard library only, for the tests and the local
compose run (e2e/). Neither talks to anything else.

    python -m portal.tests.stubs --master-port 18080 --prometheus-port 19090   # the demo scenario

The master stub knows a few users (password "pw-<name>") and records every login, logout and user list, in order;
it lists the users only for an open session of an admin. The Prometheus stub
answers only the portal's fixed queries (portal/queries.py), refuses every other expression with 400 and records
each query it gets.
"""

import argparse
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from portal import queries

# ------------------------------------------------------------------ master


class MasterStub:
    def __init__(self, users=None):
        # name: (password, admin, active)
        self.users = users if users is not None else {
            "alice": ("pw-alice", False, True), "carol": ("pw-carol", True, True),
            "bob": ("pw-bob", False, True), "dave": ("pw-dave", False, False), "erin": ("pw-erin", True, True)}
        self.logins = []        # (username, password) as received
        self.logouts = []       # tokens
        self.tokens = {}        # token: username, for the open master sessions
        self.fail_logout = False
        self.events = []        # ("login" | "logout" | "users", token or username), in order
        self.users_answer = None  # (status, body) to answer GET /api/v1/users with instead; ("raw", bytes) as is;
        # ("slow", (head, rest, seconds)): head at once, then rest one byte every seconds
        self.page_size = None     # cap the users per page, to make the portal page through them
        self.active_flag_override = None  # answer 200 with this "active" value (a master that does not refuse)
        self.lock = threading.Lock()

    def handle(self, method, path, headers, body):
        if method == "POST" and path == "/api/v1/auth/login":
            data = json.loads(body or b"{}")
            username, password = data.get("username", ""), data.get("password", "")
            with self.lock:
                self.logins.append((username, password, data.get("isHashed")))
            user = self.users.get(username)
            if user is None or user[0] != password:
                return 401, {"code": 16, "message": "invalid credentials", "details": []}
            active = user[2] if self.active_flag_override is None else self.active_flag_override
            if not active and self.active_flag_override is None:
                return 403, {"code": 7, "message": "user is not active", "details": []}
            token = "v2.public." + secrets.token_urlsafe(24)
            with self.lock:
                self.tokens[token] = username
                self.events.append(("login", username))
            return 200, {"token": token, "user": {"id": 7, "username": username, "admin": user[1],
                                                  "active": active, "displayName": username.title(),
                                                  "remote": False}}
        if method == "POST" and path == "/api/v1/auth/logout":
            token = (headers.get("Authorization") or "").removeprefix("Bearer ")
            with self.lock:
                self.logouts.append(token)
                self.events.append(("logout", token))
                if self.fail_logout:
                    return 500, {"code": 13, "message": "internal"}
                if self.tokens.pop(token, None) is None:
                    return 401, {"code": 16, "message": "invalid token"}
            return 200, {}
        if method == "GET" and path == "/api/v1/users":
            token = (headers.get("Authorization") or "").removeprefix("Bearer ")
            with self.lock:
                self.events.append(("users", token))
                username = self.tokens.get(token)
            if username is None:
                return 401, {"code": 16, "message": "invalid token"}
            if not self.users[username][1]:
                return 403, {"code": 7, "message": "admins only"}
            if self.users_answer is not None:
                return self.users_answer
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["_url"]).query)
            offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["0"])[0])
            limit = min(limit or 10 ** 6, self.page_size or 10 ** 6)
            names = sorted(self.users)
            rows = [{"id": i + 1, "username": n, "admin": self.users[n][1], "active": self.users[n][2],
                     "displayName": n.title(), "remote": False, "agentUserGroup": None}
                    for i, n in enumerate(names)][offset:offset + limit]
            return 200, {"users": rows, "pagination": {"offset": offset, "limit": limit, "startIndex": offset,
                                                       "endIndex": offset + len(rows), "total": len(names)}}
        if method == "GET" and path == "/stub/stats":
            with self.lock:
                return 200, {"logins": len(self.logins), "logouts": len(self.logouts),
                             "user_lists": sum(1 for e in self.events if e[0] == "users"),
                             "open_sessions": len(self.tokens)}
        return 404, {"code": 5, "message": "not found"}


# ------------------------------------------------------------------ Prometheus


def vector(*rows):
    """rows: (labels, value) pairs."""
    return {"resultType": "vector", "result": [{"metric": labels, "value": [0, str(value)]} for labels, value in rows]}


class PrometheusStub:
    """
    answers[expr] or answers[("instant" | "range", expr)] = callable(params) -> data, or a data dict; a fixed query
    without an answer gets an empty result.
    """

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.queries = []
        self.refused = []
        self.down = False
        self.lock = threading.Lock()

    def handle(self, method, path, headers, body):
        if self.down:
            return 503, {"status": "error", "error": "down"}
        if path == "/stub/stats":
            with self.lock:
                return 200, {"queries": len(self.queries), "distinct": len({q[1] for q in self.queries}),
                             "refused": list(self.refused)}
        if path not in ("/api/v1/query", "/api/v1/query_range"):
            return 404, {"status": "error"}
        params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(headers["_url"]).query).items()}
        expr = params.get("query", "")
        with self.lock:
            self.queries.append((path, expr, params))
        if expr not in queries.ALL:
            with self.lock:
                self.refused.append(expr)
            return 400, {"status": "error", "errorType": "bad_data", "error": "not a fixed portal query"}
        kind = "range" if path.endswith("range") else "instant"
        answer = self.answers.get((kind, expr), self.answers.get(expr))
        if callable(answer):
            answer = answer(params)
        if answer is None:
            answer = {"resultType": "matrix" if path.endswith("range") else "vector", "result": []}
        return 200, {"status": "success", "data": answer}


# ------------------------------------------------------------------ servers


def serve(stub, port=0, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def _do(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items()}
            headers["_url"] = self.path
            status, data = stub.handle(self.command, urllib.parse.urlsplit(self.path).path, headers, body)
            if status == "raw":  # a broken answer, written as it is, then the connection closes
                self.wfile.write(data)
                self.close_connection = True
                return
            if status == "slow":  # an answer written slowly, until it is complete or the client hangs up
                head, rest, seconds = data
                self.close_connection = True
                try:
                    self.wfile.write(head)
                    self.wfile.flush()
                    for i in range(len(rest)):
                        time.sleep(seconds)
                        self.wfile.write(rest[i:i + 1])
                        self.wfile.flush()
                except OSError:
                    pass
                return
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _do

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 128  # the portal sends its queries in parallel
        daemon_threads = True

    server = Server((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{server.server_address[1]}"


# ------------------------------------------------------------------ the demo scenario

PUBLIC_PROXIES = ["usca5-bwh-sla-1tb", "usca1-bwh-megaboxpro-2tb", "usca6-bwh-1tb", "us-west-racknerd-12tb"]
PRIVATE_PROXIES = ["jpty1-bwh-2tb", "miyaip-uk-01_relay_jpty1-bwh-2tb", "miyaip-uk-01_relay_usca5-bwh-sla-2tb",
                 "miyaip-jp-01_relay_usca5-bwh-sla-2tb", "miyaip-jp-01_relay_jpty1-bwh-2tb"]
FAILING_PROXY = "us-west-racknerd-12tb"
STALE_PROXY = "miyaip-jp-01_relay_jpty1-bwh-2tb"
BAR_LABELS = [{"entry": name, "protocol": protocol} for name in ["ha-general", "ha-private"] + PUBLIC_PROXIES +
              PRIVATE_PROXIES for protocol in ("http", "socks")]


def count(params):
    """The number of points of a range query."""
    return round((float(params["end"]) - float(params["start"])) / int(params["step"])) + 1


def _range(params, fn, labels_list):
    start, end, step = float(params["start"]), float(params["end"]), int(params["step"])
    result = []
    for labels in labels_list:
        values = []
        t = start
        while t <= end + 1e-6:
            value = fn(labels, int(round(t)))
            if value is not None:
                values.append([t, str(value)])
            t += step
        if values:
            result.append({"metric": labels, "values": values})
    return {"resultType": "matrix", "result": result}


def demo_answers():
    """One public proxy failing (failover working), one private proxy without recent results, status bars for every
    entry with a bucket without data, a bucket with too little data, two bad buckets and a degraded one, and usage for
    alice and carol."""
    s = queries.STATUS
    a = {}
    for pool, configured, passing in (("ha-general", 4, 3), ("ha-private", 9, 7)):
        q = s[pool]
        a[q["http"]] = vector(({"protocol": "http"}, 1))
        a[q["socks"]] = vector(({"protocol": "socks"}, 1))
        a[q["proxies_passing"]] = vector(({}, passing))
        a[q["proxies_configured"]] = vector(({}, configured))
        a[q["duration"]] = vector(({"protocol": "http"}, 0.412), ({"protocol": "socks"}, 0.388))
        a[q["last_check"]] = vector(({"protocol": "http"}, 11.2), ({"protocol": "socks"}, 12.9))
        a[q["rate_24h"]] = vector(({"protocol": "http"}, 0.9993), ({"protocol": "socks"}, 0.9986))
        a[q["coverage_24h"]] = vector(({"protocol": "http"}, 1), ({"protocol": "socks"}, 0.998))
    proxies = s["proxies"]
    rows = [(name, "ha-general", "public") for name in PUBLIC_PROXIES] + \
           [(name, "ha-private", "private") for name in PRIVATE_PROXIES]
    a[proxies["http"]] = vector(*[({"entry": n, "pool": p, "access": acc},
                                   0 if n == FAILING_PROXY else -1 if n == STALE_PROXY else 1) for n, p, acc in rows])
    a[proxies["socks"]] = vector(*[({"entry": n}, 0 if n == FAILING_PROXY else -1 if n == STALE_PROXY else 1)
                                   for n, _, _ in rows])
    a[proxies["duration_http"]] = vector(*[({"entry": n}, 0.3 + i * 0.07) for i, (n, _, _) in enumerate(rows)])
    a[proxies["duration_socks"]] = vector(*[({"entry": n}, 0.28 + i * 0.06) for i, (n, _, _) in enumerate(rows)])
    a[proxies["last_check"]] = vector(*[({"entry": n}, 600 if n == STALE_PROXY else 9 + i) for i, (n, _, _)
                                        in enumerate(rows)])
    a[proxies["rate_24h_http"]] = vector(*[({"entry": n}, 0.41 if n == FAILING_PROXY else 0.997) for n, _, _ in rows])
    a[proxies["rate_24h_socks"]] = vector(*[({"entry": n}, 0.39 if n == FAILING_PROXY else 0.995) for n, _, _ in rows])
    a[proxies["coverage_24h"]] = vector(*[({"entry": n}, 0.2 if n == STALE_PROXY else 1) for n, _, _ in rows])

    def bucket_index(params, t):
        return round((t - float(params["start"])) / int(params["step"]))

    def bars_rate(params):
        def fn(labels, t):
            i = bucket_index(params, t)
            if i == 14:
                return None                   # no samples in that bucket
            if i == 20:
                return 1.0                    # few samples, all passing
            if i in (8, 9):
                return 0.82 if labels["protocol"] == "http" else 0.9
            if i == 25:
                return 0.955
            if labels["entry"] == FAILING_PROXY and i >= count(params) - 3:
                return 0.4 if labels["protocol"] == "http" else 0.38
            return 0.9995
        return _range(params, fn, BAR_LABELS)

    def bars_coverage(params):
        def fn(labels, t):
            i = bucket_index(params, t)
            if i == 14:
                return None
            if i == 20:
                return 0.18
            if labels["entry"] == STALE_PROXY and i == count(params) - 1:
                return 0.2
            return 0.999
        return _range(params, fn, BAR_LABELS)

    for window in queries.BARS:
        a[("range", queries.BARS[window]["rate"])] = bars_rate
        a[("range", queries.BARS[window]["coverage"])] = bars_coverage

    users = {"alice": (3.1e9, 0.42e9), "carol": (12.4e9, 1.1e9), "bob": (0.2e9, 0.01e9)}
    scale = {"24h": 0.06, "7d": 0.3, "30d": 1.0}
    for window, exprs in queries.USAGE_TOTALS.items():
        for direction, expr in exprs.items():
            k = 0 if direction == "download" else 1
            a[expr] = vector(*[({"target": u}, v[k] * scale[window]) for u, v in users.items()])
    for window, exprs in queries.USAGE_TREND.items():
        step = queries.USAGE_BUCKETS[window][1]
        for direction, expr in exprs.items():
            k = 0 if direction == "download" else 1

            def trend(params, k=k, step=step, window=window):
                def fn(labels, t):
                    base = users[labels["target"]][k] * scale[window]
                    n = queries.USAGE_WINDOWS[window] / step
                    wave = 0.55 + 0.45 * (((t // step) * 7919) % 13) / 13
                    return base / n * wave
                return _range(params, fn, [{"target": u} for u in users])
            a[expr] = trend
    return a


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--master-port", type=int, default=18080)
    parser.add_argument("--prometheus-port", type=int, default=19090)
    args = parser.parse_args()
    master = MasterStub()
    # cluster users without a proxy account, one of them with a name no proxy account can have
    master.users.update({"frank": ("pw-frank", False, True), "Hana.W": ("pw-hana", False, True)})
    prometheus = PrometheusStub(demo_answers())
    _, master_url = serve(master, args.master_port, args.host)
    _, prom_url = serve(prometheus, args.prometheus_port, args.host)
    print(f"master stub {master_url}, Prometheus stub {prom_url}", flush=True)
    try:
        threading.Event().wait()
    finally:
        print(f"queries: {len(prometheus.queries)}, refused: {len(prometheus.refused)}", flush=True)


if __name__ == "__main__":
    main()
