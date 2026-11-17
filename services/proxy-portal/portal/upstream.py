"""HTTP to the upstreams (the Determined master, Prometheus): JSON in, JSON out, standard library only."""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings


class UpstreamError(Exception):
    """The upstream could not be reached or did not answer with JSON."""


class HTTPStatusError(UpstreamError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


# Never use the environment's proxy variables: the upstreams are on the private network.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_target(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if settings.UPSTREAM_LOOPBACK_ONLY:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise RuntimeError(f"tests may reach local stubs only, not {host!r}")


def request_json(url, data=None, headers=None, timeout=None):
    """GET (data None) or POST data as JSON; returns the decoded body. Raises HTTPStatusError on 4xx/5xx."""
    _check_target(url)
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="GET" if data is None else "POST")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout or settings.UPSTREAM_TIMEOUT) as response:
            raw = response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        raw = e.read(64 * 1024)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            parsed = None
        raise HTTPStatusError(e.code, parsed) from None
    except (OSError, ValueError) as e:
        raise UpstreamError(type(e).__name__) from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise UpstreamError("not JSON") from None
